"""Outlook follow-up flag completion state (PidTagFlagStatus, 0x1090) on Message.

MAPI PidTagFlagStatus values: ``None`` = unflagged, ``1`` = marked Complete,
``2`` = flagged and still open. Only completion (``1`` vs not ``1``) is exposed.
"""

from exchangelib import Message
from exchangelib.extended_properties import ExtendedProperty

FIELD_NAME = "flag_status"


class FlagStatus(ExtendedProperty):
    property_tag = 0x1090
    property_type = "Integer"


def ensure_registered() -> None:
    """Register ``flag_status`` on ``Message`` once; safe to call repeatedly."""
    if any(f.name == FIELD_NAME for f in Message.FIELDS):
        return
    Message.register(FIELD_NAME, FlagStatus)
