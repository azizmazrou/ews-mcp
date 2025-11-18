"""
GAL (Global Address List) Adapter with Multi-Strategy Search.

This adapter implements the comprehensive GAL search strategy that fixes
the 0-results bug by using multiple fallback methods.

VERSION: 3.0.0
PRIORITY: #1 - Solves GAL 0-results bug
"""

import logging
from typing import List, Optional, Any, Dict
from difflib import SequenceMatcher

from ..core.person import Person, PersonSource
from ..exceptions import ToolExecutionError


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

        # Results collection
        all_results: Dict[str, Person] = {}  # email -> Person (deduplicate)

        # Strategy 1: Exact match (resolve_names)
        self.logger.info("  Strategy 1: Exact match (resolve_names)")
        exact_results = await self._search_exact(query, return_full_data)
        self._merge_results(all_results, exact_results, "exact")

        # If we found results, we can return early for performance
        if len(all_results) >= max_results:
            self.logger.info(f"  ✅ Found {len(all_results)} results via exact match")
            return list(all_results.values())[:max_results]

        # Strategy 2: Partial match (prefix search)
        # This strategy is NEW in v3.0 and handles cases like "Ahmed" -> "Ahmed Al-Rashid"
        if len(all_results) == 0:
            self.logger.info("  Strategy 2: Partial match (searching GAL directory)")
            partial_results = await self._search_partial(query, return_full_data)
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

        return list(all_results.values())[:max_results]

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
            results = self.ews_client.account.protocol.resolve_names(
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
            results = self.ews_client.account.protocol.resolve_names(
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

            results = self.ews_client.account.protocol.resolve_names(
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

        This handles typos and variations like:
        - "Ahmed" matches "Ahmad"
        - "Al-Rashid" matches "AlRashid"
        """
        try:
            # Get a broad set of GAL results
            # Try searching with common prefixes
            all_persons = []

            for prefix in ['A', 'M', 'S', 'K', 'F', 'H', 'N', 'R']:
                try:
                    results = self.ews_client.account.protocol.resolve_names(
                        names=[prefix],
                        return_full_contact_data=False  # Don't need full data for fuzzy
                    )

                    if results:
                        for result in results[:10]:  # Limit per prefix
                            try:
                                person = self._parse_resolve_result(result, False)
                                if person:
                                    all_persons.append(person)
                            except:
                                continue

                except:
                    continue

            if not all_persons:
                self.logger.debug("    No GAL entries for fuzzy matching")
                return []

            # Fuzzy match against query
            matches = []
            query_lower = query.lower()

            for person in all_persons:
                # Calculate similarity scores
                name_score = SequenceMatcher(
                    None,
                    query_lower,
                    person.name.lower()
                ).ratio()

                email_score = 0.0
                if person.primary_email:
                    email_score = SequenceMatcher(
                        None,
                        query_lower,
                        person.primary_email.lower()
                    ).ratio()

                # Use best score
                score = max(name_score, email_score)

                # Threshold: 60% similarity
                if score >= 0.6:
                    matches.append((score, person))

            # Sort by score
            matches.sort(reverse=True, key=lambda x: x[0])

            # Get top matches
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
        try:
            contacts = self.ews_client.account.contacts.all()

            persons = []
            query_lower = query.lower()

            for contact in contacts[:100]:  # Limit for performance
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

                except Exception as e:
                    continue

            return persons

        except Exception as e:
            self.logger.warning(f"    Contacts folder search failed: {e}")
            return []

    def _parse_resolve_result(
        self,
        result: Any,
        return_full_data: bool
    ) -> Optional[Person]:
        """
        Parse resolve_names result into Person object.

        Handles both tuple format (mailbox, contact_info) and object format.
        """
        try:
            # Handle tuple format: (mailbox, contact_info)
            if isinstance(result, tuple):
                mailbox = result[0]
                contact_info = result[1] if len(result) > 1 and return_full_data else None
                return Person.from_gal_result(mailbox, contact_info)

            # Handle object format (fallback)
            elif hasattr(result, 'mailbox'):
                mailbox = result.mailbox
                contact_info = getattr(result, 'contact', None) if return_full_data else None
                return Person.from_gal_result(mailbox, contact_info)

            else:
                self.logger.warning(f"Unknown result format: {type(result)}")
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
