"""Központi str→dict coercion a tool-calls argumentumaihoz.

A wire formban ``function.arguments`` egy JSON string, de egyes provider/SDK útvonalak
már parse-olt dictet adnak vissza, és bizonyos modellek egy sima stringet (számot,
tömböt) küldenek JSON objektum helyett argumentumként. Minden downstream folyamat
``.get()``-t hív az argumentumokon (file-checkpoint ``path``, delegate_task ``goal``/
``tasks``, terminal ``command``/``workdir``, guardrails, ``memory_manager``), ezért
egy str itt ``AttributeError: 'str' object has no attribute 'get'``-t dobna — ez a
naplóban ``object has no attribute 'get'`` jelaként voorkozo hiba, amelyet a
``scripts/check_str_get_errors.sh`` egészség-számláló figyel.

Egyetlen pont: minden eszközargumentum
``agent/tool_executor.py:_parse_tool_call`` → ``_parse_tool_arguments``-en keresztül
jártak (soros, párhuzamos és szegmentált végrehajtás egyaránt), így a coercion és a
modell-nek küldött teaching üzenet itt egységpontosítva lehetséges.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# Modell-nek szóló teaching üzenet előtagja. Angol, mert a modellre szól (a magyar
# loop-n belül vagy a tool-result csatornán jut el hozzá; angol utasítást a modell
# legsikrabban áttanulja).
_TEACHING_PREFIX = "[HERMES] Tool arguments must be a JSON object, not a plain string."

# Literal JSON darabok, amiket f-string-be helyettesíten (a literal "{}" az f-string
# üres formázása, ezért változóban tároljuk).
_EMPTY_OBJECT = "{}"
_JSON_EXAMPLE = '{"path": "..."}'


def coerce_tool_arguments(
    arguments: Any, *, tool_name: str = "?"
) -> Tuple[dict, Optional[str]]:
    """Coerce one tool call's arguments into a plain ``dict``.

    * ``dict``  → visszaadott változatlanul (identity, nincs side effect; a normál
      már-parseltt útvonal).
    * ``str``   → ``json.loads``:
        - JSON objektum → dict (normál wire form, csendben tovább),
        - bármilyen más érték (sima string, szám, lista) vagy hibás JSON → ``{}``
          és egy teaching üzenet a modellnek.
    * egyéb     → ``{}`` és teaching üzenet.

    Visszatérés: ``(coerced_dict, teaching_message)``. A teaching_message ``None``,
    amikor semmi coercion nem volt szükséges és a modellnek nincs mit tanítanunk.
    A caller a teaching üzenetet a model-visible csatornákra küldi
    (tool-result hibaüzenetként a meglévő invalid-arguments útvonalon), vagy
    naplózza figyelmeztetésként.
    """
    if isinstance(arguments, dict):
        return arguments, None

    if isinstance(arguments, str):
        stripped = arguments.strip()
        if not stripped:
            return {}, (
                f"{_TEACHING_PREFIX} Tool '{tool_name}' received an empty string as "
                f"arguments; treated as an empty object {_EMPTY_OBJECT}. Send a JSON "
                f"object, e.g. {_JSON_EXAMPLE}."
            )
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return {}, (
                f"{_TEACHING_PREFIX} Tool '{tool_name}' received invalid JSON as "
                f"arguments ({exc}); treated as an empty object {_EMPTY_OBJECT}. "
                f"Send a JSON object, e.g. {_JSON_EXAMPLE}."
            )
        if isinstance(parsed, dict):
            # Normál wire form: a string egy JSON objektumot tartalmaz.
            return parsed, None
        return {}, (
            f"{_TEACHING_PREFIX} Tool '{tool_name}' arguments parsed to a "
            f"{type(parsed).__name__}, not a JSON object; treated as an empty "
            f"object {_EMPTY_OBJECT}. Send a JSON object, e.g. {_JSON_EXAMPLE}."
        )

    return {}, (
        f"{_TEACHING_PREFIX} Tool '{tool_name}' arguments were {type(arguments).__name__}, "
        f"not a string or dict; treated as an empty object {_EMPTY_OBJECT}. Send a JSON "
        f"object, e.g. {_JSON_EXAMPLE}."
    )
