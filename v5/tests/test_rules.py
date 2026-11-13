"""Inbox rules tool pack tests — driven through dispatch() with real
exchangelib 5.6.0 model classes, real EWSGateway.resolve_folder, and real
Folder hierarchies."""

from __future__ import annotations

import asyncio
import json
import time
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
from exchangelib.errors import (
    ErrorAccessDenied,
    ErrorFolderNotFound,
    ErrorItemNotFound,
)
from exchangelib.fields import BooleanField
from exchangelib.folders import (
    DeletedItems,
    Folder,
    Inbox,
    JunkEmail,
    MsgFolderRoot,
    Root,
)
from exchangelib.properties import (
    Actions,
    Address,
    Conditions,
    CopyToFolder,
    DistinguishedFolderId,
    Exceptions,
    FolderId,
    ItemId,
    Mailbox,
    MoveToFolder,
    ParentFolderId,
    Rule,
    WithinSizeRange,
)
from exchangelib.protocol import CachingProtocol
from exchangelib.services import GetFolder
from exchangelib.services.inbox_rules import (
    CreateInboxRule,
    DeleteInboxRule,
    SetInboxRule,
)
from exchangelib.util import to_xml, xml_to_str
from exchangelib.version import EXCHANGE_2016, Version

from ewsmcp.audit import AuditLog
from ewsmcp.gateway import rules as gateway_rules
from ewsmcp.gateway.client import EWSGateway
from ewsmcp.gateway.rules import GetInboxRulesWithBlob
from ewsmcp.ids import get_aliaser, kind_for_key
from ewsmcp.tools import build_registry
from ewsmcp.tools import rules as rules_pack
from ewsmcp.tools.base import Context, dispatch

SPEC = {s.name: s for s in rules_pack.TOOLS}
ENVELOPE_KEYS = {"ok", "items", "count", "total_available", "next_offset"}
EMAIL = "user@example.com"

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


def _dist(name: str) -> DistinguishedFolderId:
    return DistinguishedFolderId(id=name, mailbox=Mailbox(email_address=EMAIL))


def _build_real_folder_tree(account: Account) -> Dict[str, Any]:
    """Build real exchangelib Root/MsgFolderRoot/Folder instances offline."""
    root = Root(
        account=account, id="RAWID-ROOT", changekey="RAWCK-ROOT", name="root"
    )
    tois = MsgFolderRoot(
        root=root,
        id="RAWID-TOIS",
        changekey="RAWCK-TOIS",
        name="Top of Information Store",
        parent_folder_id=ParentFolderId(id="RAWID-ROOT"),
        _distinguished_id=_dist("msgfolderroot"),
    )

    def mk(cls, raw_id, name, parent, dist=None, folder_class="IPF.Note"):
        kwargs: Dict[str, Any] = dict(
            root=root,
            id=raw_id,
            changekey="RAWCK-" + raw_id[6:],
            name=name,
            parent_folder_id=ParentFolderId(id=parent.id),
            folder_class=folder_class,
        )
        if dist:
            kwargs["_distinguished_id"] = _dist(dist)
        return cls(**kwargs)

    inbox = mk(Inbox, "RAWID-INBOX", "Inbox", tois, "inbox")
    trash = mk(DeletedItems, "RAWID-TRASH", "Deleted Items", tois, "deleteditems")
    junk = mk(JunkEmail, "RAWID-JUNK", "Junk Email", tois, "junkemail")
    trash_old = mk(Folder, "RAWID-TRASH-OLD", "Old", trash)
    trash_deep = mk(Folder, "RAWID-TRASH-DEEP", "Deeper", trash_old)
    junk_sub = mk(Folder, "RAWID-JUNK-SUB", "Sub", junk)
    archive = mk(Folder, "RAWID-ARCH", "Archive", tois)
    projects = mk(Folder, "RAWID-PROJ", "Projects", tois)
    news = mk(Folder, "RAWID-NEWS", "Newsletters", projects)
    copies = mk(Folder, "RAWID-COPY", "Copies", projects)

    folders = {
        "root": root,
        "tois": tois,
        "inbox": inbox,
        "trash": trash,
        "junk": junk,
        "trash_old": trash_old,
        "trash_deep": trash_deep,
        "junk_sub": junk_sub,
        "archive": archive,
        "projects": projects,
        "news": news,
        "copies": copies,
    }
    root._subfolders = {f.id: f for f in folders.values()}
    account.__dict__["root"] = root
    return folders


class FakeRulesBackend:
    """Offline rule & folder EWS transport backing a REAL EWSGateway."""

    def __init__(
        self,
        account: Account,
        *,
        initial_rules: Optional[List[Rule]] = None,
        blob_exists: Optional[bool] = False,
    ) -> None:
        self.account = account
        self.tree = _build_real_folder_tree(account)
        self.live_folders: Dict[str, Any] = {
            f.id: f for f in self.tree.values()
        }
        self.rules: List[Rule] = list(initial_rules or [])
        self.blob_exists: Optional[bool] = blob_exists
        self.create_calls: List[Rule] = []
        self.set_calls: List[Rule] = []
        self.delete_calls: List[str] = []
        self.get_folder_calls: List[str] = []
        self.create_delay_s: float = 0.0
        self.fail_disabled_direct_delete: bool = False
        self.fail_readback_after_write: bool = False
        self._in_write_readback: bool = False
        self._seq = 100

    def add_real_folder(
        self, raw_id: str, name: str, parent: Any, *, live_only: bool = False
    ) -> Folder:
        root = self.tree["root"]
        fld = Folder(
            root=root,
            id=raw_id,
            changekey=f"RAWCK-{raw_id}",
            name=name,
            parent_folder_id=ParentFolderId(id=parent.id) if parent else None,
            folder_class="IPF.Note",
        )
        self.live_folders[raw_id] = fld
        if not live_only:
            root._subfolders[raw_id] = fld
        return fld

    def on_get_folder_call(self, folders):
        for item in folders:
            fid = getattr(item, "id", None)
            if not fid and getattr(item, "_distinguished_id", None) is not None:
                dfid = item._distinguished_id.id
                for cand in self.live_folders.values():
                    c_dfid = getattr(cand, "DISTINGUISHED_FOLDER_ID", None)
                    if c_dfid == dfid:
                        fid = cand.id
                        break
            self.get_folder_calls.append(str(fid))
            found = self.live_folders.get(fid)
            if found is None:
                yield ErrorFolderNotFound(f"Folder {fid!r} not found")
            elif isinstance(found, Exception):
                yield found
            else:
                yield found

    def fetch_rules(self, _account):
        if self._in_write_readback and self.fail_readback_after_write:
            self._in_write_readback = False
            raise RuntimeError("Simulated GetInboxRules failure after write")
        self._in_write_readback = False
        return list(self.rules), self.blob_exists

    def _roundtrip_rule_xml(self, rule: Rule) -> Rule:
        elem = _rewrite_rule_booleans_for_parser(
            rule.to_xml(version=self.account.version)
        )
        return Rule.from_xml(elem=elem, account=self.account)

    def on_create_rule(self, rule: Rule) -> None:
        if self.create_delay_s > 0:
            time.sleep(self.create_delay_s)
        self.create_calls.append(rule)
        self._seq += 1
        server_id = f"RAWID-CREATED-{self._seq}"
        stored = self._roundtrip_rule_xml(rule)
        stored.id = server_id
        self.rules.append(stored)
        # Simulate an untrusted side mutation on rule.id so tests prove
        # gateway.create_rule takes the id from the before/after set diff.
        rule.id = "UNTRUSTED-DISPLAY-NAME-ID"
        self._in_write_readback = True

    def on_set_rule(self, rule: Rule) -> None:
        self.set_calls.append(rule)
        stored = self._roundtrip_rule_xml(rule)
        for idx, existing in enumerate(self.rules):
            if existing.id == rule.id:
                self.rules[idx] = stored
                self._in_write_readback = True
                return
        raise ErrorItemNotFound(f"Rule {rule.id!r} not found")

    def on_delete_rule(self, rule: Rule) -> None:
        target_id = str(rule.id)
        matched = next((r for r in self.rules if r.id == target_id), None)
        if matched is None:
            raise ErrorItemNotFound(f"Rule {target_id!r} not found")
        if self.fail_disabled_direct_delete and matched.is_enabled is False:
            raise ErrorItemNotFound("Disabled rule cannot be deleted directly")
        self.delete_calls.append(target_id)
        self.rules = [r for r in self.rules if r.id != target_id]


_ACTIVE_GATEWAYS: List[EWSGateway] = []


@pytest.fixture(autouse=True)
def _cleanup_rules_state():
    rules_pack.reset_rules_lock()
    yield
    rules_pack.reset_rules_lock()
    while _ACTIVE_GATEWAYS:
        gw = _ACTIVE_GATEWAYS.pop()
        gw._pool.shutdown(wait=False)


@pytest.fixture()
def offline_account():
    CachingProtocol.clear_cache()
    try:
        yield Account(
            primary_smtp_address=EMAIL,
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
    monkeypatch.setattr(
        GetFolder,
        "call",
        lambda self, folders, **kw: backend.on_get_folder_call(folders),
    )
    monkeypatch.setattr(
        CreateInboxRule,
        "get",
        lambda self, **kw: backend.on_create_rule(kw["rule"]),
    )
    monkeypatch.setattr(
        SetInboxRule,
        "get",
        lambda self, **kw: backend.on_set_rule(kw["rule"]),
    )
    monkeypatch.setattr(
        DeleteInboxRule,
        "get",
        lambda self, **kw: backend.on_delete_rule(kw["rule"]),
    )


def _make_ctx(
    tmp_path, backend: FakeRulesBackend, **settings_overrides
) -> Context:
    defaults = dict(
        ews_email=EMAIL,
        ews_capability_tier="draft",
        send_enabled=False,
    )
    defaults.update(settings_overrides)
    settings = make_settings(**defaults)
    gateway = EWSGateway(settings)
    gateway._account = backend.account
    _ACTIVE_GATEWAYS.append(gateway)
    ctx = Context(
        settings=settings,
        gateway=gateway,
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "aliases")),
        audit=AuditLog(str(tmp_path / "audit")),
    )
    build_registry(ctx)
    return ctx


def _call(ctx: Context, tool: str, /, **kwargs) -> Dict[str, Any]:
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
    assert p2["verified"] is True
    assert p2["replaced"] is False
    assert p2["id"].startswith("r")

    built = backend.create_calls[0]
    assert isinstance(built, Rule)
    assert isinstance(built.actions, Actions)
    assert isinstance(built.conditions, Conditions)
    assert isinstance(built.actions.move_to_folder, MoveToFolder)
    assert isinstance(built.actions.copy_to_folder, CopyToFolder)
    assert built.actions.move_to_folder.distinguished_folder_id is None
    assert built.actions.move_to_folder.folder_id.id == "RAWID-NEWS"
    assert built.actions.copy_to_folder.distinguished_folder_id is None
    assert built.actions.copy_to_folder.folder_id.id == "RAWID-COPY"
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
    assert listed["source"] == "live"
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
        id="RAWID-NON-EDITABLE-1",
        display_name="Server Special",
        priority=1,
        is_enabled=True,
        actions=Actions(**action_kwargs),
    )
    backend = FakeRulesBackend(offline_account, initial_rules=[rule])
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    res = _call(ctx, "list_rules")
    assert res["source"] == "live"
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
        id="RAWID-CLEAN",
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
        id="RAWID-COMPLEX",
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
            move_to_folder=MoveToFolder(folder_id=FolderId(id="RAWID-DELETED"))
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
    assert second["actions"]["move_to_folder"].startswith("f")
    assert "RAWID-" not in json.dumps(res)


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
    assert "RAWID-" not in json.dumps(p1)
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
        id="RAWID-DEL-1",
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
        id="RAWID-EDIT-1",
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
    assert p1["preview"]["changes"] == ["conditions"]

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

    noop_p1 = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Editable Rule",
        conditions={"subject_contains": ["Beta"]},
        actions={"mark_as_read": True},
    )
    assert noop_p1["preview"]["changes"] == []
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
        id="RAWID-FWD-9",
        display_name="Auto Forward",
        priority=1,
        is_enabled=True,
        actions=Actions(
            forward_to_recipients=[Address(email_address="ext@example.com")]
        ),
    )
    backend = FakeRulesBackend(offline_account, initial_rules=[fwd_rule])
    _wire_backend(monkeypatch, backend)
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
    assert backend.delete_calls == ["RAWID-FWD-9"]


def test_create_refuses_duplicate_name_and_new_id_comes_from_before_after_diff(
    tmp_path, offline_account, monkeypatch
):
    existing = Rule(
        account=offline_account,
        id="RAWID-ORIG-1",
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
    resolved_raw = ctx.aliaser.resolve(p2["id"])
    assert resolved_raw == "RAWID-CREATED-101"


def test_unknown_rule_id_returns_not_found_without_minting_r_alias(
    tmp_path, offline_account, monkeypatch
):
    """Findings 4 & 12: unknown rule ids point to list_rules (never
    search_messages) and never mint an r-alias for a nonexistent raw id."""
    backend = FakeRulesBackend(offline_account)
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    assert ctx.aliaser.stats().get("r", 0) == 0

    for bad_id in ("r999", "RAWID-NON-EXISTENT"):
        save_res = _call(
            ctx,
            "save_rule",
            rule_id=bad_id,
            name="Missing",
            actions={"mark_as_read": True},
        )
        assert save_res["error"]["code"] == "not_found"
        assert "list_rules" in save_res["error"]["hint"]
        assert "search_messages" not in save_res["error"]["hint"]

        del_res = _call(ctx, "delete_rule", rule_id=bad_id)
        assert del_res["error"]["code"] == "not_found"
        assert "list_rules" in del_res["error"]["hint"]
        assert "search_messages" not in del_res["error"]["hint"]

    assert ctx.aliaser.stats().get("r", 0) == 0


# --- 5. Capability tiers & alias kind ----------------------------------------


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


# --- 6. Fail-closed discard guard, real FolderId & live ancestry -------------


def test_discard_guard_all_reference_forms_with_real_ewsgateway_and_folders(
    tmp_path, offline_account, monkeypatch
):
    """Findings 1 & 5: every reference form to Deleted Items, Junk, and their
    subfolders is refused for both move_to_folder and copy_to_folder using the
    real EWSGateway.resolve_folder and real exchangelib Folder classes; valid
    folders always emit a real FolderId (never DistinguishedFolderId)."""
    backend = FakeRulesBackend(offline_account, blob_exists=False)
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    f_trash = ctx.aliaser.alias_for("RAWID-TRASH", "f")
    f_old = ctx.aliaser.alias_for("RAWID-TRASH-OLD", "f")
    f_junk_sub = ctx.aliaser.alias_for("RAWID-JUNK-SUB", "f")

    discard_refs = [
        "trash",
        "Trash",
        "TRASH",
        " trash ",
        "f:trash",
        "F:TRASH",
        "deleteditems",
        "DeletedItems",
        "f:deleteditems",
        "junk",
        "JunkEmail",
        "f:junkemail",
        "Deleted Items",
        "deleted items",
        "DELETED ITEMS",
        "Junk Email",
        "Deleted Items/Old",
        "deleted items/old/deeper",
        "Junk Email/Sub",
        "RAWID-TRASH",
        "RAWID-JUNK",
        "RAWID-TRASH-OLD",
        "RAWID-TRASH-DEEP",
        "RAWID-JUNK-SUB",
        f_trash,
        f_old,
        f_junk_sub,
    ]
    for action in ("move_to_folder", "copy_to_folder"):
        for ref in discard_refs:
            res = _call(
                ctx,
                "save_rule",
                name=f"Discard-{action}-{ref}",
                conditions={"subject_contains": ["x"]},
                actions={action: ref},
            )
            assert res["ok"] is False, (action, ref, res)
            assert res["error"]["code"] == "validation", (action, ref, res)
            assert "confirm_token" not in res
    assert backend.create_calls == []

    # Even when caller passes well-known "f:inbox" / "inbox", the wire payload
    # carries <t:FolderId Id="RAWID-INBOX">, never <t:DistinguishedFolderId>.
    p1 = _call(
        ctx,
        "save_rule",
        name="Keep in Inbox",
        conditions={"subject_contains": ["keep"]},
        actions={"move_to_folder": "f:inbox", "copy_to_folder": "Archive"},
    )
    assert p1["requires_confirmation"] is True
    p2 = _call(
        ctx,
        "save_rule",
        name="Keep in Inbox",
        conditions={"subject_contains": ["keep"]},
        actions={"move_to_folder": "f:inbox", "copy_to_folder": "Archive"},
        confirm_token=p1["confirm_token"],
    )
    assert p2["ok"] is True
    created_rule = backend.create_calls[-1]
    payload_xml = xml_to_str(
        CreateInboxRule(account=offline_account).get_payload(rule=created_rule)
    )
    assert "DistinguishedFolderId" not in payload_xml
    assert 'FolderId Id="RAWID-INBOX"' in payload_xml
    assert 'FolderId Id="RAWID-ARCH"' in payload_xml


def test_discard_guard_fails_closed_when_distinguished_getfolder_is_denied(
    tmp_path, offline_account, monkeypatch
):
    """Finding 1: when GetFolder on DeletedItems/JunkEmail is denied and
    Root.get_default_folder returns DeletedItems(id=None), every move/copy
    target is refused fail-closed."""
    backend = FakeRulesBackend(offline_account, blob_exists=False)
    # Simulate deep FindFolder traversal where trash/junk lack _distinguished_id
    # so Root.get_default_folder calls get_distinguished and hits ErrorAccessDenied.
    backend.tree["trash"]._distinguished_id = None
    backend.tree["junk"]._distinguished_id = None

    def _denied(cls, root):
        raise ErrorAccessDenied("Access is denied.")

    monkeypatch.setattr(DeletedItems, "get_distinguished", classmethod(_denied))
    monkeypatch.setattr(JunkEmail, "get_distinguished", classmethod(_denied))
    monkeypatch.setattr(Folder, "test_access", lambda self: True)

    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)
    f_junk = ctx.aliaser.alias_for("RAWID-JUNK", "f")

    assert offline_account.trash.id is None
    assert offline_account.junk.id is None

    for ref in (
        "trash",
        "f:deleteditems",
        "DeletedItems",
        "junk",
        "JunkEmail",
        "Deleted Items",
        "Deleted Items/Old",
        "RAWID-TRASH",
        f_junk,
        "Archive",
        "Projects/Newsletters",
    ):
        for action in ("move_to_folder", "copy_to_folder"):
            out = _call(
                ctx,
                "save_rule",
                name=f"Denied-{action}-{ref}",
                conditions={"subject_contains": ["x"]},
                actions={action: ref},
            )
            assert out["ok"] is False, (action, ref, out)
            assert out["error"]["code"] == "validation"
            assert "confirm_token" not in out
    assert backend.create_calls == []


def test_live_ancestry_catches_reparented_folder_orphans_outside_root_and_depth(
    tmp_path, offline_account, monkeypatch
):
    """Finding 2: live GetFolder ancestry walk catches folders moved into
    Deleted Items after caching, orphans, folders outside MsgFolderRoot, and
    chains exceeding 25 levels."""
    backend = FakeRulesBackend(offline_account, blob_exists=False)
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    # Warm EWSGateway._folder_cache while Newsletters is under Projects.
    ctx.gateway._folder_map(offline_account)

    # Preview while Newsletters is legitimately under Projects.
    p1 = _call(
        ctx,
        "save_rule",
        name="Move News",
        conditions={"subject_contains": ["news"]},
        actions={"move_to_folder": "Projects/Newsletters"},
    )
    assert p1["requires_confirmation"] is True

    # Server-side move: Newsletters is moved under Deleted Items/Old WITHOUT
    # refreshing EWSGateway._folder_cache or root._subfolders!
    backend.live_folders["RAWID-NEWS"] = Folder(
        root=backend.tree["root"],
        id="RAWID-NEWS",
        changekey="RAWCK-NEWS-2",
        name="Newsletters",
        parent_folder_id=ParentFolderId(id="RAWID-TRASH-OLD"),
        folder_class="IPF.Note",
    )
    p2_reparented = _call(
        ctx,
        "save_rule",
        name="Move News",
        conditions={"subject_contains": ["news"]},
        actions={"move_to_folder": "Projects/Newsletters"},
        confirm_token=p1["confirm_token"],
    )
    assert p2_reparented["ok"] is False
    assert p2_reparented["error"]["code"] == "validation"
    assert "Deleted Items or Junk" in p2_reparented["error"]["message"]
    assert backend.create_calls == []

    # Orphan folder whose parent is missing on the server -> refused.
    orphan = Folder(
        root=backend.tree["root"],
        id="RAWID-ORPHAN",
        changekey="RAWCK-ORPHAN",
        name="Orphan",
        parent_folder_id=ParentFolderId(id="RAWID-MISSING-PARENT"),
        folder_class="IPF.Note",
    )
    backend.tree["root"]._subfolders["RAWID-ORPHAN"] = orphan
    backend.live_folders["RAWID-ORPHAN"] = orphan
    # Make the orphan resolvable by raw id, so that it is the ancestry check
    # that refuses it. (After a full rebuild the folder walk does not reach an
    # orphan at all and resolve_folder answers not_found, which is a refusal
    # too but not the one under test.)
    ctx.gateway._folder_cache[orphan.id] = orphan

    orphan_res = _call(
        ctx,
        "save_rule",
        name="Orphan Rule",
        actions={"move_to_folder": "RAWID-ORPHAN"},
    )
    assert orphan_res["ok"] is False
    assert orphan_res["error"]["code"] == "validation"
    assert "location" in orphan_res["error"]["message"]

    # Folder under Root (outside MsgFolderRoot, e.g. Recoverable Items) -> refused.
    outside = backend.add_real_folder(
        "RAWID-RECOVERABLE", "Recoverable Items", backend.tree["root"]
    )
    ctx.gateway._folder_cache[outside.id] = outside
    outside_res = _call(
        ctx,
        "save_rule",
        name="Outside Root",
        actions={"move_to_folder": "RAWID-RECOVERABLE"},
    )
    assert outside_res["ok"] is False
    assert outside_res["error"]["code"] == "validation"
    assert "location" in outside_res["error"]["message"]

    # 25 levels under MsgFolderRoot succeeds; level 26 refuses.
    parent = backend.tree["tois"]
    leaf_25 = None
    for depth in range(1, 26):
        leaf_25 = backend.add_real_folder(
            f"RAWID-LVL-{depth}", f"Lvl{depth}", parent
        )
        ctx.gateway._folder_cache[leaf_25.id] = leaf_25
        parent = leaf_25
    assert leaf_25 is not None
    ok_25 = _call(
        ctx,
        "save_rule",
        name="Depth 25",
        actions={"move_to_folder": leaf_25.id},
    )
    assert ok_25["requires_confirmation"] is True

    leaf_26 = backend.add_real_folder("RAWID-LVL-26", "Lvl26", leaf_25)
    ctx.gateway._folder_cache[leaf_26.id] = leaf_26
    too_deep = _call(
        ctx,
        "save_rule",
        name="Depth 26",
        actions={"move_to_folder": leaf_26.id},
    )
    assert too_deep["ok"] is False
    assert too_deep["error"]["code"] == "validation"
    assert "location" in too_deep["error"]["message"]


# --- 7. Separate write vs read-back & disabled-rule deletion -----------------


def test_write_succeeds_when_readback_fails_returns_ok_unverified(
    tmp_path, offline_account, monkeypatch
):
    """Finding 3: when CreateInboxRule or SetInboxRule succeeds and the
    subsequent GetInboxRules read-back fails, return ok=True, verified=False."""
    backend = FakeRulesBackend(offline_account, blob_exists=False)
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    p1 = _call(
        ctx,
        "save_rule",
        name="Unverified Create",
        conditions={"subject_contains": ["ping"]},
        actions={"mark_as_read": True},
    )
    backend.fail_readback_after_write = True
    p2 = _call(
        ctx,
        "save_rule",
        name="Unverified Create",
        conditions={"subject_contains": ["ping"]},
        actions={"mark_as_read": True},
        confirm_token=p1["confirm_token"],
    )
    assert p2["ok"] is True
    assert p2["verified"] is False
    assert p2["id"] is None
    assert p2["replaced"] is False
    assert p2["rule"]["id"] is None
    assert p2["rule"]["name"] == "Unverified Create"
    assert "list_rules" in p2["note"]
    assert len(backend.rules) == 1

    backend.fail_readback_after_write = False
    alias = _call(ctx, "list_rules")["items"][0]["id"]

    rp1 = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Unverified Replace",
        conditions={"subject_contains": ["pong"]},
        actions={"mark_as_read": True},
    )
    backend.fail_readback_after_write = True
    rp2 = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Unverified Replace",
        conditions={"subject_contains": ["pong"]},
        actions={"mark_as_read": True},
        confirm_token=rp1["confirm_token"],
    )
    assert rp2["ok"] is True
    assert rp2["verified"] is False
    assert rp2["id"] == alias
    assert rp2["replaced"] is True
    assert rp2["rule"]["name"] == "Unverified Replace"
    assert "list_rules" in rp2["note"]


def test_delete_disabled_rule_end_to_end_via_safe_neutralisation(
    tmp_path, offline_account, monkeypatch
):
    """Finding 4: when Exchange returns ErrorItemNotFound on deleting a
    disabled rule, delete_rule neutralises it with a 48-char random token +
    stop_processing_rules and deletes it."""
    disabled = Rule(
        account=offline_account,
        id="RAWID-DISABLED-9",
        display_name="Disabled Rule",
        priority=5,
        is_enabled=False,
        actions=Actions(delete=True),
    )
    backend = FakeRulesBackend(offline_account, initial_rules=[disabled])
    backend.fail_disabled_direct_delete = True
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    alias = _call(ctx, "list_rules")["items"][0]["id"]
    p1 = _call(ctx, "delete_rule", rule_id=alias)
    assert p1["requires_confirmation"] is True

    p2 = _call(
        ctx,
        "delete_rule",
        rule_id=alias,
        confirm_token=p1["confirm_token"],
    )
    assert p2["ok"] is True
    assert p2["id"] == alias
    assert p2["name"] == "Disabled Rule"
    assert backend.rules == []
    assert len(backend.set_calls) == 1
    neutral = backend.set_calls[0]
    assert neutral.is_enabled is True
    assert len(neutral.conditions.contains_subject_strings[0]) == 48
    assert neutral.actions.stop_processing_rules is True
    assert neutral.actions.delete is None


# --- 8. Server-form XML edge shapes, unnameable targets & zero raw-id leaks --


def test_server_form_xml_edges_unnameable_targets_and_zero_raw_id_leaks(
    tmp_path, offline_account, monkeypatch
):
    """Findings 5, 9, 10, 15: feed real server-form XML through the real
    gateway_rules.fetch_rules into list_rules."""
    backend = FakeRulesBackend(offline_account)
    # Also add a nameless folder to verify _folder_path falls back to an f-alias.
    nameless = Folder(
        root=backend.tree["root"],
        id="RAWID-NAMELESS",
        changekey="RAWCK-NAMELESS",
        name="",
        parent_folder_id=ParentFolderId(id="RAWID-TOIS"),
        folder_class="IPF.Note",
    )
    backend.tree["root"]._subfolders["RAWID-NAMELESS"] = nameless
    backend.live_folders["RAWID-NAMELESS"] = nameless

    ns = (
        'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
        'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"'
    )
    response_xml = (
        f'<m:GetInboxRulesResponse ResponseClass="Success" {ns}>'
        "<m:ResponseCode>NoError</m:ResponseCode>"
        "<m:OutlookRuleBlobExists>true</m:OutlookRuleBlobExists>"
        "<m:InboxRules>"
        "<t:Rule><t:RuleId>RAWID-R1</t:RuleId><t:DisplayName>Empty Move</t:DisplayName>"
        "<t:Priority>1</t:Priority><t:IsEnabled>true</t:IsEnabled>"
        "<t:Actions><t:MarkAsRead>true</t:MarkAsRead>"
        "<t:MoveToFolder/></t:Actions></t:Rule>"
        "<t:Rule><t:RuleId>RAWID-R2</t:RuleId><t:DisplayName>Unknown Move Child</t:DisplayName>"
        "<t:Priority>2</t:Priority><t:IsEnabled>true</t:IsEnabled>"
        "<t:Actions><t:MarkAsRead>true</t:MarkAsRead>"
        '<t:MoveToFolder><t:AddressListId Id="RAWID-AL"/></t:MoveToFolder>'
        "</t:Actions></t:Rule>"
        "<t:Rule><t:RuleId>RAWID-R3</t:RuleId><t:DisplayName>Distinguished Copy</t:DisplayName>"
        "<t:Priority>3</t:Priority><t:IsEnabled>true</t:IsEnabled>"
        "<t:Actions><t:MarkAsRead>true</t:MarkAsRead>"
        '<t:CopyToFolder><t:DistinguishedFolderId Id="deleteditems"/></t:CopyToFolder>'
        "</t:Actions></t:Rule>"
        "<t:Rule><t:RuleId>RAWID-R4</t:RuleId><t:DisplayName>Empty ServerReply</t:DisplayName>"
        "<t:Priority>4</t:Priority><t:IsEnabled>true</t:IsEnabled>"
        "<t:Actions><t:MarkAsRead>true</t:MarkAsRead>"
        "<t:ServerReplyWithMessage/></t:Actions></t:Rule>"
        "<t:Rule><t:RuleId>RAWID-R5</t:RuleId><t:DisplayName>Delete One</t:DisplayName>"
        "<t:Priority>5</t:Priority><t:IsEnabled>true</t:IsEnabled>"
        "<t:Actions><t:MarkAsRead>true</t:MarkAsRead>"
        "<t:Delete>1</t:Delete></t:Actions></t:Rule>"
        "<t:Rule><t:RuleId>RAWID-R6</t:RuleId><t:DisplayName>Unknown Action</t:DisplayName>"
        "<t:Priority>6</t:Priority><t:IsEnabled>true</t:IsEnabled>"
        "<t:Actions><t:MarkAsRead>true</t:MarkAsRead>"
        "<t:FutureSendAction>true</t:FutureSendAction></t:Actions></t:Rule>"
        "<t:Rule><t:RuleId>RAWID-R7</t:RuleId><t:DisplayName>Unknown Cond</t:DisplayName>"
        "<t:Priority>7</t:Priority><t:IsEnabled>true</t:IsEnabled>"
        "<t:Conditions><t:FuturePredicate>true</t:FuturePredicate>"
        "<t:ContainsSubjectStrings><t:String>v</t:String></t:ContainsSubjectStrings>"
        "</t:Conditions><t:Actions><t:MarkAsRead>true</t:MarkAsRead></t:Actions></t:Rule>"
        "<t:Rule><t:RuleId>RAWID-R8</t:RuleId><t:DisplayName>Nameless Folder</t:DisplayName>"
        "<t:Priority>8</t:Priority><t:IsEnabled>true</t:IsEnabled>"
        "<t:Actions><t:MoveToFolder>"
        '<t:FolderId Id="RAWID-NAMELESS" ChangeKey="RAWCK-NAMELESS"/>'
        "</t:MoveToFolder></t:Actions></t:Rule>"
        "</m:InboxRules></m:GetInboxRulesResponse>"
    )
    msg_elem = to_xml(response_xml.encode("utf-8")).getroot()
    monkeypatch.setattr(
        GetInboxRulesWithBlob,
        "_get_elements",
        lambda self, payload: iter(
            self._get_element_container(
                msg_elem, name=self.element_container_name
            )
        ),
    )

    ctx = _make_ctx(tmp_path, backend)
    out = _call(ctx, "list_rules")
    assert out["ok"] is True
    assert out["source"] == "live"
    assert out["outlook_only_rules"] is True

    by_name = {item["name"]: item for item in out["items"]}

    for uneditable_name, expected_tag in (
        ("Empty Move", "actions.move_to_folder"),
        ("Unknown Move Child", "actions.move_to_folder"),
        ("Distinguished Copy", "actions.copy_to_folder"),
        ("Empty ServerReply", "actions.server_reply_with_message"),
        ("Delete One", "actions.delete"),
        ("Unknown Action", "actions.FutureSendAction"),
        ("Unknown Cond", "conditions.FuturePredicate"),
    ):
        item = by_name[uneditable_name]
        assert item["editable"] is False, (uneditable_name, item)
        assert expected_tag in item["unsupported"], (uneditable_name, item)
        for act_val in item["actions"].values():
            assert act_val != "", (uneditable_name, item)

    assert by_name["Empty ServerReply"]["sends_mail"] is True
    assert by_name["Nameless Folder"]["editable"] is True
    assert by_name["Nameless Folder"]["actions"]["move_to_folder"].startswith("f")

    serialised = json.dumps(out)
    assert "RAWID-" not in serialised
    assert "RAWCK-" not in serialised


# --- 9. Token bindings (XML fingerprint & raw folder id) ---------------------


def test_token_binds_full_rule_xml_fingerprint_and_raw_target_folder_id(
    tmp_path, offline_account, monkeypatch
):
    """Findings 7 & 8: changing an uneditable rule's XML details invalidates
    delete_rule's token, and swapping a folder behind the same display path
    invalidates save_rule's token; neither secret/raw value is shown in preview."""
    reply_rule = Rule(
        account=offline_account,
        id="RAWID-TPL-RULE",
        display_name="Auto Reply",
        priority=1,
        is_enabled=True,
        actions=Actions(
            server_reply_with_message=ItemId(id="RAWID-TPL-1", changekey="CK-1")
        ),
    )
    backend = FakeRulesBackend(offline_account, initial_rules=[reply_rule])
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    alias = _call(ctx, "list_rules")["items"][0]["id"]
    del_p1 = _call(ctx, "delete_rule", rule_id=alias)
    assert del_p1["requires_confirmation"] is True
    assert "target_fingerprint" not in json.dumps(del_p1["preview"])
    assert "RAWID-" not in json.dumps(del_p1["preview"])

    # Server changes only the template ItemId (DTO fields stay identical!).
    backend.rules[0] = Rule(
        account=offline_account,
        id="RAWID-TPL-RULE",
        display_name="Auto Reply",
        priority=1,
        is_enabled=True,
        actions=Actions(
            server_reply_with_message=ItemId(id="RAWID-TPL-2", changekey="CK-2")
        ),
    )
    del_stale = _call(
        ctx, "delete_rule", rule_id=alias, confirm_token=del_p1["confirm_token"]
    )
    assert del_stale["error"]["code"] == "confirm_invalid"
    assert backend.delete_calls == []

    # Folder swap behind identical display path "Archive".
    save_p1 = _call(
        ctx,
        "save_rule",
        name="File Archive",
        conditions={"subject_contains": ["arch"]},
        actions={"move_to_folder": "Archive"},
    )
    assert save_p1["requires_confirmation"] is True
    assert "target_folder_ids" not in json.dumps(save_p1["preview"])
    assert "RAWID-" not in json.dumps(save_p1["preview"])

    # Replace "Archive" with a newly created folder of the same name and path
    # but a different raw FolderId.
    new_archive = Folder(
        root=backend.tree["root"],
        id="RAWID-ARCH-SWAPPED",
        changekey="RAWCK-ARCH-SWAPPED",
        name="Archive",
        parent_folder_id=ParentFolderId(id="RAWID-TOIS"),
        folder_class="IPF.Note",
    )
    del backend.tree["root"]._subfolders["RAWID-ARCH"]
    del backend.live_folders["RAWID-ARCH"]
    backend.tree["root"]._subfolders["RAWID-ARCH-SWAPPED"] = new_archive
    backend.live_folders["RAWID-ARCH-SWAPPED"] = new_archive
    ctx.gateway._folder_cache.clear()

    save_stale = _call(
        ctx,
        "save_rule",
        name="File Archive",
        conditions={"subject_contains": ["arch"]},
        actions={"move_to_folder": "Archive"},
        confirm_token=save_p1["confirm_token"],
    )
    assert save_stale["error"]["code"] == "confirm_invalid"
    assert backend.create_calls == []


# --- 10. Input limits, unknown kwargs, deduped resolution, lock & replace ----


def test_255_char_limits_and_unknown_top_level_args_never_consume_token(
    tmp_path, offline_account, monkeypatch
):
    """Finding 11: 255-char limits on name, categories, and condition strings
    are enforced in preview, and unknown top-level arguments fail with
    validation without minting or consuming a confirm_token."""
    backend = FakeRulesBackend(offline_account)
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    too_long = "x" * 256
    exact_255 = "y" * 255

    for bad_kwargs in (
        {"name": too_long, "actions": {"mark_as_read": True}},
        {"name": "Ok", "actions": {"assign_categories": [too_long]}},
        {
            "name": "Ok",
            "conditions": {"subject_contains": [too_long]},
            "actions": {"mark_as_read": True},
        },
        {
            "name": "Ok",
            "conditions": {"body_contains": [too_long]},
            "actions": {"mark_as_read": True},
        },
        {
            "name": "Ok",
            "actions": {"mark_as_read": True},
            "unexpected_top_arg": 123,
        },
    ):
        res = _call(ctx, "save_rule", **bad_kwargs)
        assert res["ok"] is False, bad_kwargs
        assert res["error"]["code"] == "validation"
        assert "confirm_token" not in res

    bad_list = _call(ctx, "list_rules", bogus=True)
    assert bad_list["error"]["code"] == "validation"
    bad_del = _call(ctx, "delete_rule", rule_id="r1", bogus=True)
    assert bad_del["error"]["code"] == "validation"

    # Valid 255-char strings pass preview, and passing an extra top-level kwarg
    # on phase 2 fails BEFORE consuming the valid token!
    p1 = _call(
        ctx,
        "save_rule",
        name=exact_255,
        conditions={"subject_contains": [exact_255]},
        actions={"assign_categories": [exact_255]},
    )
    assert p1["requires_confirmation"] is True

    bad_p2 = _call(
        ctx,
        "save_rule",
        name=exact_255,
        conditions={"subject_contains": [exact_255]},
        actions={"assign_categories": [exact_255]},
        extra_arg="nope",
        confirm_token=p1["confirm_token"],
    )
    assert bad_p2["error"]["code"] == "validation"
    assert backend.create_calls == []

    # Token was NOT consumed by bad_p2, so the valid phase-2 call still works.
    good_p2 = _call(
        ctx,
        "save_rule",
        name=exact_255,
        conditions={"subject_contains": [exact_255]},
        actions={"assign_categories": [exact_255]},
        confirm_token=p1["confirm_token"],
    )
    assert good_p2["ok"] is True


def test_list_rules_resolves_shared_target_folder_once_per_call(
    tmp_path, offline_account, monkeypatch
):
    """Finding 13: list_rules resolves each distinct target folder at most once."""
    rules_many = [
        Rule(
            account=offline_account,
            id=f"RAWID-MANY-{i}",
            display_name=f"Rule {i}",
            priority=i,
            is_enabled=True,
            actions=Actions(
                move_to_folder=MoveToFolder(folder_id=FolderId(id="RAWID-NEWS")),
                copy_to_folder=CopyToFolder(folder_id=FolderId(id="RAWID-GONE")),
            ),
        )
        for i in range(1, 6)
    ]
    backend = FakeRulesBackend(offline_account, initial_rules=rules_many)
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    seen_refs: List[str] = []
    real_resolve = ctx.gateway.resolve_folder

    def counting_resolve(account, ref, aliaser):
        seen_refs.append(str(ref))
        return real_resolve(account, ref, aliaser)

    monkeypatch.setattr(ctx.gateway, "resolve_folder", counting_resolve)

    out = _call(ctx, "list_rules")
    assert out["count"] == 5
    assert seen_refs.count("RAWID-NEWS") == 1
    assert seen_refs.count("RAWID-GONE") == 1


def test_parallel_confirmed_creates_with_same_name_are_serialised(
    tmp_path, offline_account, monkeypatch
):
    """Finding 14: two parallel phase-2 creates for the same rule name are
    serialised by _rules_lock so only one rule is created."""
    backend = FakeRulesBackend(offline_account)
    backend.create_delay_s = 0.05
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    spec = ctx.registry["save_rule"]
    args = {
        "name": "Concurrent Rule",
        "conditions": {"subject_contains": ["race"]},
        "actions": {"mark_as_read": True},
    }
    p1_a = _call(ctx, "save_rule", **args)
    # A token carries its expiry in whole seconds: two identical previews in
    # the same second yield the same token, and the second confirm would then
    # be refused as a replay before the race under test could happen.
    time.sleep(1.1)
    p1_b = _call(ctx, "save_rule", **args)
    assert p1_a["confirm_token"] != p1_b["confirm_token"]

    async def run_both():
        return await asyncio.gather(
            dispatch(
                ctx, spec, {**args, "confirm_token": p1_a["confirm_token"]}
            ),
            dispatch(
                ctx, spec, {**args, "confirm_token": p1_b["confirm_token"]}
            ),
        )

    res_a, res_b = asyncio.run(run_both())
    outcomes = [res_a.get("ok"), res_b.get("ok")]
    assert sorted(outcomes) == [False, True]
    failed = res_a if not res_a.get("ok") else res_b
    assert failed["error"]["code"] == "validation"
    assert len(backend.rules) == 1


def test_replace_preserves_omitted_priority_and_enabled_and_lists_changes(
    tmp_path, offline_account, monkeypatch
):
    """Finding 17: omitting priority or enabled on replace keeps the current
    rule's values, and the preview reports changed field names in changes."""
    existing = Rule(
        account=offline_account,
        id="RAWID-KEEP-1",
        display_name="Existing Custom",
        priority=7,
        is_enabled=False,
        conditions=Conditions(contains_subject_strings=["Old"]),
        actions=Actions(mark_as_read=True),
    )
    backend = FakeRulesBackend(offline_account, initial_rules=[existing])
    _wire_backend(monkeypatch, backend)
    ctx = _make_ctx(tmp_path, backend)

    alias = _call(ctx, "list_rules")["items"][0]["id"]

    # Omit priority and enabled; change only conditions and actions.
    p1 = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Existing Custom",
        conditions={"subject_contains": ["New"]},
        actions={"mark_as_read": True, "stop_processing_rules": True},
    )
    assert p1["requires_confirmation"] is True
    preview = p1["preview"]
    assert preview["rule"]["priority"] == 7
    assert preview["rule"]["enabled"] is False
    assert preview["changes"] == ["conditions", "actions"]

    p2 = _call(
        ctx,
        "save_rule",
        rule_id=alias,
        name="Existing Custom",
        conditions={"subject_contains": ["New"]},
        actions={"mark_as_read": True, "stop_processing_rules": True},
        confirm_token=p1["confirm_token"],
    )
    assert p2["ok"] is True
    assert p2["rule"]["priority"] == 7
    assert p2["rule"]["enabled"] is False
