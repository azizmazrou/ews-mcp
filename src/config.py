"""Configuration management for EWS MCP Server."""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, model_validator
from typing import Callable, Literal, Optional


def _make_int_clamp(logger) -> Callable[..., int]:
    """Factory for an int clamp-with-warn helper.

    Returns a function ``(env_name, value, low, high, default)`` that:

    * Returns ``value`` unchanged when it's inside ``[low, high]``.
    * Returns the clamped bound (and logs a WARNING) when it's outside.
    * Returns ``default`` (with a WARNING) when ``value`` is non-numeric
      (zero, negative, or a type that would spin-loop a background task).

    Used by :meth:`Settings.validate_auth_credentials` to tame the
    transport-resilience knobs.
    """

    def _clamp(env_name: str, value: int, low: int, high: int, default: int) -> int:
        try:
            ival = int(value)
        except (TypeError, ValueError):
            logger.warning(
                "%s=%r is not an integer; using default %d", env_name, value, default,
            )
            return default
        if ival < low:
            logger.warning(
                "%s=%d is below minimum %d; clamping up. Very small intervals "
                "risk spinning the event loop.",
                env_name, ival, low,
            )
            return low
        if ival > high:
            logger.warning(
                "%s=%d exceeds maximum %d; clamping down.",
                env_name, ival, high,
            )
            return high
        return ival

    return _clamp


class Settings(BaseSettings):
    """Application settings with validation."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # Exchange settings
    ews_server_url: Optional[str] = None
    ews_email: str
    ews_autodiscover: bool = True

    # Authentication
    ews_auth_type: Literal["oauth2", "basic", "ntlm"] = "oauth2"
    ews_client_id: Optional[str] = None
    ews_client_secret: Optional[str] = None
    ews_tenant_id: Optional[str] = None
    ews_username: Optional[str] = None
    ews_password: Optional[str] = None

    # Server configuration
    mcp_server_name: str = "ews-mcp-server"
    mcp_transport: Literal["stdio", "sse"] = "stdio"
    # Default to localhost. The SSE transport serves an unauthenticated-by-default
    # REST surface; operators must opt in to 0.0.0.0 and should set MCP_API_KEY
    # before doing so. See README "Known limitations".
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    # If set, the SSE/HTTP transport requires this API key in the
    # Authorization: Bearer <key> header (or X-API-Key header) on every
    # non-health endpoint. Required whenever mcp_host is not 127.0.0.1/localhost.
    mcp_api_key: Optional[str] = None
    timezone: str = "UTC"
    log_level: str = "INFO"

    # TLS for EWS/autodiscover. Default is verified TLS (secure). Set to true
    # only for corporate Exchange with self-signed certs and review the risks.
    ews_insecure_skip_verify: bool = False

    # OpenAPI/REST API Configuration
    api_base_url: Optional[str] = None  # External URL for API (e.g., https://api.example.com)
    api_base_url_internal: Optional[str] = None  # Internal Docker URL (e.g., http://ews-mcp:8000)
    api_title: str = "Exchange Web Services (EWS) MCP API"
    api_description: str = "REST API for Exchange operations via Model Context Protocol"
    api_version: str = "3.4.0"

    # Performance
    enable_cache: bool = True
    cache_ttl: int = 300
    connection_pool_size: int = 10
    request_timeout: int = 30

    # Rate limiting
    rate_limit_enabled: bool = True
    rate_limit_requests_per_minute: int = 25

    # Features
    enable_email: bool = True
    enable_calendar: bool = True
    enable_contacts: bool = True
    enable_tasks: bool = True
    enable_folders: bool = True
    enable_attachments: bool = True
    # Agent-secretary features: memory store, commitments, approvals, rules,
    # voice profile, OOF policy, briefing, meeting prep.
    enable_agent: bool = True

    # Security
    enable_audit_log: bool = True
    max_attachment_size: int = 157286400  # 150MB

    # Impersonation/Delegation settings
    ews_impersonation_enabled: bool = Field(
        default=False,
        description="Enable impersonation/delegation support for accessing other mailboxes"
    )
    ews_impersonation_type: Literal["impersonation", "delegate"] = Field(
        default="impersonation",
        description="Type of access: 'impersonation' (service account with ApplicationImpersonation role) or 'delegate' (user delegation)"
    )

    # AI Features
    enable_ai: bool = False
    ai_provider: Literal["openai", "anthropic", "local"] = "openai"
    ai_api_key: Optional[str] = None
    ai_model: Optional[str] = None  # e.g., "gpt-4", "claude-3-5-sonnet-20241022"
    ai_embedding_model: Optional[str] = None  # e.g., "text-embedding-3-small"
    ai_base_url: Optional[str] = None  # For local models or custom endpoints

    # --- Long-lived SSE / transport resilience ---------------------------
    # Keep the SSE byte stream alive across long tool executions so upstream
    # proxies (nginx, HAProxy, cloud LBs) don't reap it as idle. Values are
    # clamped in a validator below; out-of-range settings log a warning but
    # do not crash startup.
    sse_keepalive_enabled: bool = Field(
        default=True,
        description="Emit SSE comment frames to keep the stream alive through proxies.",
    )
    sse_keepalive_interval_seconds: int = Field(
        default=15,
        description="Seconds between SSE comment frames (clamped to [5, 60]).",
    )
    http_keep_alive_timeout_seconds: int = Field(
        default=300,
        description="Uvicorn HTTP keep-alive timeout in seconds (clamped to [30, 900]).",
    )
    tcp_keepalive_enabled: bool = Field(
        default=True,
        description="Set SO_KEEPALIVE (and TCP_KEEPIDLE on Linux) on accepted sockets.",
    )
    tcp_keepalive_idle_seconds: int = Field(
        default=60,
        description="Linux TCP_KEEPIDLE (time before first keepalive probe). Clamped to [30, 600].",
    )

    # Optional MCP progress notifications for long-running tool calls.
    # Distinct from SSE keepalive: those keep the byte stream alive for
    # the proxy; these keep the MCP *session* alive for client-side
    # session watchdogs. Both can run at once.
    progress_notification_enabled: bool = Field(
        default=True,
        description="Periodically send notifications/progress while a tool call is in flight.",
    )
    progress_notification_interval_seconds: int = Field(
        default=10,
        description="Seconds between progress notifications (clamped to [5, 60]).",
    )
    ai_max_tokens: int = 4096
    ai_temperature: float = 0.7
    enable_semantic_search: bool = False
    # On startup, pre-embed recent Inbox + Sent mail in the background so
    # the first semantic_search_emails call doesn't pay the cold-cache
    # tax (45-76s observed in production). Disable if the inbox is huge
    # and you prefer on-demand embedding only.
    enable_embedding_warmup: bool = True
    enable_email_classification: bool = False
    enable_smart_replies: bool = False
    enable_email_summarization: bool = False

    @model_validator(mode='after')
    def validate_auth_credentials(self) -> 'Settings':
        """Validate required credentials based on auth type."""
        if self.ews_auth_type == "oauth2":
            required = {
                "ews_client_id": self.ews_client_id,
                "ews_client_secret": self.ews_client_secret,
                "ews_tenant_id": self.ews_tenant_id
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(f"OAuth2 auth requires: {', '.join(missing)}")
        elif self.ews_auth_type in ("basic", "ntlm"):
            if not self.ews_username or not self.ews_password:
                raise ValueError(f"{self.ews_auth_type.upper()} auth requires ews_username and ews_password")

        # SSE transport binding safety: require API key if not loopback.
        if self.mcp_transport == "sse":
            loopback_hosts = {"127.0.0.1", "::1", "localhost"}
            if self.mcp_host not in loopback_hosts and not self.mcp_api_key:
                raise ValueError(
                    "SSE transport bound to a non-loopback address requires "
                    "MCP_API_KEY to be set. Either set MCP_API_KEY or bind "
                    "MCP_HOST to 127.0.0.1 and put the server behind an "
                    "auth-enforcing reverse proxy."
                )

        # Shared logger for both AI validation + transport clamping.
        import logging as _logging
        _log = _logging.getLogger(__name__)

        # Validate AI settings
        if self.enable_ai:
            if not self.ai_api_key and self.ai_provider != "local":
                raise ValueError(f"AI enabled but ai_api_key not provided for {self.ai_provider}")
            if not self.ai_model:
                if self.ai_provider == "openai":
                    self.ai_model = "gpt-4o-mini"
                elif self.ai_provider == "anthropic":
                    self.ai_model = "claude-3-5-sonnet-20241022"
                if self.ai_model:
                    _log.info(
                        f"AI_MODEL not set; defaulting to {self.ai_model!r} "
                        f"for provider={self.ai_provider}"
                    )
            if self.enable_semantic_search and not self.ai_embedding_model:
                if self.ai_provider == "openai":
                    self.ai_embedding_model = "text-embedding-3-small"
                    _log.info(
                        "AI_EMBEDDING_MODEL not set; defaulting to "
                        "'text-embedding-3-small' for semantic search"
                    )
                elif self.ai_provider == "local":
                    _log.warning(
                        "enable_semantic_search=true with AI_PROVIDER=local but "
                        "AI_EMBEDDING_MODEL is not set. Set AI_EMBEDDING_MODEL "
                        "explicitly for local/OpenAI-compatible endpoints."
                    )
            # Catch the common typo AI_EMBEDDING_MODEL=ollama (or any other
            # provider name). Ollama needs the actual model name, e.g.
            # 'nomic-embed-text'. Warn at load time so operators see it in
            # the startup log instead of on the first semantic_search call.
            if (
                self.enable_semantic_search
                and self.ai_embedding_model
                and self.ai_embedding_model.strip().lower()
                in {"ollama", "openai", "anthropic", "cohere", "voyage", "local"}
            ):
                _log.warning(
                    "AI_EMBEDDING_MODEL=%r looks like a provider name, not a "
                    "model name. Typical values: 'text-embedding-3-small' "
                    "(OpenAI), 'nomic-embed-text' (Ollama), 'bge-m3' "
                    "(BAAI). The upstream will likely return "
                    "'model not found' — update AI_EMBEDDING_MODEL.",
                    self.ai_embedding_model,
                )

        # Clamp the transport-resilience knobs. Out-of-range values are
        # accepted with a warning (never a crash) because the dev's typo
        # shouldn't take the server offline — the clamped value is still
        # safe. Zero or negative intervals would spin-loop the keepalive
        # task and DoS the event loop, so we clamp them firmly.
        _clamp_int = _make_int_clamp(_log)
        self.sse_keepalive_interval_seconds = _clamp_int(
            "SSE_KEEPALIVE_INTERVAL",
            self.sse_keepalive_interval_seconds,
            low=5, high=60, default=15,
        )
        self.http_keep_alive_timeout_seconds = _clamp_int(
            "HTTP_KEEP_ALIVE_TIMEOUT",
            self.http_keep_alive_timeout_seconds,
            low=30, high=900, default=300,
        )
        self.tcp_keepalive_idle_seconds = _clamp_int(
            "TCP_KEEPALIVE_IDLE",
            self.tcp_keepalive_idle_seconds,
            low=30, high=600, default=60,
        )
        self.progress_notification_interval_seconds = _clamp_int(
            "PROGRESS_NOTIFICATION_INTERVAL",
            self.progress_notification_interval_seconds,
            low=5, high=60, default=10,
        )

        return self

    def get_api_base_urls(self) -> list[dict[str, str]]:
        """Get API base URLs for OpenAPI schema.

        Returns list of server URLs with descriptions.
        Falls back to default localhost/docker URLs if not configured.
        """
        servers = []

        # Add external URL if configured
        if self.api_base_url:
            servers.append({
                "url": self.api_base_url,
                "description": "External API endpoint"
            })

        # Add internal Docker URL if configured
        if self.api_base_url_internal:
            servers.append({
                "url": self.api_base_url_internal,
                "description": "Internal Docker network"
            })

        # If no URLs configured, use defaults
        if not servers:
            servers = [
                {
                    "url": f"http://localhost:{self.mcp_port}",
                    "description": "Local development server"
                },
                {
                    "url": f"http://ews-mcp:{self.mcp_port}",
                    "description": "Docker container (internal network)"
                }
            ]

        return servers


# Singleton instance - lazy loading
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create settings instance (lazy loading)."""
    global _settings
    if _settings is None:
        # Pydantic reads required fields from env vars at runtime; mypy
        # can't model that, so silence the "missing argument" error.
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


# Backward compatibility - will only be evaluated when accessed
def __getattr__(name):
    """Lazy attribute access for backward compatibility."""
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
