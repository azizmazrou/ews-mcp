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
from exchangelib.errors import ErrorInvalidOperation
from exchangelib.fields import BooleanField
from exchangelib.properties import (
    Actions,
    Address,
    Conditions,
    Exceptions,
    FolderId,
    InboxRules,
    MoveToFolder,
    Rule,
)
from exchangelib.protocol import CachingProtocol
from exchangelib.util import add_xml_child, create_element, set_xml_value
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
        # The library's InboxRules element models one rule; a response carries
        # one <t:Rule> child per rule inside a single <m:InboxRules> container.
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


def test_create_rule_refuses_duplicate_name_and_uses_before_after_diff(
    offline_account, monkeypatch
):
    existing = _sample_rule(rule_id="RAW-EXISTING", name="Receipts")
    new_rule = _sample_rule(rule_id="", name="Receipts")
    new_rule.id = None

    create_calls: List[Rule] = []
    monkeypatch.setattr(
        Account, "create_rule", lambda self, r: create_calls.append(r)
    )

    with pytest.raises(ToolError) as dup_exc:
        gateway_rules.create_rule(
            offline_account, new_rule, before_rules=[existing]
        )
    assert dup_exc.value.code == "validation"
    assert create_calls == []

    # Distinct name: even if Account.create_rule assigns a wrong id via name
    # lookup, gateway.create_rule confirms the id from after_ids - before_ids.
    distinct = _sample_rule(rule_id="", name="Alerts")
    distinct.id = None

    def fake_create(self, r):
        create_calls.append(r)
        r.id = "WRONG-DISPLAY-LOOKUP-ID"

    after_server_rule = _sample_rule(rule_id="SERVER-DIFF-ID", name="Alerts")
    monkeypatch.setattr(Account, "create_rule", fake_create)
    monkeypatch.setattr(
        gateway_rules,
        "fetch_rules",
        lambda acc: ([existing, after_server_rule], False),
    )

    created = gateway_rules.create_rule(
        offline_account, distinct, before_rules=[existing]
    )
    assert len(create_calls) == 1
    assert created.id == "SERVER-DIFF-ID"
    assert distinct.id == "SERVER-DIFF-ID"


def test_create_rule_raises_upstream_error_when_no_new_id_appears(
    offline_account, monkeypatch
):
    existing = _sample_rule(rule_id="RAW-1", name="Existing")
    candidate = _sample_rule(rule_id="", name="NewRule")
    candidate.id = None

    monkeypatch.setattr(Account, "create_rule", lambda self, r: None)
    monkeypatch.setattr(
        gateway_rules, "fetch_rules", lambda acc: ([existing], False)
    )

    with pytest.raises(ToolError) as exc:
        gateway_rules.create_rule(
            offline_account, candidate, before_rules=[existing]
        )
    assert exc.value.code == "upstream_error"


def test_replace_rule_calls_account_set_rule_and_returns_stored_rule(
    offline_account, monkeypatch
):
    updated = _sample_rule(rule_id="RAW-1", name="Updated Name")
    set_calls: List[Rule] = []
    monkeypatch.setattr(
        Account, "set_rule", lambda self, r: set_calls.append(r)
    )
    monkeypatch.setattr(
        gateway_rules, "fetch_rules", lambda acc: ([updated], False)
    )

    stored = gateway_rules.replace_rule(offline_account, updated)
    assert set_calls == [updated]
    assert stored.id == "RAW-1"
    assert stored.display_name == "Updated Name"


def test_delete_rule_calls_account_delete_rule_directly_even_when_disabled(
    offline_account, monkeypatch
):
    """Rule.delete() rewrites disabled rules via SetInboxRule before deleting;
    gateway.delete_rule must call Account.delete_rule directly without touching
    SetInboxRule and preserve the id/name despite Account.delete_rule clearing
    rule.id."""
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

    def fake_set_rule(self, r):
        set_calls.append(r)

    def fake_delete_rule(self, r):
        assert r.is_enabled is False
        assert r.conditions is not None
        delete_calls.append(r.id)
        r.id = None  # mirrors exchangelib Account.delete_rule

    monkeypatch.setattr(Account, "set_rule", fake_set_rule)
    monkeypatch.setattr(Account, "delete_rule", fake_delete_rule)

    deleted_id, deleted_name = gateway_rules.delete_rule(
        offline_account, "RAW-DISABLED-1", rules=[disabled]
    )
    assert set_calls == []
    assert delete_calls == ["RAW-DISABLED-1"]
    assert deleted_id == "RAW-DISABLED-1"
    assert deleted_name == "Paused Rule"


def test_delete_rule_unknown_id_raises_not_found_with_list_rules_hint(
    offline_account,
):
    with pytest.raises(ToolError) as exc:
        gateway_rules.delete_rule(
            offline_account, "MISSING-ID", rules=[], display_ref="r42"
        )
    assert exc.value.code == "not_found"
    assert "list_rules" in (exc.value.hint or "")


# --- Deleted Items and Junk as rule targets -----------------------------------


def test_discard_folder_ids_skips_a_missing_folder_and_reraises_a_rejection():
    from types import SimpleNamespace

    from exchangelib.errors import UnauthorizedError

    class _NoJunk:
        trash = SimpleNamespace(id="TRASH-ID")

        @property
        def junk(self):
            raise RuntimeError("no such folder")

    assert gateway_rules.discard_folder_ids(_NoJunk()) == {"TRASH-ID"}

    class _Rejected:
        @property
        def trash(self):
            raise UnauthorizedError("HTTP 401")

    with pytest.raises(UnauthorizedError):
        gateway_rules.discard_folder_ids(_Rejected())


def test_is_discard_target_covers_the_folder_itself_and_everything_below_it():
    from types import SimpleNamespace

    trash = SimpleNamespace(id="TRASH-ID")
    below = SimpleNamespace(id="OLD", parts=[SimpleNamespace(id="ROOT"), trash, SimpleNamespace(id="OLD")])
    ordinary = SimpleNamespace(id="NEWS", parts=[SimpleNamespace(id="ROOT"), SimpleNamespace(id="NEWS")])
    banned = {"TRASH-ID", "JUNK-ID"}
    assert gateway_rules.is_discard_target(trash, banned) is True
    assert gateway_rules.is_discard_target(below, banned) is True
    assert gateway_rules.is_discard_target(ordinary, banned) is False
    assert gateway_rules.is_discard_target(trash, set()) is False

