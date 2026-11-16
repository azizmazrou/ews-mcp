"""Signature + behavior pins for every exchangelib API v5 calls.

The v3 tree learned this the hard way: MagicMock accepts any kwarg, so a
renamed/removed parameter sails through unit tests and fails at
runtime (`disposal_type`, `OofReply`, …). Every kwarg-bearing call site
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
from unittest.mock import patch

import pytest
from exchangelib import (
    BASIC,
    DELEGATE,
    Account,
    CalendarItem,
    Configuration,
    Credentials,
    EWSDateTime,
    EWSTimeZone,
    FileAttachment,
    HTMLBody,
    Message,
    OofSettings,
)
from exchangelib.attachments import (
    Attachment,
    AttachmentId,
    ItemAttachment,
)
from exchangelib.errors import (
    ErrorFolderNotFound,
    ErrorItemNotFound,
    ErrorServerBusy,
    UnauthorizedError,
)
from exchangelib.fields import (
    AttendeesField,
    BooleanField,
    ChoiceField,
    IntegerField,
    MailboxField,
    MimeContentField,
)
from exchangelib.folders import (
    DeletedItems,
    Folder,
    JunkEmail,
    MsgFolderRoot,
    Root,
    SingleFolderQuerySet,
)
from exchangelib.folders.collections import FolderCollection
from exchangelib.items import SEND_TO_NONE, Contact, Item, ReplyToItem
from exchangelib.properties import (
    Actions,
    Address,
    Conditions,
    ConversationId,
    CopyToFolder,
    Exceptions,
    FolderId,
    InboxRules,
    Mailbox,
    MoveToFolder,
    Rule,
)
from exchangelib.protocol import CachingProtocol, FaultTolerance, Protocol
from exchangelib.queryset import Q
from exchangelib.services import DeleteAttachment, GetFolder
from exchangelib.services.common import EWSAccountService
from exchangelib.services.inbox_rules import (
    CreateInboxRule,
    DeleteInboxRule,
    GetInboxRules,
    SetInboxRule,
    UpdateInboxRules,
)
from exchangelib.version import EXCHANGE_2016, Version
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from ewsmcp.gateway.client import RejectingFaultTolerance
from ewsmcp.gateway.rules import GetInboxRulesWithBlob
from ewsmcp.tools import calendar_people, mail_read, writes


def _params(func):
    return list(inspect.signature(func).parameters)


PINS = {
    # --- fetch / projection (mail_read._fetch_one, writes._fetch_many)
    Account.fetch: ["self", "ids", "folder", "only_fields", "chunk_size"],
    # --- draft lifecycle (writes._create_draft / _update_draft)
    Item.attach: ["self", "attachments"],
    Item.detach: ["self", "attachments"],
    Attachment.detach: ["self"],
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
    CalendarItem.recurring_master: ["self"],
    Item.refresh: ["self"],
    CalendarItem.save: ["self", "update_fields", "conflict_resolution",
                        "send_meeting_invitations"],
    # --- free/busy + GAL (calendar_people)
    Protocol.get_free_busy_info: ["self", "accounts", "start", "end",
                                  "merged_free_busy_interval", "requested_view"],
    Protocol.resolve_names: ["self", "names", "parent_folders",
                             "return_full_contact_data", "search_scope", "shape"],
    # --- calendar window expansion (list_events; max_items is the Phase D cap)
    FolderCollection.view: ["self", "start", "end", "max_items"],
    # --- delta sync primitives (the Phase E cache engine rides on these)
    Folder.sync_items: ["self", "sync_state", "only_fields", "ignore",
                        "max_changes_returned", "sync_scope"],
    Folder.sync_hierarchy: ["self", "sync_state", "only_fields"],
    Folder.refresh: ["self"],
    # --- reliability (gateway)
    FaultTolerance.__init__: ["self", "max_wait"],
    FaultTolerance.raise_response_errors: ["self", "response"],
    # --- live folder ancestry & discard verification (gateway.rules)
    SingleFolderQuerySet.__init__: ["self", "account", "folder"],
    SingleFolderQuerySet.resolve: ["self"],
    FolderCollection.resolve: ["self"],
    FolderCollection.get_folders: ["self", "additional_fields"],
    Root.get_default_folder: ["self", "folder_cls"],
    Root.get_folder: ["self", "folder"],
    # --- inbox rules (gateway.rules + tools.rules; exchangelib 5.6.0)
    Account.create_rule: ["self", "rule"],
    Account.set_rule: ["self", "rule"],
    Account.delete_rule: ["self", "rule"],
    GetInboxRules.call: ["self", "mailbox"],
    GetInboxRules.get_payload: ["self", "mailbox"],
    GetInboxRules._elem_to_obj: ["self", "elem"],
    GetInboxRules._get_element_container: ["self", "message", "name"],
    UpdateInboxRules.call: ["self", "rule", "remove_outlook_rule_blob"],
    UpdateInboxRules.get_payload: ["self", "rule", "remove_outlook_rule_blob"],
    CreateInboxRule._get_operation: ["self", "rule"],
    SetInboxRule._get_operation: ["self", "rule"],
    DeleteInboxRule._get_operation: ["self", "rule"],
    EWSAccountService._get_elements: ["self", "payload"],
    EWSAccountService._get_element_container: ["self", "message", "name"],
    EWSAccountService._elems_to_objs: ["self", "elems"],
    EWSAccountService.get: ["self", "expect_result", "kwargs"],
    Rule.from_xml: ["elem", "account"],
}


@pytest.mark.parametrize("func", list(PINS), ids=lambda f: f.__qualname__)
def test_signature_pin(func):
    assert _params(func) == PINS[func], (
        f"exchangelib changed {func.__qualname__} — update the call sites "
        "in v5/ewsmcp together with this pin"
    )


def test_item_delete_has_no_disposal_kwargs():
    """In exchangelib 5.x (unlike 4.x), delete() IS HardDelete;
    disposal_type/delete_type kwargs are gone."""
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


def test_flag_status_extended_property_pin():
    """flag_property relies on Message.register and ExtendedProperty's
    property_tag and property_type attributes."""
    from exchangelib.extended_properties import ExtendedProperty
    from ewsmcp.flag_property import FlagStatus, ensure_registered

    assert callable(getattr(Message, "register", None))
    assert callable(getattr(Message, "deregister", None))
    assert _params(Message.register) == ["attr_name", "attr_cls"]
    assert hasattr(ExtendedProperty, "property_tag")
    assert hasattr(ExtendedProperty, "property_type")
    assert issubclass(FlagStatus, ExtendedProperty)
    assert FlagStatus.property_tag == 0x1090
    assert FlagStatus.property_type == "Integer"
    ensure_registered()
    ensure_registered()
    assert [f.name for f in Message.FIELDS].count("flag_status") == 1


def test_streamable_http_session_manager_accepts_max_request_body_size():
    """http.serve_http passes max_request_body_size (added in mcp 1.30); pin
    against the real class so an older mcp fails here instead of at runtime."""
    sig = inspect.signature(StreamableHTTPSessionManager.__init__)
    for param in ("app", "json_response", "stateless", "max_request_body_size"):
        assert param in sig.parameters, param
    sig.bind(
        None,
        app=None,
        json_response=False,
        stateless=True,
        max_request_body_size=1024,
    )


def test_ewsdatetime_from_datetime_pin():
    """writes._set_oof converts stdlib ZoneInfo datetimes via EWSDateTime.from_datetime."""
    assert callable(getattr(EWSDateTime, "from_datetime", None))
    assert _params(EWSDateTime.from_datetime) == ["d"]



def test_unauthorized_error_exists_and_policy_override_matches_base():
    """RejectingFaultTolerance depends on exchangelib.errors.UnauthorizedError
    being a real exception distinct from ErrorServerBusy, and on overriding
    FaultTolerance.raise_response_errors(self, response) with matching params."""
    assert issubclass(UnauthorizedError, Exception)
    assert not issubclass(UnauthorizedError, ErrorServerBusy)
    assert issubclass(RejectingFaultTolerance, FaultTolerance)
    assert _params(RejectingFaultTolerance.raise_response_errors) == [
        "self", "response"
    ]


def test_cached_protocol_retry_policy_is_replaceable_via_protocol_config():
    """CachingProtocol hands back one Protocol per (service_endpoint, credentials),
    and Protocol.retry_policy reads protocol.config.retry_policy. _build_account
    relies on both when a cached Protocol carries an earlier gateway's policy.
    An explicit auth type and version keep this off the network."""
    CachingProtocol.clear_cache()
    try:
        creds = Credentials("svc", "secret")
        endpoint = "https://ews.example.com/EWS/Exchange.asmx"
        version = Version(build=EXCHANGE_2016)
        p1 = RejectingFaultTolerance(max_wait=60)
        p2 = RejectingFaultTolerance(max_wait=120)
        proto1 = Protocol(config=Configuration(
            service_endpoint=endpoint, credentials=creds, auth_type=BASIC,
            version=version, retry_policy=p1))
        proto2 = Protocol(config=Configuration(
            service_endpoint=endpoint, credentials=creds, auth_type=BASIC,
            version=version, retry_policy=p2))
        assert proto2 is proto1
        assert proto2.retry_policy is p1
        proto2.config.retry_policy = p2
        assert proto1.retry_policy is p2
    finally:
        CachingProtocol.clear_cache()


def test_every_forced_auth_setting_maps_to_a_scheme_the_library_accepts():
    """EWS_AUTH_TYPE_FORCE values are lower-case; exchangelib's constants are
    not (NTLM is upper-case). The gateway maps one onto the other: every value
    the setting allows must build a real Configuration."""
    from typing import get_args

    from ewsmcp.config import Settings
    from ewsmcp.gateway.client import AUTH_TYPE_BY_SETTING

    annotation = Settings.model_fields["ews_auth_type_force"].annotation
    allowed = set(get_args(get_args(annotation)[0]))
    assert allowed == set(AUTH_TYPE_BY_SETTING)
    for setting in sorted(allowed):
        config = Configuration(
            service_endpoint="https://ews.example.com/EWS/Exchange.asmx",
            credentials=Credentials("user", "secret"),
            auth_type=AUTH_TYPE_BY_SETTING[setting],
            version=Version(build=EXCHANGE_2016),
        )
        assert config.auth_type == AUTH_TYPE_BY_SETTING[setting]


def test_writes_draft_field_projections_match_real_message_fields():
    """Every field in writes.py's draft/reply projections must exist on
    exchangelib.Message and exclude mime_content."""
    # The library's own validator is the authority: it also accepts the item
    # id and changekey, which are not entries in Message.FIELDS.
    validator = Folder()
    version = Version(build=EXCHANGE_2016)
    for projection in (
        writes._REPLY_SOURCE_FIELDS,
        writes._DRAFT_ATTACH_FIELDS,
        writes._DRAFT_UPDATE_FIELDS,
        writes._DRAFT_UPDATE_ATTACH_FIELDS,
        writes._DRAFT_DELETE_FIELDS,
        writes._DRAFT_PREVIEW_FIELDS,
        writes._DRAFT_SEND_FIELDS,
        writes._EVENT_CANCEL_FIELDS,
        writes._ATTACH_MESSAGE_META_FIELDS,
    ):
        assert projection
        assert "mime_content" not in projection
        for name in projection:
            validator.validate_item_field(field=name, version=version)

    assert "mime_content" in writes._ATTACH_MESSAGE_MIME_FIELDS
    for name in writes._ATTACH_MESSAGE_MIME_FIELDS:
        validator.validate_item_field(field=name, version=version)

    # Verify offline that a real Message populated only with _REPLY_SOURCE_FIELDS
    # supports create_reply, create_reply_all, and create_forward without error.
    CachingProtocol.clear_cache()
    try:
        account = Account(
            primary_smtp_address="exec@corp.example",
            config=Configuration(
                service_endpoint="https://ews.example.com/EWS/Exchange.asmx",
                credentials=Credentials("svc", "secret"),
                auth_type=BASIC,
                version=Version(build=EXCHANGE_2016),
            ),
            autodiscover=False,
            access_type=DELEGATE,
            default_timezone=EWSTimeZone("UTC"),
        )
        msg = Message(
            account=account,
            id="RAW-MSG-1",
            changekey="CK-MSG-1",
            subject="Thread subject",
            sender=Mailbox(email_address="sender@corp.example"),
            author=Mailbox(email_address="sender@corp.example"),
            reply_to=[Mailbox(email_address="sender@corp.example")],
            to_recipients=[Mailbox(email_address="exec@corp.example")],
            cc_recipients=[Mailbox(email_address="peer@corp.example")],
            bcc_recipients=None,
        )
        body = HTMLBody("<html><body><p>ack</p></body></html>")
        rep = msg.create_reply("Re: Thread subject", body)
        assert [m.email_address for m in rep.to_recipients] == ["sender@corp.example"]
        rep_all = msg.create_reply_all("Re: Thread subject", body)
        assert rep_all.to_recipients
        fwd = msg.create_forward("Fwd: Thread subject", body, ["target@corp.example"])
        # Recipients given as plain strings are kept as given.
        assert list(fwd.to_recipients) == ["target@corp.example"]
    finally:
        CachingProtocol.clear_cache()


def test_account_rules_property_and_service_override():
    """Pin Account.rules as a property and verify GetInboxRulesWithBlob,
    CreateInboxRule, SetInboxRule, DeleteInboxRule, and SingleFolderQuerySet
    match exchangelib 5.6.0 signatures and instantiate offline without I/O."""
    assert isinstance(Account.rules, property)
    assert Account.rules.fget is not None
    assert _params(Account.rules.fget) == ["self"]
    for attr in ("trash", "junk", "msg_folder_root", "root"):
        assert hasattr(Account, attr), attr
    assert DeletedItems.DISTINGUISHED_FOLDER_ID == "deleteditems"
    assert JunkEmail.DISTINGUISHED_FOLDER_ID == "junkemail"
    assert MsgFolderRoot.DISTINGUISHED_FOLDER_ID == "msgfolderroot"
    assert issubclass(ErrorFolderNotFound, Exception)
    assert issubclass(ErrorItemNotFound, Exception)
    assert isinstance(Rule.is_enabled, BooleanField)
    assert {"self", "folders", "additional_fields", "shape"}.issubset(
        _params(GetFolder.call)
    )

    assert issubclass(GetInboxRulesWithBlob, GetInboxRules)
    assert _params(GetInboxRulesWithBlob._get_element_container) == _params(
        GetInboxRules._get_element_container
    )
    assert _params(GetInboxRulesWithBlob._elem_to_obj) == _params(
        GetInboxRules._elem_to_obj
    )
    CachingProtocol.clear_cache()
    try:
        account = Account(
            primary_smtp_address="user@example.com",
            config=Configuration(
                service_endpoint="https://ews.example.com/EWS/Exchange.asmx",
                credentials=Credentials("svc", "secret"),
                auth_type=BASIC,
                version=Version(build=EXCHANGE_2016),
            ),
            autodiscover=False,
            access_type=DELEGATE,
            default_timezone=EWSTimeZone("UTC"),
        )
        svc = GetInboxRulesWithBlob(account=account)
        assert svc.outlook_rule_blob_exists is None

        sample_rule = Rule(
            id="RAW-PIN-1",
            display_name="Pin Rule",
            priority=1,
            is_enabled=True,
            actions=Actions(mark_as_read=True),
        )
        for write_cls in (CreateInboxRule, SetInboxRule, DeleteInboxRule):
            wsvc = write_cls(account=account)
            assert callable(wsvc.get)
            payload = wsvc.get_payload(rule=sample_rule)
            assert payload is not None

        sfqs = SingleFolderQuerySet(
            account=account, folder=FolderId(id="RAWID-PIN-FOLDER")
        )
        assert list(sfqs.folder_collection.folders) == [
            FolderId(id="RAWID-PIN-FOLDER")
        ]
    finally:
        CachingProtocol.clear_cache()


def test_inbox_rule_model_fields_pin():
    """Pin Rule, Actions, Conditions, Exceptions, CopyToFolder, MoveToFolder,
    Address and InboxRules field sets so a future library addition to Actions
    or Conditions cannot slip past our allow-list / unsupported classification."""
    assert issubclass(Address, Mailbox)
    assert issubclass(Exceptions, Conditions)
    assert issubclass(MoveToFolder, CopyToFolder)
    assert [f.name for f in InboxRules.FIELDS] == ["rule"]
    assert [f.name for f in CopyToFolder.FIELDS] == [
        "folder_id",
        "distinguished_folder_id",
    ]
    assert [f.name for f in Rule.FIELDS] == [
        "id",
        "display_name",
        "priority",
        "is_enabled",
        "is_not_supported",
        "is_in_error",
        "conditions",
        "exceptions",
        "actions",
    ]
    assert [f.name for f in Actions.FIELDS] == [
        "assign_categories",
        "copy_to_folder",
        "delete",
        "forward_as_attachment_to_recipients",
        "forward_to_recipients",
        "mark_importance",
        "mark_as_read",
        "move_to_folder",
        "permanent_delete",
        "redirect_to_recipients",
        "send_sms_alert_to_recipients",
        "server_reply_with_message",
        "stop_processing_rules",
    ]


def test_message_mime_content_and_attachment_detach_pins():
    """Pin Message.mime_content, Message.size, Message/Item.detach,
    Attachment.detach, and saved-item detach via DeleteAttachment.get."""
    assert isinstance(Item.mime_content, MimeContentField)
    assert isinstance(Message.mime_content, MimeContentField)
    assert Message.mime_content.field_uri == "item:MimeContent"
    assert isinstance(Message.size, IntegerField)
    assert Message.size.field_uri == "item:Size"
    assert "mime_content" in [f.name for f in Message.FIELDS]
    assert _params(Message.detach) == ["self", "attachments"]
    assert _params(Attachment.detach) == ["self"]
    assert callable(getattr(DeleteAttachment, "get", None))
    att_fields = {f.name for f in Attachment.FIELDS}
    assert {"attachment_id", "name", "content_type", "size"}.issubset(att_fields)

    CachingProtocol.clear_cache()
    try:
        account = Account(
            primary_smtp_address="user@example.com",
            config=Configuration(
                service_endpoint="https://ews.example.com/EWS/Exchange.asmx",
                credentials=Credentials("svc", "secret"),
                auth_type=BASIC,
                version=Version(build=EXCHANGE_2016),
            ),
            autodiscover=False,
            access_type=DELEGATE,
            default_timezone=EWSTimeZone("UTC"),
        )
        raw_eml = b"From: sender@example.com\r\nSubject: Sample\r\n\r\nBody\r\n"
        source = Message(account=account, subject="Sample", mime_content=raw_eml)
        assert source.mime_content == raw_eml

        # Unsaved draft path.
        draft = Message(account=account, subject="Draft")
        eml_file = FileAttachment(
            name="Sample.eml",
            content=source.mime_content,
            content_type="message/rfc822",
        )
        draft.attach(eml_file)
        assert eml_file.parent_item is draft
        assert list(draft.attachments) == [eml_file]
        draft.detach([eml_file])
        assert list(draft.attachments) == []

        # Saved draft path (what update_draft uses): Item.detach calls
        # Attachment.detach -> DeleteAttachment.get once per attachment, and a
        # mid-batch error leaves remaining attachments in draft.attachments.
        att_one = FileAttachment(
            name="one.txt",
            content=b"1",
            attachment_id=AttachmentId(id="ATT-SAVED-1"),
        )
        att_two = FileAttachment(
            name="two.txt",
            content=b"2",
            attachment_id=AttachmentId(id="ATT-SAVED-2"),
        )
        saved_draft = Message(
            account=account,
            id="RAW-SAVED-DRAFT",
            changekey="CK-SAVED-0",
            subject="Saved draft",
            attachments=[att_one, att_two],
        )
        assert att_one.parent_item is saved_draft
        assert att_two.parent_item is saved_draft

        delete_calls: list[list[str]] = []

        def fake_get_ok(self, items, **kwargs):
            delete_calls.append([item.id for item in items])
            return None

        with patch.object(DeleteAttachment, "get", fake_get_ok):
            saved_draft.detach([att_one, att_two])
        assert delete_calls == [["ATT-SAVED-1"], ["ATT-SAVED-2"]]
        assert list(saved_draft.attachments) == []
        assert att_one.attachment_id is None
        assert att_one.parent_item is None

        # Partial failure on saved item: first succeeds, second raises.
        p_one = FileAttachment(
            name="p1.txt",
            content=b"1",
            attachment_id=AttachmentId(id="ATT-P-1"),
        )
        p_two = FileAttachment(
            name="p2.txt",
            content=b"2",
            attachment_id=AttachmentId(id="ATT-P-2"),
        )
        partial_draft = Message(
            account=account,
            id="RAW-SAVED-PARTIAL",
            changekey="CK-PARTIAL-0",
            subject="Partial draft",
            attachments=[p_one, p_two],
        )
        partial_calls: list[list[str]] = []

        def fake_get_fail_second(self, items, **kwargs):
            partial_calls.append([item.id for item in items])
            if len(partial_calls) == 2:
                raise ErrorServerBusy("busy")
            return None

        with patch.object(DeleteAttachment, "get", fake_get_fail_second):
            with pytest.raises(ErrorServerBusy):
                partial_draft.detach([p_one, p_two])
        assert partial_calls == [["ATT-P-1"], ["ATT-P-2"]]
        assert [a.name for a in partial_draft.attachments] == ["p2.txt"]
    finally:
        CachingProtocol.clear_cache()


def test_calendar_item_move_to_trash_and_meeting_fields_pins():
    """Pin CalendarItem.move_to_trash and the fields cancel_event reads to
    distinguish a plain appointment from a meeting and check its organizer."""
    assert issubclass(CalendarItem, Item)
    assert _params(CalendarItem.move_to_trash) == [
        "self",
        "send_meeting_cancellations",
        "affected_task_occurrences",
        "suppress_read_receipts",
    ]
    trash_sig = inspect.signature(CalendarItem.move_to_trash)
    assert trash_sig.parameters["send_meeting_cancellations"].default == SEND_TO_NONE

    cal_fields = {f.name for f in CalendarItem.FIELDS}
    assert {
        "item_class",
        "subject",
        "start",
        "end",
        "type",
        "is_meeting",
        "organizer",
        "my_response_type",
        "required_attendees",
        "optional_attendees",
        "resources",
    }.issubset(cal_fields)
    assert isinstance(CalendarItem.is_meeting, BooleanField)
    assert CalendarItem.is_meeting.field_uri == "calendar:IsMeeting"
    assert isinstance(CalendarItem.type, ChoiceField)
    assert CalendarItem.type.field_uri == "calendar:CalendarItemType"
    assert {c.value for c in CalendarItem.type.choices} == {
        "Exception",
        "Occurrence",
        "RecurringMaster",
        "Single",
    }
    assert isinstance(CalendarItem.organizer, MailboxField)
    assert CalendarItem.organizer.field_uri == "calendar:Organizer"
    assert isinstance(CalendarItem.required_attendees, AttendeesField)
    assert CalendarItem.required_attendees.field_uri == "calendar:RequiredAttendees"
    assert isinstance(CalendarItem.optional_attendees, AttendeesField)
    assert CalendarItem.optional_attendees.field_uri == "calendar:OptionalAttendees"
    assert isinstance(CalendarItem.my_response_type, ChoiceField)
    assert CalendarItem.my_response_type.field_uri == "calendar:MyResponseType"
    assert {c.value for c in CalendarItem.my_response_type.choices} == {
        "Accept",
        "Decline",
        "NoResponseReceived",
        "Organizer",
        "Tentative",
        "Unknown",
    }
def test_contact_fields_and_person_enrichment_pins():
    """Pin Contact field names relied on by find_people and get_contact."""
    contact_fields = {f.name for f in Contact.FIELDS}
    assert "department" in calendar_people._CONTACT_FIELDS
    assert "office" in calendar_people._CONTACT_FIELDS
    assert set(calendar_people._CONTACT_FIELDS).issubset(contact_fields)

    sample = Contact(
        display_name="Operations Lead",
        job_title="Lead",
        department="Operations",
        office="Building 2",
        company_name="Example Org",
    )
    assert sample.department == "Operations"
    assert sample.office == "Building 2"


def test_attachment_and_item_attachment_model_pins():
    """Pin Attachment, FileAttachment, ItemAttachment, and AttachmentId fields
    relied on by mail_read._get_attachment."""
    assert issubclass(FileAttachment, Attachment)
    assert issubclass(ItemAttachment, Attachment)
    att_fields = {f.name for f in Attachment.FIELDS}
    assert {"attachment_id", "name", "content_type", "size"}.issubset(att_fields)
    assert "id" in {f.name for f in AttachmentId.FIELDS}
    assert "_content" in {f.name for f in FileAttachment.FIELDS}
    assert isinstance(FileAttachment.content, property)
    assert "_item" in {f.name for f in ItemAttachment.FIELDS}
    assert isinstance(ItemAttachment.item, property)

    embedded = Message(subject="Embedded item")
    item_att = ItemAttachment(name="Embedded item", item=embedded)
    assert item_att.attachment_id is None
    assert item_att.item is embedded


def test_mail_read_importance_and_mime_content_field_pins():
    """Pin Item/Message importance, mime_content, size, and mail_read field
    projections against exchangelib 5.6.0."""
    assert isinstance(Item.importance, ChoiceField)
    assert Item.importance.field_uri == "item:Importance"
    importance_values = sorted(c.value for c in Item.importance.choices)
    assert importance_values == ["High", "Low", "Normal"]
    assert set(mail_read._IMPORTANCE_TO_EWS.values()) == set(importance_values)

    assert isinstance(Item.mime_content, MimeContentField)
    assert Item.mime_content.field_uri == "item:MimeContent"
    assert isinstance(Item.size, IntegerField)

    msg_fields = {f.name for f in Message.FIELDS}
    assert {"importance", "mime_content", "size"}.issubset(msg_fields)

    validator = Folder()
    version = Version(build=EXCHANGE_2016)
    for projection in (
        mail_read._PROJECTION,
        mail_read._CHECK_PROJECTION,
        mail_read._FULL_PROJECTION,
        mail_read._EML_SIZE_FIELDS,
    ):
        assert projection
        assert "mime_content" not in projection
        for name in projection:
            validator.validate_item_field(field=name, version=version)

    assert "mime_content" in mail_read._EML_MIME_FIELDS
    for name in mail_read._EML_MIME_FIELDS:
        validator.validate_item_field(field=name, version=version)

    for ews_val in mail_read._IMPORTANCE_TO_EWS.values():
        Q(importance=ews_val).clean(version=version)

    raw_eml = b"From: ops@example.com\r\nSubject: Pin\r\n\r\nBody.\r\n"
    sample = Message(subject="Pin", importance="High", mime_content=raw_eml)
    assert sample.importance == "High"
    assert sample.mime_content == raw_eml


