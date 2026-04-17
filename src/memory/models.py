"""Typed accessors on top of the MemoryStore KV primitive.

Every feature module (commitments, approvals, rules, voice, OOF policy)
defines a Pydantic-ish dataclass here and a small repository that wraps
``MemoryStore`` operations in typed methods. This keeps the namespace
layout in one place and prevents ad-hoc keys scattered across tool files.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal, Optional

from .store import MemoryStore, new_id
from ..exceptions import ValidationError


# --- Namespaces (documented central list) ---------------------------------


class NS:
    COMMITMENT = "commitment"      # key = commitment_id
    APPROVAL = "approval"          # key = approval_id
    RULE = "rule"                  # key = rule_id
    OOF_POLICY = "oof.policy"      # key = "current"
    VOICE = "voice.profile"        # key = "current"
    PERSON_NOTE = "person.note"    # key = normalised email
    THREAD_SNOOZE = "thread.snooze"  # key = thread/message id
    PREFS = "prefs"                # key = pref name


# --- Commitment -----------------------------------------------------------


@dataclass
class Commitment:
    """A promise: I owe X to Y by Z (or Y owes me)."""

    commitment_id: str
    description: str
    owner: str  # "me" or an email address
    counterparty: Optional[str] = None  # email address (opposite side)
    thread_id: Optional[str] = None
    message_id: Optional[str] = None
    due_at: Optional[float] = None  # epoch seconds
    status: Literal["open", "done", "cancelled"] = "open"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    resolution_note: Optional[str] = None
    source: Literal["manual", "extracted"] = "manual"
    # Optional excerpt (kept short; redact before audit logging).
    excerpt: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class CommitmentRepo:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def save(self, c: Commitment) -> Commitment:
        c.updated_at = time.time()
        self.store.set(NS.COMMITMENT, c.commitment_id, c.to_dict())
        return c

    def get(self, commitment_id: str) -> Optional[Commitment]:
        rec = self.store.get(NS.COMMITMENT, commitment_id)
        if not rec:
            return None
        return Commitment(**rec.value)

    def list(
        self,
        *,
        status: Optional[Literal["open", "done", "cancelled"]] = "open",
        owner: Optional[str] = None,
        overdue: bool = False,
        limit: int = 100,
    ) -> list[Commitment]:
        records = self.store.list(NS.COMMITMENT, limit=limit)
        out: list[Commitment] = []
        now = time.time()
        for rec in records:
            c = Commitment(**rec.value)
            if status is not None and c.status != status:
                continue
            if owner is not None and c.owner != owner:
                continue
            if overdue and (c.due_at is None or c.due_at >= now or c.status != "open"):
                continue
            out.append(c)
        # Sort: overdue first (lowest due_at), then other open, then rest.
        out.sort(key=lambda c: (c.due_at is None, c.due_at or 0))
        return out

    def resolve(
        self,
        commitment_id: str,
        outcome: Literal["done", "cancelled"],
        note: Optional[str] = None,
    ) -> Optional[Commitment]:
        c = self.get(commitment_id)
        if not c:
            return None
        c.status = outcome
        c.resolved_at = time.time()
        c.resolution_note = note
        return self.save(c)

    @staticmethod
    def new(
        description: str,
        owner: str,
        *,
        counterparty: Optional[str] = None,
        thread_id: Optional[str] = None,
        message_id: Optional[str] = None,
        due_at: Optional[float] = None,
        source: Literal["manual", "extracted"] = "manual",
        excerpt: Optional[str] = None,
    ) -> Commitment:
        if not description or not isinstance(description, str):
            raise ValidationError("description must be a non-empty string")
        if len(description) > 2000:
            raise ValidationError("description too long (max 2000 chars)")
        if excerpt and len(excerpt) > 2000:
            raise ValidationError("excerpt too long (max 2000 chars)")
        if owner not in ("me",) and "@" not in owner:
            raise ValidationError("owner must be 'me' or an email address")
        return Commitment(
            commitment_id=new_id(),
            description=description,
            owner=owner,
            counterparty=counterparty,
            thread_id=thread_id,
            message_id=message_id,
            due_at=due_at,
            source=source,
            excerpt=excerpt,
        )


# --- Approval -------------------------------------------------------------


@dataclass
class Approval:
    """A side-effectful tool invocation waiting on a human OK."""

    approval_id: str
    action: str            # tool name, e.g. "send_email"
    arguments: dict        # safe (no raw token fields) tool arguments
    requested_at: float
    expires_at: float
    status: Literal["pending", "approved", "rejected", "expired", "executed"] = "pending"
    reason: Optional[str] = None
    requested_by: Optional[str] = None
    target_mailbox: Optional[str] = None
    # Filled when the approval fires:
    decided_at: Optional[float] = None
    result_summary: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class ApprovalRepo:
    _ALLOWED_ACTIONS: set[str] = {
        "send_email", "reply_email", "forward_email",
        "delete_email", "move_email",
        "create_appointment", "update_appointment", "delete_appointment",
        "create_contact", "update_contact", "delete_contact",
        "create_task", "update_task", "delete_task", "complete_task",
        "manage_folder",
        "add_attachment", "delete_attachment",
        "oof_settings",
        "configure_oof_policy",
    }

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    @classmethod
    def allowed(cls, action: str) -> bool:
        return action in cls._ALLOWED_ACTIONS

    def submit(
        self,
        action: str,
        arguments: dict,
        *,
        ttl_seconds: int = 30 * 60,
        requested_by: Optional[str] = None,
        target_mailbox: Optional[str] = None,
    ) -> Approval:
        if not self.allowed(action):
            raise ValidationError(f"action not allowed in approval queue: {action!r}")
        if not isinstance(arguments, dict):
            raise ValidationError("arguments must be a dict")
        if ttl_seconds < 60 or ttl_seconds > 7 * 24 * 3600:
            raise ValidationError("ttl_seconds must be in [60, 604800]")
        now = time.time()
        approval = Approval(
            approval_id=new_id(),
            action=action,
            arguments=arguments,
            requested_at=now,
            expires_at=now + ttl_seconds,
            requested_by=requested_by,
            target_mailbox=target_mailbox,
        )
        self.store.set(NS.APPROVAL, approval.approval_id, approval.to_dict(), ttl_seconds=ttl_seconds)
        return approval

    def get(self, approval_id: str) -> Optional[Approval]:
        rec = self.store.get(NS.APPROVAL, approval_id)
        if not rec:
            return None
        return Approval(**rec.value)

    def list_pending(self, limit: int = 100) -> list[Approval]:
        records = self.store.list(NS.APPROVAL, limit=limit)
        now = time.time()
        out: list[Approval] = []
        for r in records:
            a = Approval(**r.value)
            if a.status != "pending":
                continue
            if a.expires_at < now:
                continue
            out.append(a)
        out.sort(key=lambda a: a.requested_at)
        return out

    def decide(
        self,
        approval_id: str,
        *,
        approve: bool,
        reason: Optional[str] = None,
        result_summary: Optional[str] = None,
    ) -> Optional[Approval]:
        """Atomically consume the pending approval.

        Returns the final Approval record or None if the id isn't pending.
        """
        # consume() is atomic — prevents double-redemption.
        rec = self.store.consume(
            NS.APPROVAL,
            approval_id,
            expect_value_key="status",
            expect_value_equal="pending",
        )
        if not rec:
            return None
        approval = Approval(**rec.value)
        approval.status = "approved" if approve else "rejected"
        approval.decided_at = time.time()
        approval.reason = reason
        approval.result_summary = result_summary
        return approval


# --- Rule engine ----------------------------------------------------------


_ALLOWED_ACTION_TYPES: set[str] = {
    "flag_importance",   # {"importance": "High"}
    "categorize",        # {"categories": ["Work", "VIP"]}
    "move_to_folder",    # {"destination": "Archive"}
    "mark_read",
    "track_commitment",  # {"description": "...", "owner": "me", "due_at": <epoch>}
    "notify_agent",      # {"note": "free-text for the LLM"}
}


@dataclass
class Rule:
    rule_id: str
    name: str
    match: dict   # {"from": "ceo@*", "subject_contains": "urgent", ...}
    actions: list[dict]
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class RuleRepo:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def save(self, rule: Rule) -> Rule:
        rule.updated_at = time.time()
        self.store.set(NS.RULE, rule.rule_id, rule.to_dict())
        return rule

    def get(self, rule_id: str) -> Optional[Rule]:
        rec = self.store.get(NS.RULE, rule_id)
        if not rec:
            return None
        return Rule(**rec.value)

    def list(self, *, enabled_only: bool = False) -> list[Rule]:
        records = self.store.list(NS.RULE, limit=200)
        rules = [Rule(**r.value) for r in records]
        if enabled_only:
            rules = [r for r in rules if r.enabled]
        rules.sort(key=lambda r: r.created_at)
        return rules

    def delete(self, rule_id: str) -> bool:
        return self.store.delete(NS.RULE, rule_id)

    @staticmethod
    def validate_actions(actions: Iterable[dict]) -> list[dict]:
        out: list[dict] = []
        for idx, a in enumerate(actions):
            if not isinstance(a, dict) or "type" not in a:
                raise ValidationError(f"action[{idx}] must be a dict with 'type'")
            if a["type"] not in _ALLOWED_ACTION_TYPES:
                raise ValidationError(
                    f"action[{idx}] type {a['type']!r} not in allow-list "
                    f"{sorted(_ALLOWED_ACTION_TYPES)}"
                )
            out.append(a)
        if not out:
            raise ValidationError("at least one action is required")
        if len(out) > 10:
            raise ValidationError("at most 10 actions per rule")
        return out

    @staticmethod
    def validate_match(match: dict) -> dict:
        if not isinstance(match, dict):
            raise ValidationError("match must be a dict")
        allowed_keys = {
            "from", "to", "subject_contains", "body_contains",
            "has_attachment", "is_unread", "categories_any", "categories_all",
            "importance",
        }
        for k in match:
            if k not in allowed_keys:
                raise ValidationError(
                    f"match key {k!r} not allowed; expected one of {sorted(allowed_keys)}"
                )
        return match


# --- Voice profile --------------------------------------------------------


@dataclass
class VoiceProfile:
    sampled_at: float
    sample_count: int
    formality: str                 # "casual" | "professional" | "formal"
    avg_length_words: int
    common_greetings: list[str]    # e.g. ["Hi", "Hey team"]
    common_signoffs: list[str]     # e.g. ["Thanks,", "Best,"]
    typical_structure: str         # free-form paragraph description
    examples: list[str]            # 3-5 short snippets as few-shot material

    def to_dict(self) -> dict:
        return asdict(self)


class VoiceRepo:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def save(self, profile: VoiceProfile) -> VoiceProfile:
        self.store.set(NS.VOICE, "current", profile.to_dict())
        return profile

    def get(self) -> Optional[VoiceProfile]:
        rec = self.store.get(NS.VOICE, "current")
        if not rec:
            return None
        return VoiceProfile(**rec.value)

    def clear(self) -> bool:
        return self.store.delete(NS.VOICE, "current")


# --- OOF policy -----------------------------------------------------------


@dataclass
class ForwardRule:
    match: dict       # same shape as RuleRepo.validate_match
    to: str           # email address (validated by caller)
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OOFPolicy:
    internal_template: Optional[str]
    external_template: Optional[str]
    vip_passthrough: bool = True
    forward_rules: list[dict] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class OOFPolicyRepo:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def save(self, policy: OOFPolicy) -> OOFPolicy:
        policy.updated_at = time.time()
        self.store.set(NS.OOF_POLICY, "current", policy.to_dict())
        return policy

    def get(self) -> Optional[OOFPolicy]:
        rec = self.store.get(NS.OOF_POLICY, "current")
        if not rec:
            return None
        return OOFPolicy(**rec.value)

    def clear(self) -> bool:
        return self.store.delete(NS.OOF_POLICY, "current")
