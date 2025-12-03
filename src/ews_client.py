"""Exchange Web Services client wrapper."""

from exchangelib import Account, Configuration, DELEGATE, Version, EWSTimeZone
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import logging
import pytz
from typing import Optional
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

                # Determine if ews_server_url is a full EWS endpoint or just a server hostname
                # Full endpoint: https://mail.example.com/EWS/Exchange.asmx
                # Server only: mail.example.com or outlook.office365.com
                ews_url = self.config.ews_server_url

                if '/EWS/' in ews_url or ews_url.endswith('.asmx'):
                    # Full EWS endpoint URL - use service_endpoint
                    self.logger.info(f"Using full EWS endpoint: {ews_url}")
                    config = Configuration(
                        service_endpoint=ews_url,
                        credentials=credentials,
                        retry_policy=None,  # Disable built-in retry, we handle it
                        max_connections=self.config.connection_pool_size
                    )
                else:
                    # Just a server hostname - let exchangelib construct the URL
                    # Strip protocol if present
                    server = ews_url.replace('https://', '').replace('http://', '').rstrip('/')
                    self.logger.info(f"Using server hostname: {server}")
                    config = Configuration(
                        server=server,
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
        """Close connection and cleanup."""
        if self._account:
            self.logger.info("Closing EWS connection")
            self._account.protocol.close()
            self._account = None
