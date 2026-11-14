"""F5 szelektív mem.db-betöltés a rendszerszintű promptba (memoria-reteg v2, 5. fázis).

A ``MemoryStore.load_from_disk`` hívja meg, a MEMORY.md-blokk UTÁN, ugyanabban a
``memory.memory_char_limit`` (2200, ``hermes_cli/config_defaults.py``) keretben:

    MEMORY.md (elsőbbség, soha nem vágódik le) + mem.db F5-szekció  <=  2200 karakter

A szekció tartalma a ``~/.hermes/memoria-v2/loader.py``-ből jön (a szelektálás
EGYETLEN forrása; a visszafejlesztés nem másolat, hanem import):

    helyzet (kanban-kártya címe + body) -> kulcsszavak -> FTS5 BM25 Top-5
      -> salience/tier szűrő (superseded_by/cold/low ki) -> formázott szabályblokk
    nincs bekötött feladat (pl. chat-session) -> global: salience+tier Top-5

Jellemzők, amiket ez a modell őriz:
  - **prompt-hook, nem agent-ciklus:** a blokk a prompt-összeállításkor készül
    (``load_from_disk``), a fő ciklusban nincs DB-hozzányúlás. Mért költség:
    ~3.8 ms/feladat, ~2.3 ms global (1676 sor, FTS5).
  - **csak FTS5:** ``hybrid=False`` -> se fastembed, se sqlite_vec nem kell az
    agent interpreterben (mért: a hibrid láb 1.1 ms alatt elhasal, lásd
    f5_measure.json). A vektor-láb a `t_8e9d4fd0` élesítése után térhet vissza.
  - **csak olvasás:** ``mode=ro`` URI, nincs ``flock``, nincs írás a mem.db-be.
  - **fail-open:** bármely kivétel / hiányzó fájl -> üres szekció, a MEMORY.md
    betöltés érintetlen marad (soha nem töri le a promptot).
  - **threat-scan:** a mem.db beszélgetésekből származik -> minden szabálysor
    végigmegy a ``scan_for_threats``-en (mint a MEMORY.md-bejegyzések), a
    keresztsoros találat az egész szekciót kidobja.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PROJECT_DIR = "~/.hermes/memoria-v2"
# A kártya body-jából ennyi elég a kulcsszavakhoz (a keyword-limit amúgy is 10).
MAX_SITUATION_CHARS = 3000
# Fejléc-foglalás a keretből (a tényleges fejléc ~60-75 karakter; a maradék
# pontos illesztését a fit-loop végzi, így a végösszeg soha nem lőheti túl).
HEADER_RESERVE = 88
# Ennél kisebb maradékba már fejléc + egy szabálysor sem fér -> nem érdemes
# betölteni (MEMORY.md tele = a mem.db nem kap helyet; dokumentált kompromisszum).
MIN_SECTION_CHARS = 150
RULE_PREFIX = "- [SZABÁLY] "

DEFAULTS: dict[str, Any] = {
    "enabled": True,     # memory.memdb.enabled
    "path": "",          # memory.memdb.path (üres -> <project>/mem.db)
    "top_k": 5,          # memory.memdb.top_k
    "mode": "auto",      # memory.memdb.mode: auto | task | global
}

_loader_module: Any = None
_loader_failed = False


# --------------------------------------------------------------------------- env

def _env(name: str) -> str:
    """Session-szintű env (a dispatcher a worker sessionba köti), fallback: process env."""
    try:
        from gateway.session_context import get_session_env
        value = get_session_env(name, "")
        if value:
            return str(value)
    except Exception:
        pass
    return os.environ.get(name, "") or ""


def project_dir(override: str | None = None) -> Path:
    raw = override or _env("HERMES_MEMORIA_V2_DIR") or DEFAULT_PROJECT_DIR
    return Path(raw).expanduser()


# ------------------------------------------------------------------ project import

def _loader():
    """A memoria-v2 loader betöltése (egyszer); None, ha nincs meg / nem indul.

    A projekt-könyvtár nem maradhat a ``sys.path`` élén (modul-árnyékolás:
    ``indexer``/``trigger``/``capture`` nevek); a loader által betett útvonalat
    az import után levesszük, a modulok a ``sys.modules``-ban maradnak."""
    global _loader_module, _loader_failed
    if _loader_module is not None:
        return _loader_module
    if _loader_failed:
        return None
    path = project_dir() / "loader.py"
    if not path.is_file():
        _loader_failed = True
        logger.debug("F5: memoria-v2 loader nincs meg: %s", path)
        return None
    parent = str(path.parent)
    inserted = parent not in sys.path
    try:
        if inserted:
            sys.path.insert(0, parent)
        spec = importlib.util.spec_from_file_location("memoria_v2_loader", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"nem lehet betölteni: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _loader_module = module
        return module
    except Exception:
        _loader_failed = True
        logger.warning("F5: memoria-v2 loader import sikertelen (%s)", path, exc_info=True)
        return None
    finally:
        if inserted and sys.path and sys.path[0] == parent:
            sys.path.pop(0)


# --------------------------------------------------------------------- task context

def _task_id() -> str:
    """A feladat, amit ez a processz visz (dispatcher-worker): env -> workspace név."""
    tid = _env("HERMES_KANBAN_TASK").strip()
    if tid:
        return tid
    ws = _env("HERMES_KANBAN_WORKSPACE").strip()
    name = Path(ws).name if ws else ""
    return name if re.fullmatch(r"t_[0-9a-z-]+", name) else ""


def task_situation(max_chars: int = MAX_SITUATION_CHARS) -> str:
    """A kanban-kártya ``cím + body`` (a helyzet a F5 kereséshez); '' ha nincs feladat.

    Olvasó, rövid időkorlátú lekérdezés a tábláról (``mode=ro``) — a mem.db-vel
    ellentétben itt NEM írásvédett a forrás, de mi csak olvasunk."""
    task_id, board = _task_id(), _env("HERMES_KANBAN_DB").strip()
    if not task_id or not board:
        return ""
    path = Path(board).expanduser()
    if not path.is_file():
        return ""
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        row = conn.execute("SELECT title, body FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
    except Exception as exc:                      # fail-open: global betöltés megy tovább
        logger.debug("F5: kanban-kártya olvasása nem sikerult (%s): %s", task_id, exc)
        return ""
    finally:
        if conn is not None:
            conn.close()
    if not row:
        return ""
    title, body = str(row[0] or ""), str(row[1] or "")
    return f"{title}\n{body[:max_chars]}".strip()


# ------------------------------------------------------------------------ section

def _header(total_rows: int, backend: str, mode: str, block_chars: int, budget: int) -> str:
    kind = "feladat-kontextus" if mode == "task" else "global (salience+tier)"
    return (f"── mem.db F5: top-5 / {total_rows} sor · {kind} · {backend} · "
            f"{block_chars}/{budget} kar ──")


def _state_block(label: str, max_chars: int, detail: str = "") -> str:
    """M1 NEVER-SILENT: minden fail-open állapot explicit, látható blokkja.

    A régi csendes fail-open (``section = ""``) helyett a prompt MINDIG látja
    a memória réteg állapotát — üres találat, hiba, hiányzó mem.db vagy
    kikapcsolt egyaránt.  Ez a riport 45. pontja: „a felhasználó sosem tudja,
    hogy a memória semmit sem adott vissza" — a láthatóság a cél.

    A ``detail`` pl. hibaüzenet mem.db-tartalmat hordozhat -> threat-scan
    (``_sanitize_section``); fenyegető-minta esetén csak a statikus címke
    marad (mindig tiszta, nem hordozó)."""
    def _fit(t: str) -> str:
        return t if len(t) <= max_chars else t[: max(1, max_chars - 1)] + "…"
    text = _fit(f"(memória: {label}" + (f" — {detail}" if detail else "") + ")")
    try:
        cleaned = _sanitize_section(text)
    except Exception:                              # threat_patterns nem elérhető
        cleaned = text
    if cleaned:
        return _fit(cleaned)
    return _fit(f"(memória: {label})")             # címke-only, mindig tiszta


def _sanitize_section(section: str) -> str:
    """Szabálysoronként threat-scan (a mem.db beszélgetésekből származik).

    Egy találat csak AZT a sort veszi ki (látható [BLOCKED] jelöléssel, a
    találat csak naplóba); ha a minta két soron átível, az egész szekció megy
    (a promptba injektált szöveg nem lehet hordozó)."""
    from tools.threat_patterns import scan_for_threats
    try:
        if not scan_for_threats(section, scope="strict"):
            return section
    except Exception:                              # ismeretlen scope / szkener-hiba
        logger.warning("F5: threat-scan nem futott — a mem.db szekció kihagyva", exc_info=True)
        return ""
    kept: list[str] = []
    for line in section.split("\n"):
        if not line.startswith(RULE_PREFIX):
            kept.append(line)
            continue
        try:
            findings = scan_for_threats(line, scope="strict")
        except Exception:
            findings = ["scan_error"]
        if findings:
            logger.warning("F5: mem.db szabálysor blokkolva: %s", ", ".join(findings))
            kept.append("- [BLOCKED: fenyegető-minta a mem.db sorban — kihagyva]")
        else:
            kept.append(line)
    out = "\n".join(kept)
    try:
        rest = scan_for_threats(out, scope="strict")
    except Exception:
        rest = []
    if rest:                                        # keresztsoros találat -> egész szekció kidobva
        logger.warning("F5: mem.db szekció eldobva (keresztsoros fenyegető-minta): %s",
                       ", ".join(rest))
        return ""
    return out


def build_section(max_chars: int, *, cfg: dict[str, Any] | None = None,
                  situation: str | None = None,
                  memdb_path: str | None = None) -> dict[str, Any]:
    """F5-szekció a promptba; M1 NEVER-SILENT: a ``section`` SOHA nem üres.

    Minden állapot (betöltött szabályok, üres találat, hiba, hiányzó mem.db,
    kikapcsolt, keret-hiány) explicit látható szakaszt ad — a régi csendes
    fail-open (``section = ""``) helyett a felhasználó mindig látja, hogy a
    memória mit csinált.  A ``reason`` továbbra is jelzi a pontos állapotot.

    ``max_chars`` = a MEMÓRIÁBAN MARADÓ keret (memory_char_limit - MEMORY.md) —
    a fejléc is beleszámít, a visszatérő szekció mindig <= max_chars.
    """
    conf = {**DEFAULTS, **(cfg or {})}
    out: dict[str, Any] = {"section": "", "reason": "", "backend": "", "mode": "",
                           "total_rows": 0, "entries": 0, "chars": 0,
                           "query_keywords": [], "stats": {}}
    # M1 NEVER-SILENT: minden korai kijárat most LÁTHATÓ állapot-szakaszt ad
    # (a régi ``section = ""`` csendes fail-open helyett).
    if not conf.get("enabled", True):
        out["reason"] = "disabled"
        out["section"] = _state_block("ki", max_chars, "memory.memdb.enabled=false")
        out["chars"] = len(out["section"])
        return out
    if max_chars < MIN_SECTION_CHARS:
        out["reason"] = f"no-budget({max_chars}<{MIN_SECTION_CHARS})"
        out["section"] = _state_block("nincs keret", max_chars,
                                      f"{max_chars}<{MIN_SECTION_CHARS} char")
        out["chars"] = len(out["section"])
        return out
    mod = _loader()
    if mod is None:
        out["reason"] = "no-loader"
        out["section"] = _state_block("loader nem elérhető", max_chars,
                                      "memoria-v2/loader.py nincs meg")
        out["chars"] = len(out["section"])
        return out
    path = memdb_path or conf.get("path") or str(project_dir() / "mem.db")
    path = str(Path(path).expanduser())
    if not Path(path).is_file():
        out["reason"] = f"no-memdb({path})"
        out["section"] = _state_block("mem.db nem elérhető", max_chars, path)
        out["chars"] = len(out["section"])
        return out
    if situation is None:
        sit_mode = str(conf.get("mode", "auto"))
        situation = task_situation() if sit_mode in ("auto", "task") else ""
        if sit_mode == "global":
            situation = ""
    mode = "task" if str(situation or "").strip() else "global"
    try:
        res = mod.load_block(situation or None, db_path=path,
                             top_k=int(conf.get("top_k", 5)),
                             max_chars=max(1, max_chars - HEADER_RESERVE),
                             mode=mode, hybrid=False)
    except Exception as exc:                        # fail-open
        logger.warning("F5: mem.db betöltés hiba (%s): %s", type(exc).__name__, exc)
        out["reason"] = f"error({type(exc).__name__})"
        out["section"] = _state_block("betöltési hiba", max_chars,
                                      f"{type(exc).__name__}: {exc}")
        out["chars"] = len(out["section"])
        return out

    block = res.get("block") or ""
    if not block:
        # M1: a load_block üres találatnál MOST explicit üres-állapotot ad,
        # ez a ág csak biztonsági háló (pl. váratlan üres block).
        out["reason"] = "empty-block"
        out.update(backend=res.get("backend", ""), mode=mode,
                   total_rows=int(res.get("total_rows") or 0),
                   stats=res.get("stats") or {})
        out["section"] = _state_block("nincs találat", max_chars,
                                      f"{out['total_rows']} sor, 0 betöltve")
        out["chars"] = len(out["section"])
        return out

    header = _header(int(res.get("total_rows") or 0), str(res.get("backend") or ""),
                     mode, len(block), max_chars)
    lines = block.split("\n")
    section = header + "\n" + "\n".join(lines)
    while len(section) > max_chars and lines:        # pontos illesztés a keretre
        lines.pop()
        block = "\n".join(lines)
        section = (header + "\n" + block) if lines else ""
    if not section:
        # M1: a keret túl szűk a fejléc + 1 sorhoz -> látható rövid állapot.
        out["reason"] = "fit-empty"
        out.update(backend=str(res.get("backend") or ""), mode=mode,
                   total_rows=int(res.get("total_rows") or 0),
                   stats=res.get("stats") or {})
        out["section"] = _state_block("nincs elférő szabály", max_chars,
                                      f"keret {max_chars} char")
        out["chars"] = len(out["section"])
        return out
    section = _sanitize_section(section)
    if not section:
        out["reason"] = "threat-blocked"
        out["section"] = _state_block("szekció blokkolva", max_chars,
                                      "fenyegető-minta a mem.db tartalmában")
        out["chars"] = len(out["section"])
        return out
    if len(section) > max_chars:                     # sanitizer-csere után is
        out["reason"] = "over-budget"
        out["section"] = _state_block("keret-túllépés", max_chars,
                                      f"{len(section)}>{max_chars} char")
        out["chars"] = len(out["section"])
        return out
    out.update(section=section, backend=str(res.get("backend") or ""), mode=mode,
               total_rows=int(res.get("total_rows") or 0),
               entries=len(res.get("entries") or []), chars=len(section),
               query_keywords=res.get("query_keywords") or [],
               stats=res.get("stats") or {})
    return out


__all__ = ["DEFAULTS", "MIN_SECTION_CHARS", "build_section", "project_dir",
           "task_situation"]
