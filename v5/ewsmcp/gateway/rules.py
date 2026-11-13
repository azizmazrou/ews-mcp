"""Synchronous Exchange Inbox rules gateway helpers (run on the EWS pool).

Wraps ``exchangelib`` 5.6.0's ``GetInboxRules`` and ``Account`` rule methods:

- Subclasses ``GetInboxRules`` to read ``<m:OutlookRuleBlobExists>`` from the
  response message before handing elements back to ``exchangelib``.
- Confirms newly created rule ids by comparing the rule-id set before and after
  ``Account.create_rule`` rather than trusting display-name lookup alone.
- Names the folders a rule may not file mail into (Deleted Items, Junk and
  what lies below them): moving every match there is a deleting rule.
- Calls ``Account.delete_rule(rule)`` directly — never ``Rule.delete()``, which
  rewrites disabled rules into enabled conditionless ``stop_processing_rules``
  rules via ``SetInboxRule`` before deleting them.
"""

from typing import Any, List, Optional, Set, Tuple

from exchangelib.errors import UnauthorizedError
from exchangelib.properties import Rule
from exchangelib.services.inbox_rules import GetInboxRules
from exchangelib.util import MNS, get_xml_attr

from ..errors import ToolError

_OUTLOOK_BLOB_TAG = f"{{{MNS}}}OutlookRuleBlobExists"


def _parse_xml_bool(raw: Optional[str]) -> Optional[bool]:
    if raw is None:
        return None
    norm = str(raw).strip().lower()
    if norm in ("true", "1"):
        return True
    if norm in ("false", "0"):
        return False
    return None


class GetInboxRulesWithBlob(GetInboxRules):
    """``GetInboxRules`` service that records ``<m:OutlookRuleBlobExists>``.

    In EWS ``GetInboxRulesResponseMessageType``, ``OutlookRuleBlobExists`` is a
    sibling of ``ResponseCode`` and ``InboxRules`` on ``message``.
    ``EWSAccountService`` passes ``message`` to ``_get_element_container``
    while consuming the response stream.
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


_DISCARD_FOLDER_ATTRS = ("trash", "junk")


def discard_folder_ids(account: Any) -> Set[str]:
    """Raw ids of Deleted Items and Junk Email (sync). A mailbox without one
    of them simply contributes nothing."""
    ids: Set[str] = set()
    for attr in _DISCARD_FOLDER_ATTRS:
        try:
            folder = getattr(account, attr, None)
            folder_id = getattr(folder, "id", None) if folder is not None else None
        except UnauthorizedError:
            raise  # never ask again after a rejection
        except Exception:
            folder_id = None
        if isinstance(folder_id, str) and folder_id:
            ids.add(folder_id)
    return ids


def is_discard_target(folder: Any, discard_ids: Set[str]) -> bool:
    """True when ``folder`` is one of ``discard_ids`` or lies below one."""
    if not discard_ids:
        return False
    seen: Set[str] = set()
    own_id = getattr(folder, "id", None)
    if own_id:
        seen.add(str(own_id))
    try:
        for part in list(getattr(folder, "parts", None) or []):
            part_id = getattr(part, "id", None)
            if part_id:
                seen.add(str(part_id))
    except UnauthorizedError:
        raise
    except Exception:
        pass
    return bool(seen & discard_ids)


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
        hint="Call list_rules to get current rule ids.",
    )


def create_rule(
    account: Any,
    rule: Rule,
    *,
    before_rules: Optional[List[Rule]] = None,
) -> Rule:
    """Create ``rule`` and confirm its server id via before/after set diff."""
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
    account.create_rule(rule)
    after_rules, _ = fetch_rules(account)
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
        raise ToolError(
            "upstream_error",
            f"Created rule {target_name!r}, but could not confirm a unique new "
            f"rule id from GetInboxRules (saw {len(added)} new id(s)).",
            hint="Call list_rules to inspect the mailbox rules.",
        )
    created = added[0]
    rule.id = created.id
    return created


def replace_rule(account: Any, rule: Rule) -> Rule:
    """Wholesale-replace an existing rule via ``Account.set_rule`` (sync)."""
    if not getattr(rule, "id", None):
        raise ValueError("Rule must have an id to replace")
    target_id = str(rule.id)
    account.set_rule(rule)
    after_rules, _ = fetch_rules(account)
    for candidate in after_rules:
        if getattr(candidate, "id", None) and str(candidate.id) == target_id:
            return candidate
    return rule


def delete_rule(
    account: Any,
    raw_rule_id: str,
    *,
    rules: Optional[List[Rule]] = None,
    display_ref: Optional[str] = None,
) -> Tuple[str, str]:
    """Delete ``raw_rule_id`` via ``Account.delete_rule`` and return ``(id, name)``."""
    if rules is None:
        rules, _ = fetch_rules(account)
    target = find_rule(rules, raw_rule_id, display_ref=display_ref)
    deleted_id = str(target.id)
    deleted_name = target.display_name or ""
    # Call Account.delete_rule directly; never Rule.delete(), which enables and
    # wipes disabled rules via SetInboxRule before deleting them.
    account.delete_rule(target)
    return deleted_id, deleted_name
