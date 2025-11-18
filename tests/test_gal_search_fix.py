"""
Comprehensive tests for GAL search functionality with tuple format handling.

Tests the FindPersonTool from contact_intelligence_tools.py with focus on:
- Tuple format handling: (mailbox, contact_info)
- Phone number extraction
- Arabic/UTF-8 text support
- Search scope isolation (gal vs email_history)
- Error handling and edge cases

All test data uses generic examples (john.doe@company.com, etc.)
"""

import pytest
from unittest.mock import Mock, MagicMock, AsyncMock, patch
from datetime import datetime, timezone

from src.tools.contact_intelligence_tools import FindPersonTool


@pytest.fixture
def mock_ews_client():
    """Create a mock EWS client for testing."""
    client = Mock()
    client.account = Mock()
    client.account.protocol = Mock()
    client.account.default_timezone = timezone.utc
    client.account.inbox = Mock()
    client.account.sent = Mock()
    return client


@pytest.fixture
def find_person_tool(mock_ews_client):
    """Create FindPersonTool instance with mocked client."""
    tool = FindPersonTool(mock_ews_client)
    return tool


class TestGALSearchTupleFormat:
    """Test GAL search with tuple format: (mailbox, contact_info)"""

    @pytest.mark.asyncio
    async def test_gal_search_basic_tuple(self, find_person_tool, mock_ews_client):
        """Test basic GAL search with tuple format returns correct results."""
        # Mock mailbox and contact_info
        mock_mailbox = Mock()
        mock_mailbox.name = "John Smith"
        mock_mailbox.email_address = "john.smith@company.com"
        mock_mailbox.routing_type = "SMTP"

        mock_contact_info = Mock()
        mock_contact_info.display_name = "John Smith"
        mock_contact_info.company_name = "Example Corp"
        mock_contact_info.department = "IT"
        mock_contact_info.job_title = "Software Engineer"
        mock_contact_info.given_name = "John"
        mock_contact_info.surname = "Smith"

        # Tuple format: (mailbox, contact_info)
        mock_result = (mock_mailbox, mock_contact_info)

        # Configure mock to return results
        mock_ews_client.account.protocol.resolve_names.return_value = [mock_result]
        mock_ews_client.account.inbox.filter.return_value.order_by.return_value.only.return_value = []
        mock_ews_client.account.sent.filter.return_value.order_by.return_value.only.return_value = []

        # Execute search
        result = await find_person_tool.execute(query="Smith", search_scope="gal")

        # Assertions
        assert result['success'] is True
        assert result['total_results'] == 1
        assert len(result['unified_results']) == 1

        contact = result['unified_results'][0]
        assert contact['name'] == "John Smith"
        assert contact['email'] == "john.smith@company.com"
        assert 'gal' in contact['sources']
        assert contact['company'] == "Example Corp"
        assert contact['department'] == "IT"
        assert contact['job_title'] == "Software Engineer"

    @pytest.mark.asyncio
    async def test_gal_search_with_phone_numbers(self, find_person_tool, mock_ews_client):
        """Test GAL search extracts phone numbers correctly."""
        mock_mailbox = Mock()
        mock_mailbox.name = "Jane Doe"
        mock_mailbox.email_address = "jane.doe@company.com"
        mock_mailbox.routing_type = "SMTP"

        # Mock phone numbers
        mock_phone1 = Mock()
        mock_phone1.label = "BusinessPhone"
        mock_phone1.phone_number = "+1-555-0100"

        mock_phone2 = Mock()
        mock_phone2.label = "MobilePhone"
        mock_phone2.phone_number = "+1-555-0101"

        mock_contact_info = Mock()
        mock_contact_info.display_name = "Jane Doe"
        mock_contact_info.phone_numbers = [mock_phone1, mock_phone2]
        mock_contact_info.business_phone = "+1-555-0100"
        mock_contact_info.mobile_phone = "+1-555-0101"

        mock_result = (mock_mailbox, mock_contact_info)
        mock_ews_client.account.protocol.resolve_names.return_value = [mock_result]
        mock_ews_client.account.inbox.filter.return_value.order_by.return_value.only.return_value = []
        mock_ews_client.account.sent.filter.return_value.order_by.return_value.only.return_value = []

        result = await find_person_tool.execute(query="Doe", search_scope="gal")

        assert result['success'] is True
        contact = result['unified_results'][0]
        assert 'phone_numbers' in contact
        assert len(contact['phone_numbers']) == 2
        assert contact['phone_numbers'][0]['type'] == "BusinessPhone"
        assert contact['phone_numbers'][0]['number'] == "+1-555-0100"
        assert contact['business_phone'] == "+1-555-0100"
        assert contact['mobile_phone'] == "+1-555-0101"

    @pytest.mark.asyncio
    async def test_gal_search_arabic_text(self, find_person_tool, mock_ews_client):
        """Test GAL search works with Arabic/UTF-8 text."""
        mock_mailbox = Mock()
        mock_mailbox.name = "أحمد محمد Ahmed Mohammed"
        mock_mailbox.email_address = "ahmed.mohammed@company.com"
        mock_mailbox.routing_type = "SMTP"

        mock_contact_info = Mock()
        mock_contact_info.display_name = "أحمد محمد"
        mock_contact_info.given_name = "أحمد"
        mock_contact_info.surname = "محمد"

        mock_result = (mock_mailbox, mock_contact_info)
        mock_ews_client.account.protocol.resolve_names.return_value = [mock_result]
        mock_ews_client.account.inbox.filter.return_value.order_by.return_value.only.return_value = []
        mock_ews_client.account.sent.filter.return_value.order_by.return_value.only.return_value = []

        result = await find_person_tool.execute(query="أحمد", search_scope="gal")

        assert result['success'] is True
        assert result['total_results'] >= 1
        contact = result['unified_results'][0]
        assert "أحمد" in contact['name']

    @pytest.mark.asyncio
    async def test_gal_search_tuple_without_contact_info(self, find_person_tool, mock_ews_client):
        """Test GAL search handles tuple with None contact_info."""
        mock_mailbox = Mock()
        mock_mailbox.name = "Bob Johnson"
        mock_mailbox.email_address = "bob.johnson@company.com"
        mock_mailbox.routing_type = "SMTP"

        # Tuple with None as contact_info
        mock_result = (mock_mailbox, None)
        mock_ews_client.account.protocol.resolve_names.return_value = [mock_result]
        mock_ews_client.account.inbox.filter.return_value.order_by.return_value.only.return_value = []
        mock_ews_client.account.sent.filter.return_value.order_by.return_value.only.return_value = []

        result = await find_person_tool.execute(query="Johnson", search_scope="gal")

        assert result['success'] is True
        assert result['total_results'] == 1
        contact = result['unified_results'][0]
        assert contact['name'] == "Bob Johnson"
        assert contact['email'] == "bob.johnson@company.com"

    @pytest.mark.asyncio
    async def test_gal_search_multiple_results(self, find_person_tool, mock_ews_client):
        """Test GAL search with multiple results."""
        # Create multiple mock results
        results = []
        for i in range(3):
            mock_mailbox = Mock()
            mock_mailbox.name = f"User {i}"
            mock_mailbox.email_address = f"user{i}@company.com"
            mock_mailbox.routing_type = "SMTP"

            mock_contact_info = Mock()
            mock_contact_info.display_name = f"User {i}"
            mock_contact_info.department = f"Department {i}"

            results.append((mock_mailbox, mock_contact_info))

        mock_ews_client.account.protocol.resolve_names.return_value = results
        mock_ews_client.account.inbox.filter.return_value.order_by.return_value.only.return_value = []
        mock_ews_client.account.sent.filter.return_value.order_by.return_value.only.return_value = []

        result = await find_person_tool.execute(query="User", search_scope="gal")

        assert result['success'] is True
        assert result['total_results'] == 3
        assert len(result['unified_results']) == 3


class TestSearchScopeIsolation:
    """Test that search scopes are properly isolated."""

    @pytest.mark.asyncio
    async def test_gal_scope_excludes_email_history(self, find_person_tool, mock_ews_client):
        """Verify scope='gal' only searches GAL, not email history."""
        mock_mailbox = Mock()
        mock_mailbox.name = "GAL User"
        mock_mailbox.email_address = "gal.user@company.com"
        mock_mailbox.routing_type = "SMTP"

        mock_result = (mock_mailbox, None)
        mock_ews_client.account.protocol.resolve_names.return_value = [mock_result]

        # Email history should not be called
        mock_ews_client.account.inbox.filter.return_value.order_by.return_value.only.return_value = []
        mock_ews_client.account.sent.filter.return_value.order_by.return_value.only.return_value = []

        result = await find_person_tool.execute(query="User", search_scope="gal")

        # Verify only GAL sources
        assert result['success'] is True
        for contact in result['unified_results']:
            assert 'gal' in contact['sources']
            assert 'email_history' not in contact['sources']

    @pytest.mark.asyncio
    async def test_email_history_scope_excludes_gal(self, find_person_tool, mock_ews_client):
        """Verify scope='email_history' only searches email history, not GAL."""
        # GAL should not be called when scope is email_history
        mock_ews_client.account.protocol.resolve_names.return_value = []

        # Mock email history
        mock_inbox_item = Mock()
        mock_sender = Mock()
        mock_sender.name = "Email User"
        mock_sender.email_address = "email.user@company.com"
        mock_inbox_item.sender = mock_sender
        mock_inbox_item.datetime_received = datetime.now(timezone.utc)

        mock_ews_client.account.inbox.filter.return_value.order_by.return_value.only.return_value = [mock_inbox_item]
        mock_ews_client.account.sent.filter.return_value.order_by.return_value.only.return_value = []

        result = await find_person_tool.execute(query="User", search_scope="email_history")

        # GAL resolve_names should not have been called
        mock_ews_client.account.protocol.resolve_names.assert_not_called()

        # Verify only email_history sources
        for contact in result['unified_results']:
            assert 'email_history' in contact['sources']
            assert 'gal' not in contact['sources']


class TestErrorHandling:
    """Test error handling and edge cases."""

    @pytest.mark.asyncio
    async def test_gal_search_no_results(self, find_person_tool, mock_ews_client):
        """Test GAL search with no results."""
        mock_ews_client.account.protocol.resolve_names.return_value = []
        mock_ews_client.account.inbox.filter.return_value.order_by.return_value.only.return_value = []
        mock_ews_client.account.sent.filter.return_value.order_by.return_value.only.return_value = []

        result = await find_person_tool.execute(query="NonexistentUser", search_scope="gal")

        assert result['success'] is True
        assert result['total_results'] == 0
        assert len(result['unified_results']) == 0

    @pytest.mark.asyncio
    async def test_gal_search_with_malformed_result(self, find_person_tool, mock_ews_client):
        """Test GAL search handles malformed results gracefully."""
        # Create a malformed result (missing attributes)
        mock_mailbox = Mock()
        mock_mailbox.name = "Valid User"
        mock_mailbox.email_address = "valid@company.com"
        mock_mailbox.routing_type = "SMTP"

        # Second result is malformed (empty mock)
        mock_bad_mailbox = Mock()
        del mock_bad_mailbox.name
        del mock_bad_mailbox.email_address

        results = [
            (mock_mailbox, None),
            (mock_bad_mailbox, None)  # This should be handled gracefully
        ]

        mock_ews_client.account.protocol.resolve_names.return_value = results
        mock_ews_client.account.inbox.filter.return_value.order_by.return_value.only.return_value = []
        mock_ews_client.account.sent.filter.return_value.order_by.return_value.only.return_value = []

        result = await find_person_tool.execute(query="User", search_scope="gal")

        # Should still succeed with at least one valid result
        assert result['success'] is True
        assert result['total_results'] >= 1

    @pytest.mark.asyncio
    async def test_empty_query(self, find_person_tool):
        """Test that empty query raises error."""
        with pytest.raises(Exception):  # Should raise ToolExecutionError
            await find_person_tool.execute(query="", search_scope="gal")


class TestOfficeLocationExtraction:
    """Test office location field extraction."""

    @pytest.mark.asyncio
    async def test_office_location_extraction(self, find_person_tool, mock_ews_client):
        """Test that office location is extracted from contact_info."""
        mock_mailbox = Mock()
        mock_mailbox.name = "Office User"
        mock_mailbox.email_address = "office.user@company.com"
        mock_mailbox.routing_type = "SMTP"

        mock_contact_info = Mock()
        mock_contact_info.display_name = "Office User"
        mock_contact_info.office_location = "Building A, Floor 3, Room 301"

        mock_result = (mock_mailbox, mock_contact_info)
        mock_ews_client.account.protocol.resolve_names.return_value = [mock_result]
        mock_ews_client.account.inbox.filter.return_value.order_by.return_value.only.return_value = []
        mock_ews_client.account.sent.filter.return_value.order_by.return_value.only.return_value = []

        result = await find_person_tool.execute(query="Office", search_scope="gal")

        assert result['success'] is True
        contact = result['unified_results'][0]
        assert 'office' in contact
        assert contact['office'] == "Building A, Floor 3, Room 301"


class TestMaxResultsLimit:
    """Test max_results parameter."""

    @pytest.mark.asyncio
    async def test_max_results_limit(self, find_person_tool, mock_ews_client):
        """Test that max_results parameter limits returned results."""
        # Create 10 mock results
        results = []
        for i in range(10):
            mock_mailbox = Mock()
            mock_mailbox.name = f"User {i}"
            mock_mailbox.email_address = f"user{i}@company.com"
            mock_mailbox.routing_type = "SMTP"
            results.append((mock_mailbox, None))

        mock_ews_client.account.protocol.resolve_names.return_value = results
        mock_ews_client.account.inbox.filter.return_value.order_by.return_value.only.return_value = []
        mock_ews_client.account.sent.filter.return_value.order_by.return_value.only.return_value = []

        # Request only 5 results
        result = await find_person_tool.execute(query="User", search_scope="gal", max_results=5)

        assert result['success'] is True
        assert len(result['unified_results']) == 5
