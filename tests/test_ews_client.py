"""Tests for EWS client."""

import pytest
from unittest.mock import Mock, patch, MagicMock

from src.ews_client import EWSClient
from src.exceptions import ConnectionError


def test_ews_client_initialization(mock_settings, mock_auth_handler):
    """Test EWS client initialization."""
    client = EWSClient(mock_settings, mock_auth_handler)

    assert client.config == mock_settings
    assert client.auth_handler == mock_auth_handler
    assert client._account is None


def test_ews_client_lazy_account_loading(mock_settings, mock_auth_handler):
    """Test lazy loading of account."""
    with patch.object(EWSClient, '_create_account') as mock_create:
        mock_account = MagicMock()
        mock_create.return_value = mock_account

        client = EWSClient(mock_settings, mock_auth_handler)

        # Account should not be created yet
        assert client._account is None

        # Accessing account should trigger creation
        account = client.account

        assert account == mock_account
        mock_create.assert_called_once()


def test_ews_client_connection_test(mock_settings, mock_auth_handler):
    """Test connection testing."""
    with patch.object(EWSClient, '_create_account') as mock_create:
        mock_account = MagicMock()
        mock_account.inbox.total_count = 10
        mock_create.return_value = mock_account

        client = EWSClient(mock_settings, mock_auth_handler)

        result = client.test_connection()

        assert result is True


def test_ews_client_connection_failure(mock_settings, mock_auth_handler):
    """Test connection failure handling."""
    from unittest.mock import PropertyMock
    with patch.object(EWSClient, '_create_account') as mock_create:
        mock_account = MagicMock()
        type(mock_account.inbox).total_count = PropertyMock(side_effect=Exception("Connection failed"))
        mock_create.return_value = mock_account

        client = EWSClient(mock_settings, mock_auth_handler)

        result = client.test_connection()

        assert result is False
