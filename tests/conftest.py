"""Pytest configuration and fixtures."""

import pytest
from unittest.mock import Mock, MagicMock
from datetime import datetime

from src.config import Settings
from src.auth import AuthHandler
from src.ews_client import EWSClient


@pytest.fixture
def mock_settings():
    """Mock settings for testing."""
    return Settings(
        ews_email="test@example.com",
        ews_auth_type="basic",
        ews_username="test@example.com",
        ews_password="test_password",
        ews_autodiscover=False,
        ews_server_url="https://test.example.com/ews",
        rate_limit_enabled=False,
        enable_audit_log=False
    )


@pytest.fixture
def mock_auth_handler(mock_settings):
    """Mock auth handler."""
    handler = Mock(spec=AuthHandler)
    handler.config = mock_settings
    handler.get_credentials = Mock(return_value=Mock())
    return handler


@pytest.fixture
def mock_ews_client(mock_settings, mock_auth_handler):
    """Mock EWS client."""
    client = Mock(spec=EWSClient)
    client.config = mock_settings
    client.auth_handler = mock_auth_handler

    # Mock account
    mock_account = MagicMock()
    mock_account.inbox = MagicMock()
    mock_account.sent = MagicMock()
    mock_account.calendar = MagicMock()
    mock_account.contacts = MagicMock()
    mock_account.tasks = MagicMock()

    client.account = mock_account
    client.test_connection = Mock(return_value=True)

    return client


@pytest.fixture
def sample_email():
    """Sample email data."""
    return {
        "to": ["recipient@example.com"],
        "subject": "Test Email",
        "body": "This is a test email body",
        "cc": [],
        "bcc": [],
        "importance": "Normal"
    }


@pytest.fixture
def sample_appointment():
    """Sample appointment data."""
    return {
        "subject": "Test Meeting",
        "start_time": datetime(2025, 1, 15, 10, 0).isoformat(),
        "end_time": datetime(2025, 1, 15, 11, 0).isoformat(),
        "location": "Conference Room A",
        "body": "Discuss project status",
        "attendees": ["attendee1@example.com", "attendee2@example.com"]
    }


@pytest.fixture
def sample_contact():
    """Sample contact data."""
    return {
        "given_name": "John",
        "surname": "Doe",
        "email_address": "john.doe@example.com",
        "phone_number": "+1234567890",
        "company": "Acme Corp",
        "job_title": "Software Engineer"
    }


@pytest.fixture
def sample_task():
    """Sample task data."""
    return {
        "subject": "Complete project report",
        "body": "Finish the Q4 report",
        "due_date": datetime(2025, 1, 31, 17, 0).isoformat(),
        "importance": "High"
    }
