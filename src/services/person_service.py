"""
PersonService - Unified person operations for EWS MCP v3.0.

This service orchestrates person discovery across multiple sources:
- Global Address List (GAL) - via GALAdapter with multi-strategy search
- Personal Contacts
- Email History (sent/received)

KEY FEATURE: Fixes GAL 0-results bug with intelligent fallback strategies.
"""

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from ..core.person import Person, PersonSource, CommunicationStats
from ..adapters.gal_adapter import GALAdapter
from ..adapters.cache_adapter import get_cache
from ..utils import safe_get


class PersonService:
    """
    Person-centric service for discovering and managing people.

    This is the CORE service in v3.0 architecture.
    All person-related operations go through this service.
    """

    def __init__(self, ews_client):
        """
        Initialize PersonService.

        Args:
            ews_client: EWSClient instance
        """
        self.ews_client = ews_client
        self.gal_adapter = GALAdapter(ews_client)
        self.cache = get_cache()
        self.logger = logging.getLogger(__name__)

    async def find_person(
        self,
        query: str,
        sources: Optional[List[str]] = None,
        include_stats: bool = True,
        time_range_days: int = 365,
        max_results: int = 50
    ) -> List[Person]:
        """
        Find people using intelligent multi-source search.

        This is the MAIN METHOD for person discovery in v3.0!

        Search strategy:
        1. Try GAL (multi-strategy search)
        2. Try Personal Contacts (if enabled)
        3. Try Email History (if enabled)
        4. Merge and deduplicate results
        5. Rank by relevance

        Args:
            query: Name, email, or domain to search
            sources: Sources to search (default: all)
            include_stats: Include communication statistics
            time_range_days: Days back for email history
            max_results: Maximum results to return

        Returns:
            List of Person objects, ranked by relevance
        """
        self.logger.info(f"🔍 PersonService.find_person: '{query}'")

        # Default to all sources
        if sources is None:
            sources = ["gal", "contacts", "email_history"]

        # Results collection: email -> Person
        all_persons: Dict[str, Person] = {}

        # Source 1: GAL Search (with multi-strategy fallback)
        if "gal" in sources:
            cache_key = f"gal_search:{query}"
            gal_persons = await self.cache.get_or_fetch(
                key=cache_key,
                fetch_func=lambda: self.gal_adapter.search(
                    query=query,
                    max_results=max_results,
                    return_full_data=True
                ),
                duration=self.cache.CACHE_DURATIONS['gal_search']
            )

            for person in gal_persons:
                email = person.primary_email
                if email:
                    all_persons[email.lower()] = person

            self.logger.info(f"  GAL: Found {len(gal_persons)} person(s)")

        # Source 2: Personal Contacts
        if "contacts" in sources:
            contact_persons = await self._search_contacts(query)
            for person in contact_persons:
                email = person.primary_email
                if email:
                    email_key = email.lower()
                    if email_key in all_persons:
                        # Merge with existing
                        all_persons[email_key] = all_persons[email_key].merge_with(person)
                    else:
                        all_persons[email_key] = person

            self.logger.info(f"  Contacts: Found {len(contact_persons)} person(s)")

        # Source 3: Email History
        if "email_history" in sources:
            email_persons = await self._search_email_history(
                query=query,
                days_back=time_range_days,
                include_stats=include_stats
            )

            for person in email_persons:
                email = person.primary_email
                if email:
                    email_key = email.lower()
                    if email_key in all_persons:
                        # Merge with existing
                        all_persons[email_key] = all_persons[email_key].merge_with(person)
                    else:
                        all_persons[email_key] = person

            self.logger.info(f"  Email History: Found {len(email_persons)} person(s)")

        # Convert to list and rank
        results = list(all_persons.values())

        # Rank by relevance
        results = self._rank_persons(results, query)

        # Limit results
        results = results[:max_results]

        self.logger.info(f"  ✅ Total: {len(results)} unique person(s) found")

        return results

    async def get_person(
        self,
        email: str,
        include_history: bool = True,
        days_back: int = 365
    ) -> Optional[Person]:
        """
        Get complete information about a specific person.

        Args:
            email: Person's email address
            include_history: Include communication history
            days_back: Days back for communication history

        Returns:
            Person object with complete information, or None if not found
        """
        self.logger.info(f"📧 PersonService.get_person: {email}")

        # Try to find person via multi-source search
        persons = await self.find_person(
            query=email,
            sources=["gal", "contacts", "email_history"],
            include_stats=include_history,
            time_range_days=days_back,
            max_results=1
        )

        if persons:
            return persons[0]

        return None

    async def get_communication_history(
        self,
        email: str,
        days_back: int = 365,
        max_emails: int = 100
    ) -> Optional[CommunicationStats]:
        """
        Get detailed communication history with a person.

        Args:
            email: Person's email address
            days_back: Days back to analyze
            max_emails: Maximum emails to scan

        Returns:
            CommunicationStats object or None
        """
        self.logger.info(f"📊 Getting communication history: {email}")

        try:
            start_date = datetime.now(self.ews_client.account.default_timezone) - timedelta(days=days_back)

            stats = CommunicationStats(
                total_emails=0,
                emails_sent=0,
                emails_received=0,
            )

            # Scan inbox for received emails
            inbox = self.ews_client.account.inbox
            received_items = inbox.filter(
                datetime_received__gte=start_date
            ).order_by('-datetime_received').only('sender', 'datetime_received')

            received_count = 0
            for item in received_items[:max_emails]:
                sender = safe_get(item, 'sender')
                if sender:
                    sender_email = safe_get(sender, 'email_address', '').lower()
                    if sender_email == email.lower():
                        received_count += 1

                        # Update timestamps
                        received_time = safe_get(item, 'datetime_received')
                        if received_time:
                            if not stats.first_contact or received_time < stats.first_contact:
                                stats.first_contact = received_time
                            if not stats.last_contact or received_time > stats.last_contact:
                                stats.last_contact = received_time

            stats.emails_received = received_count

            # Scan sent items for sent emails
            sent_items = self.ews_client.account.sent
            sent_query = sent_items.filter(
                datetime_sent__gte=start_date
            ).order_by('-datetime_sent').only('to_recipients', 'datetime_sent')

            sent_count = 0
            for item in sent_query[:max_emails]:
                recipients = safe_get(item, 'to_recipients', []) or []
                for recipient in recipients:
                    recipient_email = safe_get(recipient, 'email_address', '').lower()
                    if recipient_email == email.lower():
                        sent_count += 1

                        # Update timestamps
                        sent_time = safe_get(item, 'datetime_sent')
                        if sent_time:
                            if not stats.first_contact or sent_time < stats.first_contact:
                                stats.first_contact = sent_time
                            if not stats.last_contact or sent_time > stats.last_contact:
                                stats.last_contact = sent_time

                        break  # Only count email once

            stats.emails_sent = sent_count
            stats.total_emails = stats.emails_received + stats.emails_sent

            # Calculate emails per month
            if days_back > 0:
                months = days_back / 30
                stats.emails_per_month = round(stats.total_emails / months, 1) if months > 0 else 0

            return stats

        except Exception as e:
            self.logger.error(f"Failed to get communication history: {e}")
            return None

    async def _search_contacts(self, query: str) -> List[Person]:
        """
        Search personal contacts folder.

        Args:
            query: Search query

        Returns:
            List of Person objects from contacts
        """
        try:
            contacts = self.ews_client.account.contacts.all()

            persons = []
            query_lower = query.lower()

            for contact in contacts[:100]:  # Limit for performance
                try:
                    # Check if query matches
                    given_name = safe_get(contact, "given_name", "") or ""
                    surname = safe_get(contact, "surname", "") or ""
                    display_name = safe_get(contact, "display_name", "") or ""
                    email_addrs = safe_get(contact, "email_addresses", []) or []

                    # Get email
                    email = ""
                    if email_addrs:
                        email = email_addrs[0].email if hasattr(email_addrs[0], 'email') else ""

                    # Match query
                    if (query_lower in given_name.lower() or
                        query_lower in surname.lower() or
                        query_lower in display_name.lower() or
                        query_lower in email.lower()):

                        # Convert to Person
                        person = Person.from_contact(contact)
                        persons.append(person)

                except Exception as e:
                    self.logger.debug(f"Failed to process contact: {e}")
                    continue

            return persons

        except Exception as e:
            self.logger.warning(f"Contacts search failed: {e}")
            return []

    async def _search_email_history(
        self,
        query: str,
        days_back: int,
        include_stats: bool
    ) -> List[Person]:
        """
        Search email history for people.

        Args:
            query: Search query
            days_back: Days back to search
            include_stats: Include communication statistics

        Returns:
            List of Person objects from email history
        """
        try:
            start_date = datetime.now(self.ews_client.account.default_timezone) - timedelta(days=days_back)

            # Determine if this is a domain search
            is_domain_search = query.startswith("@")
            domain_query = query[1:].lower() if is_domain_search else None

            # Track contacts: email -> info
            contacts: Dict[str, Dict[str, Any]] = {}

            MAX_ITEMS = 2000  # Limit to prevent timeouts

            # Search Inbox
            inbox = self.ews_client.account.inbox
            inbox_items = inbox.filter(
                datetime_received__gte=start_date
            ).order_by('-datetime_received').only('sender', 'datetime_received')

            items_scanned = 0
            for item in inbox_items:
                items_scanned += 1
                if items_scanned > MAX_ITEMS:
                    break

                sender = safe_get(item, 'sender')
                if sender:
                    email = safe_get(sender, 'email_address', '').lower()
                    name = safe_get(sender, 'name', '')

                    # Apply filters
                    if domain_query:
                        if not email.endswith(f"@{domain_query}"):
                            continue
                    elif query:
                        query_lower = query.lower()
                        if query_lower not in name.lower() and query_lower not in email:
                            continue

                    # Track contact
                    if email:
                        if email not in contacts:
                            contacts[email] = {
                                "email": email,
                                "name": name,
                                "email_count": 0,
                                "last_contact": None,
                                "first_contact": None
                            }

                        contacts[email]["email_count"] += 1

                        received_time = safe_get(item, 'datetime_received')
                        if received_time:
                            if not contacts[email]["last_contact"] or received_time > contacts[email]["last_contact"]:
                                contacts[email]["last_contact"] = received_time
                            if not contacts[email]["first_contact"] or received_time < contacts[email]["first_contact"]:
                                contacts[email]["first_contact"] = received_time

            # Search Sent Items
            sent_items = self.ews_client.account.sent
            sent_query = sent_items.filter(
                datetime_sent__gte=start_date
            ).order_by('-datetime_sent').only('to_recipients', 'datetime_sent')

            items_scanned = 0
            for item in sent_query:
                items_scanned += 1
                if items_scanned > MAX_ITEMS:
                    break

                recipients = safe_get(item, 'to_recipients', []) or []
                for recipient in recipients:
                    email = safe_get(recipient, 'email_address', '').lower()
                    name = safe_get(recipient, 'name', '')

                    # Apply filters
                    if domain_query:
                        if not email.endswith(f"@{domain_query}"):
                            continue
                    elif query:
                        query_lower = query.lower()
                        if query_lower not in name.lower() and query_lower not in email:
                            continue

                    # Track contact
                    if email:
                        if email not in contacts:
                            contacts[email] = {
                                "email": email,
                                "name": name,
                                "email_count": 0,
                                "last_contact": None,
                                "first_contact": None
                            }

                        contacts[email]["email_count"] += 1

                        sent_time = safe_get(item, 'datetime_sent')
                        if sent_time:
                            if not contacts[email]["last_contact"] or sent_time > contacts[email]["last_contact"]:
                                contacts[email]["last_contact"] = sent_time
                            if not contacts[email]["first_contact"] or sent_time < contacts[email]["first_contact"]:
                                contacts[email]["first_contact"] = sent_time

            # Convert to Person objects
            persons = []
            for contact_data in contacts.values():
                # Create communication stats if requested
                stats = None
                if include_stats:
                    stats = CommunicationStats(
                        total_emails=contact_data["email_count"],
                        emails_sent=0,  # Not tracked separately here
                        emails_received=0,  # Not tracked separately here
                        first_contact=contact_data["first_contact"],
                        last_contact=contact_data["last_contact"],
                    )

                # Create minimal Person from email history
                from ..core.person import EmailAddress
                person = Person(
                    id=contact_data["email"],
                    name=contact_data["name"] or contact_data["email"],
                    email_addresses=[
                        EmailAddress(
                            address=contact_data["email"],
                            is_primary=True
                        )
                    ],
                    sources=[PersonSource.EMAIL_HISTORY],
                    communication_stats=stats
                )

                persons.append(person)

            return persons

        except Exception as e:
            self.logger.warning(f"Email history search failed: {e}")
            return []

    def _rank_persons(self, persons: List[Person], query: str) -> List[Person]:
        """
        Rank persons by relevance to query.

        Ranking criteria:
        1. Source priority (GAL > Contacts > Email History)
        2. Name/email match quality
        3. Communication volume
        4. Recency of contact
        5. VIP status

        Args:
            persons: List of Person objects
            query: Search query

        Returns:
            Sorted list of Person objects
        """

        def calculate_score(person: Person) -> float:
            score = 0.0
            query_lower = query.lower()

            # 1. Source priority (0-100)
            score += person.source_priority

            # 2. Exact match bonus (0-100)
            if person.primary_email and query_lower == person.primary_email.lower():
                score += 100
            elif query_lower in person.name.lower():
                score += 50

            # 3. Communication volume (0-50)
            if person.communication_stats:
                email_score = min(person.communication_stats.total_emails, 50)
                score += email_score

            # 4. Recency bonus (0-30)
            if person.communication_stats and person.communication_stats.last_contact:
                # Use timezone-aware datetime to avoid comparison errors
                now = datetime.now(timezone.utc)
                last_contact = person.communication_stats.last_contact
                # Ensure last_contact is timezone-aware
                if last_contact.tzinfo is None:
                    last_contact = last_contact.replace(tzinfo=timezone.utc)
                days_ago = (now - last_contact).days
                recency_score = max(0, 30 * (1 - days_ago / 365))
                score += recency_score

            # 5. VIP bonus (20)
            if person.is_vip:
                score += 20

            # 6. Completeness bonus (0-20)
            completeness = 0
            if person.job_title:
                completeness += 5
            if person.department:
                completeness += 5
            if person.organization:
                completeness += 5
            if len(person.phone_numbers) > 0:
                completeness += 5
            score += completeness

            return score

        # Sort by score (descending)
        persons.sort(key=calculate_score, reverse=True)

        return persons
