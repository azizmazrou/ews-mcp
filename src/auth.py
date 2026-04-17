"""Authentication handlers for Exchange Web Services."""

from exchangelib import Credentials, OAuth2Credentials, NTLM
import logging

from .config import Settings
from .exceptions import AuthenticationError


class AuthHandler:
    """Handle different Exchange authentication methods."""

    def __init__(self, config: Settings):
        self.config = config
        self.logger = logging.getLogger(__name__)

    def get_credentials(self) -> Credentials:
        """Get appropriate credentials based on auth type."""

        try:
            if self.config.ews_auth_type == "oauth2":
                return self._get_oauth2_credentials()
            elif self.config.ews_auth_type == "basic":
                return self._get_basic_credentials()
            elif self.config.ews_auth_type == "ntlm":
                return self._get_ntlm_credentials()
            else:
                raise ValueError(f"Unsupported auth type: {self.config.ews_auth_type}")
        except Exception as e:
            self.logger.error(f"Failed to get credentials: {e}")
            raise AuthenticationError(f"Authentication setup failed: {e}")

    def _get_oauth2_credentials(self) -> OAuth2Credentials:
        """Build OAuth2 credentials. exchangelib manages the token lifecycle
        internally via MSAL when given client_id/client_secret/tenant_id;
        pre-fetching a token here only added latency and a failure surface.
        """
        try:
            return OAuth2Credentials(
                client_id=self.config.ews_client_id,
                client_secret=self.config.ews_client_secret,
                tenant_id=self.config.ews_tenant_id,
                identity=self.config.ews_email
            )
        except Exception as e:
            self.logger.error(f"OAuth2 authentication failed: {e}")
            raise AuthenticationError(f"OAuth2 setup failed: {e}")

    def _get_basic_credentials(self) -> Credentials:
        """Get basic auth credentials."""
        try:
            return Credentials(
                username=self.config.ews_username,
                password=self.config.ews_password
            )
        except Exception as e:
            self.logger.error(f"Basic auth setup failed: {e}")
            raise AuthenticationError(f"Basic auth failed: {e}")

    def _get_ntlm_credentials(self) -> Credentials:
        """Get NTLM credentials."""
        try:
            return Credentials(
                username=self.config.ews_username,
                password=self.config.ews_password
            )
        except Exception as e:
            self.logger.error(f"NTLM auth setup failed: {e}")
            raise AuthenticationError(f"NTLM auth failed: {e}")

    def refresh_token(self) -> None:
        """Token refresh is handled inside exchangelib. Kept for API stability."""
        if self.config.ews_auth_type == "oauth2":
            self.logger.debug("OAuth2 token refresh is handled by exchangelib")
