"""memoria-v2 hybrid-search bridge for session_search discovery.

Read-only, side-effect free. Calls memoria-v2's hybrid_search (FTS5 over
mem_fts + sqlite-vec kNN over mem_vec, fused with RRF f=60 in indexer.py)
and merges the hits alongside the session FTS5 results in _discover.

Deployment
----------
fastembed + sqlite_vec are NOT importable in the gateway interpreter, but they
ARE available in the bundled python-3.14.9
(/home/endre/.hermes/tools/python-3.14.9*/bin/python3; offline, local pip
cache, verified find_spec True/True). The bridge therefore runs indexer.py in a
LONG-LIVED HPY subprocess worker that keeps the nomic-embed-text model warm,
so interactive searches do not pay the ~1.2 s model load on every call.

Why a subprocess worker instead of installing fastembed/sqlite_vec into the
gateway interpreter:
  * Zero changes to the gateway interpreter -> gateway startup time is
    unaffected (nothing is imported at gateway import time; the worker is
    spawned lazily on the first memoria query).
  * Nothing is installed into the system/gateway python (scope-tisztasag:
    the only modified file is tools/memoria_bridge.py).
  * The heavy ONNX embedding session lives in the worker, not in the gateway
    process (no permanent ~1 GiB RSS increase in the gateway).
  * A smaller embedding model was rejected: mem_vec is float[768] and already
    indexed with nomic-embed-text-v1.5; switching to a 384-d model would
    require re-indexing mem.db (touches the memoria-v2 schema/data).

Degradation contract (er requires): on ANY failure (worker spawn failure,
missing interpreter stack, missing mem.db, query error, worker death/hang) the
bridge returns the session results UNCHANGED -- so the live session path is
byte-identical with or without this bridge.
"""

from __future__ import annotations

import atexit
import glob
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

MEMORIA_DIR = Path("/home/endre/.hermes/memoria-v2")
MEMORIA_DB = MEMORIA_DIR / "mem.db"
# Same RRF constant as indexer.hybrid_search (f=60).
_RRF_F = 60
# At most this many memoria hits are considered for fusion.
_MEMORIA_TOP_K = 5

# Time the bridge waits for a single worker response before degrading.
_WORKER_READ_TIMEOUT_S = 30

# The worker program (runs under the bundled HPY interpreter). Paths come from
# environment variables so no interpreter-specific path is baked into the
# gateway process.
_WORKER_SRC = r"""
import json
import os
import sys

MEMORIA_DIR = os.environ["MEMORIA_DIR"]
MEMORIA_DB = os.environ["MEMORIA_DB"]
sys.path.insert(0, MEMORIA_DIR)

# Warm the embedding model ONCE at worker startup so every query served by
# this worker is load-free: the ~1.2 s nomic-embed-text load is paid a single
# time (worker lifetime), not per search.
from indexer import _embed_model, hybrid_search  # noqa: E402
_embed_model()


def main() -> None:
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            query = req.get("query", "")
            k = int(req.get("k", 5))
            hits = hybrid_search(
                query, k=k, source_filter=None, db_path=MEMORIA_DB
            )
            out.write(
                json.dumps({"ok": True, "hits": hits or []}, ensure_ascii=False)
                + "\n"
            )
        except Exception as exc:  # one bad query must not kill the worker
            out.write(
                json.dumps({"ok": False, "error": repr(exc)}, ensure_ascii=False)
                + "\n"
            )
        out.flush()


if __name__ == "__main__":
    main()
"""


def _bundled_python() -> str | None:
    """Path to the bundled python-3.14.9 carrying fastembed + sqlite_vec."""
    matches = glob.glob("/home/endre/.hermes/tools/python-3.14.9*/bin/python3")
    return sorted(matches)[0] if matches else None


class _MemoriaWorker:
    """Long-lived HPY subprocess serving indexer.hybrid_search with a warm model.

    Spawned lazily on the first query. All stdin/stdout access is lock
    protected so concurrent session_search calls share one worker safely.
    Any failure degrades to an empty hit list (session results unchanged).
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._dead = False
        atexit.register(self._terminate)

    def _start(self) -> bool:
        if self._dead:
            return False
        if self._proc is not None:
            return True
        python = _bundled_python()
        if python is None:
            self._dead = True
            return False
        env = dict(os.environ)
        env["MEMORIA_DIR"] = str(MEMORIA_DIR)
        env["MEMORIA_DB"] = str(MEMORIA_DB)
        try:
            self._proc = subprocess.Popen(
                [python, "-u", "-c", _WORKER_SRC],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                env=env,
            )
        except Exception:
            self._dead = True
            self._proc = None
            return False
        return True

    def _terminate(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _read_line(self, timeout: float = _WORKER_READ_TIMEOUT_S) -> str | None:
        """Read one stdout line from the worker with a watchdog timeout."""
        result: list[str] = []

        def _target() -> None:
            try:
                line = self._proc.stdout.readline()  # type: ignore[union-attr]
            except Exception:
                line = ""
            result.append(line)

        if self._proc is None or self._proc.stdout is None:
            return None
        reader = threading.Thread(target=_target, daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            return None  # worker hung
        return result[0] if result else ""

    def hybrid_search(self, query: str, k: int = _MEMORIA_TOP_K) -> list[dict[str, Any]]:
        if self._dead:
            return []
        with self._lock:
            if not self._start():
                return []
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdout is None:
                return []
            try:
                payload = json.dumps({"query": query, "k": k}, ensure_ascii=False) + "\n"
                proc.stdin.write(payload)
                proc.stdin.flush()
                line = self._read_line()
            except Exception:
                self._kill()
                return []
        if line is None or line == "":
            self._kill()
            return []
        try:
            resp = json.loads(line)
        except Exception:
            return []
        if not isinstance(resp, dict) or not resp.get("ok"):
            return []
        hits = resp.get("hits")
        return hits if isinstance(hits, list) else []

    def _kill(self) -> None:
        self._dead = True
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


_worker = _MemoriaWorker()


def _memoria_hybrid(query: str, k: int = _MEMORIA_TOP_K) -> list[dict[str, Any]]:
    """memoria-v2 hybrid search over mem.db; [] on any failure (degraded)."""
    if not query or not query.strip():
        return []
    if not MEMORIA_DB.exists():
        return []
    try:
        hits = _worker.hybrid_search(query, k=k)
    except Exception:
        return []
    return hits or []


def _memoria_entries(query: str, k: int = _MEMORIA_TOP_K) -> list[dict[str, Any]]:
    """Shape memoria hits as discovery entries (session_id=None => no session link)."""
    entries: list[dict[str, Any]] = []
    for h in _memoria_hybrid(query, k=k):
        entries.append(
            {
                "session_id": None,
                "source": "memoria-v2",
                "model": None,
                "session_started": None,
                "when": None,
                "title": None,
                "matched_role": "memory",
                "match_message_id": None,
                "snippet": str(h.get("snippet") or h.get("question") or ""),
                "bookend_start": [],
                "messages": [],
                "bookend_end": [],
                "messages_before": 0,
                "messages_after": 0,
                "detail": "adaptive",
                "_memoria": True,
                "memoria_id": str(h.get("id")),
                "memoria_source": h.get("source"),
                "memoria_score": round(float(h.get("score") or 0.0), 4),
            }
        )
    return entries


def memoria_merge(
    session_entries: list[dict[str, Any]],
    query: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    """RRF-fuse session discovery entries with memoria hits.

    Guarantee: every session entry survives in its relative order; memoria hits
    are interleaved by fused RRF score. When memoria is degraded (empty) the
    returned list is `session_entries` untouched (regression-safe).
    """
    memory_entries = _memoria_entries(query)
    if not memory_entries:
        return session_entries

    scored: list[tuple[tuple[str, Any], float, dict[str, Any]]] = []
    order: list[tuple[str, Any]] = []
    for rank, entry in enumerate(session_entries):
        key = ("s", rank)
        scored.append((key, 1.0 / (_RRF_F + rank + 1), entry))
        order.append(key)
    seen_memoria_ids: set[str] = set()
    for rank, entry in enumerate(memory_entries):
        mid = entry.get("memoria_id")
        key = ("m", mid if mid is not None else ("anon", rank))
        if mid is not None and mid in seen_memoria_ids:
            continue
        if mid is not None:
            seen_memoria_ids.add(mid)
        scored.append((key, 1.0 / (_RRF_F + rank + 1), entry))
        order.append(key)

    scored.sort(key=lambda item: (-item[1], order.index(item[0])))
    top_keys = {key for key, _, _ in scored[:limit]} if limit is not None else {key for key, _, _ in scored}
    fused = [entry for _, _, entry in scored[:limit]] if limit is not None else [entry for _, _, entry in scored]
    # Preserve: session entries displaced from the top-`limit` are appended in
    # their original relative order, so old session results never disappear.
    for key, _, entry in scored:
        if key[0] == "s" and key not in top_keys:
            fused.append(entry)
    return fused
