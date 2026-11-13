"""Signature + behavior pins for every exchangelib API v5 calls.

The v3 tree learned this the hard way: MagicMock accepts any kwarg, so a
renamed/removed parameter sails through unit tests and detonates in
production (`disposal_type`, `OofReply`, …). Every kwarg-bearing call site
in v5/ewsmcp pins the REAL `inspect.signature` here, and the lies that
drove the Phase B criticals are pinned as behavior contracts:

- ``Q(conversation_id=<str>)`` raises TypeError — the ConversationId
  OBJECT is required (get_thread critical).
- ``Folder.total_count`` is a stored field, not a live probe — reading it
  does not round-trip, which is why the connection probe must call
  ``Folder.refresh()`` (false-warm critical).
- ``CachingProtocol.clear_cache`` exists — reset() relies on it to force
  a genuine auth renegotiation.

When exchangelib is upgraded these pins fail FIRST; update the call sites
together with the pins, never the pins alone.
"""

import inspect

import pytest
from exchangelib import Account, CalendarItem, Message, OofSettings
from exchangelib.fields import IntegerField
from exchangelib.folders import Folder
from exchangelib.folders.collections import FolderCollection
from exchangelib.items import Item, ReplyToItem
from exchangelib.properties import ConversationId
from exchangelib.protocol import CachingProtocol, FaultTolerance, Protocol
from exchangelib.queryset import Q
from exchangelib.version import EXCHANGE_2016, Version


def _params(func):
    return list(inspect.signature(func).parameters)


PINS = {
    # --- fetch / projection (mail_read._fetch_one, writes._fetch_many)
    Account.fetch: ["self", "ids", "folder", "only_fields", "chunk_size"],
    # --- draft lifecycle (writes._create_draft / _update_draft)
    Message.create_reply: ["self", "subject", "body", "to_recipients",
                           "cc_recipients", "bcc_recipients", "author"],
    Message.create_reply_all: ["self", "subject", "body", "author"],
    Item.create_forward: ["self", "subject", "body", "to_recipients",
                          "cc_recipients", "bcc_recipients"],
    ReplyToItem.save: ["self", "folder"],
    Item.save: ["self", "update_fields", "conflict_resolution",
                "send_meeting_invitations"],
    Message.send: ["self", "save_copy", "copy_to_folder",
                   "conflict_resolution", "send_meeting_invitations"],
    # --- moves / deletes (writes._move_messages / _delete_messages)
    Item.move: ["self", "to_folder"],
    Item.move_to_trash: ["self", "send_meeting_cancellations",
                         "affected_task_occurrences", "suppress_read_receipts"],
    Item.soft_delete: ["self", "send_meeting_cancellations",
                       "affected_task_occurrences", "suppress_read_receipts"],
    Item.delete: ["self", "send_meeting_cancellations",
                  "affected_task_occurrences", "suppress_read_receipts"],
    # --- calendar (writes._create_event / _respond_to_event / _cancel_event)
    CalendarItem.save: ["self", "update_fields", "conflict_resolution",
                        "send_meeting_invitations"],
    # --- free/busy + GAL (calendar_people)
    Protocol.get_free_busy_info: ["self", "accounts", "start", "end",
                                  "merged_free_busy_interval", "requested_view"],
    Protocol.resolve_names: ["self", "names", "parent_folders",
                             "return_full_contact_data", "search_scope", "shape"],
    # --- calendar window expansion (list_events; max_items is the Phase D cap)
    FolderCollection.view: ["self", "start", "end", "max_items", "args", "kwargs"],
    # --- delta sync primitives (the Phase E cache engine rides on these)
    Folder.sync_items: ["self", "sync_state", "only_fields", "ignore",
                        "max_changes_returned", "sync_scope"],
    Folder.sync_hierarchy: ["self", "sync_state", "only_fields"],
    Folder.refresh: ["self"],
    # --- reliability (gateway)
    FaultTolerance.__init__: ["self", "max_wait"],
}


@pytest.mark.parametrize("func", list(PINS), ids=lambda f: f.__qualname__)
def test_signature_pin(func):
    assert _params(func) == PINS[func], (
        f"exchangelib changed {func.__qualname__} — update the call sites "
        "in v5/ewsmcp together with this pin"
    )


def test_item_delete_has_no_disposal_kwargs():
    """The 4.x -> 5.x trap that produced two production outages: delete()
    IS HardDelete; disposal_type/delete_type kwargs are gone."""
    params = _params(Item.delete)
    assert "disposal_type" not in params
    assert "delete_type" not in params


def test_calendar_responses_accept_extra_kwargs():
    """accept/decline/tentatively_accept forward **kwargs (writes passes
    body=...); cancel() is pure **kwargs (writes passes new_body=...)."""
    for method in (CalendarItem.accept, CalendarItem.decline,
                   CalendarItem.tentatively_accept):
        sig = inspect.signature(method)
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in sig.parameters.values()), method.__qualname__
        assert "message_disposition" in sig.parameters
    cancel_sig = inspect.signature(CalendarItem.cancel)
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in cancel_sig.parameters.values())


def test_oof_settings_shape():
    """5.x: internal/external replies are PLAIN STRINGS (OofReply is gone);
    the state constants and field set are what writes._set_oof relies on."""
    field_names = [f.name for f in OofSettings.FIELDS]
    assert field_names == ["state", "external_audience", "start", "end",
                           "internal_reply", "external_reply"]
    for const in ("ENABLED", "SCHEDULED", "DISABLED"):
        assert isinstance(getattr(OofSettings, const), str)
    import exchangelib
    assert not hasattr(exchangelib, "OofReply")


# --- behavior contracts: the three lies behind the Phase B criticals --------


def test_conversation_id_filter_requires_the_object():
    """Q(conversation_id=<str>) raises TypeError at clean/serialization
    time; the ConversationId object passes. get_thread passes the seed
    item's own conversation_id object for exactly this reason."""
    version = Version(build=EXCHANGE_2016)
    with pytest.raises(TypeError, match="ConversationId"):
        Q(conversation_id="ABC123").clean(version=version)
    Q(conversation_id=ConversationId("ABC123")).clean(version=version)  # no raise


def test_folder_total_count_is_a_stored_field_not_a_probe():
    """total_count is populated by refresh()/walk — reading it costs no
    network I/O, so it can NEVER serve as a connection probe. This is the
    false-warm bug's root cause, pinned so nobody 'simplifies' the probe
    back to it."""
    assert isinstance(Folder.total_count, IntegerField)


def test_folder_class_is_a_real_field():
    """folder_class is the raw EWS FolderClass string exposed verbatim by
    list_folders (mail/calendar/contacts type marker) — pinned so an
    exchangelib rename/removal breaks this test before it breaks the tool."""
    assert "folder_class" in [f.name for f in Folder.FIELDS]


def test_protocol_cache_is_evictable():
    """gateway.reset() calls CachingProtocol.clear_cache() to force a
    fresh session + auth negotiation; closing the protocol alone hands
    the same wedged instance back on the next build."""
    assert callable(getattr(CachingProtocol, "clear_cache", None))
