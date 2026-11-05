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
MAX_CHUNK = 4000  # Telegram szoveglimit 4096 kodpont, biztonsagi margoval


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
    except Exception as exc:  # noqa: BLE001 - network hiba jelzese a caller-nek
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
    """
    del pconfig, thread_id, media_files, force_document
    token = _log_token()
    if not token:
        return {"error": f"{LOG_TOKEN_ENV} nincs beallitva a cron folyamat környezetében"}
    message = (message or "").strip()
    if not message:
        return {"error": "ures uzenet"}
    target = (chat_id or "").strip() or LOG_CHAT_ID
    last_message_id = None
    num_chunks = (len(message) + MAX_CHUNK - 1) // MAX_CHUNK
    for chunk_start in range(0, len(message), MAX_CHUNK):
        chunk = message[chunk_start:chunk_start + MAX_CHUNK]
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
    return None


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
