"""Unit tests for ewsmcp.gateway.rules against real exchangelib 5.6.0 XML."""

from __future__ import annotations

from typing import List, Optional

import pytest
from exchangelib import (
    BASIC,
    DELEGATE,
    Account,
    Configuration,
    Credentials,
    EWSTimeZone,
)
from exchangelib.errors import (
    ErrorAccessDenied,
    ErrorFolderNotFound,
    ErrorInvalidIdMalformed,
    ErrorInvalidOperation,
    ErrorItemNotFound,
    UnauthorizedError,
)
from exchangelib.fields import BooleanField
from exchangelib.folders import (
    DeletedItems,
    Folder,
    JunkEmail,
    MsgFolderRoot,
    Root,
)
from exchangelib.properties import (
    Actions,
    Address,
    Conditions,
    DistinguishedFolderId,
    Exceptions,
    FolderId,
    InboxRules,
    Mailbox,
    MoveToFolder,
    ParentFolderId,
    Rule,
)
from exchangelib.protocol import CachingProtocol
from exchangelib.services.inbox_rules import (
    CreateInboxRule,
    DeleteInboxRule,
    SetInboxRule,
)
from exchangelib.util import add_xml_child, create_element, set_xml_value, to_xml
from exchangelib.version import EXCHANGE_2016, Version

from ewsmcp.errors import ToolError
from ewsmcp.gateway import rules as gateway_rules
from ewsmcp.gateway.rules import GetInboxRulesWithBlob


@pytest.fixture()
def offline_account():
    CachingProtocol.clear_cache()
    try:
        yield Account(
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
    finally:
        CachingProtocol.clear_cache()


_RULE_BOOLEAN_TAGS = frozenset(
    field.response_tag()
    for model in (Rule, Conditions, Exceptions, Actions)
    for field in model.FIELDS
    if isinstance(field, BooleanField)
)


def _rewrite_rule_booleans_for_parser(elem):
    """exchangelib serialises BooleanField requests as '1'/'0', whereas
    BooleanField.from_xml only parses 'true'/'false' (what Exchange returns).
    Rewrite BooleanField text to server form while leaving integer elements
    such as <t:Priority> untouched.
    """
    for node in elem.iter():
        if node.tag in _RULE_BOOLEAN_TAGS:
            if node.text == "1":
                node.text = "true"
            elif node.text == "0":
                node.text = "false"
    return elem


def _build_get_inbox_rules_message(
    account: Account,
    *,
    rules_list: Optional[List[Rule]],
    blob_exists: Optional[bool],
):
    """Build a GetInboxRulesResponse message using exchangelib's serializers."""
    message = create_element("m:GetInboxRulesResponse")
    message.set("ResponseClass", "Success")
    add_xml_child(message, "m:ResponseCode", "NoError")
    if blob_exists is not None:
        add_xml_child(
            message,
            "m:OutlookRuleBlobExists",
            "true" if blob_exists else "false",
        )
    if rules_list is not None:
        container = create_element("m:InboxRules")
        assert container.tag == InboxRules.response_tag()
        for rule in rules_list:
            set_xml_value(container, rule, version=account.version)
        _rewrite_rule_booleans_for_parser(container)
        message.append(container)
    return message


def _sample_rule(rule_id: str = "RAW-RULE-1", name: str = "Newsletters") -> Rule:
    return Rule(
        id=rule_id,
        display_name=name,
        priority=1,
        is_enabled=True,
        conditions=Conditions(
            from_addresses=[Address(email_address="news@example.com")],
            contains_subject_strings=["Digest"],
        ),
        actions=Actions(
            move_to_folder=MoveToFolder(folder_id=FolderId(id="FLD-NEWS")),
            mark_as_read=True,
        ),
    )


@pytest.mark.parametrize("expected_blob", [True, False, None])
def test_fetch_rules_reads_outlook_rule_blob_flag_via_library_xml(
    offline_account, monkeypatch, expected_blob
):
    msg_elem = _build_get_inbox_rules_message(
        offline_account,
        rules_list=[_sample_rule()],
        blob_exists=expected_blob,
    )

    def fake_get_elements(self, payload):
        assert payload.tag.endswith("GetInboxRules")
        return iter(
            self._get_element_container(
                msg_elem, name=self.element_container_name
            )
        )

    monkeypatch.setattr(GetInboxRulesWithBlob, "_get_elements", fake_get_elements)

    fetched, blob_flag = gateway_rules.fetch_rules(offline_account)
    assert blob_flag is expected_blob
    assert len(fetched) == 1
    assert fetched[0].id == "RAW-RULE-1"
    assert fetched[0].display_name == "Newsletters"
    assert fetched[0].priority == 1
    assert fetched[0].is_enabled is True
    assert fetched[0].actions.mark_as_read is True
    assert fetched[0].actions.move_to_folder.folder_id.id == "FLD-NEWS"


def test_fetch_rules_server_form_hand_written_xml_and_fingerprints(
    offline_account, monkeypatch
):
    """Feed hand-written server-form EWS XML (true/false booleans, xs:boolean '1',
    unknown tags, empty elements) through the real fetch_rules."""
    ns = (
        'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
        'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"'
    )
    raw_xml = (
        f'<m:GetInboxRulesResponse ResponseClass="Success" {ns}>'
        "<m:ResponseCode>NoError</m:ResponseCode>"
        "<m:OutlookRuleBlobExists>false</m:OutlookRuleBlobExists>"
        "<m:InboxRules>"
        "<t:Rule><t:RuleId>RAW-SRV-1</t:RuleId><t:DisplayName>Clean Server</t:DisplayName>"
        "<t:Priority>1</t:Priority><t:IsEnabled>true</t:IsEnabled>"
        "<t:IsNotSupported>false</t:IsNotSupported><t:IsInError>false</t:IsInError>"
        "<t:Conditions><t:HasAttachments>true</t:HasAttachments>"
        "<t:SentToMe>false</t:SentToMe></t:Conditions>"
        "<t:Actions><t:MarkAsRead>true</t:MarkAsRead>"
        "<t:Delete>false</t:Delete></t:Actions></t:Rule>"
        "<t:Rule><t:RuleId>RAW-SRV-2</t:RuleId><t:DisplayName>XsBool Delete</t:DisplayName>"
        "<t:Priority>2</t:Priority><t:IsEnabled>false</t:IsEnabled>"
        "<t:Actions><t:MarkAsRead>true</t:MarkAsRead>"
        "<t:Delete>1</t:Delete></t:Actions></t:Rule>"
        "<t:Rule><t:RuleId>RAW-SRV-3</t:RuleId><t:DisplayName>Future Tags</t:DisplayName>"
        "<t:Priority>3</t:Priority><t:IsEnabled>true</t:IsEnabled>"
        "<t:Conditions><t:FuturePredicate>true</t:FuturePredicate></t:Conditions>"
        "<t:Actions><t:MarkAsRead>true</t:MarkAsRead>"
        "<t:FutureSendAction>true</t:FutureSendAction></t:Actions></t:Rule>"
        "<t:Rule><t:RuleId>RAW-SRV-4</t:RuleId><t:DisplayName>Reply A</t:DisplayName>"
        "<t:Priority>4</t:Priority><t:IsEnabled>true</t:IsEnabled>"
        '<t:Actions><t:ServerReplyWithMessage Id="TPL-A" ChangeKey="CK-1"/></t:Actions>'
        "</t:Rule>"
        "<t:Rule><t:RuleId>RAW-SRV-4</t:RuleId><t:DisplayName>Reply A</t:DisplayName>"
        "<t:Priority>4</t:Priority><t:IsEnabled>true</t:IsEnabled>"
        '<t:Actions><t:ServerReplyWithMessage Id="TPL-B" ChangeKey="CK-1"/></t:Actions>'
        "</t:Rule>"
        "</m:InboxRules></m:GetInboxRulesResponse>"
    )
    msg_elem = to_xml(raw_xml.encode("utf-8")).getroot()

    monkeypatch.setattr(
        GetInboxRulesWithBlob,
        "_get_elements",
        lambda self, payload: iter(
            self._get_element_container(
                msg_elem, name=self.element_container_name
            )
        ),
    )

    rules_out, blob_flag = gateway_rules.fetch_rules(offline_account)
    assert blob_flag is False
    assert len(rules_out) == 5

    r_clean, r_xsbool, r_future, r_tpl_a, r_tpl_b = rules_out
    assert r_clean.is_enabled is True
    assert r_clean.conditions.has_attachments is True
    assert r_clean.conditions.sent_to_me is False
    assert r_clean.actions.delete is False
    assert gateway_rules.rule_xml_issues(r_clean) == ((), False)

    # <t:Delete>1</t:Delete> normalised so BooleanField parses True
    assert r_xsbool.is_enabled is False
    assert r_xsbool.actions.delete is True

    # Unknown server XML tags recorded in raw XML issues
    issues_future, sends_future = gateway_rules.rule_xml_issues(r_future)
    assert "conditions.FuturePredicate" in issues_future
    assert "actions.FutureSendAction" in issues_future
    assert sends_future is False

    # SHA-256 fingerprint changes when template id changes
    fp_a = gateway_rules.rule_fingerprint(r_tpl_a, offline_account)
    fp_b = gateway_rules.rule_fingerprint(r_tpl_b, offline_account)
    assert len(fp_a) == 64
    assert fp_a != fp_b


def test_fetch_rules_handles_empty_ruleset_with_blob_true(
    offline_account, monkeypatch
):
    """When no server rules exist Exchange omits <m:InboxRules>, while
    <m:OutlookRuleBlobExists>true</m:OutlookRuleBlobExists> can still be set."""
    msg_elem = _build_get_inbox_rules_message(
        offline_account,
        rules_list=None,
        blob_exists=True,
    )

    def fake_get_elements(self, payload):
        return iter(
            self._get_element_container(
                msg_elem, name=self.element_container_name
            )
        )

    monkeypatch.setattr(GetInboxRulesWithBlob, "_get_elements", fake_get_elements)

    fetched, blob_flag = gateway_rules.fetch_rules(offline_account)
    assert fetched == []
    assert blob_flag is True


def test_fetch_rules_reraises_in_band_service_exception(
    offline_account, monkeypatch
):
    """GetInboxRules lists ErrorInvalidOperation in ERRORS_TO_CATCH_IN_RESPONSE,
    yielding the exception instead of raising; fetch_rules must raise it."""

    def fake_call(self, mailbox=None):
        yield ErrorInvalidOperation("Rule store corrupt")

    monkeypatch.setattr(GetInboxRulesWithBlob, "call", fake_call)

    with pytest.raises(ErrorInvalidOperation, match="Rule store corrupt"):
        gateway_rules.fetch_rules(offline_account)


def test_create_rule_uses_create_inbox_rule_service_and_before_after_diff(
    offline_account, monkeypatch
):
    existing = _sample_rule(rule_id="RAW-EXISTING", name="Receipts")
    new_rule = _sample_rule(rule_id="", name="Receipts")
    new_rule.id = None

    create_calls: List[Rule] = []
    monkeypatch.setattr(
        Account,
        "create_rule",
        lambda self, r: (_ for _ in ()).throw(
            AssertionError("Account.create_rule must not be used")
        ),
    )
    monkeypatch.setattr(
        CreateInboxRule, "get", lambda self, **kw: create_calls.append(kw["rule"])
    )

    with pytest.raises(ToolError) as dup_exc:
        gateway_rules.create_rule(
            offline_account, new_rule, before_rules=[existing]
        )
    assert dup_exc.value.code == "validation"
    assert create_calls == []

    distinct = _sample_rule(rule_id="", name="Alerts")
    distinct.id = None
    after_server_rule = _sample_rule(rule_id="SERVER-DIFF-ID", name="Alerts")
    monkeypatch.setattr(
        gateway_rules,
        "fetch_rules",
        lambda acc: ([existing, after_server_rule], False),
    )

    created, verified = gateway_rules.create_rule(
        offline_account, distinct, before_rules=[existing]
    )
    assert verified is True
    assert len(create_calls) == 1
    assert created is not None and created.id == "SERVER-DIFF-ID"
    assert distinct.id == "SERVER-DIFF-ID"


def test_create_and_replace_rule_return_unverified_when_readback_fails_or_ambiguous(
    offline_account, monkeypatch
):
    """Finding 3: once CreateInboxRule or SetInboxRule succeeds, a read-back
    failure or missing new id must return (None, False), never an error."""
    existing = _sample_rule(rule_id="RAW-1", name="Existing")
    candidate = _sample_rule(rule_id="", name="NewRule")
    candidate.id = None

    monkeypatch.setattr(CreateInboxRule, "get", lambda self, **kw: None)
    monkeypatch.setattr(
        gateway_rules, "fetch_rules", lambda acc: ([existing], False)
    )
    created, verified = gateway_rules.create_rule(
        offline_account, candidate, before_rules=[existing]
    )
    assert created is None
    assert verified is False

    # Read-back raising a transient error after write succeeded -> (None, False)
    def boom(_acc):
        raise RuntimeError("GetInboxRules timed out after write")

    monkeypatch.setattr(gateway_rules, "fetch_rules", boom)
    created2, verified2 = gateway_rules.create_rule(
        offline_account, candidate, before_rules=[existing]
    )
    assert created2 is None
    assert verified2 is False

    monkeypatch.setattr(SetInboxRule, "get", lambda self, **kw: None)
    replaced, r_verified = gateway_rules.replace_rule(offline_account, existing)
    assert replaced is None
    assert r_verified is False


def test_replace_rule_uses_set_inbox_rule_and_translates_invalid_id(
    offline_account, monkeypatch
):
    updated = _sample_rule(rule_id="RAW-1", name="Updated Name")
    set_calls: List[Rule] = []
    monkeypatch.setattr(
        Account,
        "set_rule",
        lambda self, r: (_ for _ in ()).throw(
            AssertionError("Account.set_rule must not be used")
        ),
    )
    monkeypatch.setattr(
        SetInboxRule, "get", lambda self, **kw: set_calls.append(kw["rule"])
    )
    monkeypatch.setattr(
        gateway_rules, "fetch_rules", lambda acc: ([updated], False)
    )

    stored, verified = gateway_rules.replace_rule(
        offline_account, updated, display_ref="r1"
    )
    assert verified is True
    assert set_calls == [updated]
    assert stored is not None and stored.id == "RAW-1"

    def bad_id(self, **kw):
        raise ErrorInvalidIdMalformed("Id is malformed.")

    monkeypatch.setattr(SetInboxRule, "get", bad_id)
    with pytest.raises(ToolError) as exc:
        gateway_rules.replace_rule(offline_account, updated, display_ref="r1")
    assert exc.value.code == "not_found"
    assert "list_rules" in (exc.value.hint or "")
    assert "search_messages" not in (exc.value.hint or "")


def test_delete_rule_tries_direct_delete_first_even_when_disabled(
    offline_account, monkeypatch
):
    disabled = Rule(
        account=offline_account,
        id="RAW-DISABLED-1",
        display_name="Paused Rule",
        priority=3,
        is_enabled=False,
        conditions=Conditions(has_attachments=True),
        actions=Actions(mark_as_read=True),
    )
    set_calls: List[Rule] = []
    delete_calls: List[str] = []

    monkeypatch.setattr(
        Account,
        "delete_rule",
        lambda self, r: (_ for _ in ()).throw(
            AssertionError("Account.delete_rule must not be used")
        ),
    )
    monkeypatch.setattr(
        SetInboxRule, "get", lambda self, **kw: set_calls.append(kw["rule"])
    )
    monkeypatch.setattr(
        DeleteInboxRule,
        "get",
        lambda self, **kw: delete_calls.append(kw["rule"].id),
    )

    deleted_id, deleted_name = gateway_rules.delete_rule(
        offline_account, "RAW-DISABLED-1", rules=[disabled], display_ref="r1"
    )
    assert set_calls == []
    assert delete_calls == ["RAW-DISABLED-1"]
    assert deleted_id == "RAW-DISABLED-1"
    assert deleted_name == "Paused Rule"


def test_delete_disabled_rule_neutralises_with_48_char_token_on_item_not_found(
    offline_account, monkeypatch
):
    disabled = Rule(
        account=offline_account,
        id="RAW-DISABLED-2",
        display_name="Disabled Forwarder",
        priority=4,
        is_enabled=False,
        conditions=Conditions(has_attachments=True),
        exceptions=Exceptions(is_automatic_reply=True),
        actions=Actions(
            forward_to_recipients=[Address(email_address="ext@example.com")]
        ),
    )
    set_calls: List[Rule] = []
    delete_attempts: List[Rule] = []

    def fake_set(self, **kw):
        set_calls.append(kw["rule"])

    def fake_delete(self, **kw):
        r = kw["rule"]
        delete_attempts.append(r)
        if len(delete_attempts) == 1:
            raise ErrorItemNotFound("Disabled rule cannot be deleted directly")

    monkeypatch.setattr(SetInboxRule, "get", fake_set)
    monkeypatch.setattr(DeleteInboxRule, "get", fake_delete)

    deleted_id, deleted_name = gateway_rules.delete_rule(
        offline_account, "RAW-DISABLED-2", rules=[disabled], display_ref="r2"
    )
    assert deleted_id == "RAW-DISABLED-2"
    assert deleted_name == "Disabled Forwarder"
    assert len(set_calls) == 1
    assert len(delete_attempts) == 2

    neutral = set_calls[0]
    assert neutral.id == "RAW-DISABLED-2"
    assert neutral.is_enabled is True
    assert neutral.exceptions is None
    assert len(neutral.conditions.contains_subject_strings) == 1
    assert len(neutral.conditions.contains_subject_strings[0]) == 48
    assert neutral.actions.stop_processing_rules is True
    assert neutral.actions.forward_to_recipients is None
    assert neutral.actions.delete is None


def test_delete_disabled_rule_reports_neutralised_state_when_second_delete_fails(
    offline_account, monkeypatch
):
    disabled = Rule(
        account=offline_account,
        id="RAW-DISABLED-3",
        display_name="Stuck Rule",
        priority=2,
        is_enabled=False,
        actions=Actions(mark_as_read=True),
    )
    monkeypatch.setattr(SetInboxRule, "get", lambda self, **kw: None)

    calls = {"n": 0}

    def always_fail_delete(self, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ErrorItemNotFound("Disabled rule")
        raise RuntimeError("Transient store error")

    monkeypatch.setattr(DeleteInboxRule, "get", always_fail_delete)

    with pytest.raises(ToolError) as exc:
        gateway_rules.delete_rule(
            offline_account, "RAW-DISABLED-3", rules=[disabled], display_ref="r3"
        )
    assert exc.value.code == "upstream_error"
    assert "Stuck Rule" in exc.value.message
    assert "delete_rule" in (exc.value.hint or "")


def test_delete_enabled_rule_item_not_found_does_not_neutralise_and_hints_list_rules(
    offline_account, monkeypatch
):
    enabled = _sample_rule(rule_id="RAW-ENABLED-1", name="Active Rule")
    set_calls: List[Rule] = []
    monkeypatch.setattr(
        SetInboxRule, "get", lambda self, **kw: set_calls.append(kw["rule"])
    )
    monkeypatch.setattr(
        DeleteInboxRule,
        "get",
        lambda self, **kw: (_ for _ in ()).throw(
            ErrorItemNotFound("Already deleted")
        ),
    )

    with pytest.raises(ToolError) as exc:
        gateway_rules.delete_rule(
            offline_account, "RAW-ENABLED-1", rules=[enabled], display_ref="r1"
        )
    assert set_calls == []
    assert exc.value.code == "not_found"
    assert "list_rules" in (exc.value.hint or "")
    assert "search_messages" not in (exc.value.hint or "")


def test_delete_rule_unknown_id_raises_not_found_with_list_rules_hint(
    offline_account,
):
    with pytest.raises(ToolError) as exc:
        gateway_rules.delete_rule(
            offline_account, "MISSING-ID", rules=[], display_ref="r42"
        )
    assert exc.value.code == "not_found"
    assert "list_rules" in (exc.value.hint or "")


# --- Fail-closed Deleted Items and Junk verification --------------------------


def test_discard_folder_ids_fails_closed_except_for_folder_not_found_and_reraises_401(
    offline_account,
):
    root = Root(account=offline_account, id="RAWID-ROOT", name="root")
    real_trash = DeletedItems(
        root=root,
        id="RAWID-TRASH",
        name="Deleted Items",
        _distinguished_id=DistinguishedFolderId(
            id="deleteditems",
            mailbox=Mailbox(email_address="user@example.com"),
        ),
    )

    class _ServerLacksJunk:
        trash = real_trash

        @property
        def junk(self):
            raise ErrorFolderNotFound("No JunkEmail on this mailbox")

    assert gateway_rules.discard_folder_ids(_ServerLacksJunk()) == {"RAWID-TRASH"}

    # Generic error -> fail closed with validation
    class _TransientFailure:
        trash = real_trash

        @property
        def junk(self):
            raise RuntimeError("timeout")

    with pytest.raises(ToolError) as exc_rt:
        gateway_rules.discard_folder_ids(_TransientFailure())
    assert exc_rt.value.code == "validation"
    assert "Deleted Items or Junk Email" in exc_rt.value.message

    # Access denied returning DeletedItems(id=None) -> fail closed with validation
    denied_trash = DeletedItems(
        root=root,
        id=None,
        _distinguished_id=DistinguishedFolderId(
            id="deleteditems",
            mailbox=Mailbox(email_address="user@example.com"),
        ),
    )

    class _IdNoneTrash:
        trash = denied_trash
        junk = JunkEmail(root=root, id="RAWID-JUNK", name="Junk Email")

    with pytest.raises(ToolError) as exc_none:
        gateway_rules.discard_folder_ids(_IdNoneTrash())
    assert exc_none.value.code == "validation"

    # ErrorAccessDenied raised directly -> fail closed with validation
    class _DeniedJunk:
        trash = real_trash

        @property
        def junk(self):
            raise ErrorAccessDenied("GetFolder denied")

    with pytest.raises(ToolError) as exc_ad:
        gateway_rules.discard_folder_ids(_DeniedJunk())
    assert exc_ad.value.code == "validation"

    # HTTP 401 -> re-raised for the auth circuit breaker
    class _Rejected:
        @property
        def trash(self):
            raise UnauthorizedError("HTTP 401")

    with pytest.raises(UnauthorizedError):
        gateway_rules.discard_folder_ids(_Rejected())


def test_is_discard_target_checks_real_folder_classes_distinguished_ids_and_ids(
    offline_account,
):
    root = Root(account=offline_account, id="RAWID-ROOT", name="root")
    tois = MsgFolderRoot(
        root=root,
        id="RAWID-TOIS",
        name="Top of Information Store",
        parent_folder_id=ParentFolderId(id="RAWID-ROOT"),
    )
    trash = DeletedItems(
        root=root,
        id="RAWID-TRASH",
        name="Deleted Items",
        parent_folder_id=ParentFolderId(id="RAWID-TOIS"),
    )
    junk = JunkEmail(
        root=root,
        id="RAWID-JUNK",
        name="Junk Email",
        parent_folder_id=ParentFolderId(id="RAWID-TOIS"),
    )
    below_trash = Folder(
        root=root,
        id="RAWID-OLD",
        name="Old",
        parent_folder_id=ParentFolderId(id="RAWID-TRASH"),
    )
    ordinary = Folder(
        root=root,
        id="RAWID-NEWS",
        name="News",
        parent_folder_id=ParentFolderId(id="RAWID-TOIS"),
    )
    root._subfolders = {
        f.id: f for f in (root, tois, trash, junk, below_trash, ordinary)
    }

    # Even with an empty id set, real DeletedItems/JunkEmail classes and their
    # descendants are recognised as discard targets (never fail-open).
    assert gateway_rules.is_discard_target(trash, set()) is True
    assert gateway_rules.is_discard_target(junk, set()) is True
    assert gateway_rules.is_discard_target(below_trash, set()) is True
    assert gateway_rules.is_discard_target(ordinary, set()) is False

    # Plain Folder with matching raw id is also caught via discard_ids.
    plain_trash = Folder(
        root=root,
        id="RAWID-TRASH",
        name="Deleted Items",
        parent_folder_id=ParentFolderId(id="RAWID-TOIS"),
    )
    assert gateway_rules.is_discard_target(plain_trash, {"RAWID-TRASH"}) is True
