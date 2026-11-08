"""Tests for AIAgent._repair_tool_call — tool-name normalization.

Regression guard for #14784: Claude-style models sometimes emit
class-like tool-call names (``TodoTool_tool``, ``Patch_tool``,
``BrowserClick_tool``, ``PatchTool``). Before the fix they returned
"Unknown tool" even though the target tool was registered under a
snake_case name. The repair routine now normalizes CamelCase,
strips trailing ``_tool`` / ``-tool`` / ``tool`` suffixes (up to
twice to handle double-tacked suffixes like ``TodoTool_tool``), and
falls back to fuzzy match.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


VALID = {
    "todo",
    "patch",
    "browser_click",
    "browser_navigate",
    "web_search",
    "read_file",
    "write_file",
    "terminal",
    "execute_code",
    "session_search",
}


@pytest.fixture
def repair():
    """Return a bound _repair_tool_call built on a minimal shell agent.

    We avoid constructing a real AIAgent (which pulls in credential
    resolution, session DB, etc.) because the repair routine only
    reads self.valid_tool_names. A SimpleNamespace stub is enough to
    bind the unbound function.
    """
    from run_agent import AIAgent
    stub = SimpleNamespace(valid_tool_names=VALID)
    return AIAgent._repair_tool_call.__get__(stub, AIAgent)


class TestExistingBehaviorStillWorks:
    """Pre-existing repairs must keep working (no regressions)."""

    def test_lowercase_already_matches(self, repair):
        assert repair("browser_click") == "browser_click"







class TestClassLikeEmissions:
    """Regression coverage for #14784 — CamelCase + _tool suffix variants."""

    def test_camel_case_no_suffix(self, repair):
        assert repair("BrowserClick") == "browser_click"









class TestEdgeCases:
    """Edge inputs that must not crash or produce surprising results."""

    def test_empty_string(self, repair):
        assert repair("") is None





class TestVolcEngineXmlPollution:
    """Regression coverage for #33007 — VolcEngine ``api/plan`` endpoint
    leaks raw XML attribute fragments into ``tool_use.name``.

    Observed in production with the ``anthropic_messages`` API mode:

        terminal" parameter="command" string="true
        execute_code" parameter="code" string="true
        session_search" parameter="session_id" string="true

    The fix trims at the first ``"``/``'``/``<``/``>`` so the rest of
    the repair pipeline can resolve the cleaned name to a real tool.
    """

    def test_terminal_with_xml_attribute_pollution(self, repair):
        # Exact pattern from the bug report (terminal call).
        polluted = 'terminal" parameter="command" string="true'
        assert repair(polluted) == "terminal"




    def test_tool_name_with_trailing_quote_only(self, repair):
        # Minimal leak — just a stray trailing quote, no full attribute.
        assert repair('terminal"') == "terminal"



    def test_clean_tool_name_unaffected_by_sanitizer(self, repair):
        # Pure passthrough — no XML/quote chars, no change.
        assert repair("execute_code") == "execute_code"
        assert repair("session_search") == "session_search"

    def test_space_separated_name_still_normalizes(self, repair):
        # Critical: the XML strip must NOT consume whitespace, or the
        # legitimate ``"write file" -> write_file`` repair path breaks.
        assert repair("write file") == "write_file"


    def test_leading_quote_falls_through_to_fuzzy_match(self, repair):
        # Sanitizer only trims when the XML char is at idx > 0 — a
        # name that *starts* with a quote is left untouched so the
        # rest of the pipeline (fuzzy match at 0.7 cutoff) can still
        # recover the obvious target.
        assert repair('"terminal"') == "terminal"

class TestShellToTerminalAlias:
    """Regression coverage for 'shell' -> 'terminal' alias.
    
    Model-generated 'shell' tool calls must be repaired to 'terminal'.
    Other unknown names must NOT be repaired (ensure new patterns are detected).
    """
    
    def test_shell_repairs_to_terminal(self, repair):
        """'shell' should repair to 'terminal' via explicit alias."""
        assert repair("shell") == "terminal"
    
    def test_terminal_still_works(self, repair):
        """Direct 'terminal' calls should still work (no regression)."""
        assert repair("terminal") == "terminal"
    
    def test_unknown_name_returns_none(self, repair):
        """Truly unknown names (not in aliases or fuzzy match) return None."""
        # 'bash' is close to 'terminal' but below 0.7 fuzzy threshold
        # ('b'->'te' is ~0.09 similarity, well below 0.7)
        assert repair("bash") is None
        # 'run' has no alias and won't fuzzy-match to anything valid
        assert repair("run") is None
    
    def test_case_insensitive_alias(self, repair):
        """Alias should work on lowercase (already normalized before alias check)."""
        # The lowered version is already checked, so 'shell' -> 'terminal'
        assert repair("shell") == "terminal"


class TestAliasOnlyForShell:
    """Ensure the alias dict is narrow: only 'shell' -> 'terminal' is supported.
    
    Other potentially confused names (bash, run, sh, cmd, exec, command) 
    should return None to detect new patterns early.
    """
    
    def test_bash_not_aliased(self, repair):
        """'bash' should NOT be repaired (too different from 'terminal' for fuzzy)."""
        assert repair("bash") is None
    
    def test_sh_not_aliased(self, repair):
        """'sh' should NOT be repaired (too short for fuzzy match)."""
        assert repair("sh") is None
    
    def test_cmd_not_aliased(self, repair):
        """'cmd' should NOT be repaired."""
        assert repair("cmd") is None
    
    def test_exec_not_aliased(self, repair):
        """'exec' should NOT be repaired."""
        assert repair("exec") is None
    
    def test_command_not_aliased(self, repair):
        """'command' should NOT be repaired (not in valid tools)."""
        assert repair("command") is None
