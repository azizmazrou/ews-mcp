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
            "description": "Create a new contact in Exchange",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "given_name": {
                        "type": "string",
                        "description": "First name"
                    },
                    "surname": {
                        "type": "string",
                        "description": "Last name"
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
                    }
                },
                "required": ["given_name", "surname", "email_address"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Create contact."""
        # Validate input
        request = self.validate_input(CreateContactRequest, **kwargs)

        try:
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
                account=self.ews_client.account,
                folder=self.ews_client.account.contacts,
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
                email=request.email_address
            )

        except Exception as e:
            self.logger.error(f"Failed to create contact: {e}")
            raise ToolExecutionError(f"Failed to create contact: {e}")


class SearchContactsTool(BaseTool):
    """Tool for searching contacts."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "search_contacts",
            "description": "Search contacts by name or email",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (name or email)"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results",
                        "default": 50,
                        "maximum": 1000
                    }
                },
                "required": ["query"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Search contacts."""
        query = kwargs.get("query", "")
        max_results = kwargs.get("max_results", 50)

        try:
            # Search in contacts folder
            items = self.ews_client.account.contacts.all()

            # Apply filter
            # Note: exchangelib has limited query capabilities for contacts
            # We'll filter in memory for broader compatibility
            contacts = []
            count = 0

            for item in items:
                if count >= max_results:
                    break

                # Check if query matches
                given_name = safe_get(item, "given_name", "") or ""
                surname = safe_get(item, "surname", "") or ""
                display_name = safe_get(item, "display_name", "") or ""
                email_addrs = safe_get(item, "email_addresses", [])

                # Get email from list
                email = ""
                if email_addrs:
                    email = email_addrs[0].email if hasattr(email_addrs[0], 'email') else ""
                email = email or ""  # Ensure email is never None

                # Match query
                query_lower = query.lower()
                if (query_lower in given_name.lower() or
                    query_lower in surname.lower() or
                    query_lower in display_name.lower() or
                    query_lower in email.lower()):

                    contact_data = {
                        "item_id": ews_id_to_str(safe_get(item, "id", None)) or "unknown",
                        "display_name": display_name or f"{given_name} {surname}".strip(),
                        "given_name": given_name,
                        "surname": surname,
                        "email": email,
                        "company": safe_get(item, "company_name", "") or "",
                        "job_title": safe_get(item, "job_title", "") or ""
                    }
                    contacts.append(contact_data)
                    count += 1

            self.logger.info(f"Found {len(contacts)} matching contacts")

            return format_success_response(
                f"Found {len(contacts)} matching contacts",
                contacts=contacts
            )

        except Exception as e:
            self.logger.error(f"Failed to search contacts: {e}")
            raise ToolExecutionError(f"Failed to search contacts: {e}")


class GetContactsTool(BaseTool):
    """Tool for listing all contacts."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "get_contacts",
            "description": "List all contacts",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of contacts to retrieve",
                        "default": 50,
                        "maximum": 1000
                    }
                }
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Get all contacts."""
        max_results = kwargs.get("max_results", 50)

        try:
            # Get contacts
            items = self.ews_client.account.contacts.all()[:max_results]

            contacts = []
            for item in items:
                given_name = safe_get(item, "given_name", "") or ""
                surname = safe_get(item, "surname", "") or ""
                display_name = safe_get(item, "display_name", "") or ""
                email_addrs = safe_get(item, "email_addresses", [])

                email = ""
                if email_addrs:
                    email = email_addrs[0].email if hasattr(email_addrs[0], 'email') else ""
                email = email or ""  # Ensure email is never None

                contact_data = {
                    "item_id": ews_id_to_str(safe_get(item, "id", None)) or "unknown",
                    "display_name": display_name or f"{given_name} {surname}".strip(),
                    "given_name": given_name,
                    "surname": surname,
                    "email": email,
                    "company": safe_get(item, "company_name", "") or "",
                    "job_title": safe_get(item, "job_title", "") or ""
                }
                contacts.append(contact_data)

            self.logger.info(f"Retrieved {len(contacts)} contacts")

            return format_success_response(
                f"Retrieved {len(contacts)} contacts",
                contacts=contacts
            )

        except Exception as e:
            self.logger.error(f"Failed to get contacts: {e}")
            raise ToolExecutionError(f"Failed to get contacts: {e}")


class UpdateContactTool(BaseTool):
    """Tool for updating contacts."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "update_contact",
            "description": "Update an existing contact",
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
                    }
                },
                "required": ["item_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Update contact."""
        item_id = kwargs.get("item_id")

        try:
            # Get the contact
            contact = self.ews_client.account.contacts.get(id=item_id)

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
                item_id=item_id
            )

        except Exception as e:
            self.logger.error(f"Failed to update contact: {e}")
            raise ToolExecutionError(f"Failed to update contact: {e}")


class DeleteContactTool(BaseTool):
    """Tool for deleting contacts."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "delete_contact",
            "description": "Delete a contact",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Contact item ID to delete"
                    }
                },
                "required": ["item_id"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Delete contact."""
        item_id = kwargs.get("item_id")

        try:
            # Get and delete the contact
            contact = self.ews_client.account.contacts.get(id=item_id)
            contact.delete()

            self.logger.info(f"Deleted contact {item_id}")

            return format_success_response(
                "Contact deleted successfully",
                item_id=item_id
            )

        except Exception as e:
            self.logger.error(f"Failed to delete contact: {e}")
            raise ToolExecutionError(f"Failed to delete contact: {e}")


class ResolveNamesTool(BaseTool):
    """Tool for resolving partial names or email addresses."""

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "resolve_names",
            "description": "Resolve partial names or email addresses to full contact information from Active Directory or Contacts",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name_query": {
                        "type": "string",
                        "description": "Partial name or email address to resolve"
                    },
                    "return_full_info": {
                        "type": "boolean",
                        "description": "Return detailed contact information",
                        "default": False
                    },
                    "search_scope": {
                        "type": "string",
                        "enum": ["Contacts", "ActiveDirectory", "All"],
                        "description": "Where to search for the name",
                        "default": "All"
                    }
                },
                "required": ["name_query"]
            }
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Resolve names to contacts."""
        name_query = kwargs.get("name_query")
        return_full_info = kwargs.get("return_full_info", False)
        search_scope = kwargs.get("search_scope", "All")

        if not name_query:
            raise ToolExecutionError("name_query is required")

        try:
            # Use exchangelib's resolve_names method
            resolved = self.ews_client.account.protocol.resolve_names(
                names=[name_query],
                return_full_contact_data=return_full_info
            )

            # Format results
            results = []
            for resolution in resolved:
                # KEY FIX: result is a tuple (mailbox, contact_info)
                if resolution and isinstance(resolution, tuple):
                    mailbox = resolution[0]
                    contact_info = resolution[1] if len(resolution) > 1 else None

                    if mailbox:
                        result = {
                            "name": safe_get(mailbox, 'name', ''),
                            "email": safe_get(mailbox, 'email_address', ''),
                            "routing_type": safe_get(mailbox, 'routing_type', 'SMTP'),
                            "mailbox_type": safe_get(mailbox, 'mailbox_type', 'Mailbox')
                        }

                        # Add contact details if available and requested
                        if return_full_info and contact_info:
                            contact_details = {
                                "given_name": safe_get(contact_info, 'given_name', ''),
                                "surname": safe_get(contact_info, 'surname', ''),
                                "display_name": safe_get(contact_info, 'display_name', ''),
                                "company": safe_get(contact_info, 'company_name', ''),
                                "job_title": safe_get(contact_info, 'job_title', ''),
                                "department": safe_get(contact_info, 'department', ''),
                                "office": safe_get(contact_info, 'office_location', '')
                            }

                            # Extract phone numbers properly
                            if hasattr(contact_info, 'phone_numbers'):
                                phone_numbers = safe_get(contact_info, 'phone_numbers', [])
                                if phone_numbers:
                                    contact_details["phone_numbers"] = []
                                    for phone in phone_numbers:
                                        if hasattr(phone, 'phone_number'):
                                            contact_details["phone_numbers"].append({
                                                "type": safe_get(phone, 'label', 'Unknown'),
                                                "number": safe_get(phone, 'phone_number', '')
                                            })
                            else:
                                contact_details["phone_numbers"] = []

                            # Add additional phone fields if available
                            if hasattr(contact_info, 'business_phone'):
                                contact_details["business_phone"] = safe_get(contact_info, 'business_phone', '')
                            if hasattr(contact_info, 'mobile_phone'):
                                contact_details["mobile_phone"] = safe_get(contact_info, 'mobile_phone', '')

                            result["contact_details"] = contact_details

                        results.append(result)
                # Fallback: Handle old format (object with .mailbox attribute)
                elif resolution:
                    mailbox = resolution.mailbox if hasattr(resolution, 'mailbox') else None

                    if mailbox:
                        result = {
                            "name": safe_get(mailbox, 'name', ''),
                            "email": safe_get(mailbox, 'email_address', ''),
                            "routing_type": safe_get(mailbox, 'routing_type', 'SMTP'),
                            "mailbox_type": safe_get(mailbox, 'mailbox_type', 'Mailbox')
                        }

                        # Add contact details if available and requested
                        if return_full_info and hasattr(resolution, 'contact'):
                            contact = resolution.contact
                            result["contact_details"] = {
                                "given_name": safe_get(contact, 'given_name'),
                                "surname": safe_get(contact, 'surname'),
                                "company": safe_get(contact, 'company_name'),
                                "job_title": safe_get(contact, 'job_title'),
                                "phone_numbers": safe_get(contact, 'phone_numbers', []),
                                "department": safe_get(contact, 'department')
                            }

                        results.append(result)

            self.logger.info(f"Resolved {len(results)} match(es) for '{name_query}'")

            return format_success_response(
                f"Found {len(results)} match(es) for '{name_query}'",
                query=name_query,
                results=results,
                count=len(results)
            )

        except Exception as e:
            self.logger.error(f"Failed to resolve names: {e}")
            raise ToolExecutionError(f"Failed to resolve names: {e}")
