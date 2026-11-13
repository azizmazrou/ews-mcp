"""Inbox rules tool pack tests — driven through dispatch() with real
exchangelib 5.6.0 model classes (Rule, Actions, Conditions, Exceptions)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import jsonschema
import pytest
from conftest import make_settings
from exchangelib import (
    BASIC,
    DELEGATE,
    Account,
    Configuration,
    Credentials,
    EWSTimeZone,
)
from exchangelib.fields import BooleanField
from exchangelib.properties import (
    Actions,
    Address,
    Conditions,
    CopyToFolder,
    Exceptions,
    FolderId,
    ItemId,
    MoveToFolder,
    Rule,
    WithinSizeRange,
)
from exchangelib.protocol import CachingProtocol
from exchangelib.version import EXCHANGE_2016, Version

from ewsmcp.audit import AuditLog
from ewsmcp.errors import ToolError
from ewsmcp.gateway import rules as gateway_rules
from ewsmcp.ids import get_aliaser, kind_for_key
from ewsmcp.tools import build_registry
from ewsmcp.tools import rules as rules_pack
from ewsmcp.tools.base import Context, dispatch

SPEC = {s.name: s for s in rules_pack.TOOLS}
ENVELOPE_KEYS = {"ok", "items", "count", "total_available", "next_offset"}

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


class FakeRulesBackend:
    """In-memory rule store + folder map attached to a real offline Account."""

    def __init__(
        self,
        account: Account,
        *,
        initial_rules: Optional[List[Rule]] = None,
        blob_exists: Optional[bool] = False,
    ) -> None:
        self.account = account
        self.rules: List[Rule] = list(initial_rules or [])
        self.blob_exists: Optional[bool] = blob_exists
        self.create_calls: List[Rule] = []
        self.set_calls: List[Rule] = []
        self.delete_calls: List[str] = []
        self._seq = 100
        self.folders: Dict[str, Any] = {}
        self.add_folder("FLD-ARCHIVE", "Archive")
        self.add_folder("FLD-NEWS", "Projects/Newsletters")
        self.add_folder("FLD-COPY", "Projects/Copies")
        # Raw ids of Deleted Items and Junk Email as the gateway reports them.
        self.discard_ids: set = set()

    def add_folder(self, raw_id: str, path: str) -> SimpleNamespace:
        name = path.rsplit("/", 1)[-1]
        folder = SimpleNamespace(
            id=raw_id,
            changekey=f"CK-{raw_id}",
            name=name,
            path=path,
        )
        self.folders[raw_id] = folder
        self.folders[path] = folder
        self.folders[path.lower()] = folder
        return folder

    async def call(self, fn):
        return fn(self.account)

    def resolve_folder(self, account, ref, aliaser):
        key = aliaser.resolve(ref.strip())
        folder = self.folders.get(key) or self.folders.get(key.lower())
        if folder is None:
            raise ToolError("not_found", f"No folder matches {ref!r}.")
        return folder

    def fetch_rules(self, _account):
        return list(self.rules), self.blob_exists

    def _roundtrip_rule_xml(self, rule: Rule) -> Rule:
        elem = _rewrite_rule_booleans_for_parser(
            rule.to_xml(version=self.account.version)
        )
        return Rule.from_xml(elem=elem, account=self.account)

    def on_create_rule(self, rule: Rule) -> None:
        self.create_calls.append(rule)
        self._seq += 1
        server_id = f"RAW-CREATED-{self._seq}"
        stored = self._roundtrip_rule_xml(rule)
        stored.id = server_id
        self.rules.append(stored)
        # Simulate a buggy/distracted lookup on rule.id so tests prove
        # gateway.create_rule takes the id from the before/after set diff.
        rule.id = "UNTRUSTED-DISPLAY-NAME-ID"

    def on_set_rule(self, rule: Rule) -> None:
        self.set_calls.append(rule)
        stored = self._roundtrip_rule_xml(rule)
        for idx, existing in enumerate(self.rules):
            if existing.id == rule.id:
                self.rules[idx] = stored
                return
        self.rules.append(stored)

    def on_delete_rule(self, rule: Rule) -> None:
        self.delete_calls.append(str(rule.id))
        self.rules = [r for r in self.rules if r.id != rule.id]
        rule.id = None


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


def _wire_backend(monkeypatch, backend: FakeRulesBackend) -> None:
    monkeypatch.setattr(gateway_rules, "fetch_rules", backend.fetch_rules)
    # The real helper asks the account for its well-known folders, which an
    # offline account cannot answer.
    monkeypatch.setattr(
        gateway_rules, "discard_folder_ids", lambda _account: set(backend.discard_ids)
    )
    monkeypatch.setattr(
        Account, "create_rule", lambda self, r: backend.on_create_rule(r)
    )
    monkeypatch.setattr(
        Account, "set_rule", lambda self, r: backend.on_set_rule(r)
    )
    monkeypatch.setattr(
        Account, "delete_rule", lambda self, r: backend.on_delete_rule(r)
    )


def _make_ctx(
    tmp_path, backend: FakeRulesBackend, **settings_overrides
) -> Context:
    defaults = dict(
        ews_capability_tier="draft",
        send_enabled=False,
    )
    defaults.update(settings_overrides)
    ctx = Context(
        settings=make_settings(**defaults),
        gateway=backend,
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "aliases")),
        audit=AuditLog(str(tmp_path / "audit")),
    )
    build_registry(ctx)
    return ctx


def _call(ctx: Context, tool: str, /, **kwargs) -> Dict[str, Any]:
    # Positional-only: save_rule itself takes a `name` argument.
    spec = ctx.registry.get(tool) or SPEC[tool]
    return asyncio.run(dispatch(ctx, spec, dict(kwargs)))


# --- 1. Bidirectional mapping with real exchangelib classes ------------------


def test_bidirectional_mapping_sets_only_allowlisted_fields_and_roundtrips(
    tmp_path, offline_account, monkeypatch
):
    backend = FakeRulesBackend(offline_account, blob_exists=False)
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    all_conditions = {
        "from_addresses": ["Alice <alice@example.com>"],
        "sent_to_addresses": ["team@example.com"],
        "subject_contains": ["[Digest]"],
        "body_contains": ["unsubscribe"],
        "subject_or_body_contains": ["bulletin"],
        "sender_contains": ["@example.com"],
        "recipient_contains": ["team@"],
        "has_attachments": True,
        "importance": "high",
        "sent_to_me": True,
        "sent_cc_me": True,
        "sent_only_to_me": True,
        "sent_to_or_cc_me": True,
        "not_sent_to_me": True,
    }
    all_actions = {
        "move_to_folder": "Projects/Newsletters",
        "copy_to_folder": "Projects/Copies",
        "assign_categories": ["News", "Weekly"],
        "mark_as_read": True,
        "mark_importance": "low",
        "stop_processing_rules": True,
    }

    p1 = _call(
        ctx,
        "save_rule",
        name="Full Writable Rule",
        priority=2,
        enabled=True,
        conditions=all_conditions,
        actions=all_actions,
    )
    assert p1["requires_confirmation"] is True
    assert backend.create_calls == []

    p2 = _call(
        ctx,
        "save_rule",
        name="Full Writable Rule",
        priority=2,
        enabled=True,
        conditions=all_conditions,
        actions=all_actions,
        confirm_token=p1["confirm_token"],
    )
    assert p2["ok"] is True
    assert p2["replaced"] is False
    assert p2["id"].startswith("r")

    built = backend.create_calls[0]
    assert isinstance(built, Rule)
    assert isinstance(built.actions, Actions)
    assert isinstance(built.conditions, Conditions)
    assert isinstance(built.actions.move_to_folder, MoveToFolder)
    assert isinstance(built.actions.copy_to_folder, CopyToFolder)
    assert built.actions.assign_categories == ["News", "Weekly"]
    assert built.actions.mark_as_read is True
    assert built.actions.mark_importance == "Low"
    assert built.actions.stop_processing_rules is True

    # Every non-writable Actions attribute on the real library class is None.
    for forbidden_attr in (
        "delete",
        "permanent_delete",
        "forward_to_recipients",
        "forward_as_attachment_to_recipients",
        "redirect_to_recipients",
        "send_sms_alert_to_recipients",
        "server_reply_with_message",
    ):
        assert getattr(built.actions, forbidden_attr) is None, forbidden_attr

    listed = _call(ctx, "list_rules")
    assert ENVELOPE_KEYS.issubset(listed.keys())
    assert listed["outlook_only_rules"] is False
    assert listed["count"] == 1
    dto = listed["items"][0]
    assert dto["id"] == p2["id"]
    assert dto["priority"] == 2
    assert dto["enabled"] is True
    assert dto["editable"] is True
    assert "unsupported" not in dto
    assert "sends_mail" not in dto
    assert dto["actions"] == all_actions
    expected_conds = dict(all_conditions, from_addresses=["alice@example.com"])
    assert dto["conditions"] == expected_conds

    # Also verify priority=1 (<t:Priority>1</t:Priority> stays an integer) and
    # enabled=False (<t:IsEnabled>0</t:IsEnabled> round-trips as false).
    disable_p1 = _call(
        ctx,
        "save_rule",
        rule_id=p2["id"],
        name="Full Writable Rule",
        priority=1,
        enabled=False,
        conditions=all_conditions,
        actions=all_actions,
    )
    disable_p2 = _call(
        ctx,
        "save_rule",
        rule_id=p2["id"],
        name="Full Writable Rule",
        priority=1,
        enabled=False,
        conditions=all_conditions,
        actions=all_actions,
        confirm_token=disable_p1["confirm_token"],
    )
    assert disable_p2["ok"] is True
    assert disable_p2["rule"]["priority"] == 1
    assert disable_p2["rule"]["enabled"] is False


@pytest.mark.parametrize(
    ("action_kwargs", "expected_field", "expected_sends", "expected_rcpts"),
    [
        (
            {"forward_to_recipients": [Address(email_address="fwd@example.com")]},
            "actions.forward_to_recipients",
            True,
            ["fwd@example.com"],
        ),
        (
            {
                "forward_as_attachment_to_recipients": [
                    Address(email_address="att@example.com")
                ]
            },
            "actions.forward_as_attachment_to_recipients",
            True,
            ["att@example.com"],
        ),
        (
            {"redirect_to_recipients": [Address(email_address="red@example.com")]},
            "actions.redirect_to_recipients",
            True,
            ["red@example.com"],
        ),
        (
            {
                "send_sms_alert_to_recipients": [
                    Address(email_address="sms@example.com")
                ]
            },
            "actions.send_sms_alert_to_recipients",
            True,
            ["sms@example.com"],
        ),
        (
            {"server_reply_with_message": ItemId(id="TPL-1", changekey="CK-1")},
            "actions.server_reply_with_message",
            True,
            [],
        ),
        (
            {"delete": True},
            "actions.delete",
            False,
            None,
        ),
        (
            {"permanent_delete": True},
            "actions.permanent_delete",
            False,
            None,
        ),
    ],
)
def test_non_writable_actions_reported_uneditable_and_flag_sends_mail(
    tmp_path,
    offline_account,
    monkeypatch,
    action_kwargs,
    expected_field,
    expected_sends,
    expected_rcpts,
):
    rule = Rule(
        account=offline_account,
        id="RAW-NON-EDITABLE-1",
        display_name="Server Special",
        priority=1,
        is_enabled=True,
        actions=Actions(**action_kwargs),
    )
    backend = FakeRulesBackend(offline_account, initial_rules=[rule])
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    res = _call(ctx, "list_rules")
    item = res["items"][0]
    assert item["editable"] is False
    assert expected_field in item["unsupported"]
    if expected_sends:
        assert item["sends_mail"] is True
        assert item["recipients"] == expected_rcpts
    else:
        assert "sends_mail" not in item
        assert "recipients" not in item


def test_exceptions_readonly_conditions_error_flag_and_false_booleans(
    tmp_path, offline_account, monkeypatch
):
    """Exchange serialises inactive booleans as false; only true predicates
    make a rule uneditable or in_error."""
    clean_with_false_bools = Rule(
        account=offline_account,
        id="RAW-CLEAN",
        display_name="Clean Rule",
        priority=1,
        is_enabled=True,
        is_not_supported=False,
        is_in_error=False,
        conditions=Conditions(
            contains_subject_strings=["Invoice"],
            is_approval_request=False,
        ),
        exceptions=Exceptions(is_automatic_reply=False),
        actions=Actions(
            mark_as_read=True,
            delete=False,
            permanent_delete=False,
        ),
    )
    complex_rule = Rule(
        account=offline_account,
        id="RAW-COMPLEX",
        display_name="Outlook Complex",
        priority=2,
        is_enabled=False,
        is_not_supported=True,
        is_in_error=True,
        conditions=Conditions(
            within_size_range=WithinSizeRange(
                minimum_size=1024, maximum_size=1048576
            )
        ),
        exceptions=Exceptions(
            from_addresses=[Address(email_address="vip@example.com")]
        ),
        actions=Actions(
            move_to_folder=MoveToFolder(folder_id=FolderId(id="FLD-DELETED"))
        ),
    )
    backend = FakeRulesBackend(
        offline_account,
        initial_rules=[clean_with_false_bools, complex_rule],
        blob_exists=True,
    )
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    res = _call(ctx, "list_rules")
    assert res["outlook_only_rules"] is True
    first, second = res["items"]

    assert first["editable"] is True
    assert "unsupported" not in first
    assert "in_error" not in first

    assert second["editable"] is False
    assert second["enabled"] is False
    assert second["in_error"] is True
    assert "is_not_supported" in second["unsupported"]
    assert "conditions.within_size_range" in second["unsupported"]
    assert "exceptions.from_addresses" in second["unsupported"]
    # Orphaned folder id falls back to an f-alias, never leaking raw EWS id.
    assert second["actions"]["move_to_folder"].startswith("f")
    assert "FLD-DELETED" not in json.dumps(res)


# --- 2. Non-writable action keys are refused as validation -------------------


@pytest.mark.parametrize(
    ("bad_key", "bad_val"),
    [
        ("forward_to", ["leak@example.com"]),
        ("redirect_to", ["leak@example.com"]),
        ("forward_as_attachment_to", ["leak@example.com"]),
        ("delete", True),
        ("permanent_delete", True),
        ("server_reply", "TPL-1"),
        ("send_sms_alert_to", ["sms@example.com"]),
        ("forward_to_recipients", ["leak@example.com"]),
        ("redirect_to_recipients", ["leak@example.com"]),
        ("forward_as_attachment_to_recipients", ["leak@example.com"]),
        ("send_sms_alert_to_recipients", ["sms@example.com"]),
        ("server_reply_with_message", "TPL-1"),
    ],
)
def test_every_forbidden_action_argument_is_refused_as_validation(
    tmp_path, offline_account, monkeypatch, bad_key, bad_val
):
    backend = FakeRulesBackend(offline_account)
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend, ews_capability_tier="full")
    schema = ctx.registry["save_rule"].public_schema()["inputSchema"]

    for actions_payload in (
        {bad_key: bad_val},
        {"mark_as_read": True, bad_key: bad_val},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                {"name": "Sneaky", "actions": actions_payload}, schema
            )
        res = _call(ctx, "save_rule", name="Sneaky", actions=actions_payload)
        assert res["ok"] is False
        assert res["error"]["code"] == "validation"
        assert bad_key in res["error"]["message"]
    assert backend.create_calls == []


def test_false_boolean_condition_is_refused_as_validation(
    tmp_path, offline_account, monkeypatch
):
    backend = FakeRulesBackend(offline_account)
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    res = _call(
        ctx,
        "save_rule",
        name="Misleading Negation",
        conditions={"has_attachments": False},
        actions={"mark_as_read": True},
    )
    assert res["ok"] is False
    assert res["error"]["code"] == "validation"


# --- 3. Two-phase confirm, preview warnings & TOCTOU -------------------------


@pytest.mark.parametrize(
    ("blob_flag", "conditions", "expect_all_match", "expect_blob_warn"),
    [
        (False, {"subject_contains": ["News"]}, False, False),
        (False, None, True, False),
        (True, {"subject_contains": ["News"]}, False, True),
        (None, {"subject_contains": ["News"]}, False, True),
        (True, {}, True, True),
    ],
)
def test_save_rule_phase_one_executes_nothing_and_carries_warnings(
    tmp_path,
    offline_account,
    monkeypatch,
    blob_flag,
    conditions,
    expect_all_match,
    expect_blob_warn,
):
    backend = FakeRulesBackend(offline_account, blob_exists=blob_flag)
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    kwargs: Dict[str, Any] = {
        "name": "Triage",
        "actions": {"move_to_folder": "Archive"},
    }
    if conditions is not None:
        kwargs["conditions"] = conditions

    p1 = _call(ctx, "save_rule", **kwargs)
    assert p1["ok"] is True
    assert p1["requires_confirmation"] is True
    assert backend.create_calls == []
    assert backend.set_calls == []

    preview = p1["preview"]
    assert preview["outlook_only_rules"] is blob_flag
    assert preview["rule"]["actions"] == {"move_to_folder": "Archive"}
    warnings = preview.get("warnings", [])
    assert (
        any("matches every incoming message" in w for w in warnings)
        is expect_all_match
    )
    assert (
        any(
            "rules disabled in Outlook and client-side send rules will be lost if you proceed"
            in w
            for w in warnings
        )
        is expect_blob_warn
    )
    if not expect_all_match and not expect_blob_warn:
        assert "warnings" not in preview


@pytest.mark.parametrize("blob_flag", [True, None, False])
def test_delete_rule_phase_one_executes_nothing_and_carries_blob_warning(
    tmp_path, offline_account, monkeypatch, blob_flag
):
    existing = Rule(
        account=offline_account,
        id="RAW-DEL-1",
        display_name="Old Rule",
        priority=1,
        is_enabled=True,
        actions=Actions(mark_as_read=True),
    )
    backend = FakeRulesBackend(
        offline_account, initial_rules=[existing], blob_exists=blob_flag
    )
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    listed = _call(ctx, "list_rules")
    rule_alias = listed["items"][0]["id"]

    p1 = _call(ctx, "delete_rule", rule_id=rule_alias)
    assert p1["requires_confirmation"] is True
    assert backend.delete_calls == []
    preview = p1["preview"]
    assert preview["rule"]["name"] == "Old Rule"
    warnings = preview.get("warnings", [])
    if blob_flag in (True, None):
        assert any(
            "rules disabled in Outlook and client-side send rules will be lost if you proceed"
            in w
            for w in warnings
        )
    else:
        assert "warnings" not in preview


def test_confirm_tokens_bind_arguments_server_state_and_are_single_use(
    tmp_path, offline_account, monkeypatch
):
    existing = Rule(
        account=offline_account,
        id="RAW-EDIT-1",
        display_name="Editable Rule",
        priority=1,
        is_enabled=True,
        conditions=Conditions(contains_subject_strings=["Alpha"]),
        actions=Actions(mark_as_read=True),
    )
    backend = FakeRulesBackend(offline_account, initial_rules=[existing])
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)
    alias = _call(ctx, "list_rules")["items"][0]["id"]

    p1 = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Editable Rule",
        conditions={"subject_contains": ["Beta"]},
        actions={"mark_as_read": True},
    )
    assert p1["preview"]["current"]["conditions"] == {
        "subject_contains": ["Alpha"]
    }

    # Changing arguments in phase 2 invalidates the token.
    tampered = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Editable Rule",
        conditions={"subject_contains": ["Gamma"]},
        actions={"mark_as_read": True},
        confirm_token=p1["confirm_token"],
    )
    assert tampered["error"]["code"] == "confirm_invalid"
    assert backend.set_calls == []

    # Server-side TOCTOU modification between preview and confirm invalidates token.
    backend.rules[0].conditions = Conditions(contains_subject_strings=["Changed"])
    stale = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Editable Rule",
        conditions={"subject_contains": ["Beta"]},
        actions={"mark_as_read": True},
        confirm_token=p1["confirm_token"],
    )
    assert stale["error"]["code"] == "confirm_invalid"
    assert backend.set_calls == []

    # Fresh preview + confirm succeeds, and replaying the token is rejected.
    fresh = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Editable Rule",
        conditions={"subject_contains": ["Beta"]},
        actions={"mark_as_read": True},
    )
    done = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Editable Rule",
        conditions={"subject_contains": ["Beta"]},
        actions={"mark_as_read": True},
        confirm_token=fresh["confirm_token"],
    )
    assert done["ok"] is True
    assert done["replaced"] is True
    assert done["rule"]["editable"] is True
    assert done["rule"]["actions"] == {"mark_as_read": True}
    assert done["rule"]["conditions"] == {"subject_contains": ["Beta"]}

    replay = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Editable Rule",
        conditions={"subject_contains": ["Beta"]},
        actions={"mark_as_read": True},
        confirm_token=fresh["confirm_token"],
    )
    assert replay["error"]["code"] == "confirm_invalid"

    # Even when server state before and after save is identical (matching hash),
    # a consumed confirm_token cannot be replayed.
    noop_p1 = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Editable Rule",
        conditions={"subject_contains": ["Beta"]},
        actions={"mark_as_read": True},
    )
    noop_done = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Editable Rule",
        conditions={"subject_contains": ["Beta"]},
        actions={"mark_as_read": True},
        confirm_token=noop_p1["confirm_token"],
    )
    assert noop_done["ok"] is True
    consumed_replay = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Editable Rule",
        conditions={"subject_contains": ["Beta"]},
        actions={"mark_as_read": True},
        confirm_token=noop_p1["confirm_token"],
    )
    assert consumed_replay["error"]["code"] == "confirm_invalid"
    assert "consumed" in consumed_replay["error"]["message"]


# --- 4. Non-editable replace refusal, duplicate name, diff id, delete --------


def test_replace_refuses_non_editable_rule_and_delete_allows_it(
    tmp_path, offline_account, monkeypatch
):
    fwd_rule = Rule(
        account=offline_account,
        id="RAW-FWD-9",
        display_name="Auto Forward",
        priority=1,
        is_enabled=True,
        actions=Actions(
            forward_to_recipients=[Address(email_address="ext@example.com")]
        ),
    )
    backend = FakeRulesBackend(offline_account, initial_rules=[fwd_rule])
    _wire_backend(monkeypatch, backend)
    # Denylist everything to prove delete_rule is never blocked by forwarding
    # recipients on the rule being removed.
    ctx = _make_ctx(
        tmp_path, backend, ews_recipient_denylist="*@*"
    )
    alias = _call(ctx, "list_rules")["items"][0]["id"]

    refused = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Auto Forward",
        actions={"mark_as_read": True},
    )
    assert refused["ok"] is False
    assert refused["error"]["code"] == "validation"
    assert "actions.forward_to_recipients" in refused["error"]["message"]
    assert backend.set_calls == []

    del_p1 = _call(ctx, "delete_rule", rule_id=alias)
    assert del_p1["requires_confirmation"] is True
    del_p2 = _call(
        ctx,
        "delete_rule",
        rule_id=alias,
        confirm_token=del_p1["confirm_token"],
    )
    assert del_p2["ok"] is True
    assert del_p2["id"] == alias
    assert del_p2["name"] == "Auto Forward"
    assert backend.delete_calls == ["RAW-FWD-9"]


def test_create_refuses_duplicate_name_and_new_id_comes_from_before_after_diff(
    tmp_path, offline_account, monkeypatch
):
    existing = Rule(
        account=offline_account,
        id="RAW-ORIG-1",
        display_name="Newsletters",
        priority=1,
        is_enabled=True,
        actions=Actions(mark_as_read=True),
    )
    backend = FakeRulesBackend(offline_account, initial_rules=[existing])
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    dup = _call(
        ctx,
        "save_rule",
        name="Newsletters",
        actions={"move_to_folder": "Archive"},
    )
    assert dup["ok"] is False
    assert dup["error"]["code"] == "validation"
    assert backend.create_calls == []

    p1 = _call(
        ctx,
        "save_rule",
        name="Alerts",
        actions={"move_to_folder": "Archive"},
    )
    p2 = _call(
        ctx,
        "save_rule",
        name="Alerts",
        actions={"move_to_folder": "Archive"},
        confirm_token=p1["confirm_token"],
    )
    assert p2["ok"] is True
    assert p2["replaced"] is False
    # FakeRulesBackend sets rule.id = "UNTRUSTED-DISPLAY-NAME-ID" inside
    # Account.create_rule; the alias must resolve to RAW-CREATED-101 from diff.
    resolved_raw = ctx.aliaser.resolve(p2["id"])
    assert resolved_raw == "RAW-CREATED-101"


def test_unknown_rule_id_returns_not_found_with_list_rules_hint(
    tmp_path, offline_account, monkeypatch
):
    backend = FakeRulesBackend(offline_account)
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    for bad_id in ("r999", "RAW-NON-EXISTENT"):
        save_res = _call(
            ctx,
            "save_rule",
            rule_id=bad_id,
            name="Missing",
            actions={"mark_as_read": True},
        )
        assert save_res["error"]["code"] == "not_found"
        assert "list_rules" in save_res["error"]["hint"]

        del_res = _call(ctx, "delete_rule", rule_id=bad_id)
        assert del_res["error"]["code"] == "not_found"
        assert "list_rules" in del_res["error"]["hint"]


# --- 6. Capability tiers & alias kind ----------------------------------------


def test_tier_gating_and_rule_id_alias_kind(
    tmp_path, offline_account, monkeypatch
):
    assert kind_for_key("rule_id") == "r"

    backend = FakeRulesBackend(offline_account)
    _wire_backend(monkeypatch, backend)

    read_ctx = _make_ctx(tmp_path / "read", backend, ews_capability_tier="read")
    assert "list_rules" in read_ctx.registry
    assert "save_rule" not in read_ctx.registry
    assert "delete_rule" not in read_ctx.registry

    listed = _call(read_ctx, "list_rules")
    assert listed["ok"] is True
    blocked_save = _call(
        read_ctx, "save_rule", name="Blocked", actions={"mark_as_read": True}
    )
    assert blocked_save["error"]["code"] == "tier_blocked"
    blocked_del = _call(read_ctx, "delete_rule", rule_id="r1")
    assert blocked_del["error"]["code"] == "tier_blocked"

    draft_ctx = _make_ctx(
        tmp_path / "draft", backend, ews_capability_tier="draft"
    )
    assert {"list_rules", "save_rule", "delete_rule"}.issubset(
        draft_ctx.registry.keys()
    )


# --- A rule may not file mail into Deleted Items or Junk ----------------------


@pytest.mark.parametrize("action", ["move_to_folder", "copy_to_folder"])
@pytest.mark.parametrize("target", ["Deleted Items", "Junk Email", "Deleted Items/Old"])
def test_rule_cannot_file_mail_into_deleted_items_or_junk(
    tmp_path, offline_account, monkeypatch, action, target
):
    """Moving every match to Deleted Items or Junk is a deleting rule under
    another name; neither phase may accept it and nothing may be created."""
    backend = FakeRulesBackend(offline_account, blob_exists=False)
    trash = backend.add_folder("FLD-TRASH", "Deleted Items")
    backend.add_folder("FLD-JUNK", "Junk Email")
    below = backend.add_folder("FLD-TRASH-OLD", "Deleted Items/Old")
    below.parts = [SimpleNamespace(id="ROOT"), SimpleNamespace(id="TOP"), trash, below]
    backend.discard_ids = {"FLD-TRASH", "FLD-JUNK"}
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    out = _call(
        ctx,
        "save_rule",
        name="Discard",
        conditions={"subject_contains": ["x"]},
        actions={action: target},
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "validation"
    assert "Deleted Items or Junk" in out["error"]["message"]
    assert "confirm_token" not in out
    assert backend.create_calls == []


def test_rule_may_file_mail_into_an_ordinary_folder_when_discard_folders_exist(
    tmp_path, offline_account, monkeypatch
):
    backend = FakeRulesBackend(offline_account, blob_exists=False)
    backend.add_folder("FLD-TRASH", "Deleted Items")
    backend.discard_ids = {"FLD-TRASH", "FLD-JUNK"}
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    preview = _call(
        ctx,
        "save_rule",
        name="File newsletters",
        conditions={"subject_contains": ["Digest"]},
        actions={"move_to_folder": "Projects/Newsletters"},
    )
    assert preview.get("confirm_token"), preview

