"""Exchange Inbox rules (mail filters) — EWS's GetInboxRules/UpdateInboxRules
operations, which exchangelib 5.0.3 does not implement (verified: no
get_inbox_rules.py/update_inbox_rules.py under exchangelib/services/, and
no Rule type anywhere in the package). Built directly on exchangelib's
EWSAccountService/create_element/set_xml_value primitives, following
GetUserOofSettings/SetUserOofSettings (exchangelib/services/) as the
closest shipped precedent for an account-level get/set operation.

Schema verified against Microsoft's own EWS reference (GetInboxRules,
UpdateInboxRules, Conditions, Actions, FromAddresses, FlaggedForAction,
Sensitivity, WithinSizeRange, WithinDateRange pages). Conditions cover
every predicate Outlook/OWA's own rule editor exposes — everything under
its "Contains these words / Was sent or received / My name is / Is
flagged with / Is / Size / Received" menus — except the four that
aren't in that menu at all (``Categories``, ``ItemClasses``,
``MessageClassifications``, ``FromConnectedAccounts``: enterprise/
admin-only predicates with no OWA UI). ``unsupported_fields`` on a
parsed rule flags those four if an existing rule uses one, so it's never
silently misrepresented.

Element order within Conditions/Actions follows the EWS schema's own
sequence (verified against the MSDN Conditions/Actions pages) — EWS
validates element order, not just presence.
"""

from typing import Any, Callable, Dict, List, Optional, Tuple

from exchangelib.services.common import EWSAccountService
from exchangelib.util import MNS, TNS, add_xml_child, create_element

# --- low-level EWS services --------------------------------------------------


class GetInboxRules(EWSAccountService):
    """MSDN: getinboxrules-operation

    GetInboxRulesResponse carries ResponseClass directly on itself (no
    nested ResponseMessages/ResponseMessage wrapper — verified against the
    MSDN example), so the base EWSService machinery's "not delivered in a
    list" fallback already does the right thing; no _get_element_container
    override needed here (contrast GetUserOofSettings, whose response DOES
    nest a ResponseMessage and needs one).
    """

    SERVICE_NAME = "GetInboxRules"
    element_container_name = f"{{{MNS}}}InboxRules"

    def call(self) -> List[Any]:
        return list(self._elems_to_objs(self._get_elements(payload=self.get_payload())))

    def _elem_to_obj(self, elem):
        return elem  # raw <t:Rule> element — parsed by parse_rule_element()

    @classmethod
    def _get_elements_in_container(cls, container):
        return container.findall(f"{{{TNS}}}Rule")

    def get_payload(self):
        return create_element(f"m:{self.SERVICE_NAME}")


class UpdateInboxRules(EWSAccountService):
    """MSDN: updateinboxrules-operation

    One operation per call (create XOR set XOR delete) — this codebase
    never batches rule mutations, so partial-failure (``RuleOperationErrors``,
    only relevant when a single request carries multiple operations) is
    out of scope: a single operation's response is either whole-success or
    a raised EWS error (handled generically by the base class, same as
    every other service).
    """

    SERVICE_NAME = "UpdateInboxRules"
    returns_elements = False

    def call(self, operation: Any) -> bool:
        list(self._get_elements(payload=self.get_payload(operation)))
        return True

    def get_payload(self, operation: Any):
        payload = create_element(f"m:{self.SERVICE_NAME}")
        operations = create_element("m:Operations")
        operations.append(operation)
        payload.append(operations)
        return payload


# --- condition/action schema (key, XML tag, kind, enum-map), IN EWS ORDER ---
#
# kind: flag (presence marker) | strings (<String> list) | addresses
# (<Address><EmailAddress> list) | value (single text, mapped through the
# 4th tuple element) | raw_text (single text, used verbatim — EWS's own
# enum spelling, e.g. FlaggedForAction's "DoNotForward"/"ReplyToAll") |
# folder (actions only, resolved through the resolve_folder callback).
# WithinDateRange/WithinSizeRange combine two fields into one element and
# so aren't representable as a single (key, tag) row — see
# build_conditions_element/parse_conditions_element below.

_IMPORTANCE_TO_XML = {"low": "Low", "normal": "Normal", "high": "High"}
_SENSITIVITY_TO_XML = {"normal": "Normal", "personal": "Personal",
                       "private": "Private", "confidential": "Confidential"}

# The exact (case-sensitive) EWS enum spelling — exposed as-is in the tool
# schema rather than invented snake_case aliases (FYI/ReplyToAll/
# DoNotForward don't map cleanly to one convention).
FLAGGED_FOR_ACTION_VALUES = [
    "Any", "Call", "DoNotForward", "FollowUp", "FYI", "Forward",
    "NoResponseNecessary", "Read", "Reply", "ReplyToAll", "Review",
]

# Order matches the Conditions page's schema block exactly (minus
# Categories/ItemClasses/MessageClassifications/FromConnectedAccounts —
# not in OWA's UI; WithinDateRange/WithinSizeRange are last, handled
# outside this list).
_CONDITION_FIELDS: List[Tuple[str, str, str, Optional[Dict[str, str]]]] = [
    ("body_contains", "ContainsBodyStrings", "strings", None),
    ("header_contains", "ContainsHeaderStrings", "strings", None),
    ("recipient_contains", "ContainsRecipientStrings", "strings", None),
    ("sender_contains", "ContainsSenderStrings", "strings", None),
    ("subject_or_body_contains", "ContainsSubjectOrBodyStrings", "strings", None),
    ("subject_contains", "ContainsSubjectStrings", "strings", None),
    ("flagged_for_action", "FlaggedForAction", "raw_text", None),
    ("from_addresses", "FromAddresses", "addresses", None),
    ("has_attachments", "HasAttachments", "flag", None),
    ("importance", "Importance", "value", _IMPORTANCE_TO_XML),
    ("is_approval_request", "IsApprovalRequest", "flag", None),
    ("is_automatic_forward", "IsAutomaticForward", "flag", None),
    ("is_automatic_reply", "IsAutomaticReply", "flag", None),
    ("is_encrypted", "IsEncrypted", "flag", None),
    ("is_meeting_request", "IsMeetingRequest", "flag", None),
    ("is_meeting_response", "IsMeetingResponse", "flag", None),
    ("is_ndr", "IsNDR", "flag", None),
    ("is_permission_controlled", "IsPermissionControlled", "flag", None),
    ("is_read_receipt", "IsReadReceipt", "flag", None),
    ("is_signed", "IsSigned", "flag", None),
    ("is_voicemail", "IsVoicemail", "flag", None),
    ("not_sent_to_me", "NotSentToMe", "flag", None),
    ("sent_cc_me", "SentCcMe", "flag", None),
    ("sent_only_to_me", "SentOnlyToMe", "flag", None),
    ("sent_to_addresses", "SentToAddresses", "addresses", None),
    ("sent_to_me", "SentToMe", "flag", None),
    ("sent_to_or_cc_me", "SentToOrCcMe", "flag", None),
    ("sensitivity", "Sensitivity", "value", _SENSITIVITY_TO_XML),
]

# Order matches the Actions page's schema block exactly.
_ACTION_FIELDS: List[Tuple[str, str, str, Optional[Dict[str, str]]]] = [
    ("copy_to_folder", "CopyToFolder", "folder", None),
    ("delete", "Delete", "flag", None),
    ("forward_to", "ForwardToRecipients", "addresses", None),
    ("mark_importance", "MarkImportance", "value", _IMPORTANCE_TO_XML),
    ("mark_as_read", "MarkAsRead", "flag", None),
    ("move_to_folder", "MoveToFolder", "folder", None),
    ("permanent_delete", "PermanentDelete", "flag", None),
    ("redirect_to", "RedirectToRecipients", "addresses", None),
    ("stop_processing", "StopProcessingRules", "flag", None),
]

# WithinDateRange/WithinSizeRange are last in the Conditions schema order.
_KNOWN_CONDITION_TAGS = {tag for _, tag, _, _ in _CONDITION_FIELDS} | {
    "WithinDateRange", "WithinSizeRange",
}
_KNOWN_ACTION_TAGS = {tag for _, tag, _, _ in _ACTION_FIELDS}


# --- building (dict -> XML) --------------------------------------------------


def _string_list_element(tag: str, values: List[str]):
    elem = create_element(f"t:{tag}")
    for v in values:
        add_xml_child(elem, "t:String", v)
    return elem


def _address_list_element(tag: str, emails: List[str]):
    elem = create_element(f"t:{tag}")
    for email in emails:
        addr = create_element("t:Address")
        add_xml_child(addr, "t:EmailAddress", email)
        elem.append(addr)
    return elem


def _folder_id_element(folder: Any):
    attrs = {"Id": str(folder.id)}
    changekey = getattr(folder, "changekey", None)
    if changekey:
        attrs["ChangeKey"] = str(changekey)
    return create_element("t:FolderId", attrs=attrs)


def _build_size_range_element(min_bytes: Optional[int], max_bytes: Optional[int]):
    elem = create_element("t:WithinSizeRange")
    if min_bytes is not None:
        add_xml_child(elem, "t:MinimumSize", int(min_bytes))
    if max_bytes is not None:
        add_xml_child(elem, "t:MaximumSize", int(max_bytes))
    return elem


def _build_date_range_element(start_iso: Optional[str], end_iso: Optional[str]):
    elem = create_element("t:WithinDateRange")
    if start_iso:
        add_xml_child(elem, "t:StartDateTime", start_iso)
    if end_iso:
        add_xml_child(elem, "t:EndDateTime", end_iso)
    return elem


def _build_predicate_block(tag: str, fields: List[Tuple[str, str, str, Optional[Dict[str, str]]]],
                           values: Dict[str, Any],
                           resolve_folder: Optional[Callable[[str], Any]]):
    elem = create_element(f"t:{tag}")
    for key, xml_tag, kind, enum_map in fields:
        value = values.get(key)
        if not value:
            continue
        if kind == "flag":
            elem.append(create_element(f"t:{xml_tag}"))
        elif kind == "value":
            sub = create_element(f"t:{xml_tag}")
            sub.text = (enum_map or {}).get(value, str(value))
            elem.append(sub)
        elif kind == "raw_text":
            sub = create_element(f"t:{xml_tag}")
            sub.text = str(value)
            elem.append(sub)
        elif kind == "strings":
            elem.append(_string_list_element(xml_tag, value))
        elif kind == "addresses":
            elem.append(_address_list_element(xml_tag, value))
        elif kind == "folder":
            if resolve_folder is None:
                raise ValueError(f"{key} given but no folder resolver supplied")
            wrapper = create_element(f"t:{xml_tag}")
            wrapper.append(_folder_id_element(resolve_folder(value)))
            elem.append(wrapper)
    return elem


def build_conditions_element(conditions: Dict[str, Any]):
    conditions = conditions or {}
    elem = _build_predicate_block("Conditions", _CONDITION_FIELDS, conditions, None)
    start_iso, end_iso = conditions.get("received_after"), conditions.get("received_before")
    if start_iso or end_iso:
        elem.append(_build_date_range_element(start_iso, end_iso))
    min_b, max_b = conditions.get("min_size_bytes"), conditions.get("max_size_bytes")
    if min_b is not None or max_b is not None:
        elem.append(_build_size_range_element(min_b, max_b))
    return elem


def build_actions_element(actions: Dict[str, Any], resolve_folder: Callable[[str], Any]):
    return _build_predicate_block("Actions", _ACTION_FIELDS, actions or {}, resolve_folder)


def build_rule_element(*, rule_id: Optional[str], display_name: str, priority: int,
                       is_enabled: bool, conditions: Dict[str, Any], actions: Dict[str, Any],
                       resolve_folder: Callable[[str], Any]):
    elem = create_element("t:Rule")
    if rule_id:
        add_xml_child(elem, "t:RuleId", rule_id)
    add_xml_child(elem, "t:DisplayName", display_name)
    add_xml_child(elem, "t:Priority", int(priority))
    add_xml_child(elem, "t:IsEnabled", bool(is_enabled))
    elem.append(build_conditions_element(conditions))
    elem.append(build_actions_element(actions, resolve_folder))
    return elem


def build_create_operation(rule_elem: Any):
    op = create_element("t:CreateRuleOperation")
    op.append(rule_elem)
    return op


def build_set_operation(rule_elem: Any):
    op = create_element("t:SetRuleOperation")
    op.append(rule_elem)
    return op


def build_delete_operation(rule_id: str):
    op = create_element("t:DeleteRuleOperation")
    add_xml_child(op, "t:RuleId", rule_id)
    return op


# --- parsing (XML -> dict) --------------------------------------------------


def _parse_predicate_block(block_elem: Any, fields: List[Tuple[str, str, str, Optional[Dict[str, str]]]],
                           known_tags: set) -> Tuple[Dict[str, Any], List[str]]:
    result: Dict[str, Any] = {}
    unsupported: List[str] = []
    if block_elem is None:
        return result, unsupported
    for key, xml_tag, kind, enum_map in fields:
        child = block_elem.find(f"{{{TNS}}}{xml_tag}")
        if child is None:
            continue
        if kind == "flag":
            result[key] = True
        elif kind == "value":
            from_map = {v: k for k, v in (enum_map or {}).items()}
            result[key] = from_map.get(child.text, (child.text or "").lower())
        elif kind == "raw_text":
            result[key] = child.text
        elif kind == "strings":
            result[key] = [s.text for s in child.findall(f"{{{TNS}}}String") if s.text]
        elif kind == "addresses":
            emails = [a.findtext(f"{{{TNS}}}EmailAddress") for a in child.findall(f"{{{TNS}}}Address")]
            result[key] = [e for e in emails if e]
        elif kind == "folder":
            fid = child.find(f"{{{TNS}}}FolderId")
            result[key] = fid.get("Id") if fid is not None else None
    for child in block_elem:
        local = child.tag.split("}", 1)[-1]
        if local not in known_tags:
            unsupported.append(local)
    return result, unsupported


def _parse_size_range(elem: Optional[Any]) -> Dict[str, Any]:
    if elem is None:
        return {}
    result: Dict[str, Any] = {}
    min_el = elem.find(f"{{{TNS}}}MinimumSize")
    max_el = elem.find(f"{{{TNS}}}MaximumSize")
    if min_el is not None and min_el.text:
        result["min_size_bytes"] = int(min_el.text)
    if max_el is not None and max_el.text:
        result["max_size_bytes"] = int(max_el.text)
    return result


def _parse_date_range(elem: Optional[Any]) -> Dict[str, Any]:
    if elem is None:
        return {}
    result: Dict[str, Any] = {}
    start_el = elem.find(f"{{{TNS}}}StartDateTime")
    end_el = elem.find(f"{{{TNS}}}EndDateTime")
    if start_el is not None and start_el.text:
        result["received_after"] = start_el.text
    if end_el is not None and end_el.text:
        result["received_before"] = end_el.text
    return result


def parse_conditions_element(elem: Optional[Any]) -> Tuple[Dict[str, Any], List[str]]:
    result, unsupported = _parse_predicate_block(elem, _CONDITION_FIELDS, _KNOWN_CONDITION_TAGS)
    if elem is not None:
        result.update(_parse_date_range(elem.find(f"{{{TNS}}}WithinDateRange")))
        result.update(_parse_size_range(elem.find(f"{{{TNS}}}WithinSizeRange")))
    return result, unsupported


def parse_rule_element(elem: Any) -> Dict[str, Any]:
    """``elem`` is a raw ``<t:Rule>`` element from GetInboxRules. Folder ids
    in the result (``move_to_folder``/``copy_to_folder``) are RAW EWS ids —
    the caller (tools/rules.py) aliases them, matching every other DTO in
    this codebase (DESIGN.md §Ids)."""

    def text(tag: str) -> Optional[str]:
        child = elem.find(f"{{{TNS}}}{tag}")
        return child.text if child is not None else None

    conditions, cond_unsupported = parse_conditions_element(elem.find(f"{{{TNS}}}Conditions"))
    actions, act_unsupported = _parse_predicate_block(
        elem.find(f"{{{TNS}}}Actions"), _ACTION_FIELDS, _KNOWN_ACTION_TAGS)
    unsupported = cond_unsupported + act_unsupported
    priority_text = text("Priority")

    return {
        "rule_id": text("RuleId"),
        "display_name": text("DisplayName") or "",
        "priority": int(priority_text) if priority_text and priority_text.isdigit() else None,
        "is_enabled": (text("IsEnabled") or "").strip().lower() in ("true", "1"),
        "conditions": conditions,
        "actions": actions,
        "unsupported_fields": unsupported or None,
    }
