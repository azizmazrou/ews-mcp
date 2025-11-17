"""Authentication handlers for Exchange Web Services."""

from exchangelib import Credentials, OAuth2Credentials, NTLM
from msal import ConfidentialClientApplication
from typing import Optional
import logging

from .config import Settings
from .exceptions import AuthenticationError


class AuthHandler:
    """Handle different Exchange authentication methods."""

    def __init__(self, config: Settings):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self._token_cache: Optional[str] = None

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
        """Get OAuth2 credentials using MSAL."""
        try:
            # Create MSAL app
            app = ConfidentialClientApplication(
                client_id=self.config.ews_client_id,
                client_credential=self.config.ews_client_secret,
                authority=f"https://login.microsoftonline.com/{self.config.ews_tenant_id}"
            )

            # Get token
            scopes = ["https://outlook.office365.com/.default"]
            result = app.acquire_token_for_client(scopes=scopes)

            if "access_token" not in result:
                error = result.get("error_description", "Unknown error")
                raise AuthenticationError(f"Failed to acquire OAuth2 token: {error}")

            access_token = result["access_token"]
            self._token_cache = access_token

            # Return OAuth2 credentials
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
        """Refresh OAuth2 token if needed."""
        if self.config.ews_auth_type == "oauth2":
            self.logger.info("Refreshing OAuth2 token")
            self._get_oauth2_credentials()
