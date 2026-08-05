"""Environment-driven configuration (12-factor; every knob defaults safe)."""

from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Path fragments that identify cloud-synced folders. The data dir holds
# mail-at-rest (alias DB, audit chain, cache mirror) — it must never ride
# a sync client onto other machines or a vendor cloud.
_SYNCED_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud")


def split_csv(raw: str) -> list:
    """Comma-separated env value → stripped, non-empty items."""
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # --- Exchange upstream -------------------------------------------------
    ews_server_url: str
    ews_email: str
    ews_username: Optional[str] = None
    ews_password: Optional[str] = None
    # NEVER pin auth_type against this Exchange: the front door only works
    # via exchangelib auto-negotiation (verified live 2026-06-12; pinning
    # BASIC/NTLM both fail). Escape hatch for a *different* server only.
    ews_auth_type_force: Optional[Literal["basic", "ntlm", "digest"]] = None
    ews_insecure_skip_verify: bool = False
    ews_tz: str = "Asia/Riyadh"
    request_timeout: int = 30

    # --- Reliability --------------------------------------------------------
    ews_warmup_max_backoff_seconds: int = 300
    ews_heartbeat_seconds: int = 600
    ews_retry_max_wait_seconds: int = 300
    ews_max_concurrency: int = 4
    circuit_failure_threshold: int = 5
    circuit_open_seconds: int = 60

    # --- Safety -------------------------------------------------------------
    ews_capability_tier: Literal["read", "draft", "full"] = "draft"
    send_enabled: bool = False  # kill-switch: v5 defaults SAFE (off)
    send_confirm_secret: Optional[str] = None
    confirm_ttl_seconds: int = 600  # ONE default everywhere (== confirm.DEFAULT_TTL_SECONDS)
    ews_recipient_allowlist: str = ""
    ews_recipient_denylist: str = ""
    ews_max_sends_per_hour: int = 10

    # --- Inbound auth (AUTH_MODE=static is today's behaviour, bit for bit) ---
    # oidc: the caller presents an end-user OIDC access token; the server is an
    # OAuth2 resource server and every gate is evaluated per principal. static:
    # one shared MCP_API_KEY, one mailbox, no identity anywhere.
    auth_mode: Literal["static", "oidc"] = "static"
    auth_issuer: Optional[str] = None
    auth_audience: Optional[str] = None  # THIS server's resource id, never the EWS one
    auth_jwks_url: Optional[str] = None
    auth_allowed_algs: str = "RS256,ES256"  # allowlist; HS* is refused (see validator)
    auth_clock_skew_seconds: int = 60
    auth_jwks_ttl_seconds: int = 3600
    # Cooldown between forced JWKS refetches: without it, spraying unknown
    # `kid`s turns this server into a DoS amplifier against the IdP.
    auth_jwks_min_refetch_seconds: int = 60
    auth_mailbox_claim: str = "upn,email,preferred_username"  # ordered, first non-empty wins
    auth_email_domain_allowlist: str = ""  # fnmatch globs; empty = any domain
    auth_required_scope: str = ""

    # --- Upstream (Exchange) auth in oidc mode ------------------------------
    # obo: exchange the caller's token for an EWS-audience one (recommended).
    # dual_header: the agent supplies both tokens; passthrough: the same token
    # for both, which breaks the resource-server audience rule (guarded).
    auth_upstream_mode: Literal["obo", "dual_header", "passthrough"] = "obo"
    auth_allow_token_passthrough: bool = False
    auth_obo_style: Literal["aad", "rfc8693"] = "aad"
    auth_obo_token_url: Optional[str] = None
    auth_obo_client_id: Optional[str] = None
    auth_obo_client_secret: Optional[str] = None  # env only, never committed
    auth_ews_scope: Optional[str] = None
    auth_token_expiry_margin_seconds: int = 120
    auth_token_cache_max: int = 500

    # --- Per-principal gateway pool -----------------------------------------
    auth_gateway_pool_max: int = 50
    auth_gateway_idle_ttl_seconds: int = 1800
    # EWS_MAX_CONCURRENCY is the SERVER-WIDE pool; this is the per-caller share.
    ews_max_concurrency_per_user: int = 4

    # --- Multi-tenant storage ------------------------------------------------
    auth_data_dir_naming: Literal["hash", "smtp"] = "hash"
    data_dir_namespace_salt: Optional[str] = None  # required in oidc mode
    audit_identity: Literal["hash", "smtp"] = "hash"

    # --- Serving ------------------------------------------------------------
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_api_key: Optional[str] = None
    log_level: str = "INFO"

    # --- Storage (NEVER a synced folder) -------------------------------------
    data_dir: str = ""  # empty → ~/.ewsmcp; always resolved to an absolute path
    data_dir_allow_synced: bool = False  # explicit opt-out of the synced-path guard

    # --- Cache mirror (cache-first reads; false = pure EWS, fully functional)
    ews_cache_enabled: bool = True
    ews_cache_folders: str = "inbox,sent"
    ews_cache_sync_seconds: int = 45
    ews_cache_hierarchy_seconds: int = 600
    ews_cache_window_days: int = 365
    ews_cache_purge_on_boot: bool = False  # admin path: wipe + resync from scratch

    # --- Optional semantic tier (adapter; core stays dependency-free) --------
    ews_semantic_index: Literal["none", "pgvector"] = "none"
    ews_semantic_pg_dsn: Optional[str] = None  # from env only, never committed
    ews_semantic_ollama_url: str = "http://localhost:11434"
    ews_semantic_model: str = "bge-m3"  # 1024-d, Arabic-capable

    # --- Response economy ----------------------------------------------------
    default_page_size: int = Field(default=20, le=50)
    body_max_chars: int = 4000

    @model_validator(mode="after")
    def _resolve_data_dir(self) -> "Settings":
        raw = self.data_dir or str(Path.home() / ".ewsmcp")
        resolved = Path(raw).expanduser().resolve()
        if not self.data_dir_allow_synced:
            lowered = str(resolved).lower()
            marker = next((m for m in _SYNCED_MARKERS if m in lowered), None)
            if marker is not None:
                raise ValueError(
                    f"DATA_DIR {resolved} appears to be inside a cloud-synced "
                    f"folder ({marker!r}). It stores mail-at-rest (aliases, "
                    "audit chain, cache) and must stay local — point DATA_DIR "
                    "at a local path, or set DATA_DIR_ALLOW_SYNCED=true to "
                    "accept the risk deliberately."
                )
        self.data_dir = str(resolved)
        return self

    @model_validator(mode="after")
    def _validate_auth_mode(self) -> "Settings":
        """Refuse to boot on an incoherent auth configuration.

        Every branch here fails CLOSED. A half-configured ``oidc`` server that
        started anyway would either reject everyone or — far worse — fall back
        to the single static mailbox and hand it to unauthenticated callers.
        """
        if any(a.strip().upper().startswith("HS") for a in split_csv(self.auth_allowed_algs)):
            raise ValueError(
                "AUTH_ALLOWED_ALGS must not contain an HS* algorithm: the JWKS "
                "public key would double as the HMAC secret, making token "
                "forgery trivial. Use RS256/ES256."
            )
        if self.auth_mode != "oidc":
            return self
        self._validate_oidc_requirements()
        return self

    @property
    def per_caller_upstream(self) -> bool:
        """True when each caller's own token opens their own mailbox.

        This is the line between "authenticated but one mailbox" and true
        multi-mailbox service, and every multi-tenant guard hangs off it
        rather than off ``auth_mode`` — so per-caller storage, the cache
        refusal and the semantic refusal all come into force together.

        ``dual_header`` and ``passthrough`` also produce a per-caller token,
        so they count too; only a deployment with no upstream token at all
        stays on the single static credential.
        """
        return self.auth_mode == "oidc"

    def _validate_oidc_requirements(self) -> None:
        """Coherence of the oidc knobs, checked before anything is served."""
        missing = [
            name
            for name in ("auth_issuer", "auth_audience", "auth_jwks_url",
                         "data_dir_namespace_salt")
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError(
                "AUTH_MODE=oidc requires " + ", ".join(m.upper() for m in missing)
            )
        if self.mcp_transport == "stdio":
            raise ValueError(
                "AUTH_MODE=oidc needs MCP_TRANSPORT=http: stdio has no HTTP "
                "layer to carry a bearer token, so no caller could be "
                "identified. Local stdio use stays on AUTH_MODE=static."
            )
        if not self.per_caller_upstream:
            # The upstream is still the single static credential: the OBO
            # client is unused and the single-tenant stores stay correct, so
            # none of the guards below applies yet.
            return
        if self.auth_upstream_mode == "obo":
            missing_obo = [
                name
                for name in ("auth_obo_token_url", "auth_obo_client_id",
                             "auth_obo_client_secret", "auth_ews_scope")
                if not getattr(self, name)
            ]
            if missing_obo:
                raise ValueError(
                    "AUTH_UPSTREAM_MODE=obo requires "
                    + ", ".join(m.upper() for m in missing_obo)
                )
        if self.auth_upstream_mode == "passthrough" and not self.auth_allow_token_passthrough:
            raise ValueError(
                "AUTH_UPSTREAM_MODE=passthrough forwards this server's own "
                "audience token to Exchange, which breaks the resource-server "
                "audience rule (confused deputy). Set "
                "AUTH_ALLOW_TOKEN_PASSTHROUGH=true to accept that deliberately, "
                "or use obo/dual_header."
            )
        if self.ews_semantic_index != "none":
            raise ValueError(
                "EWS_SEMANTIC_INDEX must be 'none' once callers open their own "
                "mailboxes: the embeddings table has no tenant column yet, so "
                "one caller's vectors would be searchable by another."
            )



def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
