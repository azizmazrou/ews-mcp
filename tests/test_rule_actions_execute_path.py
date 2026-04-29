"""Execute-path tests for the rule action dispatcher.

History: ``rule_tools._apply_actions`` had three defects in three lines
in the ``move_to_folder`` branch:

  1. Lazy ``from .folder_tools import resolve_folder_for_account`` —
     the function lives in ``email_tools.py``, so this raised ImportError.
  2. Missing ``await`` on a coroutine.
  3. Wrong kwarg name (``folder_name=`` instead of ``folder_identifier=``).

All three were masked by the per-action ``try/except Exception`` that
turns any failure into a logged ``"error"`` field — callers that don't
inspect per-action logs see the rule run as ``"ok"`` overall while no
mail is actually moved.

These tests exercise the action dispatcher directly so future regressions
fail loudly. Each named after the action type it pins.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.tools.rule_tools import _apply_actions


class _FakeFolder:
    def __init__(self, name: str):
        self.name = name


class _FakeMessage:
    """Typed-enough stub: rejects unknown kwargs to ``save()`` /
    ``delete()`` / ``move()``. ``MagicMock`` accepts anything and was the
    original mock-drift source."""

    def __init__(self):
        self.importance = "Normal"
        self.categories: list = []
        self.is_read = False
        self.id = "AAMk-fake"
        self.move_calls: list = []
        self.save_calls: list = []

    def move(self, folder):
        # Specifically reject coroutines — the fixed-bug regression.
        import inspect as _inspect
        assert not _inspect.iscoroutine(folder), (
            "move() received a coroutine — resolve_folder_for_account was "
            "not awaited in the action dispatcher."
        )
        self.move_calls.append(folder)

    def save(self, *, update_fields=None):
        # Pin the kwarg name. exchangelib uses ``update_fields``; if the
        # source ever passes something else the test fails.
        self.save_calls.append(tuple(update_fields or ()))


@pytest.fixture
def fake_account():
    return MagicMock(name="account")


@pytest.fixture
def fake_tool():
    """Stand-in for the BaseTool — ``_apply_actions`` only uses it to
    reach the memory store for ``track_commitment``, which we don't
    exercise in the move-folder path."""
    return MagicMock(name="tool")


@pytest.mark.asyncio
async def test_move_to_folder_resolves_and_moves(monkeypatch, fake_tool, fake_account):
    """The original bug: ``move_to_folder`` action did nothing because
    of three stacked defects. Pin the contract: resolver is called with
    the destination as a positional arg, and ``message.move(folder)``
    receives the resolved folder (not a coroutine)."""
    target = _FakeFolder("Archive/2026")
    resolver = AsyncMock(return_value=target)
    monkeypatch.setattr(
        "src.tools.rule_tools.resolve_folder_for_account", resolver
    )

    message = _FakeMessage()
    actions = [{"type": "move_to_folder", "destination": "Archive/2026"}]
    out = await _apply_actions(
        fake_tool, fake_account, message, actions, dry_run=False
    )

    resolver.assert_awaited_once_with(fake_account, "Archive/2026")
    assert message.move_calls == [target]
    assert out[0]["status"] == "ok"
    assert out[0]["destination"] == "Archive/2026"


@pytest.mark.asyncio
async def test_move_to_folder_dry_run_does_not_resolve_or_move(
    monkeypatch, fake_tool, fake_account
):
    resolver = AsyncMock(return_value=_FakeFolder("Archive"))
    monkeypatch.setattr(
        "src.tools.rule_tools.resolve_folder_for_account", resolver
    )

    message = _FakeMessage()
    actions = [{"type": "move_to_folder", "destination": "Archive"}]
    out = await _apply_actions(
        fake_tool, fake_account, message, actions, dry_run=True
    )

    resolver.assert_not_awaited()
    assert message.move_calls == []
    assert out[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_move_to_folder_missing_destination_is_validation_error(
    fake_tool, fake_account
):
    message = _FakeMessage()
    out = await _apply_actions(
        fake_tool, fake_account, message, [{"type": "move_to_folder"}],
        dry_run=False,
    )
    # Per-action error handling preserves the failure as logged status.
    assert out[0]["status"] == "error"
    assert "destination" in out[0]["error"].lower()


@pytest.mark.asyncio
async def test_flag_importance_uses_update_fields_kwarg(fake_tool, fake_account):
    """``message.save(update_fields=["importance"])`` — pin the kwarg
    name so a rename in source/exchangelib fails here."""
    message = _FakeMessage()
    actions = [{"type": "flag_importance", "importance": "High"}]
    out = await _apply_actions(
        fake_tool, fake_account, message, actions, dry_run=False
    )
    assert message.importance == "High"
    assert message.save_calls == [("importance",)]
    assert out[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_categorize_merges_existing_unique(fake_tool, fake_account):
    message = _FakeMessage()
    message.categories = ["Existing"]
    actions = [{"type": "categorize", "categories": ["Project-X", "Existing"]}]
    out = await _apply_actions(
        fake_tool, fake_account, message, actions, dry_run=False
    )
    assert message.categories == ["Existing", "Project-X"]
    assert message.save_calls == [("categories",)]
    assert out[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_mark_read_flips_flag_and_saves(fake_tool, fake_account):
    message = _FakeMessage()
    actions = [{"type": "mark_read"}]
    out = await _apply_actions(
        fake_tool, fake_account, message, actions, dry_run=False
    )
    assert message.is_read is True
    assert message.save_calls == [("is_read",)]
    assert out[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_unknown_action_type_logged_as_error(fake_tool, fake_account):
    message = _FakeMessage()
    actions = [{"type": "nonsense"}]
    out = await _apply_actions(
        fake_tool, fake_account, message, actions, dry_run=False
    )
    assert out[0]["status"] == "error"
    assert "unknown action type" in out[0]["error"].lower()
