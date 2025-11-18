"""Contact Intelligence Tools for EWS MCP Server.

Provides advanced contact search and analysis capabilities:
- FindPersonTool: Search across GAL, email history, and domains
- GetCommunicationHistoryTool: Analyze communication patterns with contacts
- AnalyzeNetworkTool: Professional network intelligence

VERSION: 2025-11-18-ENHANCED-REVAMP
CHANGES:
- Enhanced _search_gal() method with phone number extraction
- Added comprehensive error handling with per-result try/catch
- Improved logging with clear section markers (=== GAL Search Start ===)
- Enhanced contact data extraction:
  * Phone numbers (phone_numbers array, business_phone, mobile_phone)
  * Office location
  * All contact details with None-checking
- Better use of getattr() for safe attribute access
- Maintains clean architecture:
  * ONE call to resolve_names() with return_full_contact_data=True
  * Clean tuple handling: (mailbox, contact_info)
  * Fallback for object format
- Extracts: name, email, display_name, given_name, surname, company,
           department, job_title, phone_numbers, office
"""

import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict
import re

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, safe_get


class FindPersonTool(BaseTool):
    """Unified contact search across multiple sources."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "find_person",
            "description": "Search for contacts across Global Address List (GAL), email history, and domains. Supports Arabic text (UTF-8).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Name, email, or domain to search (e.g., 'John Doe', 'john@example.com', '@example.com')"
                    },
                    "search_scope": {
                        "type": "string",
                        "enum": ["all", "gal", "email_history", "domain"],
                        "description": "Where to search for contacts",
                        "default": "all"
                    },
                    "include_stats": {
                        "type": "boolean",
                        "description": "Include communication statistics",
                        "default": True
                    },
                    "time_range_days": {
                        "type": "integer",
                        "description": "Days back to search email history",
                        "default": 365
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return",
                        "default": 50,
                        "maximum": 100
                    }
                },
                "required": ["query"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute unified contact search."""
        query = kwargs.get("query", "").strip()
        search_scope = kwargs.get("search_scope", "all")
        include_stats = kwargs.get("include_stats", True)
        time_range_days = kwargs.get("time_range_days", 365)
        max_results = kwargs.get("max_results", 50)

        if not query:
            raise ToolExecutionError("Query parameter is required")

        try:
            # Determine if this is a domain search
            is_domain_search = query.startswith("@")
            domain_query = query[1:].lower() if is_domain_search else None

            # Results dictionary: email -> contact info
            unified_results = {}

            # 1. Search GAL (Global Address List)
            if search_scope in ["all", "gal"] and not is_domain_search:
                # Only include personal contacts if search_scope is "all"
                include_personal = (search_scope == "all")
                self.logger.info(f"Searching GAL for: {query} (include_personal_contacts={include_personal})")
                gal_results = await self._search_gal(query, include_personal_contacts=include_personal)
                for contact in gal_results:
                    email = contact.get("email", "").lower()
                    if email and email not in unified_results:
                        contact["sources"] = ["gal"]
                        contact["email_count"] = 0
                        unified_results[email] = contact
                self.logger.info(f"Found {len(gal_results)} contacts in GAL")

            # 2. Search Email History
            if search_scope in ["all", "email_history", "domain"]:
                self.logger.info(f"Searching email history (past {time_range_days} days)")
                email_results = await self._search_email_history(
                    query if not is_domain_search else None,
                    domain_query,
                    time_range_days,
                    include_stats
                )

                for contact in email_results:
                    email = contact.get("email", "").lower()
                    if email:
                        if email in unified_results:
                            # Merge with existing
                            if "email_history" not in unified_results[email]["sources"]:
                                unified_results[email]["sources"].append("email_history")
                            unified_results[email]["email_count"] = contact.get("email_count", 0)
                            unified_results[email]["last_contact"] = contact.get("last_contact")
                            unified_results[email]["first_contact"] = contact.get("first_contact")
                        else:
                            # New contact from email history
                            contact["sources"] = ["email_history"]
                            unified_results[email] = contact

                self.logger.info(f"Found {len(email_results)} contacts in email history")

            # 3. Deduplicate and rank results
            results_list = list(unified_results.values())

            # Ranking criteria:
            # 1. Number of sources (GAL + email history = higher rank)
            # 2. Email volume (more emails = higher rank)
            # 3. Recency (recent contact = higher rank)
            def rank_contact(contact):
                source_score = len(contact.get("sources", [])) * 1000
                email_score = contact.get("email_count", 0)

                # Recency score (recent = higher)
                last_contact = contact.get("last_contact")
                recency_score = 0
                if last_contact:
                    try:
                        if isinstance(last_contact, str):
                            last_dt = datetime.fromisoformat(last_contact.replace('Z', '+00:00'))
                        else:
                            last_dt = last_contact
                        days_ago = (datetime.now(last_dt.tzinfo) - last_dt).days
                        recency_score = max(0, 365 - days_ago)  # 0-365 points
                    except:
                        pass

                return source_score + email_score + recency_score

            results_list.sort(key=rank_contact, reverse=True)
            results_list = results_list[:max_results]

            # 4. Format results
            formatted_results = []
            for contact in results_list:
                result = {
                    "name": contact.get("name", ""),
                    "email": contact.get("email", ""),
                    "sources": contact.get("sources", []),
                }

                # Add optional fields
                if contact.get("company"):
                    result["company"] = contact["company"]
                if contact.get("job_title"):
                    result["job_title"] = contact["job_title"]
                if contact.get("department"):
                    result["department"] = contact["department"]
                if contact.get("display_name"):
                    result["display_name"] = contact["display_name"]
                if contact.get("office"):
                    result["office"] = contact["office"]

                # Add phone numbers if available
                if contact.get("phone_numbers"):
                    result["phone_numbers"] = contact["phone_numbers"]
                if contact.get("business_phone"):
                    result["business_phone"] = contact["business_phone"]
                if contact.get("mobile_phone"):
                    result["mobile_phone"] = contact["mobile_phone"]

                # Add stats if requested
                if include_stats:
                    result["email_count"] = contact.get("email_count", 0)
                    result["last_contact"] = contact.get("last_contact")
                    result["first_contact"] = contact.get("first_contact")

                formatted_results.append(result)

            return format_success_response(
                f"Found {len(formatted_results)} contact(s) for '{query}'",
                query=query,
                search_scope=search_scope,
                total_results=len(formatted_results),
                unified_results=formatted_results
            )

        except Exception as e:
            self.logger.error(f"Failed to search for person: {e}")
            raise ToolExecutionError(f"Failed to search for person: {e}")

    async def _search_gal(self, query: str, include_personal_contacts: bool = False) -> List[Dict[str, Any]]:
        """Search Global Address List with enhanced contact data extraction.

        This is a direct port of the working search_gal() function with:
        - Simple resolve_names() call with return_full_contact_data=True
        - Proper tuple handling: (mailbox, contact_info)
        - Fallback for object format
        - Enhanced phone number extraction
        - Comprehensive logging

        Args:
            query: Search query string
            include_personal_contacts: If True, also search Contacts folder (currently not implemented)
        """
        contacts = []

        try:
            self.logger.info(f"=== GAL Search Start === Query: '{query}'")

            # ONE simple call to resolve_names - matches working Python code
            results = self.ews_client.account.protocol.resolve_names(
                names=[query],
                return_full_contact_data=True
            )

            if not results:
                self.logger.warning("GAL search returned 0 results")
                return contacts

            self.logger.info(f"GAL returned {len(results)} raw result(s)")

            for idx, result in enumerate(results):
                try:
                    # Handle tuple format: (mailbox, contact_info)
                    if isinstance(result, tuple):
                        mailbox = result[0]
                        contact_info = result[1] if len(result) > 1 else None

                        contact = {
                            'name': getattr(mailbox, 'name', 'N/A'),
                            'email': getattr(mailbox, 'email_address', 'N/A'),
                            'routing_type': getattr(mailbox, 'routing_type', 'SMTP'),
                        }

                        # Extract additional details from contact_info
                        if contact_info:
                            if hasattr(contact_info, 'display_name') and contact_info.display_name:
                                contact['display_name'] = contact_info.display_name
                            if hasattr(contact_info, 'given_name') and contact_info.given_name:
                                contact['given_name'] = contact_info.given_name
                            if hasattr(contact_info, 'surname') and contact_info.surname:
                                contact['surname'] = contact_info.surname
                            if hasattr(contact_info, 'company_name') and contact_info.company_name:
                                contact['company'] = contact_info.company_name
                            if hasattr(contact_info, 'department') and contact_info.department:
                                contact['department'] = contact_info.department
                            if hasattr(contact_info, 'job_title') and contact_info.job_title:
                                contact['job_title'] = contact_info.job_title

                            # Extract phone numbers
                            if hasattr(contact_info, 'phone_numbers') and contact_info.phone_numbers:
                                phones = []
                                for phone in contact_info.phone_numbers:
                                    phone_entry = {}
                                    if hasattr(phone, 'label'):
                                        phone_entry['type'] = phone.label
                                    if hasattr(phone, 'phone_number'):
                                        phone_entry['number'] = phone.phone_number
                                    if phone_entry:
                                        phones.append(phone_entry)
                                if phones:
                                    contact['phone_numbers'] = phones

                            # Also check individual phone fields
                            if hasattr(contact_info, 'business_phone') and contact_info.business_phone:
                                contact['business_phone'] = contact_info.business_phone
                            if hasattr(contact_info, 'mobile_phone') and contact_info.mobile_phone:
                                contact['mobile_phone'] = contact_info.mobile_phone
                            if hasattr(contact_info, 'office_location') and contact_info.office_location:
                                contact['office'] = contact_info.office_location

                    else:
                        # Handle object format (fallback)
                        contact = {
                            'name': result.mailbox.name if hasattr(result, 'mailbox') else 'N/A',
                            'email': result.mailbox.email_address if hasattr(result, 'mailbox') else 'N/A',
                            'routing_type': result.mailbox.routing_type if hasattr(result, 'mailbox') else 'SMTP',
                        }

                        if hasattr(result, 'contact') and result.contact:
                            c = result.contact
                            if hasattr(c, 'display_name') and c.display_name:
                                contact['display_name'] = c.display_name
                            if hasattr(c, 'given_name') and c.given_name:
                                contact['given_name'] = c.given_name
                            if hasattr(c, 'surname') and c.surname:
                                contact['surname'] = c.surname
                            if hasattr(c, 'company_name') and c.company_name:
                                contact['company'] = c.company_name
                            if hasattr(c, 'department') and c.department:
                                contact['department'] = c.department
                            if hasattr(c, 'job_title') and c.job_title:
                                contact['job_title'] = c.job_title

                    contacts.append(contact)

                    # Log each contact found
                    name = contact.get('name', 'N/A')
                    email = contact.get('email', 'N/A')
                    company = contact.get('company', '')
                    self.logger.debug(f"  GAL [{idx + 1}] {name} <{email}>" + (f" @ {company}" if company else ""))

                except Exception as e:
                    self.logger.error(f"Error processing GAL result {idx}: {e}")
                    import traceback
                    self.logger.error(traceback.format_exc())
                    continue

            self.logger.info(f"=== GAL Search Complete === Found {len(contacts)} contact(s)")
            return contacts

        except Exception as e:
            self.logger.error(f"=== GAL Search Failed === Error: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return contacts

    async def _search_email_history(
        self,
        name_query: Optional[str],
        domain_query: Optional[str],
        days_back: int,
        include_stats: bool
    ) -> List[Dict[str, Any]]:
        """Search email history for contacts with pagination."""
        try:
            start_date = datetime.now(self.ews_client.account.default_timezone) - timedelta(days=days_back)

            # Dictionary to track contacts: email -> info
            contacts = {}

            # CRITICAL: Use same limit as get_communication_history to prevent timeouts
            MAX_ITEMS_TO_SCAN = 2000

            # Search Inbox
            inbox = self.ews_client.account.inbox
            inbox_items = inbox.filter(
                datetime_received__gte=start_date
            ).order_by('-datetime_received').only('sender', 'datetime_received')

            items_scanned = 0
            for item in inbox_items:
                items_scanned += 1
                if items_scanned > MAX_ITEMS_TO_SCAN:
                    self.logger.warning(
                        f"Reached scan limit of {MAX_ITEMS_TO_SCAN} items in Inbox. "
                        f"Results may be incomplete. Consider reducing time_range_days."
                    )
                    break

                sender = safe_get(item, 'sender')
                if sender:
                    email = safe_get(sender, 'email_address', '').lower()
                    name = safe_get(sender, 'name', '')

                    # Apply filters
                    if domain_query:
                        if not email.endswith(f"@{domain_query}"):
                            continue
                    elif name_query:
                        query_lower = name_query.lower()
                        # Check if query matches name or email (partial match)
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

                        # Update timestamps
                        received_time = safe_get(item, 'datetime_received')
                        if received_time:
                            if not contacts[email]["last_contact"] or received_time > contacts[email]["last_contact"]:
                                contacts[email]["last_contact"] = received_time
                            if not contacts[email]["first_contact"] or received_time < contacts[email]["first_contact"]:
                                contacts[email]["first_contact"] = received_time

            self.logger.info(f"Scanned {items_scanned} items in Inbox, found {len(contacts)} unique contacts")

            # Search Sent Items
            sent_items = self.ews_client.account.sent
            sent_query = sent_items.filter(
                datetime_sent__gte=start_date
            ).order_by('-datetime_sent').only('to_recipients', 'datetime_sent')

            items_scanned = 0
            for item in sent_query:
                items_scanned += 1
                if items_scanned > MAX_ITEMS_TO_SCAN:
                    self.logger.warning(
                        f"Reached scan limit of {MAX_ITEMS_TO_SCAN} items in Sent folder. "
                        f"Results may be incomplete."
                    )
                    break

                # Handle None recipients safely
                recipients = safe_get(item, 'to_recipients', []) or []
                for recipient in recipients:
                    email = safe_get(recipient, 'email_address', '').lower()
                    name = safe_get(recipient, 'name', '')

                    # Apply filters
                    if domain_query:
                        if not email.endswith(f"@{domain_query}"):
                            continue
                    elif name_query:
                        query_lower = name_query.lower()
                        # Check if query matches name or email (partial match)
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

                        # Update timestamps
                        sent_time = safe_get(item, 'datetime_sent')
                        if sent_time:
                            if not contacts[email]["last_contact"] or sent_time > contacts[email]["last_contact"]:
                                contacts[email]["last_contact"] = sent_time
                            if not contacts[email]["first_contact"] or sent_time < contacts[email]["first_contact"]:
                                contacts[email]["first_contact"] = sent_time

            self.logger.info(f"Scanned {items_scanned} items in Sent folder")
            self.logger.info(f"Total unique contacts found: {len(contacts)}")

            # Convert timestamps to ISO format
            for contact in contacts.values():
                if contact["last_contact"]:
                    contact["last_contact"] = contact["last_contact"].isoformat()
                if contact["first_contact"]:
                    contact["first_contact"] = contact["first_contact"].isoformat()

            results = list(contacts.values())
            self.logger.info(f"Returning {len(results)} contacts from email history search")
            return results

        except Exception as e:
            self.logger.warning(f"Email history search failed: {e}")
            return []


class GetCommunicationHistoryTool(BaseTool):
    """Analyze communication history with a specific contact."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_communication_history",
            "description": "Get detailed communication history and statistics with a specific person",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "Email address of the contact"
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "Number of days back to analyze",
                        "default": 365
                    },
                    "max_emails": {
                        "type": "integer",
                        "description": "Maximum number of recent emails to include",
                        "default": 10
                    },
                    "include_topics": {
                        "type": "boolean",
                        "description": "Extract topics from email subjects",
                        "default": True
                    }
                },
                "required": ["email"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Get communication history with a contact."""
        email = kwargs.get("email", "").strip().lower()
        days_back = kwargs.get("days_back", 365)
        max_emails = kwargs.get("max_emails", 10)
        include_topics = kwargs.get("include_topics", True)

        if not email:
            raise ToolExecutionError("Email parameter is required")

        try:
            start_date = datetime.now(self.ews_client.account.default_timezone) - timedelta(days=days_back)

            # Statistics
            stats = {
                "total_emails": 0,
                "received": 0,
                "sent": 0,
                "first_contact": None,
                "last_contact": None
            }

            # Timeline: month -> count
            timeline = defaultdict(int)

            # Topics: subject keywords
            topics = defaultdict(int)

            # Recent emails
            recent_emails = []

            # 1. Search Inbox (received emails)
            # Add pagination to prevent timeouts with large mailboxes
            MAX_ITEMS_TO_SCAN = 2000  # Limit total items scanned to prevent timeout
            inbox = self.ews_client.account.inbox
            received_items = inbox.filter(
                datetime_received__gte=start_date
            ).order_by('-datetime_received').only('sender', 'subject', 'datetime_received', 'text_body')

            received_list = []
            items_scanned = 0
            for item in received_items:
                items_scanned += 1
                if items_scanned > MAX_ITEMS_TO_SCAN:
                    self.logger.warning(
                        f"Reached scan limit of {MAX_ITEMS_TO_SCAN} items. "
                        f"Results may be incomplete for very active contacts. "
                        f"Consider reducing days_back parameter."
                    )
                    break

                sender = safe_get(item, 'sender')
                if sender:
                    sender_email = safe_get(sender, 'email_address', '').lower()
                    if sender_email == email:
                        received_list.append(item)

            stats["received"] = len(received_list)

            # Process received emails
            for item in received_list:
                received_time = safe_get(item, 'datetime_received')
                if received_time:
                    # Update first/last contact
                    if not stats["first_contact"] or received_time < stats["first_contact"]:
                        stats["first_contact"] = received_time
                    if not stats["last_contact"] or received_time > stats["last_contact"]:
                        stats["last_contact"] = received_time

                    # Update timeline
                    month_key = received_time.strftime("%Y-%m")
                    timeline[month_key] += 1

                    # Extract topics from subject
                    if include_topics:
                        subject = safe_get(item, 'subject', '')
                        self._extract_topics(subject, topics)

            # Get recent received emails
            received_list.sort(key=lambda x: safe_get(x, 'datetime_received', datetime.min), reverse=True)
            for item in received_list[:max_emails]:
                recent_emails.append({
                    "direction": "received",
                    "subject": safe_get(item, 'subject', ''),
                    "date": safe_get(item, 'datetime_received').isoformat() if safe_get(item, 'datetime_received') else None,
                    "preview": (safe_get(item, 'text_body', '') or '')[:200]
                })

            # 2. Search Sent Items (sent emails)
            # Add pagination to prevent timeouts with large mailboxes
            sent_items = self.ews_client.account.sent
            sent_query = sent_items.filter(
                datetime_sent__gte=start_date
            ).order_by('-datetime_sent').only('to_recipients', 'subject', 'datetime_sent', 'text_body')

            sent_list = []
            items_scanned = 0
            for item in sent_query:
                items_scanned += 1
                if items_scanned > MAX_ITEMS_TO_SCAN:
                    self.logger.warning(
                        f"Reached scan limit of {MAX_ITEMS_TO_SCAN} items in Sent folder. "
                        f"Results may be incomplete. Consider reducing days_back parameter."
                    )
                    break

                # Ensure recipients is always a list (can be None from EWS)
                recipients = safe_get(item, 'to_recipients', []) or []
                for recipient in recipients:
                    recipient_email = safe_get(recipient, 'email_address', '').lower()
                    if recipient_email == email:
                        sent_list.append(item)
                        break  # Only count each email once

            stats["sent"] = len(sent_list)

            # Process sent emails
            for item in sent_list:
                sent_time = safe_get(item, 'datetime_sent')
                if sent_time:
                    # Update first/last contact
                    if not stats["first_contact"] or sent_time < stats["first_contact"]:
                        stats["first_contact"] = sent_time
                    if not stats["last_contact"] or sent_time > stats["last_contact"]:
                        stats["last_contact"] = sent_time

                    # Update timeline
                    month_key = sent_time.strftime("%Y-%m")
                    timeline[month_key] += 1

                    # Extract topics from subject
                    if include_topics:
                        subject = safe_get(item, 'subject', '')
                        self._extract_topics(subject, topics)

            # Get recent sent emails
            sent_list.sort(key=lambda x: safe_get(x, 'datetime_sent', datetime.min), reverse=True)
            for item in sent_list[:max_emails // 2]:  # Half of max for sent
                recent_emails.append({
                    "direction": "sent",
                    "subject": safe_get(item, 'subject', ''),
                    "date": safe_get(item, 'datetime_sent').isoformat() if safe_get(item, 'datetime_sent') else None,
                    "preview": (safe_get(item, 'text_body', '') or '')[:200]
                })

            # 3. Calculate total and format results
            stats["total_emails"] = stats["received"] + stats["sent"]

            # Format timestamps
            if stats["first_contact"]:
                stats["first_contact"] = stats["first_contact"].isoformat()
            if stats["last_contact"]:
                stats["last_contact"] = stats["last_contact"].isoformat()

            # Calculate frequency (emails per month)
            if days_back > 0:
                months = days_back / 30
                stats["emails_per_month"] = round(stats["total_emails"] / months, 1) if months > 0 else 0

            # Sort timeline
            timeline_list = [
                {"month": month, "count": count}
                for month, count in sorted(timeline.items())
            ]

            # Sort topics by frequency
            top_topics = sorted(
                [{"topic": topic, "count": count} for topic, count in topics.items()],
                key=lambda x: x["count"],
                reverse=True
            )[:10]  # Top 10 topics

            # Sort recent emails by date
            recent_emails.sort(
                key=lambda x: x["date"] if x["date"] else "",
                reverse=True
            )

            return format_success_response(
                f"Communication history with {email}",
                email=email,
                statistics=stats,
                timeline=timeline_list,
                topics=top_topics if include_topics else [],
                recent_emails=recent_emails[:max_emails]
            )

        except Exception as e:
            self.logger.error(f"Failed to get communication history: {e}")
            raise ToolExecutionError(f"Failed to get communication history: {e}")

    def _extract_topics(self, subject: str, topics: Dict[str, int]):
        """Extract keywords from email subject."""
        if not subject:
            return

        # Remove common prefixes
        subject = re.sub(r'^(RE:|FW:|FWD:)\s*', '', subject, flags=re.IGNORECASE)

        # Extract words (at least 3 characters, not all caps unless acronym)
        words = re.findall(r'\b[A-Za-z]{3,}\b', subject)

        # Common stop words to ignore
        stop_words = {
            'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can', 'your',
            'from', 'with', 'have', 'this', 'that', 'will', 'was', 'been', 'has'
        }

        for word in words:
            word_lower = word.lower()
            if word_lower not in stop_words and len(word) >= 4:
                # Use original case if it's likely an acronym (all caps)
                key = word if word.isupper() and len(word) <= 5 else word_lower.capitalize()
                topics[key] += 1


class AnalyzeNetworkTool(BaseTool):
    """Analyze professional network and communication patterns."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "analyze_network",
            "description": "Analyze professional network, identify top contacts, VIPs, dormant relationships, and domain statistics",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "analysis_type": {
                        "type": "string",
                        "enum": ["overview", "top_contacts", "by_domain", "dormant", "vip"],
                        "description": "Type of network analysis to perform",
                        "default": "overview"
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "Number of days back to analyze",
                        "default": 90
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Number of top results to return",
                        "default": 20,
                        "maximum": 50
                    },
                    "dormant_threshold_days": {
                        "type": "integer",
                        "description": "Days without contact to consider dormant",
                        "default": 60
                    },
                    "vip_email_threshold": {
                        "type": "integer",
                        "description": "Minimum emails to qualify as VIP",
                        "default": 10
                    }
                }
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Analyze professional network."""
        analysis_type = kwargs.get("analysis_type", "overview")
        days_back = kwargs.get("days_back", 90)
        top_n = kwargs.get("top_n", 20)
        dormant_threshold = kwargs.get("dormant_threshold_days", 60)
        vip_threshold = kwargs.get("vip_email_threshold", 10)

        try:
            # Gather all contacts from email history
            self.logger.info(f"Analyzing network for past {days_back} days")
            contacts = await self._gather_contacts(days_back)

            if not contacts:
                return format_success_response(
                    "No contacts found in the specified time range",
                    analysis_type=analysis_type,
                    results=[]
                )

            # Perform analysis based on type
            if analysis_type == "top_contacts":
                results = self._analyze_top_contacts(contacts, top_n)
            elif analysis_type == "by_domain":
                results = self._analyze_by_domain(contacts, top_n)
            elif analysis_type == "dormant":
                results = self._analyze_dormant(contacts, dormant_threshold, days_back)
            elif analysis_type == "vip":
                results = self._analyze_vip(contacts, vip_threshold, days_back)
            else:  # overview
                results = self._analyze_overview(contacts, top_n, dormant_threshold, vip_threshold, days_back)

            return format_success_response(
                f"Network analysis: {analysis_type}",
                analysis_type=analysis_type,
                period_days=days_back,
                total_contacts=len(contacts),
                **results
            )

        except Exception as e:
            self.logger.error(f"Failed to analyze network: {e}")
            raise ToolExecutionError(f"Failed to analyze network: {e}")

    async def _gather_contacts(self, days_back: int) -> Dict[str, Dict[str, Any]]:
        """Gather all contacts from email history."""
        start_date = datetime.now(self.ews_client.account.default_timezone) - timedelta(days=days_back)
        contacts = {}

        # Search Inbox
        inbox = self.ews_client.account.inbox
        inbox_items = inbox.filter(
            datetime_received__gte=start_date
        ).only('sender', 'datetime_received')

        for item in inbox_items[:2000]:  # Limit for performance
            sender = safe_get(item, 'sender')
            if sender:
                email = safe_get(sender, 'email_address', '').lower()
                name = safe_get(sender, 'name', '')
                received_time = safe_get(item, 'datetime_received')

                if email and received_time:
                    if email not in contacts:
                        # Extract domain
                        domain = email.split('@')[1] if '@' in email else 'unknown'
                        contacts[email] = {
                            "email": email,
                            "name": name,
                            "domain": domain,
                            "received": 0,
                            "sent": 0,
                            "last_contact": None,
                            "first_contact": None
                        }

                    contacts[email]["received"] += 1

                    if not contacts[email]["last_contact"] or received_time > contacts[email]["last_contact"]:
                        contacts[email]["last_contact"] = received_time
                    if not contacts[email]["first_contact"] or received_time < contacts[email]["first_contact"]:
                        contacts[email]["first_contact"] = received_time

        # Search Sent Items
        sent_items = self.ews_client.account.sent
        sent_query = sent_items.filter(
            datetime_sent__gte=start_date
        ).only('to_recipients', 'datetime_sent')

        for item in sent_query[:2000]:  # Limit for performance
            recipients = safe_get(item, 'to_recipients', [])
            sent_time = safe_get(item, 'datetime_sent')

            for recipient in recipients:
                email = safe_get(recipient, 'email_address', '').lower()
                name = safe_get(recipient, 'name', '')

                if email and sent_time:
                    if email not in contacts:
                        # Extract domain
                        domain = email.split('@')[1] if '@' in email else 'unknown'
                        contacts[email] = {
                            "email": email,
                            "name": name,
                            "domain": domain,
                            "received": 0,
                            "sent": 0,
                            "last_contact": None,
                            "first_contact": None
                        }

                    contacts[email]["sent"] += 1

                    if not contacts[email]["last_contact"] or sent_time > contacts[email]["last_contact"]:
                        contacts[email]["last_contact"] = sent_time
                    if not contacts[email]["first_contact"] or sent_time < contacts[email]["first_contact"]:
                        contacts[email]["first_contact"] = sent_time

        # Calculate total emails for each contact
        for contact in contacts.values():
            contact["total_emails"] = contact["received"] + contact["sent"]

        return contacts

    def _analyze_top_contacts(self, contacts: Dict, top_n: int) -> Dict[str, Any]:
        """Analyze top contacts by email volume."""
        sorted_contacts = sorted(
            contacts.values(),
            key=lambda x: x["total_emails"],
            reverse=True
        )[:top_n]

        results = []
        for contact in sorted_contacts:
            results.append({
                "name": contact["name"],
                "email": contact["email"],
                "total_emails": contact["total_emails"],
                "received": contact["received"],
                "sent": contact["sent"],
                "last_contact": contact["last_contact"].isoformat() if contact["last_contact"] else None
            })

        return {"top_contacts": results}

    def _analyze_by_domain(self, contacts: Dict, top_n: int) -> Dict[str, Any]:
        """Analyze contacts grouped by domain."""
        # Group by domain
        domains = defaultdict(lambda: {"count": 0, "emails": 0, "contacts": []})

        for contact in contacts.values():
            domain = contact["domain"]
            domains[domain]["count"] += 1
            domains[domain]["emails"] += contact["total_emails"]
            domains[domain]["contacts"].append({
                "name": contact["name"],
                "email": contact["email"],
                "total_emails": contact["total_emails"]
            })

        # Sort domains by email volume
        sorted_domains = sorted(
            [
                {
                    "domain": domain,
                    "contact_count": info["count"],
                    "total_emails": info["emails"],
                    "top_contacts": sorted(info["contacts"], key=lambda x: x["total_emails"], reverse=True)[:5]
                }
                for domain, info in domains.items()
            ],
            key=lambda x: x["total_emails"],
            reverse=True
        )[:top_n]

        return {"domains": sorted_domains}

    def _analyze_dormant(self, contacts: Dict, threshold_days: int, analysis_days: int) -> Dict[str, Any]:
        """Identify dormant relationships."""
        now = datetime.now(self.ews_client.account.default_timezone)
        threshold_date = now - timedelta(days=threshold_days)
        analysis_start = now - timedelta(days=analysis_days)

        dormant = []
        for contact in contacts.values():
            last_contact = contact["last_contact"]
            first_contact = contact["first_contact"]

            # Dormant: last contact was before threshold, but was active before
            if last_contact and last_contact < threshold_date:
                # Must have had reasonable activity (at least 3 emails)
                if contact["total_emails"] >= 3:
                    days_since = (now - last_contact).days
                    dormant.append({
                        "name": contact["name"],
                        "email": contact["email"],
                        "total_emails": contact["total_emails"],
                        "last_contact": last_contact.isoformat(),
                        "days_since_contact": days_since
                    })

        # Sort by total emails (most important dormant relationships first)
        dormant.sort(key=lambda x: x["total_emails"], reverse=True)

        return {
            "dormant_contacts": dormant,
            "threshold_days": threshold_days
        }

    def _analyze_vip(self, contacts: Dict, email_threshold: int, analysis_days: int) -> Dict[str, Any]:
        """Identify VIP contacts."""
        now = datetime.now(self.ews_client.account.default_timezone)
        recent_threshold = now - timedelta(days=30)  # Active in last 30 days

        vips = []
        for contact in contacts.values():
            # VIP criteria: high email volume AND recent contact
            if contact["total_emails"] >= email_threshold:
                last_contact = contact["last_contact"]
                if last_contact and last_contact >= recent_threshold:
                    vips.append({
                        "name": contact["name"],
                        "email": contact["email"],
                        "domain": contact["domain"],
                        "total_emails": contact["total_emails"],
                        "received": contact["received"],
                        "sent": contact["sent"],
                        "last_contact": last_contact.isoformat(),
                        "emails_per_day": round(contact["total_emails"] / analysis_days, 2)
                    })

        # Sort by total emails
        vips.sort(key=lambda x: x["total_emails"], reverse=True)

        return {
            "vip_contacts": vips,
            "criteria": f"Minimum {email_threshold} emails and contact within last 30 days"
        }

    def _analyze_overview(
        self,
        contacts: Dict,
        top_n: int,
        dormant_threshold: int,
        vip_threshold: int,
        analysis_days: int
    ) -> Dict[str, Any]:
        """Comprehensive network overview."""
        # Get all analyses
        top_contacts = self._analyze_top_contacts(contacts, min(top_n, 10))
        domains = self._analyze_by_domain(contacts, min(top_n, 10))
        dormant = self._analyze_dormant(contacts, dormant_threshold, analysis_days)
        vips = self._analyze_vip(contacts, vip_threshold, analysis_days)

        # Calculate summary statistics
        total_emails = sum(c["total_emails"] for c in contacts.values())
        avg_emails_per_contact = round(total_emails / len(contacts), 1) if contacts else 0

        return {
            "summary": {
                "total_contacts": len(contacts),
                "total_emails": total_emails,
                "avg_emails_per_contact": avg_emails_per_contact,
                "vip_count": len(vips["vip_contacts"]),
                "dormant_count": len(dormant["dormant_contacts"])
            },
            "top_contacts": top_contacts["top_contacts"][:5],
            "top_domains": domains["domains"][:5],
            "vip_contacts": vips["vip_contacts"][:5],
            "dormant_contacts": dormant["dormant_contacts"][:5]
        }
