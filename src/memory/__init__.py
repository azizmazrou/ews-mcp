"""Per-mailbox persistent memory + typed repositories for agentic features.

Public API
----------
::

    from src.memory import MemoryStore, NS
    from src.memory.models import Commitment, CommitmentRepo, Approval, ApprovalRepo

    store = MemoryStore.for_mailbox(ews_client.account.primary_smtp_address)
    commitments = CommitmentRepo(store)
    commitments.save(CommitmentRepo.new("Send Alice Q1 numbers", owner="me"))
"""

from .store import MemoryRecord, MemoryStore, new_id
from .models import (
    NS,
    Commitment,
    CommitmentRepo,
    Approval,
    ApprovalRepo,
    Rule,
    RuleRepo,
    VoiceProfile,
    VoiceRepo,
    ForwardRule,
    OOFPolicy,
    OOFPolicyRepo,
)

__all__ = [
    "MemoryStore",
    "MemoryRecord",
    "NS",
    "new_id",
    "Commitment",
    "CommitmentRepo",
    "Approval",
    "ApprovalRepo",
    "Rule",
    "RuleRepo",
    "VoiceProfile",
    "VoiceRepo",
    "ForwardRule",
    "OOFPolicy",
    "OOFPolicyRepo",
]
