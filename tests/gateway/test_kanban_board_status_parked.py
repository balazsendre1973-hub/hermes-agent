"""Dispatcher: a `board_status.json` `parked` listáján lévő boardokat a
dispatcher soha nem indítja (t_db31a662).

A parkolt board kártyáinak az állapota nem változik (nem blocked, nem
archived) — csak nem dispatchelhető. A skip naplózás csak állapotváltáskor
történik, így egy üres gateway csendet tart fenn.

Forráspatter: `error_card_gate.board_status` / `next_card_watch`
`load_parked_boards` — ugyanaz a `~/.hermes/kanban/board_status.json`,
konfigolható `kanban.board_status_file`-val, fail-open.
"""

from __future__ import annotations

from gateway.kanban_watchers_common import parked_board_slugs
from gateway.kanban_watchers_dispatcher import (
    _DispatcherSettings,
    _KanbanDispatcher,
)


class _FakeKB:
    """Minimal kanban_db stand-in: csak a live board slug-jei.

    `DEFAULT_BOARD` a `_board_slugs` fallback-ja, ha egy board-nál nincs slug.
    """

    DEFAULT_BOARD = "default"

    def __init__(self, slugs: list[str]) -> None:
        self._slugs = list(slugs)

    def list_boards(self, include_archived: bool = False) -> list[dict]:
        return [{"slug": s} for s in self._slugs]


def _settings(**overrides) -> _DispatcherSettings:
    base = dict(
        interval=1.0,
        max_spawn=None,
        max_in_progress=None,
        failure_limit=1,
        stale_timeout_seconds=0,
        reconcile_orphans=False,
        default_assignee=None,
        max_in_progress_per_profile=None,
    )
    base.update(overrides)
    return _DispatcherSettings(**base)


def test_parked_board_is_skipped_and_logged_once(tmp_path, caplog):
    status = tmp_path / "board_status.json"
    status.write_text(
        '{"updated_at":"2026-11-07T18:30:00+01:00","active":["default"],'
        '"parked":["developer","btcusd-jev"]}',
        encoding="utf-8",
    )
    kb = _FakeKB(["default", "developer", "btcusd-jev"])
    disp = _KanbanDispatcher(kb, _settings(), {"board_status_file": str(status)})

    with caplog.at_level("INFO", logger="gateway.run"):
        assert disp._board_slugs() == ["default"]

    assert any(
        "board developer is parked; skipping" in r.getMessage()
        for r in caplog.records
    )
    assert any(
        "board btcusd-jev is parked; skipping" in r.getMessage()
        for r in caplog.records
    )

    # Egyzelfde parked lista a következő tickben → nincs ismételt naplózás.
    caplog.records.clear()
    assert disp._board_slugs() == ["default"]
    assert not any(
        "is parked; skipping" in r.getMessage() for r in caplog.records
    )


def test_parked_to_active_resumes_on_next_tick(tmp_path, caplog):
    status = tmp_path / "board_status.json"
    status.write_text('{"active":["default"],"parked":["developer"]}', encoding="utf-8")
    kb = _FakeKB(["default", "developer"])
    disp = _KanbanDispatcher(kb, _settings(), {"board_status_file": str(status)})

    assert disp._board_slugs() == ["default"]

    # Fin a developer boardot visszamozgatja az active listába.
    status.write_text(
        '{"active":["default","developer"],"parked":[]}', encoding="utf-8"
    )
    with caplog.at_level("INFO", logger="gateway.run"):
        assert disp._board_slugs() == ["default", "developer"]

    assert any(
        "board developer is active again; resuming dispatch" in r.getMessage()
        for r in caplog.records
    )


def test_missing_file_fail_open_all_boards_active(tmp_path, caplog):
    kb = _FakeKB(["default", "developer"])
    disp = _KanbanDispatcher(
        kb, _settings(), {"board_status_file": str(tmp_path / "missing.json")}
    )
    assert disp._board_slugs() == ["default", "developer"]
    assert not caplog.records


def test_invalid_json_fail_open_all_boards_active(tmp_path, caplog):
    status = tmp_path / "board_status.json"
    status.write_text("{not valid json", encoding="utf-8")
    kb = _FakeKB(["default", "developer"])
    disp = _KanbanDispatcher(
        kb, _settings(), {"board_status_file": str(status)}
    )
    assert disp._board_slugs() == ["default", "developer"]
    assert not caplog.records


def test_parked_board_slugs_parses_list(tmp_path):
    status = tmp_path / "board_status.json"
    status.write_text('{"parked":["developer","btcusd-jev"]}', encoding="utf-8")
    assert parked_board_slugs({"board_status_file": str(status)}) == {
        "developer",
        "btcusd-jev",
    }


def test_tick_once_never_touches_parked_board(tmp_path, caplog):
    status = tmp_path / "board_status.json"
    status.write_text('{"active":["default"],"parked":["developer"]}', encoding="utf-8")
    kb = _FakeKB(["default", "developer"])
    disp = _KanbanDispatcher(kb, _settings(), {"board_status_file": str(status)})

    called: list[str] = []
    disp.tick_once_for_board = lambda slug: called.append(slug)  # type: ignore[method-assign]

    results = disp.tick_once()
    assert called == ["default"]
    assert "developer" not in called
    assert [slug for slug, _ in results] == ["default"]
