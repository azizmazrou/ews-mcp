"""Exchange Web Services client wrapper."""

from exchangelib import Account, Configuration, DELEGATE, IMPERSONATION, Version, EWSTimeZone
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import logging
import pytz
from typing import Optional, Dict
import urllib3

from .config import Settings
from .auth import AuthHandler
from .exceptions import ConnectionError, AuthenticationError

# Suppress SSL warnings when using NoVerifyHTTPAdapter
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class EWSClient:
    """Exchange Web Services client wrapper with connection management."""

    def __init__(self, config: Settings, auth_handler: AuthHandler):
        self.config = config
        self.auth_handler = auth_handler
        self.logger = logging.getLogger(__name__)
        self._account: Optional[Account] = None
        self._impersonated_accounts: Dict[str, Account] = {}

        # Configure exchangelib
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

    @property
    def account(self) -> Account:
        """Lazy load account connection."""
        if self._account is None:
            self._account = self._create_account()
        return self._account

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, Exception))
    )
    def _create_account(self) -> Account:
        """Create Exchange account with retry logic."""
        try:
            self.logger.info(f"Connecting to Exchange for {self.config.ews_email}")
            self.logger.info(f"Using timezone: {self.config.timezone}")

            # Get credentials
            credentials = self.auth_handler.get_credentials()

            # Get timezone - use EWSTimeZone from exchangelib
            try:
                tz = EWSTimeZone(self.config.timezone)
                self.logger.info(f"Successfully loaded timezone: {self.config.timezone}")
            except Exception as e:
                self.logger.warning(f"Failed to load timezone {self.config.timezone}, falling back to UTC: {e}")
                tz = EWSTimeZone('UTC')

            # Use autodiscovery or manual configuration
            if self.config.ews_autodiscover:
                self.logger.info("Using autodiscovery")

                # Set timeout for autodiscovery
                BaseProtocol.TIMEOUT = self.config.request_timeout

                account = Account(
                    primary_smtp_address=self.config.ews_email,
                    credentials=credentials,
                    autodiscover=True,
                    access_type=DELEGATE,
                    default_timezone=tz
                )
            else:
                if not self.config.ews_server_url:
                    raise ConnectionError("EWS_SERVER_URL required when autodiscover is disabled")

                self.logger.info(f"Using manual configuration: {self.config.ews_server_url}")

                # Construct the full EWS endpoint URL
                # Users can provide:
                #   - Full URL: https://mail.company.com/EWS/Exchange.asmx
                #   - Partial URL: https://mail.company.com
                #   - Just hostname: mail.company.com
                ews_input = self.config.ews_server_url.strip()

                # Normalize the URL to always be a full EWS endpoint
                if ews_input.endswith('/EWS/Exchange.asmx'):
                    # Already a full endpoint
                    ews_url = ews_input
                elif '/EWS/' in ews_input:
                    # Has /EWS/ but might be missing Exchange.asmx
                    if not ews_input.endswith('.asmx'):
                        ews_url = ews_input.rstrip('/') + '/Exchange.asmx'
                    else:
                        ews_url = ews_input
                else:
                    # Just a hostname or URL without /EWS/ path
                    # Strip protocol and trailing slashes
                    server = ews_input.replace('https://', '').replace('http://', '').rstrip('/')
                    # Construct full EWS endpoint URL
                    ews_url = f"https://{server}/EWS/Exchange.asmx"

                self.logger.info(f"Using EWS endpoint: {ews_url}")

                # Always use service_endpoint to bypass autodiscovery completely
                config = Configuration(
                    service_endpoint=ews_url,
                    credentials=credentials,
                    retry_policy=None,  # Disable built-in retry, we handle it
                    max_connections=self.config.connection_pool_size
                )

                # Set timeout on the protocol
                BaseProtocol.TIMEOUT = self.config.request_timeout

                account = Account(
                    primary_smtp_address=self.config.ews_email,
                    config=config,
                    autodiscover=False,
                    access_type=DELEGATE,
                    default_timezone=tz
                )

            # Test the connection
            _ = account.root.tree()
            self.logger.info("Successfully connected to Exchange")

            return account

        except AuthenticationError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to create account: {e}")
            raise ConnectionError(f"Failed to connect to Exchange: {e}")

    def test_connection(self) -> bool:
        """Test EWS connection."""
        try:
            # Try a simple operation
            _ = self.account.inbox.total_count
            self.logger.info("Connection test successful")
            return True
        except Exception as e:
            self.logger.error(f"Connection test failed: {e}")
            return False

    def close(self) -> None:
        """Close all connections and cleanup."""
        # Close impersonated accounts first
        self.clear_impersonation_cache()

        # Close primary account
        if self._account:
            self.logger.info("Closing EWS connection")
            self._account.protocol.close()
            self._account = None

    def get_account(self, target_mailbox: Optional[str] = None) -> Account:
        """
        Get Exchange account, optionally for a different mailbox.

        Args:
            target_mailbox: Email address to impersonate/delegate.
                           If None, returns primary account.

        Returns:
            Account object for the specified mailbox

        Raises:
            ConnectionError: If impersonation is not enabled or fails
        """
        # Return primary account if no target specified
        if not target_mailbox or target_mailbox.lower() == self.config.ews_email.lower():
            return self.account

        # Check if impersonation is enabled
        if not self.config.ews_impersonation_enabled:
            raise ConnectionError(
                f"Impersonation not enabled. Set EWS_IMPERSONATION_ENABLED=true "
                f"to access mailbox: {target_mailbox}"
            )

        # Check cache first
        cache_key = target_mailbox.lower()
        if cache_key in self._impersonated_accounts:
            self.logger.debug(f"Using cached account for {target_mailbox}")
            return self._impersonated_accounts[cache_key]

        # Create impersonated account
        try:
            self.logger.info(f"Creating impersonated account for {target_mailbox}")

            # Determine access type
            access_type = (
                IMPERSONATION
                if self.config.ews_impersonation_type == "impersonation"
                else DELEGATE
            )

            # Get timezone
            try:
                tz = EWSTimeZone(self.config.timezone)
            except Exception:
                tz = EWSTimeZone('UTC')

            # Get credentials
            credentials = self.auth_handler.get_credentials()

            # Create account with same config but different target
            if self.config.ews_autodiscover:
                impersonated_account = Account(
                    primary_smtp_address=target_mailbox,
                    credentials=credentials,
                    autodiscover=True,
                    access_type=access_type,
                    default_timezone=tz
                )
            else:
                # Reuse existing configuration with same endpoint
                config = Configuration(
                    service_endpoint=self._get_ews_url(),
                    credentials=credentials,
                    retry_policy=None,
                    max_connections=self.config.connection_pool_size
                )

                impersonated_account = Account(
                    primary_smtp_address=target_mailbox,
                    config=config,
                    autodiscover=False,
                    access_type=access_type,
                    default_timezone=tz
                )

            # Test connection
            _ = impersonated_account.root.tree()

            # Cache the account
            self._impersonated_accounts[cache_key] = impersonated_account
            self.logger.info(f"Successfully connected to mailbox: {target_mailbox}")

            return impersonated_account

        except Exception as e:
            self.logger.error(f"Failed to access mailbox {target_mailbox}: {e}")
            raise ConnectionError(
                f"Failed to access mailbox {target_mailbox}: {e}. "
                f"Ensure the service account has ApplicationImpersonation role "
                f"or delegate access to this mailbox."
            )

    def _get_ews_url(self) -> str:
        """Get normalized EWS endpoint URL."""
        ews_input = self.config.ews_server_url.strip()
        if ews_input.endswith('/EWS/Exchange.asmx'):
            return ews_input
        elif '/EWS/' in ews_input:
            if not ews_input.endswith('.asmx'):
                return ews_input.rstrip('/') + '/Exchange.asmx'
            return ews_input
        else:
            server = ews_input.replace('https://', '').replace('http://', '').rstrip('/')
            return f"https://{server}/EWS/Exchange.asmx"

    def clear_impersonation_cache(self) -> None:
        """Clear cached impersonated accounts."""
        for email, account in self._impersonated_accounts.items():
            try:
                account.protocol.close()
                self.logger.debug(f"Closed impersonated connection for {email}")
            except Exception:
                pass
        self._impersonated_accounts.clear()
        self.logger.info("Impersonation cache cleared")
