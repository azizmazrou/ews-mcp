"""Tests for the agent-secretary feature stack.

Covers:
* MemoryStore (key validation, isolation, size caps, TTL, atomic consume)
* Repositories (CommitmentRepo, ApprovalRepo, RuleRepo, VoiceRepo, OOFPolicyRepo)
* Rule matching (fnmatch, subject/body, categories, importance)
* Redaction helpers in the new logging middleware are covered in
  tests/test_redaction.py (separate file) if present; here we focus on
  the agent stack.
"""

from __future__ import annotations

import os
import pathlib
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, ".")

from src.exceptions import ToolExecutionError, ValidationError
from src.memory import (
    ApprovalRepo,
    Commitment,
    CommitmentRepo,
    MemoryStore,
    OOFPolicy,
    OOFPolicyRepo,
    Rule,
    RuleRepo,
    VoiceProfile,
    VoiceRepo,
    new_id,
)


# --- MemoryStore ----------------------------------------------------------


def test_memory_roundtrip(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    record = store.set("test", "k1", {"a": 1})
    assert record.key == "k1"
    assert record.value == {"a": 1}

    fetched = store.get("test", "k1")
    assert fetched is not None
    assert fetched.value == {"a": 1}

    assert store.delete("test", "k1") is True
    assert store.get("test", "k1") is None


def test_memory_isolation_between_mailboxes(tmp_path):
    alice = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    bob = MemoryStore.for_mailbox("bob@corp.com", base_dir=tmp_path)
    alice.set("notes", "topic", "alice data")
    assert bob.get("notes", "topic") is None


def test_memory_key_validation(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    for bad in ("../evil", "", "a" * 200, "has space", "a/b"):
        with pytest.raises(ValidationError):
            store.set("test", bad, "x")


def test_memory_namespace_validation(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    for bad in ("../test", "has space"):
        with pytest.raises(ValidationError):
            store.set(bad, "k", "x")


def test_memory_value_size_cap(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    with pytest.raises(ValidationError):
        store.set("test", "big", "x" * (2 * 1024 * 1024))


def test_memory_ttl_expires(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    store.set("test", "short", "value", ttl_seconds=1)
    assert store.get("test", "short") is not None
    time.sleep(1.1)
    assert store.get("test", "short") is None


def test_memory_list_prefix_filter(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    store.set("test", "user.alice", 1)
    store.set("test", "user.bob", 2)
    store.set("test", "other", 3)

    users = store.list("test", prefix="user.")
    assert {r.key for r in users} == {"user.alice", "user.bob"}


def test_memory_atomic_consume(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    store.set("test", "token", {"status": "pending", "data": "x"})
    consumed = store.consume(
        "test", "token", expect_value_key="status", expect_value_equal="pending"
    )
    assert consumed is not None and consumed.value["data"] == "x"

    # Second consume must return None (token already redeemed).
    double = store.consume(
        "test", "token", expect_value_key="status", expect_value_equal="pending"
    )
    assert double is None


def test_memory_file_lives_inside_jail(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    assert pathlib.Path(store.db_path).resolve().is_relative_to(tmp_path.resolve())


# --- Commitments ----------------------------------------------------------


def test_commitment_lifecycle(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    repo = CommitmentRepo(store)
    commitment = CommitmentRepo.new(
        "Send quarterly numbers", owner="me", due_at=time.time() + 3600
    )
    saved = repo.save(commitment)
    assert saved.status == "open"

    resolved = repo.resolve(saved.commitment_id, outcome="done", note="sent Tuesday")
    assert resolved.status == "done"
    assert resolved.resolution_note == "sent Tuesday"


def test_commitment_overdue_filter(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    repo = CommitmentRepo(store)
    repo.save(CommitmentRepo.new("past due", owner="me", due_at=time.time() - 3600))
    repo.save(CommitmentRepo.new("future", owner="me", due_at=time.time() + 3600))
    repo.save(CommitmentRepo.new("no due", owner="me"))
    overdue = repo.list(status="open", overdue=True)
    assert [c.description for c in overdue] == ["past due"]


def test_commitment_validation():
    with pytest.raises(ValidationError):
        CommitmentRepo.new("", owner="me")
    with pytest.raises(ValidationError):
        CommitmentRepo.new("x" * 3000, owner="me")
    with pytest.raises(ValidationError):
        CommitmentRepo.new("valid", owner="not-an-email-or-me")


# --- Approvals ------------------------------------------------------------


def test_approval_submit_and_decide(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    repo = ApprovalRepo(store)
    approval = repo.submit(
        "send_email",
        {"to": ["x@y"], "subject": "hi", "body": "hi"},
        ttl_seconds=600,
    )
    assert approval.status == "pending"
    decided = repo.decide(approval.approval_id, approve=True, reason="ok")
    assert decided and decided.status == "approved"


def test_approval_rejects_unknown_action(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    repo = ApprovalRepo(store)
    with pytest.raises(ValidationError):
        repo.submit("eval_arbitrary_python", {})


def test_approval_double_consume_refused(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    repo = ApprovalRepo(store)
    approval = repo.submit("send_email", {"to": ["x@y"], "subject": "s", "body": "b"})
    first = repo.decide(approval.approval_id, approve=True)
    second = repo.decide(approval.approval_id, approve=False)
    assert first is not None
    assert second is None


def test_approval_rejects_bad_ttl(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    repo = ApprovalRepo(store)
    with pytest.raises(ValidationError):
        repo.submit("send_email", {"to": ["x"], "subject": "s", "body": "b"}, ttl_seconds=1)


# --- Rules ----------------------------------------------------------------


def test_rule_action_allow_list():
    with pytest.raises(ValidationError):
        RuleRepo.validate_actions([{"type": "run_shell", "cmd": "rm -rf /"}])
    ok = RuleRepo.validate_actions([{"type": "flag_importance", "importance": "High"}])
    assert ok == [{"type": "flag_importance", "importance": "High"}]


def test_rule_match_key_allow_list():
    with pytest.raises(ValidationError):
        RuleRepo.validate_match({"sql": "DROP TABLE"})
    cleaned = RuleRepo.validate_match({"from": "ceo@corp"})
    assert cleaned == {"from": "ceo@corp"}


def test_rule_matching_predicate():
    from src.tools.rule_tools import _match_one

    msg = MagicMock()
    msg.sender = MagicMock(email_address="ceo@corp.com")
    msg.to_recipients = [MagicMock(email_address="alice@corp.com")]
    msg.subject = "URGENT: budget review"
    msg.text_body = "Please approve"
    msg.body = None
    msg.has_attachments = True
    msg.is_read = False
    msg.importance = "High"
    msg.categories = ["Work"]

    assert _match_one({"from": "ceo@*.com"}, msg) is True
    assert _match_one({"from": "intern@*.com"}, msg) is False
    assert _match_one({"subject_contains": "budget"}, msg) is True
    assert _match_one({"body_contains": "approve"}, msg) is True
    assert _match_one({"has_attachment": True}, msg) is True
    assert _match_one({"is_unread": True}, msg) is True
    assert _match_one({"importance": "High"}, msg) is True
    assert _match_one({"categories_any": ["Work"]}, msg) is True
    assert _match_one({"categories_all": ["Work", "VIP"]}, msg) is False
    # All conditions must match (AND semantics):
    assert _match_one({"from": "ceo@*.com", "subject_contains": "budget"}, msg) is True
    assert _match_one({"from": "ceo@*.com", "subject_contains": "holiday"}, msg) is False


# --- Voice profile & OOF policy ------------------------------------------


def test_voice_repo_roundtrip(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    repo = VoiceRepo(store)
    profile = VoiceProfile(
        sampled_at=time.time(),
        sample_count=10,
        formality="professional",
        avg_length_words=80,
        common_greetings=["Hi team"],
        common_signoffs=["Thanks,"],
        typical_structure="One-paragraph, bullet points for options.",
        examples=["Thanks, Alice"],
    )
    repo.save(profile)
    fetched = repo.get()
    assert fetched is not None
    assert fetched.formality == "professional"


def test_oof_policy_roundtrip(tmp_path):
    store = MemoryStore.for_mailbox("alice@corp.com", base_dir=tmp_path)
    repo = OOFPolicyRepo(store)
    policy = OOFPolicy(
        internal_template="I'm out of office",
        external_template=None,
        vip_passthrough=True,
        forward_rules=[{"match": {"from": "boss@*"}, "to": "delegate@corp.com"}],
    )
    repo.save(policy)
    fetched = repo.get()
    assert fetched is not None
    assert len(fetched.forward_rules) == 1


# --- Reserved namespaces refused from generic memory tools ----------------


@pytest.mark.asyncio
async def test_memory_tools_refuse_reserved(tmp_path, monkeypatch):
    monkeypatch.setenv("EWS_MEMORY_DIR", str(tmp_path))
    # Reload the store module so EWS_MEMORY_DIR takes effect.
    import importlib
    from src.memory import store as store_module
    importlib.reload(store_module)

    # Build a minimal fake EWSClient + BaseTool-compatible instance.
    from src.tools.memory_tools import MemorySetTool

    fake_client = MagicMock()
    fake_client.config.ews_email = "alice@corp.com"
    tool = MemorySetTool(fake_client)

    with pytest.raises(ValidationError):
        await tool.execute(namespace="approval", key="k", value="v")
    with pytest.raises(ValidationError):
        await tool.execute(namespace="rule", key="k", value="v")


# --- Tool registration sanity --------------------------------------------


def test_agent_tools_exported():
    from src.tools import (
        MemorySetTool,
        TrackCommitmentTool,
        SubmitForApprovalTool,
        BuildVoiceProfileTool,
        RuleCreateTool,
        ConfigureOOFPolicyTool,
        GenerateBriefingTool,
        PrepareMeetingTool,
    )
    # Nothing to assert; the import itself is the test.
    assert all([
        MemorySetTool, TrackCommitmentTool, SubmitForApprovalTool,
        BuildVoiceProfileTool, RuleCreateTool, ConfigureOOFPolicyTool,
        GenerateBriefingTool, PrepareMeetingTool,
    ])
