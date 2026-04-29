"""Contract tests pinning the real ``exchangelib`` API surface.

The single recurring class of regression in this project is **mock-drift**:
``MagicMock().delete(disposal_type=X)`` silently records any kwarg, so a
unit test that mocks an exchangelib object will stay green while the real
call against a 5.x server raises ``TypeError``. The OOF/``OofReply`` and
``Item.delete(disposal_type=...)`` outages were both this exact pattern.

These tests pin the signatures of every exchangelib symbol the production
code calls with kwargs. If ``exchangelib`` upgrades and a kwarg appears,
disappears, or is renamed, CI fails *before* the bad call ships.

Adding a new ``exchangelib`` API call site? Add the corresponding
signature pin here. Treat this file as the canonical inventory.
"""
from __future__ import annotations

import inspect

import pytest


# ---------------------------------------------------------------------------
# Item / Message / CalendarItem mutation methods
# ---------------------------------------------------------------------------


def test_item_delete_takes_no_disposal_type_kwarg():
    """``Item.delete()`` already does HARD_DELETE in 5.x. The legacy
    ``disposal_type`` / ``delete_type`` kwargs raised ``TypeError`` on the
    live NAS for every ``delete_email(permanent=True)`` call until #108."""
    from exchangelib.items import Item
    params = set(inspect.signature(Item.delete).parameters)
    assert "disposal_type" not in params
    assert "delete_type" not in params


def test_item_save_accepts_update_fields():
    """``rule_tools._apply_actions`` calls ``message.save(update_fields=[...])``
    for ``flag_importance`` / ``categorize`` / ``mark_read``. If exchangelib
    renames the kwarg (``only_fields``? ``fields``?), every rule action that
    persists state silently fails with the per-action try/except masking it."""
    from exchangelib.items import Item
    params = set(inspect.signature(Item.save).parameters)
    assert "update_fields" in params, (
        "Item.save() lost the update_fields kwarg. Update src/tools/rule_tools.py "
        "and any other call site that passes update_fields=."
    )


def test_message_send_accepts_save_copy():
    """``EmailService.send_message`` and ``email_tools`` send paths call
    ``message.send(save_copy=...)``. Drift here breaks every send."""
    from exchangelib import Message
    params = set(inspect.signature(Message.send).parameters)
    assert "save_copy" in params
    # ``save_copy`` was previously the kwarg; if exchangelib renames to
    # ``copy_to_sent_folder`` we want to know.


def test_account_fetch_signature_accepts_iterable():
    """``GetEmailsBulkTool`` calls ``account.fetch([Message(id=x), ...])``.
    Pin that ``fetch`` is still the bulk method and accepts a positional
    iterable."""
    from exchangelib import Account
    sig = inspect.signature(Account.fetch)
    params = list(sig.parameters)
    # ``fetch(self, ids, ...)``
    assert params[0] == "self"
    assert "ids" in params, (
        "Account.fetch() lost its ids parameter. GetEmailsBulkTool will break."
    )


# ---------------------------------------------------------------------------
# OofSettings — the OofReply outage
# ---------------------------------------------------------------------------


def test_oofsettings_internal_reply_is_string_field():
    """``OofSettings.internal_reply`` and ``external_reply`` are
    ``MessageField(value_cls=str)`` in 5.x — they take plain strings, not
    a wrapper. The lazy ``from exchangelib import OofReply`` raised
    ``ImportError`` on every ``oof_settings(action="set", ...)`` until
    the fix in commit 62cfb26.

    This test asserts the FIELD CLASS the production code expects to be
    a string. If exchangelib re-introduces a wrapper, the field's
    ``value_cls`` will change and we'll see it before prod does."""
    from exchangelib import OofSettings
    fields_by_name = {f.name: f for f in OofSettings.FIELDS}
    assert "internal_reply" in fields_by_name
    assert "external_reply" in fields_by_name
    # ``MessageField`` in 5.x exposes value_cls=str; if exchangelib
    # re-wraps it, this fails and the source needs updating.
    assert fields_by_name["internal_reply"].value_cls is str, (
        "OofSettings.internal_reply.value_cls is no longer str. The "
        "source in src/tools/oof_tools.py assigns plain strings — adjust "
        "if a wrapper class is back."
    )
    assert fields_by_name["external_reply"].value_cls is str


def test_oofsettings_construct_with_string_replies():
    """End-to-end constructor pin: building an OofSettings the way the
    source does in src/tools/oof_tools.py:_set_settings must succeed
    against the real class (not a MagicMock)."""
    from exchangelib import OofSettings
    oof = OofSettings(
        state="Disabled",
        external_audience="All",
        internal_reply="hi internal",
        external_reply="hi external",
    )
    assert oof.internal_reply == "hi internal"
    assert oof.external_reply == "hi external"


def test_no_oofreply_symbol_in_exchangelib():
    """The ``OofReply`` wrapper class was removed in exchangelib 5.x.
    Detect any future re-introduction so we can decide whether to wrap
    again or keep using strings."""
    import exchangelib
    assert not hasattr(exchangelib, "OofReply"), (
        "exchangelib.OofReply is back. Decide whether to re-wrap "
        "internal_reply/external_reply in src/tools/oof_tools.py or keep "
        "the current string assignment."
    )


# ---------------------------------------------------------------------------
# FileAttachment — the inline content_id outage
# ---------------------------------------------------------------------------


def test_file_attachment_init_accepts_content_id():
    """``add_attachment(is_inline=True)`` must pass ``content_id`` through
    to ``FileAttachment(...)`` so HTML bodies can reference inline images
    via ``cid:<id>``. The schema dropped this kwarg before the fix in
    commit 0fb0948 — pin it so the next refactor doesn't lose it."""
    from exchangelib import FileAttachment
    fields_by_name = {f.name: f for f in FileAttachment.FIELDS}
    assert "content_id" in fields_by_name, (
        "FileAttachment lost the content_id field. Inline images "
        "(cid:foo) will not render. Adjust src/tools/attachment_tools.py."
    )
    assert "is_inline" in fields_by_name


def test_file_attachment_can_be_constructed_inline_with_content_id():
    """Construct the way ``AddAttachmentTool`` does in production."""
    from exchangelib import FileAttachment
    att = FileAttachment(
        name="logo.png",
        content=b"\x89PNG\r\n\x1a\n",
        content_id="logo123",
        is_inline=True,
    )
    assert att.content_id == "logo123"
    assert att.is_inline is True


# ---------------------------------------------------------------------------
# Folder / CalendarItem call sites used by the tools
# ---------------------------------------------------------------------------


def test_folder_save_takes_no_unexpected_kwargs():
    """``manage_folder`` calls ``folder.save()`` after rename. Pin that
    save() doesn't suddenly require kwargs we don't pass."""
    from exchangelib.folders import Folder
    sig = inspect.signature(Folder.save)
    # All non-self parameters should have defaults — we call save() bare.
    for name, p in sig.parameters.items():
        if name == "self":
            continue
        assert p.default is not inspect.Parameter.empty, (
            f"Folder.save() now requires {name!r} — every manage_folder "
            "rename/move call will break. Update src/tools/folder_tools.py."
        )


def test_calendar_item_save_accepts_send_meeting_invitations():
    """``CreateAppointmentTool`` and ``UpdateAppointmentTool`` pass
    ``send_meeting_invitations=...`` to ``CalendarItem.save()``. Pin it
    so the kwarg can't disappear silently."""
    from exchangelib import CalendarItem
    params = set(inspect.signature(CalendarItem.save).parameters)
    assert "send_meeting_invitations" in params, (
        "CalendarItem.save() lost send_meeting_invitations. Calendar "
        "create/update tools must be reviewed."
    )


def test_q_constructor_accepts_keyword_filters():
    """The advanced search builder constructs ``Q(subject__icontains=...)``
    and similar. ``Q`` is internal API; pin that the lookup we depend on
    is still recognised."""
    from exchangelib import Q
    # Constructing a Q with arbitrary kwargs should not raise; the
    # filter() consumer is the real validator, but the constructor
    # accepting **kwargs is our contract.
    q = Q(subject__icontains="hello")
    assert q is not None


# ---------------------------------------------------------------------------
# Indexed properties (contact_tools imports)
# ---------------------------------------------------------------------------


def test_indexed_properties_email_address_phone_number_importable():
    """``contact_tools.py`` imports ``EmailAddress`` and ``PhoneNumber``
    from ``exchangelib.indexed_properties``. The module path has shifted
    historically (``exchangelib.properties`` vs ``indexed_properties``)."""
    from exchangelib.indexed_properties import EmailAddress, PhoneNumber  # noqa: F401


# ---------------------------------------------------------------------------
# parametrized inventory — every kwargs-bearing call site we depend on
# ---------------------------------------------------------------------------

# (api_callable, expected_kwarg_names)
_KWARG_CONTRACTS = [
    # Contract-style: (path, kwarg, must_be_present_or_absent: 'present'|'absent')
    ("exchangelib.items.Item.delete", "disposal_type", "absent"),
    ("exchangelib.items.Item.delete", "delete_type", "absent"),
    ("exchangelib.items.Item.save", "update_fields", "present"),
    ("exchangelib.Message.send", "save_copy", "present"),
    ("exchangelib.Account.fetch", "ids", "present"),
    ("exchangelib.CalendarItem.save", "send_meeting_invitations", "present"),
]


@pytest.mark.parametrize("dotted_path,kwarg,expectation", _KWARG_CONTRACTS)
def test_exchangelib_kwarg_contract(dotted_path, kwarg, expectation):
    """Single source of truth: one row per (api, kwarg) we depend on."""
    module_path, attr = dotted_path.rsplit(".", 1)
    # Walk the dotted path, since some live under .items / .folders / etc.
    parts = module_path.split(".")
    obj = __import__(parts[0])
    for p in parts[1:]:
        obj = getattr(obj, p)
    func = getattr(obj, attr)
    params = set(inspect.signature(func).parameters)
    if expectation == "present":
        assert kwarg in params, (
            f"{dotted_path} lost kwarg {kwarg!r} — production code passes it."
        )
    else:
        assert kwarg not in params, (
            f"{dotted_path} now accepts {kwarg!r} — production code assumes "
            "it does not. Revisit and decide whether to pass it."
        )
