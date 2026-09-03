"""Registers the Outlook follow-up flag's completion state (PidTagFlagStatus,
MAPI property tag 0x1090) as a queryable/writable field on Message.

writes.py previously concluded flag completion was simply unavailable
("exchangelib 5.0.3 exposes no first-class follow-up flag field, and
pretending would be mock-drift bait") and used categories as a workaround
marker instead. Live-tested against a real mailbox: it IS available via
exchangelib's ExtendedProperty registration mechanism - reading, writing
(item.save()), and filtering (folder.filter(flag_status=1)) all verified
end to end (baseline count 0 -> set flag_status=1 on one message -> count
1, correct match -> revert to None -> count 0 again).

Value meanings (real MAPI semantics, PidTagFlagStatus): None = not
flagged, 1 = flagged and marked Complete, 2 = flagged and still open
(Outlook's plain "Flagged" state). This module only cares about the
completion state (1 vs not-1), not the full flag lifecycle.
"""
from exchangelib import Message
from exchangelib.extended_properties import ExtendedProperty


class FlagStatus(ExtendedProperty):
    property_tag = 0x1090
    property_type = "Integer"


_registered = False


def ensure_registered() -> None:
    """Idempotent - safe to call from every module that needs the field,
    regardless of import order."""
    global _registered
    if _registered:
        return
    Message.register("flag_status", FlagStatus)
    _registered = True
