"""rules tool pack — driven through dispatch() so every dispatcher gate
(tier, recipient guard, two-phase confirm, alias resolution) is exercised
exactly as production would, same approach as test_writes.py.

GetInboxRules/UpdateInboxRules are monkeypatched to fakes: the real
classes need an actual exchangelib Protocol/session (network), which
test_gateway_rules.py's pure XML build/parse tests deliberately avoid.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from lxml import etree

from conftest import make_settings

from ewsmcp.audit import AuditLog
from ewsmcp.ids import get_aliaser
from ewsmcp.tools import rules
from ewsmcp.tools.base import Context, dispatch

TNS = "http://schemas.microsoft.com/exchange/services/2006/types"
SPEC = {s.name: s for s in rules.TOOLS}


class FakeGateway:
    def __init__(self, account):
        self.account = account
        self.folders: Dict[str, Any] = {}

    async def call(self, fn):
        return fn(self.account)

    def resolve_folder(self, account, ref, aliaser):
        return self.folders[ref]


def make_account():
    return SimpleNamespace()


def make_ctx(tmp_path, account, **overrides) -> Context:
    return Context(
        settings=make_settings(**overrides),
        gateway=FakeGateway(account),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "aliases")),
        audit=AuditLog(str(tmp_path / "audit")),
    )


def full_ctx(tmp_path, account, **overrides) -> Context:
    return make_ctx(tmp_path, account, send_enabled=True,
                    ews_capability_tier="full", **overrides)


def call(ctx: Context, name: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return asyncio.run(dispatch(ctx, SPEC[name], dict(kwargs)))


def _rule_xml(rule_id: str, display_name: str, priority: int = 1,
             is_enabled: bool = True, actions_xml: str = "<StopProcessingRules/>") -> str:
    return f"""<Rule xmlns="{TNS}">
      <RuleId>{rule_id}</RuleId>
      <DisplayName>{display_name}</DisplayName>
      <Priority>{priority}</Priority>
      <IsEnabled>{"true" if is_enabled else "false"}</IsEnabled>
      <Conditions/>
      <Actions>{actions_xml}</Actions>
    </Rule>"""


class FakeGetInboxRules:
    elements: List[Any] = []

    def __init__(self, account):
        self.account = account

    def call(self):
        return list(FakeGetInboxRules.elements)


class FakeUpdateInboxRules:
    calls: List[Any] = []

    def __init__(self, account):
        self.account = account

    def call(self, operation):
        FakeUpdateInboxRules.calls.append(operation)
        return True


@pytest.fixture(autouse=True)
def _patch_ews_services(monkeypatch):
    FakeGetInboxRules.elements = []
    FakeUpdateInboxRules.calls = []
    monkeypatch.setattr(rules, "GetInboxRules", FakeGetInboxRules)
    monkeypatch.setattr(rules, "UpdateInboxRules", FakeUpdateInboxRules)
    yield


# --- list_rules ----------------------------------------------------------------


def test_list_rules_aliases_rule_id_and_move_to_folder(tmp_path):
    ctx = make_ctx(tmp_path, make_account(), ews_capability_tier="read")
    FakeGetInboxRules.elements = [etree.fromstring(_rule_xml(
        "RAW-RULE-1", "Archive newsletters",
        actions_xml='<MoveToFolder><FolderId Id="RAW-FOLDER-9"/></MoveToFolder>',
    ).encode())]

    res = call(ctx, "list_rules", {})

    assert res["ok"] is True and res["count"] == 1
    row = res["items"][0]
    assert row["display_name"] == "Archive newsletters"
    assert ctx.aliaser.resolve(row["id"]) == "RAW-RULE-1"
    assert ctx.aliaser.resolve(row["actions"]["move_to_folder"]) == "RAW-FOLDER-9"


def test_list_rules_flags_unsupported_fields(tmp_path):
    ctx = make_ctx(tmp_path, make_account(), ews_capability_tier="read")
    FakeGetInboxRules.elements = [etree.fromstring(_rule_xml(
        "RAW-RULE-2", "Complex", actions_xml="<AssignCategories/>").encode())]

    res = call(ctx, "list_rules", {})

    assert res["items"][0]["unsupported_fields"] == ["AssignCategories"]


# --- create_rule -----------------------------------------------------------------


def test_create_rule_requires_at_least_one_action(tmp_path):
    ctx = full_ctx(tmp_path, make_account())
    res = call(ctx, "create_rule", {"display_name": "x"})
    assert res["ok"] is False
    assert res["error"]["code"] == "validation"
    assert FakeUpdateInboxRules.calls == []


def test_create_rule_move_only_no_confirm_needed_at_draft_tier(tmp_path):
    account = make_account()
    ctx = make_ctx(tmp_path, account, ews_capability_tier="draft", send_enabled=False)
    ctx.gateway.folders["f:inbox"] = SimpleNamespace(id="RAW-FOLDER-1", changekey=None)
    FakeGetInboxRules.elements = [etree.fromstring(_rule_xml(
        "RAW-NEW-1", "Move stuff", priority=5,
        actions_xml='<MoveToFolder><FolderId Id="RAW-FOLDER-1"/></MoveToFolder>',
    ).encode())]

    res = call(ctx, "create_rule", {
        "display_name": "Move stuff", "priority": 5, "move_to_folder": "f:inbox",
    })

    assert res["ok"] is True
    assert "requires_confirmation" not in res
    assert res["display_name"] == "Move stuff"
    assert ctx.aliaser.resolve(res["id"]) == "RAW-NEW-1"
    op = FakeUpdateInboxRules.calls[0]
    assert op.tag == f"{{{TNS}}}CreateRuleOperation"


def test_create_rule_forward_to_blocked_by_kill_switch_at_phase_two(tmp_path):
    ctx = make_ctx(tmp_path, make_account(), ews_capability_tier="full", send_enabled=False)
    kwargs = {"display_name": "Forward", "forward_to": ["out@ext.com"]}

    p1 = call(ctx, "create_rule", kwargs)
    assert p1["requires_confirmation"] is True  # preview still issued (matches create_event)

    p2 = call(ctx, "create_rule", {**kwargs, "confirm_token": p1["confirm_token"]})
    assert p2["ok"] is False
    assert p2["error"]["code"] == "kill_switch"
    assert FakeUpdateInboxRules.calls == []


def test_create_rule_forward_to_two_phase_confirm_then_succeeds(tmp_path):
    ctx = full_ctx(tmp_path, make_account())
    kwargs = {"display_name": "Forward mine", "forward_to": ["out@ext.com"]}

    p1 = call(ctx, "create_rule", kwargs)
    assert p1["requires_confirmation"] is True
    assert FakeUpdateInboxRules.calls == []

    FakeGetInboxRules.elements = [etree.fromstring(_rule_xml(
        "RAW-NEW-2", "Forward mine",
        actions_xml=('<ForwardToRecipients><Address><EmailAddress>out@ext.com'
                     '</EmailAddress></Address></ForwardToRecipients>'),
    ).encode())]
    p2 = call(ctx, "create_rule", {**kwargs, "confirm_token": p1["confirm_token"]})

    assert p2["ok"] is True
    assert len(FakeUpdateInboxRules.calls) == 1


def test_create_rule_forward_to_respects_recipient_denylist(tmp_path):
    ctx = full_ctx(tmp_path, make_account(), ews_recipient_denylist="*@blocked.com")
    res = call(ctx, "create_rule", {"display_name": "x", "forward_to": ["leak@blocked.com"]})
    assert res["ok"] is False
    assert res["error"]["code"] == "recipient_blocked"
    assert FakeUpdateInboxRules.calls == []


def test_create_rule_owa_style_recipient_contains(tmp_path):
    """End-to-end regression for the real "BMW OUT" rule shape: contains
    a word in the recipient address -> move to folder, stop processing."""
    account = make_account()
    ctx = make_ctx(tmp_path, account, ews_capability_tier="draft", send_enabled=False)
    ctx.gateway.folders["BMW"] = SimpleNamespace(id="RAW-BMW-FOLDER", changekey=None)
    FakeGetInboxRules.elements = [etree.fromstring(_rule_xml(
        "RAW-BMW-11", "BMW OUT", priority=11,
        actions_xml='<MoveToFolder><FolderId Id="RAW-BMW-FOLDER"/></MoveToFolder><StopProcessingRules/>',
    ).encode())]

    res = call(ctx, "create_rule", {
        "display_name": "BMW OUT", "priority": 11,
        "recipient_contains": ["bmw."], "move_to_folder": "BMW", "stop_processing": True,
    })

    assert res["ok"] is True
    assert "unsupported_fields" not in res
    op = FakeUpdateInboxRules.calls[0]
    rule_elem = op[0]
    cond = rule_elem.find(f"{{{TNS}}}Conditions/{{{TNS}}}ContainsRecipientStrings/{{{TNS}}}String")
    assert cond.text == "bmw."


def test_create_rule_date_grammar_resolved_to_iso(tmp_path):
    ctx = full_ctx(tmp_path, make_account())
    FakeGetInboxRules.elements = [etree.fromstring(_rule_xml("RAW-D1", "Recent").encode())]

    res = call(ctx, "create_rule", {
        "display_name": "Recent", "received_after": "today", "mark_as_read": True,
    })

    assert res["ok"] is True
    op = FakeUpdateInboxRules.calls[0]
    start = op[0].find(f"{{{TNS}}}Conditions/{{{TNS}}}WithinDateRange/{{{TNS}}}StartDateTime")
    assert start is not None and "T00:00:00" in start.text


def test_create_rule_boolean_and_enum_conditions_build_correctly(tmp_path):
    ctx = full_ctx(tmp_path, make_account())
    FakeGetInboxRules.elements = [etree.fromstring(_rule_xml("RAW-D2", "Flagged").encode())]

    res = call(ctx, "create_rule", {
        "display_name": "Flagged", "sent_to_me": True,
        "sensitivity": "confidential", "flagged_for_action": "FollowUp",
        "mark_as_read": True,
    })

    assert res["ok"] is True
    conditions = FakeUpdateInboxRules.calls[0][0].find(f"{{{TNS}}}Conditions")
    assert conditions.find(f"{{{TNS}}}SentToMe") is not None
    assert conditions.findtext(f"{{{TNS}}}Sensitivity") == "Confidential"
    assert conditions.findtext(f"{{{TNS}}}FlaggedForAction") == "FollowUp"


# --- update_rule -----------------------------------------------------------------


def test_update_rule_resolves_rule_id_alias_to_raw_id(tmp_path):
    ctx = full_ctx(tmp_path, make_account())
    alias = ctx.aliaser.alias_for("RAW-EXISTING", "r")

    res = call(ctx, "update_rule", {
        "rule_id": alias, "display_name": "Renamed", "stop_processing": True,
    })

    assert res["ok"] is True
    op = FakeUpdateInboxRules.calls[0]
    assert op.tag == f"{{{TNS}}}SetRuleOperation"
    assert op[0].find(f"{{{TNS}}}RuleId").text == "RAW-EXISTING"


def test_update_rule_requires_at_least_one_action(tmp_path):
    ctx = full_ctx(tmp_path, make_account())
    alias = ctx.aliaser.alias_for("RAW-X", "r")
    res = call(ctx, "update_rule", {"rule_id": alias, "display_name": "y"})
    assert res["ok"] is False
    assert res["error"]["code"] == "validation"


# --- delete_rule -----------------------------------------------------------------


def test_delete_rule_two_phase_confirm_then_sends_delete_operation(tmp_path):
    ctx = full_ctx(tmp_path, make_account())
    alias = ctx.aliaser.alias_for("RAW-DEL", "r")

    p1 = call(ctx, "delete_rule", {"rule_id": alias})
    assert p1["requires_confirmation"] is True
    assert FakeUpdateInboxRules.calls == []

    p2 = call(ctx, "delete_rule", {"rule_id": alias, "confirm_token": p1["confirm_token"]})
    assert p2 == {"ok": True, "deleted": True}
    op = FakeUpdateInboxRules.calls[0]
    assert op.tag == f"{{{TNS}}}DeleteRuleOperation"
    assert op.find(f"{{{TNS}}}RuleId").text == "RAW-DEL"
