"""
GAL (Global Address List) Adapter with Multi-Strategy Search.

This adapter implements the comprehensive GAL search strategy that fixes
the 0-results bug by using multiple fallback methods.

VERSION: 3.4.0
PRIORITY: #1 - Solves GAL 0-results bug
"""

import asyncio
import logging
import time
from typing import List, Optional, Any, Dict, Tuple
from difflib import SequenceMatcher

from ..core.person import Person, PersonSource
from ..exceptions import ToolExecutionError

# exchangelib symbols used by GAL parsing. Guarded at module top so an
# import failure surfaces at startup, not deep inside a tool execution.
try:
    from exchangelib.errors import (
        ErrorNameResolutionNoResults,
        ErrorNameResolutionMultipleResults,
    )
except Exception:  # pragma: no cover - import guard
    ErrorNameResolutionNoResults = None  # type: ignore[assignment]
    ErrorNameResolutionMultipleResults = None  # type: ignore[assignment]

try:
    from exchangelib.properties import Mailbox as _ExchangeMailbox
except Exception:  # pragma: no cover - import guard
    _ExchangeMailbox = None  # type: ignore[assignment]


# Class-level negative-lookup cache: shared across adapter instances so an
# agent making many find_person calls in a session doesn't re-scan the GAL
# for the same missing name. Keyed by lower-cased (mailbox, query) so
# impersonated search doesn't leak.
_NEGATIVE_CACHE_TTL_SECONDS = 60
_NEGATIVE_CACHE: Dict[Tuple[str, str], float] = {}
_NEGATIVE_CACHE_LOCK = asyncio.Lock()


def _is_no_results_error(exc: BaseException) -> bool:
    """True if ``exc`` is an exchangelib "no matches" error."""
    if ErrorNameResolutionNoResults is None:
        return False
    return isinstance(
        exc, (ErrorNameResolutionNoResults, ErrorNameResolutionMultipleResults)
    )


class GALAdapter:
    """
    GAL search with intelligent multi-strategy fallback.

    Strategies (in order):
    1. Exact match (resolve_names) - fastest
    2. Partial match (prefix search) - handles incomplete names
    3. Domain search - find all users from @domain.com
    4. Fuzzy matching - handles typos and variations

    This ensures we NEVER return 0 results when people exist.
    """

    def __init__(self, ews_client):
        """
        Initialize GAL adapter.

        Args:
            ews_client: EWSClient instance
        """
        self.ews_client = ews_client
        self.logger = logging.getLogger(__name__)

    async def search(
        self,
        query: str,
        max_results: int = 50,
        return_full_data: bool = True
    ) -> List[Person]:
        """
        Multi-strategy GAL search.

        This is the KEY METHOD that fixes the 0-results bug!

        Args:
            query: Search query (name, email, or partial)
            max_results: Maximum results to return
            return_full_data: Include full contact details

        Returns:
            List of Person objects found via any strategy
        """
        self.logger.info(f"🔍 GAL Search v3.0: '{query}' (max_results={max_results})")

        # Fast path: recent negative lookup. Any find_person run for this
        # query within the TTL returns [] in ~1ms instead of re-hitting the
        # GAL for 20+ seconds.
        cache_key = await self._negative_cache_key(query)
        async with _NEGATIVE_CACHE_LOCK:
            expires = _NEGATIVE_CACHE.get(cache_key)
            if expires is not None and expires > time.time():
                self.logger.info(
                    "  negative-cache hit for %r (miss ≤ %ds ago)",
                    query, _NEGATIVE_CACHE_TTL_SECONDS,
                )
                return []
            # Expired — drop it lazily.
            if expires is not None:
                _NEGATIVE_CACHE.pop(cache_key, None)

        # Results collection
        all_results: Dict[str, Person] = {}  # email -> Person (deduplicate)

        # Strategy 1 & 2 are independent and slow (each does an EWS round
        # trip). Run them in parallel; either one's results are fine.
        self.logger.info("  Strategy 1+2: exact + partial (parallel)")
        exact_results, partial_results = await asyncio.gather(
            self._search_exact(query, return_full_data),
            self._search_partial(query, return_full_data),
            return_exceptions=False,
        )
        self._merge_results(all_results, exact_results, "exact")

        # Short-circuit on exact-match hit.
        if len(all_results) >= max_results:
            self.logger.info(f"  ✅ Found {len(all_results)} results via exact match")
            return list(all_results.values())[:max_results]

        if not all_results:
            self._merge_results(all_results, partial_results, "partial")

        if len(all_results) >= max_results:
            self.logger.info(f"  ✅ Found {len(all_results)} results via partial match")
            return list(all_results.values())[:max_results]

        # Strategy 3: Domain search (if query contains @)
        if '@' in query:
            self.logger.info("  Strategy 3: Domain search")
            domain = query.split('@')[-1]
            domain_results = await self._search_domain(domain, return_full_data)
            self._merge_results(all_results, domain_results, "domain")

        if len(all_results) >= max_results:
            self.logger.info(f"  ✅ Found {len(all_results)} results via domain search")
            return list(all_results.values())[:max_results]

        # Strategy 4: Fuzzy match on GAL results
        # Only use if we still have no results
        if len(all_results) == 0:
            self.logger.info("  Strategy 4: Fuzzy matching")
            fuzzy_results = await self._search_fuzzy(query, return_full_data)
            self._merge_results(all_results, fuzzy_results, "fuzzy")

        # Return results
        result_count = len(all_results)
        if result_count > 0:
            self.logger.info(f"  ✅ GAL Search Complete: {result_count} person(s) found")
        else:
            self.logger.warning(f"  ⚠️ GAL Search Complete: 0 results for '{query}'")
            # Remember this miss so the next identical call returns
            # immediately instead of paying the ~30s strategy walk again.
            async with _NEGATIVE_CACHE_LOCK:
                _NEGATIVE_CACHE[cache_key] = time.time() + _NEGATIVE_CACHE_TTL_SECONDS

        return list(all_results.values())[:max_results]

    async def _negative_cache_key(self, query: str) -> Tuple[str, str]:
        """Build the (mailbox, query) tuple key used by the negative cache.

        Resolving the primary mailbox requires a sync property access on
        the exchangelib Account — do it in a thread to keep the search
        coroutine responsive even on first-time account construction.
        """
        def _get_mbx() -> str:
            try:
                return str(self.ews_client.account.primary_smtp_address or "").lower()
            except Exception:
                return ""
        mbx = await asyncio.to_thread(_get_mbx)
        return mbx, (query or "").strip().lower()

    async def _search_exact(
        self,
        query: str,
        return_full_data: bool
    ) -> List[Person]:
        """
        Strategy 1: Exact match using resolve_names.

        This is the original v2.x method, fast but limited.
        """
        try:
            results = await asyncio.to_thread(
                self.ews_client.account.protocol.resolve_names,
                names=[query],
                return_full_contact_data=return_full_data
            )

            if not results:
                self.logger.debug("    No exact matches found")
                return []

            persons = []
            for result in results:
                try:
                    person = self._parse_resolve_result(result, return_full_data)
                    if person:
                        persons.append(person)
                except Exception as e:
                    self.logger.warning(f"    Failed to parse result: {e}")
                    continue

            self.logger.debug(f"    Exact match: {len(persons)} person(s)")
            return persons

        except Exception as e:
            # "Name not resolved" is the normal no-match path, not an error.
            if _is_no_results_error(e):
                self.logger.debug("    resolve_names: no match for this query")
                return []
            self.logger.warning(f"    Exact search failed: {e}")
            return []

    async def _search_partial(
        self,
        query: str,
        return_full_data: bool
    ) -> List[Person]:
        """
        Strategy 2: Partial match via directory search.

        This is the KEY FIX for the GAL bug!

        Uses different Exchange methods that support partial matching:
        - Searching the GAL directory with wildcard
        - Prefix matching on display names
        """
        try:
            # METHOD A: Try resolve_names with wildcard
            # Some Exchange servers support wildcards
            wildcard_query = f"{query}*"
            results = await asyncio.to_thread(
                self.ews_client.account.protocol.resolve_names,
                names=[wildcard_query],
                return_full_contact_data=return_full_data
            )

            if results:
                persons = []
                for result in results:
                    try:
                        person = self._parse_resolve_result(result, return_full_data)
                        if person:
                            persons.append(person)
                    except Exception as e:
                        self.logger.warning(f"    Failed to parse wildcard result: {e}")
                        continue

                if persons:
                    self.logger.debug(f"    Partial match (wildcard): {len(persons)} person(s)")
                    return persons

            # METHOD B: Search contacts folder for matches
            # This catches people in personal contacts
            contacts_results = await self._search_contacts_folder(query)
            if contacts_results:
                self.logger.debug(f"    Partial match (contacts): {len(contacts_results)} person(s)")
                return contacts_results

            self.logger.debug("    No partial matches found")
            return []

        except Exception as e:
            self.logger.warning(f"    Partial search failed: {e}")
            return []

    async def _search_domain(
        self,
        domain: str,
        return_full_data: bool
    ) -> List[Person]:
        """
        Strategy 3: Domain-based search.

        Find all users from a specific domain (e.g., @sdb.gov.sa).

        This is useful when searching for "everyone at SDB".
        """
        try:
            # Try searching with domain query
            domain_query = f"*@{domain}"

            results = await asyncio.to_thread(
                self.ews_client.account.protocol.resolve_names,
                names=[domain_query],
                return_full_contact_data=return_full_data
            )

            if not results:
                self.logger.debug(f"    No results for domain: {domain}")
                return []

            persons = []
            for result in results:
                try:
                    person = self._parse_resolve_result(result, return_full_data)
                    if person and person.primary_email and person.primary_email.endswith(f"@{domain}"):
                        persons.append(person)
                except Exception as e:
                    self.logger.warning(f"    Failed to parse domain result: {e}")
                    continue

            self.logger.debug(f"    Domain search: {len(persons)} person(s)")
            return persons

        except Exception as e:
            self.logger.warning(f"    Domain search failed: {e}")
            return []

    async def _search_fuzzy(
        self,
        query: str,
        return_full_data: bool
    ) -> List[Person]:
        """
        Strategy 4: Fuzzy matching.

        Last resort: try to match similar names using fuzzy matching.
        Uses the first character of the query as a single broad GAL search
        instead of iterating over hardcoded prefixes.
        """
        try:
            # Single broad query using the first character of the search term
            prefix = query[0] if query else ''
            if not prefix:
                return []

            all_persons = []
            try:
                results = await asyncio.to_thread(
                    self.ews_client.account.protocol.resolve_names,
                    names=[prefix],
                    return_full_contact_data=False
                )
                if results:
                    for result in results[:80]:  # Limit total results
                        try:
                            person = self._parse_resolve_result(result, False)
                            if person:
                                all_persons.append(person)
                        except Exception:
                            continue
            except Exception:
                pass

            if not all_persons:
                self.logger.debug("    No GAL entries for fuzzy matching")
                return []

            # Fuzzy match against query
            matches = []
            query_lower = query.lower()

            for person in all_persons:
                name_score = SequenceMatcher(
                    None, query_lower, person.name.lower()
                ).ratio()

                email_score = 0.0
                if person.primary_email:
                    email_score = SequenceMatcher(
                        None, query_lower, person.primary_email.lower()
                    ).ratio()

                score = max(name_score, email_score)
                if score >= 0.6:
                    matches.append((score, person))

            matches.sort(reverse=True, key=lambda x: x[0])
            fuzzy_results = [person for _, person in matches[:20]]

            if fuzzy_results:
                self.logger.debug(f"    Fuzzy match: {len(fuzzy_results)} person(s)")

            return fuzzy_results

        except Exception as e:
            self.logger.warning(f"    Fuzzy search failed: {e}")
            return []

    async def _search_contacts_folder(self, query: str) -> List[Person]:
        """
        Search personal contacts folder.

        Fallback method when GAL doesn't return results.
        """
        def _blocking():
            persons = []
            query_lower = query.lower()
            try:
                contacts = self.ews_client.account.contacts.all()
                for contact in list(contacts)[:100]:  # Limit for performance
                    try:
                        # Check if query matches
                        given_name = getattr(contact, "given_name", "") or ""
                        surname = getattr(contact, "surname", "") or ""
                        display_name = getattr(contact, "display_name", "") or ""
                        email_addrs = getattr(contact, "email_addresses", []) or []

                        # Get email
                        email = ""
                        if email_addrs:
                            email = email_addrs[0].email if hasattr(email_addrs[0], 'email') else ""

                        # Match
                        if (query_lower in given_name.lower() or
                            query_lower in surname.lower() or
                            query_lower in display_name.lower() or
                            query_lower in email.lower()):

                            # Convert to Person
                            person = Person.from_contact(contact)
                            persons.append(person)

                    except Exception:
                        continue
            except Exception as e:
                self.logger.warning(f"    Contacts folder search failed: {e}")
            return persons

        try:
            return await asyncio.to_thread(_blocking)
        except Exception as e:
            self.logger.warning(f"    Contacts folder search failed: {e}")
            return []

    def _parse_resolve_result(
        self,
        result: Any,
        return_full_data: bool
    ) -> Optional[Person]:
        """
        Parse resolve_names / fuzzy-match results into Person objects.

        Handles four shapes:

        * ``(mailbox, contact_info)`` tuple — the exchangelib return type for
          ``resolve_names(return_full_contact_data=True)``.
        * An object with a ``.mailbox`` attribute — legacy resolve-name shape.
        * A raw ``exchangelib.properties.Mailbox`` — returned by the
          fuzzy-match / GAL-scan strategies. Previously this branch fell
          through to the "unknown format" warning and was silently
          discarded (Bug 3).
        * A resolve-names error (``ErrorNameResolutionNoResults`` etc.) —
          downgraded to DEBUG because every "no match" path raises one.
        """
        try:
            # 1. Legacy tuple format.
            if isinstance(result, tuple):
                mailbox = result[0]
                contact_info = result[1] if len(result) > 1 and return_full_data else None
                return Person.from_gal_result(mailbox, contact_info)

            # 2. Object with .mailbox attribute.
            if hasattr(result, "mailbox") and getattr(result, "mailbox", None) is not None:
                mailbox = result.mailbox
                contact_info = getattr(result, "contact", None) if return_full_data else None
                return Person.from_gal_result(mailbox, contact_info)

            # 3. Raw Mailbox (the Bug 3 path). exchangelib populates
            #    ``.email_address`` and ``.name`` directly on the Mailbox.
            if _ExchangeMailbox is not None and isinstance(result, _ExchangeMailbox):
                if not getattr(result, "email_address", None):
                    return None
                return Person.from_gal_result(result, None)

            # 4. Exchange "no matches" errors from resolve_names — expected
            #    on every miss; don't pollute logs at WARNING.
            if (
                ErrorNameResolutionNoResults is not None
                and isinstance(result, ErrorNameResolutionNoResults)
            ):
                self.logger.debug("    resolve_names: no match for this strategy")
                return None

            # Multiple-results errors carry structured hints; walk into
            # .candidates if available, otherwise ignore.
            if (
                ErrorNameResolutionMultipleResults is not None
                and isinstance(result, ErrorNameResolutionMultipleResults)
            ):
                self.logger.debug("    resolve_names: multiple matches (ambiguous)")
                return None

            # Last-resort: log at DEBUG with the type so we can add new
            # branches without the operator noticing today.
            self.logger.debug(
                "_parse_resolve_result: unhandled type %s; skipping",
                type(result).__name__,
            )
            return None

        except Exception as e:
            self.logger.warning(f"Failed to parse resolve result: {e}")
            return None

    def _merge_results(
        self,
        all_results: Dict[str, Person],
        new_results: List[Person],
        strategy: str
    ) -> None:
        """
        Merge new results into all_results dict.

        Deduplicates by email and merges Person data.
        """
        for person in new_results:
            email_key = person.primary_email
            if not email_key:
                continue

            email_key = email_key.lower()

            if email_key in all_results:
                # Merge with existing
                all_results[email_key] = all_results[email_key].merge_with(person)
            else:
                # Add new
                all_results[email_key] = person

            # Track which strategy found this person
            if strategy not in [s.value for s in person.sources]:
                if strategy == "exact":
                    person.add_source(PersonSource.GAL)
                elif strategy == "partial":
                    person.add_source(PersonSource.GAL)
                elif strategy == "domain":
                    person.add_source(PersonSource.GAL)
                elif strategy == "fuzzy":
                    person.add_source(PersonSource.FUZZY_MATCH)
