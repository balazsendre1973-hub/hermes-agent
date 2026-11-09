"""log platform plugin — cron kimenet kézbesítése a Log csatornára (Telegram).

A job szöveges kimenete a LOG_TELEGRAM_TOKEN mögötti bot privát chatjába
(chat_id 8832329921 = Endre Balázs DM = Log csatorna) kerül.
LLM-mentes, kikerülő (standalone) küldő: nincs hozzá live adapter, a magkódot
nem módosítja (LiveUpdate-biztos).
"""

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("plugins.log")

PLATFORM_NAME = "log"
LOG_TOKEN_ENV = "LOG_TELEGRAM_TOKEN"
LOG_CHAT_ID = "8832329921"  # Endre Balazs DM = Log csatorna
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
# A Telegram 4096-os limitje UTF-16 EGYSÉG, nem kód pont: 4000 darab astral emoji 8000
# egység → 400 (retry_after) → a teljes kézbesítés elbukik. Ezért itt is UTF-16 a mérték.
MAX_CHUNK = 4000  # UTF-16 egység, biztonsági margoval a 4096 alatt


def _utf16_len(text: str) -> int:
    """UTF-16 egységek száma — ugyanaz, mint a ``gateway/platforms/base.py`` ``utf16_lennel``.

    A plugin nem importálhat gateway-modult (a betöltés a gateway folyamatában, a
    platform-regisztráció idején történik), ezért a mérték itt él. Egy astral emoji (CJK
    Ext B, emoji) 2 egység, amit a Python ``len()`` 1-nek számol.
    """
    return len(text.encode("utf-16-le")) // 2


def _chunk_utf16(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """``text`` darabjai, amelyek mindegyike ≤ ``limit`` UTF-16 egység.

    Kód pontokra vágni (a régi ``text[i:i + MAX_CHUNK]``) 4000 emoji esetén 8000 egységes
    darabot ad, amit a Telegram 400-zal utasít vissza. Itt az egység-szélesség szerint
    vágunk, így egy surrogate pair (emoji) sohasem esik két darabra.
    """
    chunks: list[str] = []
    start = 0
    units = 0
    for idx, ch in enumerate(text):
        width = 2 if ord(ch) > 0xFFFF else 1
        if units and units + width > limit:
            chunks.append(text[start:idx])
            start, units = idx, 0
        units += width
    if start < len(text):
        chunks.append(text[start:])
    return chunks


def _redact_outbound(text: str) -> str:
    """Titok-mentesítés a kimenő szövegen — ugyanaz a fail-closed politika, mint a
    ``cron/scheduler_delivery._redact_cron_payload``.

    A cron útvonal a lane-ek előtt redaktál, de a közvetlen útvonal (``hermes send
    --target log``, ``tools/send_message_tool``) nem: a küldő fölött itt sem bízunk meg,
    a ``force=True`` a biztonsági határ, amit a ``security.redact_secrets`` preferencia
    nem kapcsolhat ki. Ha a redaktálás maga hibázik, a payload nem megy ki."""
    if not text:
        return text
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(text, force=True)
    except Exception as exc:
        logger.warning("log platform: redaction failed, payload dropped: %s", exc)
        return "[REDACTED - redaction failed]"


def _send_one(token: str, chat_id: str, text: str):
    """Egy Telegram sendMessage. (result_dict, None) vagy (None, errmsg)."""
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        TELEGRAM_SEND_URL.format(token=token),
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        return None, f"Telegram API HTTP {exc.code}: {detail}"
    except Exception as exc:
        return None, f"Telegram send failed: {exc}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None, f"Telegram API invalid JSON: {body[:200]}"
    if not data.get("ok"):
        return None, f"Telegram API rejected: {data.get('description', '')[:200]}"
    msg_id = (data.get("result") or {}).get("message_id")
    return {"ok": True, "message_id": str(msg_id) if msg_id is not None else None}, None


def _log_token() -> str:
    """LOG_TELEGRAM_TOKEN a futó profil titok-scope-jából (multiplex cron tick a job-owning
    profile scope-jával fut), os.environ fallback. Üres string, ha sehol nincs."""
    try:
        from agent.secret_scope import get_secret
        token = (get_secret(LOG_TOKEN_ENV, "") or "").strip()
    except Exception:
        token = ""
    return token or os.environ.get(LOG_TOKEN_ENV, "").strip()


async def log_standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """PlatformEntry.standalone_sender_fn: cron delivery a Log csatornára.

    Visszaadja a _send_to_platform-al kompatibilis dictet:
    {"success": True, "message_id": "..."} | {"error": "..."}.

    Biztonsági határ: a payload MIELŐTT bármi mást történik redaktálva van (lásd
    _redact_outbound), és a chunkok UTF-16 egység szerint vannak darabolva (lásd
    _chunk_utf16) — a Telegram limitje UTF-16, nem kód pont.
    """
    del pconfig, thread_id, media_files, force_document
    # AZ ELSŐ lépés: bármely küldő jöhet (cron, `hermes send --target log`, tool) és egy
    # titok a szövegben egyenesen a Log csatornára menne — itt nem bízunk a küldőben.
    message = _redact_outbound(message or "")
    token = _log_token()
    if not token:
        return {"error": f"{LOG_TOKEN_ENV} nincs beallitva a cron folyamat környezetében"}
    message = message.strip()
    if not message:
        return {"error": "ures uzenet"}
    target = (chat_id or "").strip() or LOG_CHAT_ID
    last_message_id = None
    chunks = _chunk_utf16(message)
    num_chunks = len(chunks)
    for chunk in chunks:
        result, err = _send_one(token, target, chunk)
        if err is not None:
            return {"error": err}
        last_message_id = result.get("message_id")
    logger.info(
        "log platform: %d darab chunk elkeszitve, utolso message_id=%s chat_id=%s",
        num_chunks, last_message_id, target,
    )
    return {
        "success": True,
        "message_id": str(last_message_id) if last_message_id is not None else None,
        "platform": PLATFORM_NAME,
        "chat_id": target,
    }


def _check_log_requirements() -> bool:
    """PASSIVE dependency probe: csak a stdlib kell, a token a scope/env alapján van."""
    return bool(_log_token())


def _log_adapter_factory(pconfig):
    """Nincs live adapter a log platformhoz (standalone csatorna). None-t ad vissza."""
    del pconfig


def register(ctx) -> None:
    """Plugin belépési pont; a futo gateway betolti az indulaskor."""
    ctx.register_platform(
        name="log",
        label="Log channel (Telegram)",
        adapter_factory=_log_adapter_factory,
        check_fn=_check_log_requirements,
        # Konvenció szerint a TELJES home-channel env változó neve (lásd BUZZ_HOME_CHANNEL,
        # DISCORD_HOME_CHANNEL): deliver=log (chat id nélkül) innen olvassa a célt.
        cron_deliver_env_var="LOG_HOME_CHANNEL",
        standalone_only=True,
        standalone_sender_fn=log_standalone_send,
        emoji="\U0001f4c5",
        max_message_length=0,
        pii_safe=False,
        allow_update_command=False,
        platform_hint=(
            "The 'log' platform is a send-only cron delivery lane: posts job output to the "
            "Telegram bot behind LOG_TELEGRAM_TOKEN (Log channel). No inbound, no LLM."
        ),
    )
