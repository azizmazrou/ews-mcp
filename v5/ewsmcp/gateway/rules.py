"""Synchronous Exchange Inbox rules gateway helpers (run on the EWS pool).

Wraps ``exchangelib`` 5.6.0's Inbox rules services and folder verification:

- Subclasses ``GetInboxRules`` to read ``<m:OutlookRuleBlobExists>`` from the
  response message and capture a SHA-256 XML fingerprint plus raw-XML shape
  diagnostics before ``Rule.from_xml`` drops unknown or empty elements.
- Calls ``CreateInboxRule``, ``SetInboxRule``, and ``DeleteInboxRule`` directly
  (never ``Account.create_rule`` or ``Rule.delete``), keeping the server write
  separate from read-back verification so a completed write never surfaces as
  an error if read-back fails.
- Enforces fail-closed Deleted Items / Junk Email and live folder ancestry
  verification up to the message root before any rule target folder is accepted.
- Safely deletes disabled rules when Exchange rejects ``DeleteInboxRule`` with
  ``ErrorItemNotFound`` by neutralising the rule to a 48-character random
  ``contains_subject_strings`` token + ``stop_processing_rules`` before
  re-attempting ``DeleteInboxRule``.
"""

from collections import OrderedDict
import hashlib
import secrets
import threading
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

from exchangelib import Account
from exchangelib.errors import (
    ErrorFolderNotFound,
    ErrorItemNotFound,
    UnauthorizedError,
)
from exchangelib.fields import BooleanField
from exchangelib.folders import DeletedItems, JunkEmail, SingleFolderQuerySet
from exchangelib.properties import (
    Actions,
    Conditions,
    Exceptions,
    FolderId,
    Rule,
)
from exchangelib.services.inbox_rules import (
    CreateInboxRule,
    DeleteInboxRule,
    GetInboxRules,
    SetInboxRule,
)
from exchangelib.util import MNS, TNS, get_xml_attr, xml_to_str
from exchangelib.version import EXCHANGE_2016, Version

from ..errors import ToolError
from .client import WELL_KNOWN

_OUTLOOK_BLOB_TAG = f"{{{MNS}}}OutlookRuleBlobExists"
_DISCARD_FOLDER_ATTRS = ("trash", "junk")
_DISCARD_DISTINGUISHED_IDS = frozenset({"deleteditems", "junkemail"})
_RULE_ID_HINT = "Call list_rules to get current rule ids."
MAX_FOLDER_ANCESTRY_DEPTH = 25


class RuleXmlMeta(NamedTuple):
    """Raw server XML metadata captured before ``Rule.from_xml`` runs."""

    fingerprint: str
    extra_unsupported: Tuple[str, ...]
    extra_sends_mail: bool


_PARSED_RULE_META: "OrderedDict[int, Tuple[Rule, RuleXmlMeta]]" = OrderedDict()
_PARSED_RULE_META_LOCK = threading.Lock()
_PARSED_RULE_META_MAX = 2048


def _field_xml_tag(field: Any) -> str:
    local = getattr(field, "field_uri", None) or getattr(
        getattr(field, "value_cls", None), "ELEMENT_NAME", None
    )
    return f"{{{TNS}}}{local}"


_RULE_FIELDS_BY_TAG: Dict[str, Any] = {
    _field_xml_tag(f): f for f in Rule.FIELDS
}
_COND_FIELDS_BY_TAG: Dict[str, Any] = {
    _field_xml_tag(f): f for f in Conditions.FIELDS
}
_EXC_FIELDS_BY_TAG: Dict[str, Any] = {
    _field_xml_tag(f): f for f in Exceptions.FIELDS
}
_ACT_FIELDS_BY_TAG: Dict[str, Any] = {
    _field_xml_tag(f): f for f in Actions.FIELDS
}
_SENDING_ACTION_NAMES = frozenset({
    "forward_to_recipients",
    "forward_as_attachment_to_recipients",
    "redirect_to_recipients",
    "send_sms_alert_to_recipients",
    "server_reply_with_message",
})


def _local_tag(tag: Any) -> str:
    if isinstance(tag, str):
        return tag.split("}", 1)[-1]
    return "unknown"


def _parse_xml_bool(raw: Optional[str]) -> Optional[bool]:
    if raw is None:
        return None
    norm = str(raw).strip().lower()
    if norm in ("true", "1"):
        return True
    if norm in ("false", "0"):
        return False
    return None


def _normalise_boolean_elem(elem: Any) -> bool:
    """Normalise xs:boolean ``1``/``0`` in place; return False if unreadable."""
    raw = (elem.text or "").strip()
    low = raw.lower()
    if raw == "1":
        elem.text = "true"
        return True
    if raw == "0":
        elem.text = "false"
        return True
    return low in ("true", "false")


def _record_rule_meta(rule: Rule, meta: RuleXmlMeta) -> None:
    key = id(rule)
    with _PARSED_RULE_META_LOCK:
        _PARSED_RULE_META[key] = (rule, meta)
        _PARSED_RULE_META.move_to_end(key)
        while len(_PARSED_RULE_META) > _PARSED_RULE_META_MAX:
            _PARSED_RULE_META.popitem(last=False)


def _lookup_rule_meta(rule: Rule) -> Optional[RuleXmlMeta]:
    with _PARSED_RULE_META_LOCK:
        entry = _PARSED_RULE_META.get(id(rule))
    if entry is not None and entry[0] is rule:
        return entry[1]
    return None


def rule_xml_issues(rule: Rule) -> Tuple[Tuple[str, ...], bool]:
    """Return ``(extra_unsupported, extra_sends_mail)`` captured from raw XML."""
    meta = _lookup_rule_meta(rule)
    if meta is None:
        return (), False
    return meta.extra_unsupported, meta.extra_sends_mail


def rule_fingerprint(rule: Rule, account: Any = None) -> str:
    """Return a SHA-256 hex digest of the serialised ``<t:Rule>`` XML."""
    meta = _lookup_rule_meta(rule)
    if meta is not None:
        return meta.fingerprint
    version = getattr(account, "version", None) or getattr(
        getattr(rule, "account", None), "version", None
    ) or Version(build=EXCHANGE_2016)
    try:
        raw_xml = xml_to_str(rule.to_xml(version=version))
    except Exception:
        raw_xml = repr(rule)
    return hashlib.sha256(raw_xml.encode("utf-8")).hexdigest()


def _is_parsed_non_boolean_empty(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, (list, tuple, set, str)):
        return len(val) == 0
    return False


class GetInboxRulesWithBlob(GetInboxRules):
    """``GetInboxRules`` service that records ``<m:OutlookRuleBlobExists>``,
    raw ``<t:Rule>`` SHA-256 fingerprints, and present-but-unreadable XML tags.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.outlook_rule_blob_exists: Optional[bool] = None

    def _get_element_container(self, message: Any, name: Any = None) -> Any:
        container = super()._get_element_container(message, name=name)
        self.outlook_rule_blob_exists = _parse_xml_bool(
            get_xml_attr(message, _OUTLOOK_BLOB_TAG)
        )
        return container

    def _elem_to_obj(self, elem: Any) -> Rule:
        fingerprint = hashlib.sha256(
            xml_to_str(elem).encode("utf-8")
        ).hexdigest()

        extra_unsupported: List[str] = []
        extra_sends_mail = False
        present_non_bool: List[Tuple[str, str]] = []

        def _add_issue(label: str) -> None:
            if label not in extra_unsupported:
                extra_unsupported.append(label)

        for child in elem:
            if not isinstance(child.tag, str):
                continue
            rfield = _RULE_FIELDS_BY_TAG.get(child.tag)
            if rfield is None:
                _add_issue(f"rule.{_local_tag(child.tag)}")
                continue
            if isinstance(rfield, BooleanField):
                if not _normalise_boolean_elem(child):
                    _add_issue(rfield.name)
                continue
            if rfield.name in ("conditions", "exceptions", "actions"):
                prefix = rfield.name
                fmap = (
                    _COND_FIELDS_BY_TAG
                    if prefix == "conditions"
                    else _EXC_FIELDS_BY_TAG
                    if prefix == "exceptions"
                    else _ACT_FIELDS_BY_TAG
                )
                for sub in child:
                    if not isinstance(sub.tag, str):
                        continue
                    sfield = fmap.get(sub.tag)
                    if sfield is None:
                        _add_issue(f"{prefix}.{_local_tag(sub.tag)}")
                        continue
                    if isinstance(sfield, BooleanField):
                        if not _normalise_boolean_elem(sub):
                            _add_issue(f"{prefix}.{sfield.name}")
                    else:
                        present_non_bool.append((prefix, sfield.name))

        rule = super()._elem_to_obj(elem)

        for prefix, fname in present_non_bool:
            if prefix == "exceptions":
                _add_issue(f"exceptions.{fname}")
                continue
            container_obj = getattr(rule, prefix, None)
            val = getattr(container_obj, fname, None) if container_obj is not None else None
            if _is_parsed_non_boolean_empty(val):
                _add_issue(f"{prefix}.{fname}")
                if prefix == "actions" and fname in _SENDING_ACTION_NAMES:
                    extra_sends_mail = True

        _record_rule_meta(
            rule,
            RuleXmlMeta(
                fingerprint=fingerprint,
                extra_unsupported=tuple(extra_unsupported),
                extra_sends_mail=extra_sends_mail,
            ),
        )
        return rule


# --- fail-closed discard-folder & live ancestry checks -----------------------


def _discard_target_error(label: str) -> ToolError:
    return ToolError(
        "validation",
        f"{label}: a rule may not file mail into Deleted Items or Junk Email, "
        "or a folder below them (that would be a deleting rule).",
        hint=(
            "Choose another folder. A rule that discards mail can only be "
            "made in Outlook."
        ),
    )


def _unverifiable_discard_error() -> ToolError:
    return ToolError(
        "validation",
        "Cannot verify that the target is not Deleted Items or Junk Email.",
        hint="Ensure Deleted Items and Junk Email folders are accessible via EWS.",
    )


def _unverifiable_location_error(label: str) -> ToolError:
    return ToolError(
        "validation",
        f"{label}: cannot verify the folder's location.",
        hint="Use list_folders and pass a mail folder inside the mailbox.",
    )


def is_well_known_discard_ref(ref: Optional[str]) -> bool:
    """True when ``ref`` names Deleted Items or Junk in ``WELL_KNOWN``."""
    if not isinstance(ref, str) or not ref.strip():
        return False
    key = ref.strip().lower()
    mapped = WELL_KNOWN.get(key) or WELL_KNOWN.get(f"f:{key}")
    return mapped in ("trash", "junk")


def is_discard_folder_node(
    folder: Any, discard_ids: Optional[Set[str]] = None
) -> bool:
    """True when ``folder`` itself is Deleted Items or Junk by class,
    distinguished id, or raw id."""
    if folder is None:
        return False
    if isinstance(folder, (DeletedItems, JunkEmail)):
        return True
    cls_dfid = getattr(folder, "DISTINGUISHED_FOLDER_ID", None)
    if isinstance(cls_dfid, str) and cls_dfid.lower() in _DISCARD_DISTINGUISHED_IDS:
        return True
    inst_dfid = getattr(getattr(folder, "_distinguished_id", None), "id", None)
    if isinstance(inst_dfid, str) and inst_dfid.lower() in _DISCARD_DISTINGUISHED_IDS:
        return True
    own_id = getattr(folder, "id", None)
    if discard_ids and isinstance(own_id, str) and own_id in discard_ids:
        return True
    return False


def discard_folder_ids(account: Any) -> Set[str]:
    """Return verified raw ids of Deleted Items and Junk Email (fail closed).

    Only ``ErrorFolderNotFound`` (the server explicitly reports the
    distinguished folder does not exist on this mailbox) may be skipped. Any
    other error or a folder returned with ``id=None`` raises ``validation``.
    """
    ids: Set[str] = set()
    for attr in _DISCARD_FOLDER_ATTRS:
        try:
            folder = getattr(account, attr)
        except UnauthorizedError:
            raise
        except ErrorFolderNotFound:
            continue
        except Exception as exc:
            raise _unverifiable_discard_error() from exc
        folder_id = getattr(folder, "id", None) if folder is not None else None
        if not isinstance(folder_id, str) or not folder_id.strip():
            raise _unverifiable_discard_error()
        ids.add(folder_id)
    return ids


def is_discard_target(folder: Any, discard_ids: Set[str]) -> bool:
    """Second-opinion check on ``folder`` and its cached ``parts`` chain."""
    if is_discard_folder_node(folder, discard_ids):
        return True
    try:
        parts = list(getattr(folder, "parts", None) or [])
    except UnauthorizedError:
        raise
    except Exception:
        parts = []
    for part in parts:
        if is_discard_folder_node(part, discard_ids):
            return True
    return False


def _fetch_folder_live(account: Any, folder_id: str, *, label: str) -> Any:
    try:
        resolved = SingleFolderQuerySet(
            account=account,
            folder=FolderId(id=folder_id),
        ).resolve()
    except UnauthorizedError:
        raise
    except Exception as exc:
        raise _unverifiable_location_error(label) from exc
    if isinstance(resolved, UnauthorizedError):
        raise resolved
    if isinstance(resolved, Exception) or resolved is None:
        raise _unverifiable_location_error(label)
    return resolved


def verify_rule_target_folder(
    account: Any,
    folder: Any,
    *,
    ref: str,
    label: str,
    discard_ids: Optional[Set[str]] = None,
) -> Tuple[Any, Set[str]]:
    """Verify live that ``folder`` is a real folder under ``msg_folder_root``
    and neither is nor lies below Deleted Items or Junk Email (sync).

    Returns ``(fresh_target_folder, discard_ids)``.
    """
    if is_well_known_discard_ref(ref):
        raise _discard_target_error(label)
    if is_discard_target(folder, discard_ids or set()):
        raise _discard_target_error(label)
    if discard_ids is None:
        discard_ids = discard_folder_ids(account)
    if is_discard_target(folder, discard_ids):
        raise _discard_target_error(label)

    target_id = getattr(folder, "id", None)
    if not isinstance(target_id, str) or not target_id.strip():
        raise _unverifiable_location_error(label)

    try:
        msg_root = account.msg_folder_root
    except UnauthorizedError:
        raise
    except Exception as exc:
        raise _unverifiable_location_error(label) from exc
    msg_root_id = getattr(msg_root, "id", None) if msg_root is not None else None
    if (
        not isinstance(msg_root_id, str)
        or not msg_root_id.strip()
        or target_id == msg_root_id
    ):
        raise _unverifiable_location_error(label)

    current_id = target_id
    visited: Set[str] = set()
    fresh_target: Any = None
    reached_root = False

    for _ in range(MAX_FOLDER_ANCESTRY_DEPTH):
        if current_id in visited:
            raise _unverifiable_location_error(label)
        visited.add(current_id)
        if current_id in discard_ids:
            raise _discard_target_error(label)

        fresh = _fetch_folder_live(account, current_id, label=label)
        if fresh_target is None:
            fresh_target = fresh
        if is_discard_folder_node(fresh, discard_ids):
            raise _discard_target_error(label)

        parent_ref = getattr(fresh, "parent_folder_id", None)
        parent_id = (
            getattr(parent_ref, "id", None) if parent_ref is not None else None
        )
        if (
            not isinstance(parent_id, str)
            or not parent_id.strip()
            or parent_id == current_id
        ):
            raise _unverifiable_location_error(label)
        if parent_id in discard_ids:
            raise _discard_target_error(label)
        if parent_id == msg_root_id:
            reached_root = True
            break
        current_id = parent_id

    if not reached_root or fresh_target is None:
        raise _unverifiable_location_error(label)
    fresh_id = getattr(fresh_target, "id", None)
    if not isinstance(fresh_id, str) or not fresh_id.strip():
        raise _unverifiable_location_error(label)
    return fresh_target, discard_ids


# --- rule read & write operations --------------------------------------------


def _raise_if_rule_id_error(exc: Exception, display_ref: Optional[str]) -> None:
    if isinstance(exc, UnauthorizedError):
        raise exc
    lowered = f"{type(exc).__name__} {exc}".lower()
    if "erroritemnotfound" in lowered or "errorinvalidid" in lowered:
        handle = display_ref or "rule_id"
        raise ToolError(
            "not_found",
            f"No rule matches {handle!r}.",
            hint=_RULE_ID_HINT,
        ) from exc


def fetch_rules(account: Any) -> Tuple[List[Rule], Optional[bool]]:
    """Return ``(rules, outlook_rule_blob_exists)`` for ``account`` (sync)."""
    service = GetInboxRulesWithBlob(account=account)
    rules: List[Rule] = []
    for item in service.call():
        if isinstance(item, Exception):
            raise item
        if item is not None:
            rules.append(item)
    return rules, service.outlook_rule_blob_exists


def find_rule(
    rules: List[Rule],
    raw_rule_id: str,
    *,
    display_ref: Optional[str] = None,
) -> Rule:
    """Locate ``raw_rule_id`` in ``rules`` or raise ``not_found``."""
    for rule in rules:
        if getattr(rule, "id", None) and str(rule.id) == raw_rule_id:
            return rule
    handle = display_ref or raw_rule_id
    raise ToolError(
        "not_found",
        f"No rule matches {handle!r}.",
        hint=_RULE_ID_HINT,
    )


def create_rule(
    account: Any,
    rule: Rule,
    *,
    before_rules: Optional[List[Rule]] = None,
) -> Tuple[Optional[Rule], bool]:
    """Create ``rule`` via ``CreateInboxRule`` and read back its server id.

    Returns ``(created_rule, verified)``. Once ``CreateInboxRule`` succeeds,
    any read-back failure or ambiguous id diff returns ``(None, False)`` rather
    than raising an error for a write that already happened.
    """
    if before_rules is None:
        before_rules, _ = fetch_rules(account)
    target_name = rule.display_name or ""
    for existing in before_rules:
        if (existing.display_name or "") == target_name:
            raise ToolError(
                "validation",
                f"A rule named {target_name!r} already exists; pass rule_id to "
                "replace an existing rule or choose a distinct name.",
                hint="Call list_rules to inspect existing rules and their ids.",
            )
    before_ids = {
        str(existing.id)
        for existing in before_rules
        if getattr(existing, "id", None)
    }

    CreateInboxRule(account=account).get(rule=rule)

    try:
        after_rules, _ = fetch_rules(account)
    except UnauthorizedError:
        raise
    except Exception:
        return None, False

    added = [
        candidate
        for candidate in after_rules
        if getattr(candidate, "id", None) and str(candidate.id) not in before_ids
    ]
    if len(added) > 1:
        by_name = [
            candidate
            for candidate in added
            if (candidate.display_name or "") == target_name
        ]
        if len(by_name) == 1:
            added = by_name
    if len(added) != 1:
        return None, False
    created = added[0]
    rule.id = created.id
    return created, True


def replace_rule(
    account: Any,
    rule: Rule,
    *,
    display_ref: Optional[str] = None,
) -> Tuple[Optional[Rule], bool]:
    """Wholesale-replace an existing rule via ``SetInboxRule`` (sync).

    Returns ``(stored_rule, verified)``. Once ``SetInboxRule`` succeeds, any
    read-back failure returns ``(None, False)`` rather than raising an error.
    """
    if not getattr(rule, "id", None):
        raise ValueError("Rule must have an id to replace")
    target_id = str(rule.id)

    try:
        SetInboxRule(account=account).get(rule=rule)
    except Exception as exc:
        _raise_if_rule_id_error(exc, display_ref)
        raise

    try:
        after_rules, _ = fetch_rules(account)
    except UnauthorizedError:
        raise
    except Exception:
        return None, False

    for candidate in after_rules:
        if getattr(candidate, "id", None) and str(candidate.id) == target_id:
            return candidate, True
    return None, False


def delete_rule(
    account: Any,
    raw_rule_id: str,
    *,
    rules: Optional[List[Rule]] = None,
    display_ref: Optional[str] = None,
) -> Tuple[str, str]:
    """Delete ``raw_rule_id`` via ``DeleteInboxRule`` and return ``(id, name)``."""
    if rules is None:
        rules, _ = fetch_rules(account)
    target = find_rule(rules, raw_rule_id, display_ref=display_ref)
    deleted_id = str(target.id)
    deleted_name = target.display_name or ""
    handle = display_ref or deleted_id

    try:
        DeleteInboxRule(account=account).get(rule=target)
        return deleted_id, deleted_name
    except UnauthorizedError:
        raise
    except ErrorItemNotFound as exc:
        if getattr(target, "is_enabled", None) is not False:
            raise ToolError(
                "not_found",
                f"No rule matches {handle!r}.",
                hint=_RULE_ID_HINT,
            ) from exc
    except Exception as exc:
        _raise_if_rule_id_error(exc, handle)
        raise

    # Disabled-rule workaround: Exchange rejects DeleteInboxRule on a disabled
    # rule with ErrorItemNotFound. Replace it first with an enabled rule that
    # cannot match anything (48-character random subject token and
    # stop_processing_rules only), then delete.
    neutral_name = (deleted_name or "Disabled rule")[:255]
    raw_prio = getattr(target, "priority", None)
    neutral_prio = (
        raw_prio
        if isinstance(raw_prio, int)
        and not isinstance(raw_prio, bool)
        and raw_prio >= 1
        else 10**6
    )
    token_48 = secrets.token_hex(24)
    rule_kwargs: Dict[str, Any] = {
        "id": deleted_id,
        "display_name": neutral_name,
        "priority": neutral_prio,
        "is_enabled": True,
        "conditions": Conditions(contains_subject_strings=[token_48]),
        "exceptions": None,
        "actions": Actions(stop_processing_rules=True),
    }
    if isinstance(account, Account):
        rule_kwargs["account"] = account
    neutralised = Rule(**rule_kwargs)

    try:
        SetInboxRule(account=account).get(rule=neutralised)
    except Exception as exc:
        _raise_if_rule_id_error(exc, handle)
        raise

    try:
        DeleteInboxRule(account=account).get(rule=neutralised)
    except UnauthorizedError:
        raise
    except Exception as exc:
        raise ToolError(
            "upstream_error",
            f"Rule {neutral_name!r} ({handle}) was neutralised to a safe "
            "no-match stop_processing_rules rule so Exchange could delete it, "
            f"but the final delete failed ({type(exc).__name__}: {exc}).",
            hint=(
                f"Call delete_rule again for {neutral_name!r} ({handle}) to "
                "remove the neutralised rule."
            ),
        ) from exc

    return deleted_id, deleted_name
