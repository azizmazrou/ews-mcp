"""Contact Intelligence Tools for EWS MCP Server.

Provides advanced contact search and analysis capabilities:
- FindPersonTool: Search across GAL, email history, and domains
- GetCommunicationHistoryTool: Analyze communication patterns with contacts
- AnalyzeNetworkTool: Professional network intelligence

VERSION: 3.3.0 - TOOL CONSOLIDATION
CHANGES:
- NOW USES PersonService with multi-strategy GAL search (FIXES 0-RESULTS BUG!)
- Person-centric architecture with proper Person objects
- Enhanced GAL search with 4 fallback strategies:
  1. Exact match (resolve_names)
  2. Partial match (prefix/wildcard)
  3. Domain search (@domain.com)
  4. Fuzzy matching
- Unified person discovery across GAL, Contacts, and Email History
- Intelligent result ranking and deduplication
- Full phone number and contact detail extraction
- Communication statistics integration
"""

import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict
import re

from .base import BaseTool
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, safe_get
from ..services.person_service import PersonService


class FindPersonTool(BaseTool):
    """Unified contact search across GAL, contacts folder, and email history.

    Replaces: find_person, resolve_names, search_contacts, get_contacts.
    """

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "find_person",
            "description": "Search for people across GAL, contacts, and email history.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Name, email, or domain to search. Optional when source='contacts' (lists all contacts)"
                    },
                    "source": {
                        "type": "string",
                        "enum": ["all", "gal", "contacts", "email_history", "domain"],
                        "description": "Where to search: all (GAL+contacts+email), gal (Active Directory only), contacts (personal contacts), email_history, domain",
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
                        "maximum": 1000
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                }
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute unified contact search."""
        query = kwargs.get("query", "").strip()
        source = kwargs.get("source", "all")
        include_stats = kwargs.get("include_stats", True)
        time_range_days = kwargs.get("time_range_days", 365)
        max_results = kwargs.get("max_results", 50)
        target_mailbox = kwargs.get("target_mailbox")

        # source="contacts" with no query lists all contacts
        if source == "contacts" and not query:
            return await self._list_contacts(max_results, target_mailbox)

        if not query:
            raise ToolExecutionError("Query parameter is required (except when source='contacts' to list all)")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            person_service = PersonService(self.ews_client)

            # Map source to PersonService sources
            sources = []
            if source == "all":
                sources = ["gal", "contacts", "email_history"]
            elif source == "gal":
                sources = ["gal"]
            elif source == "contacts":
                sources = ["contacts"]
            elif source == "email_history":
                sources = ["email_history"]
            elif source == "domain":
                sources = ["gal", "email_history"]

            persons = await person_service.find_person(
                query=query,
                sources=sources,
                include_stats=include_stats,
                time_range_days=time_range_days,
                max_results=max_results
            )

            formatted_results = []
            for person in persons:
                result = {
                    "name": person.name,
                    "email": person.primary_email or "",
                    "sources": [s.value for s in person.sources],
                }

                if person.display_name:
                    result["display_name"] = person.display_name
                if person.given_name:
                    result["given_name"] = person.given_name
                if person.surname:
                    result["surname"] = person.surname
                if person.organization:
                    result["company"] = person.organization
                if person.job_title:
                    result["job_title"] = person.job_title
                if person.department:
                    result["department"] = person.department
                if person.office_location:
                    result["office"] = person.office_location

                if person.phone_numbers:
                    result["phone_numbers"] = [
                        {"type": p.type, "number": p.number}
                        for p in person.phone_numbers
                    ]

                if include_stats and person.communication_stats:
                    stats = person.communication_stats
                    result["email_count"] = stats.total_emails
                    result["last_contact"] = stats.last_contact.isoformat() if stats.last_contact else None
                    result["first_contact"] = stats.first_contact.isoformat() if stats.first_contact else None
                else:
                    result["email_count"] = 0

                formatted_results.append(result)

            return format_success_response(
                f"Found {len(formatted_results)} contact(s) for '{query}'",
                query=query,
                source=source,
                total_results=len(formatted_results),
                unified_results=formatted_results,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to search for person: {e}")
            raise ToolExecutionError(f"Failed to search for person: {e}")

    async def _list_contacts(self, max_results: int, target_mailbox) -> Dict[str, Any]:
        """List all personal contacts (replaces get_contacts tool)."""
        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            items = account.contacts.all()[:max_results]

            contacts = []
            for item in items:
                given_name = safe_get(item, "given_name", "") or ""
                surname = safe_get(item, "surname", "") or ""
                display_name = safe_get(item, "display_name", "") or ""
                email_addrs = safe_get(item, "email_addresses", [])

                email = ""
                if email_addrs:
                    email = email_addrs[0].email if hasattr(email_addrs[0], 'email') else ""
                email = email or ""

                from ..utils import ews_id_to_str as _ews_id
                contacts.append({
                    "item_id": _ews_id(safe_get(item, "id", None)) or "unknown",
                    "display_name": display_name or f"{given_name} {surname}".strip(),
                    "given_name": given_name,
                    "surname": surname,
                    "email": email,
                    "company": safe_get(item, "company_name", "") or "",
                    "job_title": safe_get(item, "job_title", "") or ""
                })

            return format_success_response(
                f"Retrieved {len(contacts)} contacts",
                contacts=contacts,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to list contacts: {e}")
            raise ToolExecutionError(f"Failed to list contacts: {e}")


class AnalyzeContactsTool(BaseTool):
    """Unified contact analysis: communication history, network analysis, VIPs, dormant contacts.

    Replaces: get_communication_history, analyze_network.
    """

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "analyze_contacts",
            "description": "Analyze communication history, network patterns, top contacts, VIPs, and dormant relationships.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "analysis_type": {
                        "type": "string",
                        "enum": ["communication_history", "overview", "top_contacts", "by_domain", "dormant", "vip"],
                        "description": "Type of analysis: communication_history (with a specific person), overview/top_contacts/by_domain/dormant/vip (network analysis)"
                    },
                    "email": {
                        "type": "string",
                        "description": "Email address (required for communication_history)"
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "Days back to analyze",
                        "default": 90
                    },
                    "max_emails": {
                        "type": "integer",
                        "description": "Max recent emails to include (communication_history)",
                        "default": 10
                    },
                    "include_topics": {
                        "type": "boolean",
                        "description": "Extract topics from subjects (communication_history)",
                        "default": True
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Number of top results (network analysis)",
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
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["analysis_type"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Route to appropriate analysis type."""
        analysis_type = kwargs.get("analysis_type")

        if analysis_type == "communication_history":
            return await self._communication_history(**kwargs)
        else:
            return await self._network_analysis(**kwargs)

    async def _communication_history(self, **kwargs) -> Dict[str, Any]:
        """Get communication history with a specific contact. Uses server-side sender filter."""
        email = kwargs.get("email", "").strip().lower()
        days_back = kwargs.get("days_back", 365)
        max_emails = kwargs.get("max_emails", 10)
        include_topics = kwargs.get("include_topics", True)
        target_mailbox = kwargs.get("target_mailbox")

        if not email:
            raise ToolExecutionError("email is required for communication_history analysis")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            start_date = datetime.now(account.default_timezone) - timedelta(days=days_back)

            stats = {"total_emails": 0, "received": 0, "sent": 0, "first_contact": None, "last_contact": None}
            timeline = defaultdict(int)
            topics = defaultdict(int)
            recent_emails = []

            MAX_ITEMS_TO_SCAN = 2000

            # 1. Search Inbox — server-side filter by sender email
            inbox = account.inbox
            received_items = inbox.filter(
                datetime_received__gte=start_date,
                sender__email_address=email
            ).order_by('-datetime_received').only('sender', 'subject', 'datetime_received', 'text_body')

            received_list = []
            for item in received_items[:MAX_ITEMS_TO_SCAN]:
                received_list.append(item)

            stats["received"] = len(received_list)

            for item in received_list:
                received_time = safe_get(item, 'datetime_received')
                if received_time:
                    if not stats["first_contact"] or received_time < stats["first_contact"]:
                        stats["first_contact"] = received_time
                    if not stats["last_contact"] or received_time > stats["last_contact"]:
                        stats["last_contact"] = received_time
                    timeline[received_time.strftime("%Y-%m")] += 1
                    if include_topics:
                        self._extract_topics(safe_get(item, 'subject', ''), topics)

            for item in received_list[:max_emails]:
                recent_emails.append({
                    "direction": "received",
                    "subject": safe_get(item, 'subject', ''),
                    "date": safe_get(item, 'datetime_received').isoformat() if safe_get(item, 'datetime_received') else None,
                    "preview": (safe_get(item, 'text_body', '') or '')[:200]
                })

            # 2. Search Sent Items — client-side filter (no server-side recipient filter in EWS)
            sent_items = account.sent
            sent_query = sent_items.filter(
                datetime_sent__gte=start_date
            ).order_by('-datetime_sent').only('to_recipients', 'subject', 'datetime_sent', 'text_body')

            sent_list = []
            items_scanned = 0
            for item in sent_query:
                items_scanned += 1
                if items_scanned > MAX_ITEMS_TO_SCAN:
                    break
                recipients = safe_get(item, 'to_recipients', []) or []
                for recipient in recipients:
                    if safe_get(recipient, 'email_address', '').lower() == email:
                        sent_list.append(item)
                        break

            stats["sent"] = len(sent_list)

            for item in sent_list:
                sent_time = safe_get(item, 'datetime_sent')
                if sent_time:
                    if not stats["first_contact"] or sent_time < stats["first_contact"]:
                        stats["first_contact"] = sent_time
                    if not stats["last_contact"] or sent_time > stats["last_contact"]:
                        stats["last_contact"] = sent_time
                    timeline[sent_time.strftime("%Y-%m")] += 1
                    if include_topics:
                        self._extract_topics(safe_get(item, 'subject', ''), topics)

            sent_list.sort(key=lambda x: safe_get(x, 'datetime_sent', datetime.min), reverse=True)
            for item in sent_list[:max_emails // 2]:
                recent_emails.append({
                    "direction": "sent",
                    "subject": safe_get(item, 'subject', ''),
                    "date": safe_get(item, 'datetime_sent').isoformat() if safe_get(item, 'datetime_sent') else None,
                    "preview": (safe_get(item, 'text_body', '') or '')[:200]
                })

            stats["total_emails"] = stats["received"] + stats["sent"]
            if stats["first_contact"]:
                stats["first_contact"] = stats["first_contact"].isoformat()
            if stats["last_contact"]:
                stats["last_contact"] = stats["last_contact"].isoformat()
            if days_back > 0:
                months = days_back / 30
                stats["emails_per_month"] = round(stats["total_emails"] / months, 1) if months > 0 else 0

            timeline_list = [{"month": m, "count": c} for m, c in sorted(timeline.items())]
            top_topics = sorted(
                [{"topic": t, "count": c} for t, c in topics.items()],
                key=lambda x: x["count"], reverse=True
            )[:10]
            recent_emails.sort(key=lambda x: x["date"] if x["date"] else "", reverse=True)

            return format_success_response(
                f"Communication history with {email}",
                email=email,
                statistics=stats,
                timeline=timeline_list,
                topics=top_topics if include_topics else [],
                recent_emails=recent_emails[:max_emails],
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to get communication history: {e}")
            raise ToolExecutionError(f"Failed to get communication history: {e}")

    def _extract_topics(self, subject: str, topics: Dict[str, int]):
        """Extract keywords from email subject."""
        if not subject:
            return
        subject = re.sub(r'^(RE:|FW:|FWD:)\s*', '', subject, flags=re.IGNORECASE)
        words = re.findall(r'\b[A-Za-z]{3,}\b', subject)
        stop_words = {
            'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can', 'your',
            'from', 'with', 'have', 'this', 'that', 'will', 'was', 'been', 'has'
        }
        for word in words:
            word_lower = word.lower()
            if word_lower not in stop_words and len(word) >= 4:
                key = word if word.isupper() and len(word) <= 5 else word_lower.capitalize()
                topics[key] += 1

    async def _network_analysis(self, **kwargs) -> Dict[str, Any]:
        """Analyze professional network patterns."""
        analysis_type = kwargs.get("analysis_type", "overview")
        days_back = kwargs.get("days_back", 90)
        top_n = kwargs.get("top_n", 20)
        dormant_threshold = kwargs.get("dormant_threshold_days", 60)
        vip_threshold = kwargs.get("vip_email_threshold", 10)
        target_mailbox = kwargs.get("target_mailbox")

        try:
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            contacts = await self._gather_contacts(days_back, account)

            if not contacts:
                return format_success_response(
                    "No contacts found in the specified time range",
                    analysis_type=analysis_type,
                    results=[]
                )

            if analysis_type == "top_contacts":
                results = self._analyze_top_contacts(contacts, top_n)
            elif analysis_type == "by_domain":
                results = self._analyze_by_domain(contacts, top_n)
            elif analysis_type == "dormant":
                results = self._analyze_dormant(contacts, dormant_threshold, days_back, account)
            elif analysis_type == "vip":
                results = self._analyze_vip(contacts, vip_threshold, days_back, account)
            else:
                results = self._analyze_overview(contacts, top_n, dormant_threshold, vip_threshold, days_back, account)

            return format_success_response(
                f"Network analysis: {analysis_type}",
                analysis_type=analysis_type,
                period_days=days_back,
                total_contacts=len(contacts),
                mailbox=mailbox,
                **results
            )

        except Exception as e:
            self.logger.error(f"Failed to analyze network: {e}")
            raise ToolExecutionError(f"Failed to analyze network: {e}")

    async def _gather_contacts(self, days_back: int, account) -> Dict[str, Dict[str, Any]]:
        """Gather all contacts from email history."""
        start_date = datetime.now(account.default_timezone) - timedelta(days=days_back)
        contacts = {}

        inbox = account.inbox
        for item in inbox.filter(datetime_received__gte=start_date).only('sender', 'datetime_received')[:2000]:
            sender = safe_get(item, 'sender')
            if sender:
                email = safe_get(sender, 'email_address', '').lower()
                name = safe_get(sender, 'name', '')
                received_time = safe_get(item, 'datetime_received')
                if email and received_time:
                    if email not in contacts:
                        domain = email.split('@')[1] if '@' in email else 'unknown'
                        contacts[email] = {"email": email, "name": name, "domain": domain, "received": 0, "sent": 0, "last_contact": None, "first_contact": None}
                    contacts[email]["received"] += 1
                    if not contacts[email]["last_contact"] or received_time > contacts[email]["last_contact"]:
                        contacts[email]["last_contact"] = received_time
                    if not contacts[email]["first_contact"] or received_time < contacts[email]["first_contact"]:
                        contacts[email]["first_contact"] = received_time

        sent_items = account.sent
        for item in sent_items.filter(datetime_sent__gte=start_date).only('to_recipients', 'datetime_sent')[:2000]:
            recipients = safe_get(item, 'to_recipients', [])
            sent_time = safe_get(item, 'datetime_sent')
            for recipient in recipients:
                email = safe_get(recipient, 'email_address', '').lower()
                name = safe_get(recipient, 'name', '')
                if email and sent_time:
                    if email not in contacts:
                        domain = email.split('@')[1] if '@' in email else 'unknown'
                        contacts[email] = {"email": email, "name": name, "domain": domain, "received": 0, "sent": 0, "last_contact": None, "first_contact": None}
                    contacts[email]["sent"] += 1
                    if not contacts[email]["last_contact"] or sent_time > contacts[email]["last_contact"]:
                        contacts[email]["last_contact"] = sent_time
                    if not contacts[email]["first_contact"] or sent_time < contacts[email]["first_contact"]:
                        contacts[email]["first_contact"] = sent_time

        for contact in contacts.values():
            contact["total_emails"] = contact["received"] + contact["sent"]
        return contacts

    def _analyze_top_contacts(self, contacts: Dict, top_n: int) -> Dict[str, Any]:
        sorted_contacts = sorted(contacts.values(), key=lambda x: x["total_emails"], reverse=True)[:top_n]
        return {"top_contacts": [
            {"name": c["name"], "email": c["email"], "total_emails": c["total_emails"],
             "received": c["received"], "sent": c["sent"],
             "last_contact": c["last_contact"].isoformat() if c["last_contact"] else None}
            for c in sorted_contacts
        ]}

    def _analyze_by_domain(self, contacts: Dict, top_n: int) -> Dict[str, Any]:
        domains = defaultdict(lambda: {"count": 0, "emails": 0, "contacts": []})
        for c in contacts.values():
            domains[c["domain"]]["count"] += 1
            domains[c["domain"]]["emails"] += c["total_emails"]
            domains[c["domain"]]["contacts"].append({"name": c["name"], "email": c["email"], "total_emails": c["total_emails"]})
        sorted_domains = sorted([
            {"domain": d, "contact_count": info["count"], "total_emails": info["emails"],
             "top_contacts": sorted(info["contacts"], key=lambda x: x["total_emails"], reverse=True)[:5]}
            for d, info in domains.items()
        ], key=lambda x: x["total_emails"], reverse=True)[:top_n]
        return {"domains": sorted_domains}

    def _analyze_dormant(self, contacts: Dict, threshold_days: int, analysis_days: int, account) -> Dict[str, Any]:
        now = datetime.now(account.default_timezone)
        threshold_date = now - timedelta(days=threshold_days)
        dormant = []
        for c in contacts.values():
            if c["last_contact"] and c["last_contact"] < threshold_date and c["total_emails"] >= 3:
                dormant.append({"name": c["name"], "email": c["email"], "total_emails": c["total_emails"],
                                "last_contact": c["last_contact"].isoformat(), "days_since_contact": (now - c["last_contact"]).days})
        dormant.sort(key=lambda x: x["total_emails"], reverse=True)
        return {"dormant_contacts": dormant, "threshold_days": threshold_days}

    def _analyze_vip(self, contacts: Dict, email_threshold: int, analysis_days: int, account) -> Dict[str, Any]:
        now = datetime.now(account.default_timezone)
        recent_threshold = now - timedelta(days=30)
        vips = []
        for c in contacts.values():
            if c["total_emails"] >= email_threshold and c["last_contact"] and c["last_contact"] >= recent_threshold:
                vips.append({"name": c["name"], "email": c["email"], "domain": c["domain"],
                             "total_emails": c["total_emails"], "received": c["received"], "sent": c["sent"],
                             "last_contact": c["last_contact"].isoformat(),
                             "emails_per_day": round(c["total_emails"] / analysis_days, 2)})
        vips.sort(key=lambda x: x["total_emails"], reverse=True)
        return {"vip_contacts": vips, "criteria": f"Minimum {email_threshold} emails and contact within last 30 days"}

    def _analyze_overview(self, contacts: Dict, top_n: int, dormant_threshold: int, vip_threshold: int, analysis_days: int, account) -> Dict[str, Any]:
        top_contacts = self._analyze_top_contacts(contacts, min(top_n, 10))
        domains = self._analyze_by_domain(contacts, min(top_n, 10))
        dormant = self._analyze_dormant(contacts, dormant_threshold, analysis_days, account)
        vips = self._analyze_vip(contacts, vip_threshold, analysis_days, account)
        total_emails = sum(c["total_emails"] for c in contacts.values())
        avg = round(total_emails / len(contacts), 1) if contacts else 0
        return {
            "summary": {"total_contacts": len(contacts), "total_emails": total_emails, "avg_emails_per_contact": avg,
                        "vip_count": len(vips["vip_contacts"]), "dormant_count": len(dormant["dormant_contacts"])},
            "top_contacts": top_contacts["top_contacts"][:5],
            "top_domains": domains["domains"][:5],
            "vip_contacts": vips["vip_contacts"][:5],
            "dormant_contacts": dormant["dormant_contacts"][:5]
        }
