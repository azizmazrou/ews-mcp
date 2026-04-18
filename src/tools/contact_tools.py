"""Contact operation tools for EWS MCP Server."""

from typing import Any, Dict
from exchangelib import Contact
from exchangelib.indexed_properties import EmailAddress, PhoneNumber

from .base import BaseTool
from ..models import CreateContactRequest
from ..exceptions import ToolExecutionError
from ..utils import format_success_response, safe_get, ews_id_to_str


class CreateContactTool(BaseTool):
    """Tool for creating contacts."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "create_contact",
            "description": (
                "Create a new contact. Provide ``given_name`` + ``surname`` "
                "(preferred) OR ``full_name`` (split on first space as a "
                "deprecated alias)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "given_name": {
                        "type": "string",
                        "description": "First name (preferred)"
                    },
                    "surname": {
                        "type": "string",
                        "description": "Last name (preferred)"
                    },
                    "full_name": {
                        "type": "string",
                        "description": (
                            "Deprecated alias. If supplied and given_name/"
                            "surname are missing, the string is split on the "
                            "first space into given_name + surname."
                        )
                    },
                    "email_address": {
                        "type": "string",
                        "description": "Email address"
                    },
                    "phone_number": {
                        "type": "string",
                        "description": "Phone number (optional)"
                    },
                    "company": {
                        "type": "string",
                        "description": "Company name (optional)"
                    },
                    "job_title": {
                        "type": "string",
                        "description": "Job title (optional)"
                    },
                    "department": {
                        "type": "string",
                        "description": "Department (optional)"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                # full_name is accepted as a deprecated alias that fills in
                # given_name / surname before validation. The declared
                # required set stays as the canonical trio so schema-aware
                # clients keep generating the right shape.
                "required": ["given_name", "surname", "email_address"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Create contact."""
        target_mailbox = kwargs.get("target_mailbox")

        # Bug 6: accept ``full_name`` as a deprecated alias. Split on the
        # first whitespace run so "Alice Al-Rashid" -> given="Alice",
        # surname="Al-Rashid", and "Alice" -> given="Alice", surname="".
        full_name = kwargs.pop("full_name", None)
        if full_name and not kwargs.get("given_name") and not kwargs.get("surname"):
            import re as _re
            parts = _re.split(r"\s+", str(full_name).strip(), maxsplit=1)
            kwargs["given_name"] = parts[0]
            kwargs["surname"] = parts[1] if len(parts) > 1 else ""
            self.logger.info(
                "create_contact: full_name -> given_name=%r surname=%r",
                kwargs["given_name"], kwargs["surname"],
            )

        # Validate input
        request = self.validate_input(CreateContactRequest, **kwargs)

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Generate display_name from provided names (required by exchangelib)
            if request.given_name and request.surname:
                display_name = f"{request.given_name} {request.surname}"
            elif request.given_name:
                display_name = request.given_name
            elif request.surname:
                display_name = request.surname
            else:
                # Fallback to email username if no name provided
                display_name = request.email_address.split('@')[0]

            # Create contact
            contact = Contact(
                account=account,
                folder=account.contacts,
                given_name=request.given_name,
                surname=request.surname,
                display_name=display_name,
            )

            # Add email address
            contact.email_addresses = [
                EmailAddress(email=request.email_address, label='EmailAddress1')
            ]

            # Set optional fields
            if request.phone_number:
                contact.phone_numbers = [PhoneNumber(label='BusinessPhone', phone_number=request.phone_number)]

            if request.company:
                contact.company_name = request.company

            if request.job_title:
                contact.job_title = request.job_title

            if request.department:
                contact.department = request.department

            # Save contact
            contact.save()

            self.logger.info(f"Created contact: {request.given_name} {request.surname}")

            return format_success_response(
                "Contact created successfully",
                item_id=ews_id_to_str(contact.id) if hasattr(contact, "id") else None,
                display_name=f"{request.given_name} {request.surname}",
                email=request.email_address,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to create contact: {e}")
            raise ToolExecutionError(f"Failed to create contact: {e}")


class UpdateContactTool(BaseTool):
    """Tool for updating contacts."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "update_contact",
            "description": "Update an existing contact.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Contact item ID"
                    },
                    "given_name": {
                        "type": "string",
                        "description": "New first name (optional)"
                    },
                    "surname": {
                        "type": "string",
                        "description": "New last name (optional)"
                    },
                    "email_address": {
                        "type": "string",
                        "description": "New email address (optional)"
                    },
                    "phone_number": {
                        "type": "string",
                        "description": "New phone number (optional)"
                    },
                    "company": {
                        "type": "string",
                        "description": "New company name (optional)"
                    },
                    "job_title": {
                        "type": "string",
                        "description": "New job title (optional)"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["item_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Update contact."""
        item_id = kwargs.get("item_id")
        target_mailbox = kwargs.get("target_mailbox")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get the contact
            contact = account.contacts.get(id=item_id)

            # Update fields
            if "given_name" in kwargs:
                contact.given_name = kwargs["given_name"]

            if "surname" in kwargs:
                contact.surname = kwargs["surname"]

            if "email_address" in kwargs:
                contact.email_addresses = [
                    EmailAddress(email=kwargs["email_address"], label='EmailAddress1')
                ]

            if "phone_number" in kwargs:
                contact.phone_numbers = [PhoneNumber(label='BusinessPhone', phone_number=kwargs["phone_number"])]

            if "company" in kwargs:
                contact.company_name = kwargs["company"]

            if "job_title" in kwargs:
                contact.job_title = kwargs["job_title"]

            # Save changes
            contact.save()

            self.logger.info(f"Updated contact {item_id}")

            return format_success_response(
                "Contact updated successfully",
                item_id=item_id,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to update contact: {e}")
            raise ToolExecutionError(f"Failed to update contact: {e}")


class DeleteContactTool(BaseTool):
    """Tool for deleting contacts."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_contact",
            "description": "Delete a contact.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Contact item ID to delete"
                    },
                    "target_mailbox": {
                        "type": "string",
                        "description": "Email address to operate on (requires impersonation/delegate access)"
                    }
                },
                "required": ["item_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Delete contact."""
        item_id = kwargs.get("item_id")
        target_mailbox = kwargs.get("target_mailbox")

        try:
            # Get account (primary or impersonated)
            account = self.get_account(target_mailbox)
            mailbox = self.get_mailbox_info(target_mailbox)

            # Get and delete the contact
            contact = account.contacts.get(id=item_id)
            contact.delete()

            self.logger.info(f"Deleted contact {item_id}")

            return format_success_response(
                "Contact deleted successfully",
                item_id=item_id,
                mailbox=mailbox
            )

        except Exception as e:
            self.logger.error(f"Failed to delete contact: {e}")
            raise ToolExecutionError(f"Failed to delete contact: {e}")


