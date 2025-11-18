"""Person model - First-class entity for EWS MCP v3.0.

A real human being in your professional network.
"""

from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class PersonSource(str, Enum):
    """Source where person information was found."""
    GAL = "gal"  # Global Address List
    CONTACTS = "contacts"  # Personal Contacts
    EMAIL_HISTORY = "email_history"  # Sent/Received emails
    FUZZY_MATCH = "fuzzy_match"  # Fuzzy search result


class EmailAddress(BaseModel):
    """Email address with metadata."""
    address: EmailStr
    label: str = "primary"  # primary, work, personal, etc.
    is_primary: bool = True
    routing_type: str = "SMTP"


class PhoneNumber(BaseModel):
    """Phone number with type."""
    number: str
    type: str = "business"  # business, mobile, home, etc.


class CommunicationStats(BaseModel):
    """Communication statistics with a person."""
    total_emails: int = 0
    emails_sent: int = 0
    emails_received: int = 0
    first_contact: Optional[datetime] = None
    last_contact: Optional[datetime] = None
    emails_per_month: float = 0.0
    response_rate: Optional[float] = None  # 0-1 score
    avg_response_time_hours: Optional[float] = None


class Person(BaseModel):
    """
    A real human being in your professional network.

    This is the core entity in v3.0 person-centric architecture.
    All interactions with people go through this model.
    """

    # Core identity
    id: str = Field(..., description="Unique identifier (usually primary email)")
    name: str = Field(..., description="Full name (e.g., 'Ahmed Al-Rashid')")
    display_name: Optional[str] = Field(None, description="How to address them")
    given_name: Optional[str] = Field(None, description="First name")
    surname: Optional[str] = Field(None, description="Last name")

    # Contact information
    email_addresses: List[EmailAddress] = Field(
        default_factory=list,
        description="Email addresses (primary + aliases)"
    )
    phone_numbers: List[PhoneNumber] = Field(
        default_factory=list,
        description="Phone numbers"
    )

    # Professional information
    organization: Optional[str] = Field(None, description="Company/organization")
    department: Optional[str] = Field(None, description="Department")
    job_title: Optional[str] = Field(None, description="Job title")
    office_location: Optional[str] = Field(None, description="Office location")

    # Relationship data
    communication_stats: Optional[CommunicationStats] = Field(
        None,
        description="Communication history statistics"
    )

    # Source tracking
    sources: List[PersonSource] = Field(
        default_factory=list,
        description="Where this person was found (GAL, contacts, etc.)"
    )
    is_internal: bool = Field(False, description="Same organization?")
    is_vip: bool = Field(False, description="High importance contact?")

    # Metadata
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    # Additional data
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional custom metadata"
    )

    @property
    def primary_email(self) -> Optional[str]:
        """Get primary email address."""
        if self.email_addresses:
            for email in self.email_addresses:
                if email.is_primary:
                    return email.address
            # Fallback to first email
            return self.email_addresses[0].address
        return None

    @property
    def full_name(self) -> str:
        """Get full name with fallback logic."""
        if self.name:
            return self.name
        if self.given_name and self.surname:
            return f"{self.given_name} {self.surname}"
        if self.display_name:
            return self.display_name
        return self.primary_email or "Unknown"

    @property
    def relationship_strength(self) -> float:
        """
        Calculate relationship strength (0-1 score).

        Based on:
        - Email volume
        - Recency of contact
        - Response rate
        - VIP status
        """
        if not self.communication_stats:
            return 0.0

        score = 0.0

        # Email volume (max 0.3)
        email_score = min(self.communication_stats.total_emails / 100, 0.3)
        score += email_score

        # Recency (max 0.3)
        if self.communication_stats.last_contact:
            days_ago = (datetime.now() - self.communication_stats.last_contact).days
            recency_score = max(0, 0.3 * (1 - days_ago / 365))
            score += recency_score

        # Response rate (max 0.2)
        if self.communication_stats.response_rate:
            score += 0.2 * self.communication_stats.response_rate

        # VIP boost (0.2)
        if self.is_vip:
            score += 0.2

        return min(score, 1.0)

    @property
    def source_priority(self) -> int:
        """
        Get source priority for ranking.

        Higher priority = more authoritative source
        """
        priorities = {
            PersonSource.GAL: 100,  # Highest - official directory
            PersonSource.CONTACTS: 80,  # High - manually added
            PersonSource.EMAIL_HISTORY: 60,  # Medium - proven contact
            PersonSource.FUZZY_MATCH: 20,  # Low - uncertain match
        }

        if not self.sources:
            return 0

        return max(priorities.get(source, 0) for source in self.sources)

    def add_source(self, source: PersonSource) -> None:
        """Add a source if not already present."""
        if source not in self.sources:
            self.sources.append(source)
            self.updated_at = datetime.now()

    def merge_with(self, other: "Person") -> "Person":
        """
        Merge with another Person instance.

        Combines information from multiple sources.
        Higher priority sources win conflicts.
        """
        # Use the person with higher priority source as base
        if self.source_priority >= other.source_priority:
            base = self.model_copy(deep=True)
            merge = other
        else:
            base = other.model_copy(deep=True)
            merge = self

        # Merge email addresses
        existing_emails = {e.address.lower() for e in base.email_addresses}
        for email in merge.email_addresses:
            if email.address.lower() not in existing_emails:
                base.email_addresses.append(email)

        # Merge phone numbers
        existing_phones = {p.number for p in base.phone_numbers}
        for phone in merge.phone_numbers:
            if phone.number not in existing_phones:
                base.phone_numbers.append(phone)

        # Merge sources
        for source in merge.sources:
            base.add_source(source)

        # Use more complete professional info
        if not base.organization and merge.organization:
            base.organization = merge.organization
        if not base.department and merge.department:
            base.department = merge.department
        if not base.job_title and merge.job_title:
            base.job_title = merge.job_title
        if not base.office_location and merge.office_location:
            base.office_location = merge.office_location

        # Merge communication stats (sum totals, use latest dates)
        if merge.communication_stats:
            if not base.communication_stats:
                base.communication_stats = merge.communication_stats
            else:
                stats = base.communication_stats
                merge_stats = merge.communication_stats

                stats.total_emails += merge_stats.total_emails
                stats.emails_sent += merge_stats.emails_sent
                stats.emails_received += merge_stats.emails_received

                if merge_stats.last_contact:
                    if not stats.last_contact or merge_stats.last_contact > stats.last_contact:
                        stats.last_contact = merge_stats.last_contact

                if merge_stats.first_contact:
                    if not stats.first_contact or merge_stats.first_contact < stats.first_contact:
                        stats.first_contact = merge_stats.first_contact

        # Merge VIP status (any VIP = VIP)
        base.is_vip = base.is_vip or merge.is_vip

        # Merge metadata
        base.metadata.update(merge.metadata)

        base.updated_at = datetime.now()

        return base

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = self.model_dump()

        # Add computed properties
        data["primary_email"] = self.primary_email
        data["full_name"] = self.full_name
        data["relationship_strength"] = self.relationship_strength
        data["source_priority"] = self.source_priority

        return data

    @classmethod
    def from_gal_result(
        cls,
        mailbox: Any,
        contact_info: Optional[Any] = None
    ) -> "Person":
        """
        Create Person from GAL search result.

        Args:
            mailbox: Mailbox object from resolve_names()
            contact_info: Optional Contact object with extended data
        """
        # Extract email
        email = getattr(mailbox, "email_address", None)
        if not email:
            raise ValueError("Mailbox must have email_address")

        # Extract name
        name = getattr(mailbox, "name", None) or email

        # Create email address
        email_addr = EmailAddress(
            address=email,
            label="primary",
            is_primary=True,
            routing_type=getattr(mailbox, "routing_type", "SMTP")
        )

        # Base person
        person = cls(
            id=email.lower(),
            name=name,
            email_addresses=[email_addr],
            sources=[PersonSource.GAL],
        )

        # Add extended contact info if available
        if contact_info:
            person.display_name = getattr(contact_info, "display_name", None)
            person.given_name = getattr(contact_info, "given_name", None)
            person.surname = getattr(contact_info, "surname", None)
            person.organization = getattr(contact_info, "company_name", None)
            person.department = getattr(contact_info, "department", None)
            person.job_title = getattr(contact_info, "job_title", None)
            person.office_location = getattr(contact_info, "office_location", None)

            # Extract phone numbers
            phone_numbers_raw = getattr(contact_info, "phone_numbers", [])
            if phone_numbers_raw:
                for phone in phone_numbers_raw:
                    phone_number = getattr(phone, "phone_number", None)
                    phone_label = getattr(phone, "label", "business")
                    if phone_number:
                        person.phone_numbers.append(
                            PhoneNumber(number=phone_number, type=phone_label)
                        )

            # Also check individual phone fields
            business_phone = getattr(contact_info, "business_phone", None)
            if business_phone:
                person.phone_numbers.append(
                    PhoneNumber(number=business_phone, type="business")
                )

            mobile_phone = getattr(contact_info, "mobile_phone", None)
            if mobile_phone:
                person.phone_numbers.append(
                    PhoneNumber(number=mobile_phone, type="mobile")
                )

        return person

    @classmethod
    def from_email_contact(
        cls,
        mailbox: Any,
        stats: Optional[CommunicationStats] = None
    ) -> "Person":
        """
        Create Person from email sender/recipient.

        Args:
            mailbox: Mailbox object from email
            stats: Optional communication statistics
        """
        email = getattr(mailbox, "email_address", None)
        if not email:
            raise ValueError("Mailbox must have email_address")

        name = getattr(mailbox, "name", None) or email

        email_addr = EmailAddress(
            address=email,
            label="primary",
            is_primary=True,
            routing_type=getattr(mailbox, "routing_type", "SMTP")
        )

        return cls(
            id=email.lower(),
            name=name,
            email_addresses=[email_addr],
            sources=[PersonSource.EMAIL_HISTORY],
            communication_stats=stats,
        )

    @classmethod
    def from_contact(cls, contact: Any) -> "Person":
        """
        Create Person from Exchange Contact object.

        Args:
            contact: Contact object from contacts folder
        """
        # Get email from contact
        email_addrs = getattr(contact, "email_addresses", [])
        if not email_addrs:
            raise ValueError("Contact must have at least one email address")

        primary_email = email_addrs[0].email if hasattr(email_addrs[0], "email") else str(email_addrs[0])

        # Extract name
        given_name = getattr(contact, "given_name", None)
        surname = getattr(contact, "surname", None)
        display_name = getattr(contact, "display_name", None)

        if given_name and surname:
            name = f"{given_name} {surname}"
        elif display_name:
            name = display_name
        else:
            name = primary_email

        # Create email addresses
        emails = []
        for idx, email_obj in enumerate(email_addrs):
            email = email_obj.email if hasattr(email_obj, "email") else str(email_obj)
            emails.append(EmailAddress(
                address=email,
                label=getattr(email_obj, "label", f"email{idx+1}"),
                is_primary=(idx == 0)
            ))

        # Create person
        person = cls(
            id=primary_email.lower(),
            name=name,
            display_name=display_name,
            given_name=given_name,
            surname=surname,
            email_addresses=emails,
            sources=[PersonSource.CONTACTS],
            organization=getattr(contact, "company_name", None),
            department=getattr(contact, "department", None),
            job_title=getattr(contact, "job_title", None),
        )

        # Extract phone numbers
        phone_numbers_raw = getattr(contact, "phone_numbers", [])
        if phone_numbers_raw:
            for phone in phone_numbers_raw:
                phone_number = getattr(phone, "phone_number", None)
                phone_label = getattr(phone, "label", "business")
                if phone_number:
                    person.phone_numbers.append(
                        PhoneNumber(number=phone_number, type=phone_label)
                    )

        return person
