"""Tests for authentication."""

import pytest
from unittest.mock import Mock, patch

from src.auth import AuthHandler
from src.exceptions import AuthenticationError


def test_basic_auth_credentials(mock_settings):
    """Test basic authentication credentials."""
    mock_settings.ews_auth_type = "basic"
    handler = AuthHandler(mock_settings)

    credentials = handler._get_basic_credentials()

    assert credentials is not None
    assert credentials.username == mock_settings.ews_username
    assert credentials.password == mock_settings.ews_password


def test_oauth2_auth_requires_credentials():
    """Test OAuth2 requires proper credentials."""
    from src.config import Settings

    with pytest.raises(ValueError):
        # Missing OAuth2 credentials should raise error
        Settings(
            ews_email="test@example.com",
            ews_auth_type="oauth2"
            # Missing client_id, client_secret, tenant_id
        )


def test_auth_handler_invalid_type(mock_settings):
    """Test invalid authentication type."""
    mock_settings.ews_auth_type = "invalid"
    handler = AuthHandler(mock_settings)

    with pytest.raises(AuthenticationError):
        handler.get_credentials()
