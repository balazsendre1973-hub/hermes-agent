"""Unit teszt az agent/tool_arg_coercion.py coercion segédfüggvényhez.

Független a Hermes futtatásoktól (csak stdlib). Futtatható két módon:

    .venv/bin/python -m pytest tests/test_tool_arg_coercion.py -q   (10/10 PASS)
    .venv/bin/python tests/test_tool_arg_coercion.py                (10× PASS sor)
"""

import importlib.util
import sys
from pathlib import Path

REPO_COPY = Path(__file__).resolve().parent.parent / "agent" / "tool_arg_coercion.py"

spec = importlib.util.spec_from_file_location("tool_arg_coercion", REPO_COPY)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
coerce = mod.coerce_tool_arguments


def _teaching_ok(msg, tool_name):
    return bool(msg) and "JSON object" in msg and tool_name in msg


# 1. dict bemenet: változatlanul továbbadva (identity), teaching None
def test_dict_bemenet_visszaadva_unchanged():
    d = {"path": "README.md", "mode": "replace"}
    got, msg = coerce(d, tool_name="write_file")
    assert got is d
    assert msg is None


# 2. str, érvényes JSON objektum (normál wire form): dictté, teaching None (csendes)
def test_str_json_objektum_dict_teaching_none():
    got, msg = coerce('{"q": "alpha"}', tool_name="web_search")
    assert got == {"q": "alpha"}
    assert msg is None


# 3. str, sima szöveg (a modell stringet küldött objektum helyett): {} + teaching
def test_str_sima_szoveg_empty_plus_teaching():
    got, msg = coerce('just read the file', tool_name="read_file")
    assert got == {}
    assert _teaching_ok(msg, "read_file")


# 4. str, hibás JSON: {} + teaching
def test_str_hibas_json_empty_plus_teaching():
    got, msg = coerce('{"path": ', tool_name="write_file")
    assert got == {}
    assert _teaching_ok(msg, "write_file")


# 5. str, üres: {} + teaching
def test_str_ures_empty_plus_teaching():
    got, msg = coerce('', tool_name="terminal")
    assert got == {}
    assert _teaching_ok(msg, "terminal")


# 6. str, JSON tömb (nem objektum): {} + teaching
def test_str_json_tomb_empty_plus_teaching():
    got, msg = coerce('[1, 2, 3]', tool_name="delegate_task")
    assert got == {}
    assert _teaching_ok(msg, "delegate_task")


# 7. str, JSON szám: {} + teaching
def test_str_json_szam_empty_plus_teaching():
    got, msg = coerce('42', tool_name="web_search")
    assert got == {}
    assert _teaching_ok(msg, "web_search")


# 8. nem str, nem dict (pl. int): {} + teaching
def test_int_bemenet_empty_plus_teaching():
    got, msg = coerce(7, tool_name="web_search")
    assert got == {}
    assert _teaching_ok(msg, "web_search")


# 9. None bemenet: {} + teaching
def test_none_bemenet_empty_plus_teaching():
    got, msg = coerce(None, tool_name="write_file")
    assert got == {}
    assert _teaching_ok(msg, "write_file")


# 10. teaching üzenet tartalmi ellenőrzése
def test_teaching_tartalma_utasitast_szamol():
    got, msg = coerce('hello', tool_name="terminal")
    assert "Send a JSON object" in msg
    assert "terminal" in msg


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
