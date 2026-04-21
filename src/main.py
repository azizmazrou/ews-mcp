"""Main MCP Server implementation for Exchange Web Services."""

import asyncio
import hmac
import logging
import os
import socket
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route

from .config import get_settings
from .auth import AuthHandler
from .ews_client import EWSClient
from .middleware.logging import setup_logging, AuditLogger
from .middleware.error_handler import ErrorHandler
from .middleware.rate_limiter import RateLimiter
from .exceptions import EWSMCPException
from .logging_system import get_logger
from .openapi_adapter import OpenAPIAdapter
from .utils import safe_json_dumps

# Import all tool classes (67 total: 63 base + 4 optional AI)
from .tools import (
    CreateDraftTool, CreateReplyDraftTool, CreateForwardDraftTool,
    SendEmailTool, ReadEmailsTool, SearchEmailsTool, GetEmailDetailsTool,
    DeleteEmailTool, MoveEmailTool, UpdateEmailTool, CopyEmailTool,
    ReplyEmailTool, ForwardEmailTool,
    # Calendar tools (7)
    CreateAppointmentTool, GetCalendarTool, UpdateAppointmentTool,
    DeleteAppointmentTool, RespondToMeetingTool, CheckAvailabilityTool,
    FindMeetingTimesTool,
    # Contact tools (3) — search/get/resolve merged into FindPersonTool
    CreateContactTool, UpdateContactTool, DeleteContactTool,
    # Task tools (5)
    CreateTaskTool, GetTasksTool, UpdateTaskTool,
    CompleteTaskTool, DeleteTaskTool,
    # Attachment tools (7)
    ListAttachmentsTool, DownloadAttachmentTool,
    AddAttachmentTool, DeleteAttachmentTool, ReadAttachmentTool,
    GetEmailMimeTool, AttachEmailToDraftTool,
    # Search tools (1) — advanced_search/full_text_search merged into search_emails
    SearchByConversationTool,
    # Folder tools (3) — create/delete/rename/move merged into manage_folder
    ListFoldersTool, FindFolderTool, ManageFolderTool,
    # Out-of-Office tools (1) — get/set merged into oof_settings
    OofSettingsTool,
    # AI tools (4 - conditionally enabled)
    SemanticSearchEmailsTool, ClassifyEmailTool,
    SummarizeEmailTool, SuggestRepliesTool,
    # Contact Intelligence tools (2) — communication_history/analyze_network merged into analyze_contacts
    FindPersonTool, AnalyzeContactsTool,
    # --- Agent-secretary tools ---
    # Memory (4)
    MemorySetTool, MemoryGetTool, MemoryListTool, MemoryDeleteTool,
    # Commitments (4)
    TrackCommitmentTool, ListCommitmentsTool, ResolveCommitmentTool,
    ExtractCommitmentsTool,
    # Approvals (5)
    SubmitForApprovalTool, ListPendingApprovalsTool, ApproveTool, RejectTool,
    ExecuteApprovedActionTool,
    # Voice profile (2)
    BuildVoiceProfileTool, GetVoiceProfileTool,
    # Rule engine (5)
    RuleCreateTool, RuleListTool, RuleDeleteTool, RuleSimulateTool,
    EvaluateRulesOnMessageTool,
    # OOF policy (3)
    ConfigureOOFPolicyTool, GetOOFPolicyTool, ApplyOOFPolicyTool,
    # Compound tools (2)
    GenerateBriefingTool, PrepareMeetingTool,
)


# Errors that indicate the Exchange connection pool simply wasn't
# ready yet. Used by the embedding warmup's retry loop to distinguish
# "Exchange not ready" (retry) from "schema problem" (give up).
_TRANSIENT_ERROR_NAMES = (
    "ConnectionError",
    "RemoteDisconnected",
    "ProtocolError",
    "ReadTimeout",
    "ConnectTimeout",
    "TimeoutError",
    "SSLError",
    "MaxRetryError",
    "ChunkedEncodingError",
)


def _is_transient_error(errors) -> bool:
    """Heuristic: any of the per-folder errors look retryable?"""
    for _name, exc in errors:
        exc_type = type(exc).__name__
        if exc_type in _TRANSIENT_ERROR_NAMES:
            return True
        # Some exchangelib errors wrap the real exception — check the
        # message too.
        msg = str(exc)
        if any(tok in msg for tok in (
            "Connection aborted", "RemoteDisconnected",
            "Max retries exceeded", "Connection refused",
            "timed out",
        )):
            return True
    return False


# ---------------------------------------------------------------------------
# SSE transport resilience helpers
# ---------------------------------------------------------------------------
#
# Background. The MCP SDK's ``SseServerTransport`` writes Server-Sent-Events
# directly on the ASGI send callable. Long tool executions can sit idle for
# 60-240s without emitting events, which triggers proxy idle timeouts and
# closes the stream. The client's POST to /messages then fires against a
# dead session and we see ``ClosedResourceError``.
#
# Fix: emit SSE *comment* frames (``: keepalive <epoch>\n\n``) on a fixed
# interval. Comment frames are ignored by spec-compliant clients but keep
# proxies and load balancers from reaping the stream. To avoid interleaving
# with the SDK's own event writes (which would corrupt HTTP/1.1 chunked
# encoding), every send call goes through a per-connection ``asyncio.Lock``.
#
# The helpers below are module-level so they can be unit-tested without
# spinning up the server. Each is narrowly typed; the ASGI ``send`` callable
# is typed as ``Callable[[Dict[str, Any]], Awaitable[None]]``.
# ---------------------------------------------------------------------------


# ASGI send signature. The ASGI spec types the message as a
# ``MutableMapping[str, Any]`` (not a concrete ``dict``) — using
# ``MutableMapping`` here so mypy is happy when we pass our wrapped
# callable into ``sse.connect_sse``.
from collections.abc import MutableMapping
ASGISend = Callable[[MutableMapping[str, Any]], Awaitable[None]]

# Errors that mean "peer has gone away". Module-level so both the SSE
# keepalive loop and the progress loop can reference the same set.
# ``anyio.ClosedResourceError`` is imported lazily to avoid a hard import
# dependency when anyio isn't present (it ships with the MCP SDK, so in
# practice it always is).
_PEER_GONE_ERROR_NAMES = frozenset({
    "BrokenResourceError",
    "ClosedResourceError",
    "ConnectionResetError",
    "BrokenPipeError",
    "EndOfStream",
})


def _peer_gone(exc: BaseException) -> bool:
    """True if ``exc`` looks like a client-disconnect, not a server bug.

    Comparison is by class name + a few strings so we don't have to pin
    anyio import order.
    """
    name = type(exc).__name__
    if name in _PEER_GONE_ERROR_NAMES:
        return True
    msg = str(exc)
    return any(tok in msg for tok in (
        "Broken pipe", "Connection reset", "Connection aborted",
        "closed resource",
    ))


# Module-level counter of active SSE connections. Used for /health and as
# a cheap leak detector in tests. Protected by a threading.Lock because
# ASGI apps can run under multi-threaded workers in other deployments;
# even though uvicorn here is single-threaded-asyncio, the lock is cheap
# and keeps the read/increment atomic.
_ACTIVE_SSE: Dict[str, int] = {"count": 0}
_ACTIVE_SSE_LOCK = threading.Lock()


def _sse_active_count() -> int:
    """Return the current number of live SSE connections."""
    with _ACTIVE_SSE_LOCK:
        return _ACTIVE_SSE["count"]


def _sse_active_inc() -> None:
    with _ACTIVE_SSE_LOCK:
        _ACTIVE_SSE["count"] += 1


def _sse_active_dec() -> None:
    with _ACTIVE_SSE_LOCK:
        if _ACTIVE_SSE["count"] > 0:
            _ACTIVE_SSE["count"] -= 1


# SSE proxy-buster headers. Some reverse proxies (nginx in particular)
# buffer responses until EOF unless explicitly told otherwise — that
# buffering defeats the whole point of SSE. ``X-Accel-Buffering: no`` is
# the nginx-specific opt-out. ``Cache-Control: no-transform`` prevents
# gzip'ing the stream (a chunked gzip response waits for EOF too).
_SSE_PROXY_HEADERS: Tuple[Tuple[bytes, bytes], ...] = (
    (b"cache-control", b"no-cache, no-transform"),
    (b"x-accel-buffering", b"no"),
    (b"connection", b"keep-alive"),
)


def _merge_sse_headers(existing: Iterable[Tuple[bytes, bytes]]) -> List[List[bytes]]:
    """Return the SDK-provided headers plus our proxy-buster set.

    Does not overwrite a header the SDK already supplied (case-insensitive
    match). Always returns mutable list-of-lists so it can be fed back to
    ``send``.
    """
    # Normalize to list so ASGI accepts it.
    out: List[List[bytes]] = []
    seen: set = set()
    for name, value in existing:
        nlower = name.lower() if isinstance(name, bytes) else str(name).encode().lower()
        seen.add(nlower)
        out.append([name if isinstance(name, bytes) else str(name).encode(),
                    value if isinstance(value, bytes) else str(value).encode()])
    for name, value in _SSE_PROXY_HEADERS:
        if name not in seen:
            out.append([name, value])
    return out


def _wrap_send_with_sse_headers(send: ASGISend) -> Tuple[ASGISend, asyncio.Event, asyncio.Lock]:
    """Wrap ``send`` so the first http.response.start gets the SSE
    proxy-buster headers, and so every write is serialised through a
    per-connection ``asyncio.Lock``.

    Returns:
        wrapped_send: drop-in replacement for the ASGI send callable.
        headers_sent: event set once the first ``http.response.start``
            has been flushed — the keepalive loop waits on it before
            sending its first comment frame (sending a comment before
            headers would 500 the request).
        send_lock: the lock shared by wrapped_send and the keepalive
            loop. Two writers on raw ``send`` would corrupt the
            HTTP/1.1 chunked body.
    """
    headers_sent = asyncio.Event()
    send_lock = asyncio.Lock()

    async def _wrapped(message: MutableMapping[str, Any]) -> None:
        if message.get("type") == "http.response.start":
            # Clone the message so we don't mutate the SDK's dict.
            merged: Dict[str, Any] = dict(message)
            merged["headers"] = _merge_sse_headers(message.get("headers") or [])
            async with send_lock:
                await send(merged)
            headers_sent.set()
            return
        async with send_lock:
            await send(message)

    return _wrapped, headers_sent, send_lock


async def _keepalive_loop(
    send: ASGISend,
    send_lock: asyncio.Lock,
    headers_sent: asyncio.Event,
    interval_seconds: int,
    logger: logging.Logger,
) -> None:
    """Emit SSE comment frames every ``interval_seconds`` until cancelled.

    * Waits for ``headers_sent`` before the first write — sending a
      comment before the response headers would 500 the request.
    * Every frame is a plain ASCII ``: keepalive <epoch>\\n\\n``. No
      user-controlled content reaches this function (defence against
      header injection).
    * On peer-disconnect errors, exits cleanly at DEBUG so log level
      INFO stays quiet in normal operation.
    * If the send_lock is held (the SDK is mid-write), skip this tick
      rather than queue — a slow client must not be able to pin server
      memory by backlogging keepalives.

    ``send`` here is the *wrapped* callable returned by
    :func:`_wrap_send_with_sse_headers`; it takes the ``send_lock``
    internally. We therefore read ``send_lock.locked()`` only to decide
    whether to skip this tick, and never acquire the lock ourselves
    (that would deadlock against the wrapper).
    """
    try:
        await headers_sent.wait()
    except asyncio.CancelledError:
        raise

    # Hard-cap the interval locally too — the Settings validator clamps,
    # but a direct caller (tests, future refactor) shouldn't be able to
    # pass 0 and spin-loop the event loop.
    interval = max(1, int(interval_seconds))

    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

        # Opportunistic: if the SDK is writing, skip this tick. This keeps
        # us from queuing up backlog during a burst.
        if send_lock.locked():
            continue

        frame = f": keepalive {int(time.time())}\n\n".encode("ascii")
        try:
            await send({
                "type": "http.response.body",
                "body": frame,
                "more_body": True,
            })
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if _peer_gone(exc):
                logger.debug("sse keepalive: peer closed (%s); exiting loop",
                             type(exc).__name__)
                return
            logger.warning("sse keepalive: unexpected %s: %s",
                           type(exc).__name__, exc)
            return


def _enable_tcp_keepalive(
    sock: socket.socket, idle_seconds: int, logger: logging.Logger,
) -> None:
    """Turn on OS-level TCP keepalive for an accepted socket.

    * ``SO_KEEPALIVE`` is always set when supported.
    * On Linux, also set ``TCP_KEEPIDLE`` / ``TCP_KEEPINTVL`` /
      ``TCP_KEEPCNT`` so the first probe fires at ``idle_seconds`` and
      we give up after ~3 lost probes (default 9). This matches proxy
      idle timeouts in the 30-120s range.
    * On Windows, use ``SIO_KEEPALIVE_VALS``.
    * On anything else, the ``SO_KEEPALIVE`` call is still made; the
      platform-specific tuning is skipped silently.

    All ``setsockopt`` calls are guarded with ``hasattr`` so this
    function never crashes on a platform that lacks a constant.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError as exc:
        logger.debug("SO_KEEPALIVE failed: %s", exc)
        return

    if sys.platform.startswith("linux"):
        for attr, value in (
            ("TCP_KEEPIDLE", max(1, int(idle_seconds))),
            ("TCP_KEEPINTVL", 15),
            ("TCP_KEEPCNT", 3),
        ):
            opt = getattr(socket, attr, None)
            if opt is None:
                continue
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
            except OSError as exc:
                logger.debug("setsockopt %s=%d failed: %s", attr, value, exc)
    elif sys.platform == "win32" and hasattr(socket, "SIO_KEEPALIVE_VALS"):
        try:
            # (onoff, keepalivetime_ms, keepaliveinterval_ms)
            sock.ioctl(
                socket.SIO_KEEPALIVE_VALS,
                (1, max(1, int(idle_seconds)) * 1000, 15 * 1000),
            )
        except OSError as exc:
            logger.debug("SIO_KEEPALIVE_VALS failed: %s", exc)


def _authorized_request(
    headers: Iterable[Tuple[bytes, bytes]], api_key: Optional[str],
) -> bool:
    """Constant-time auth check for the Bearer / X-API-Key headers.

    ``hmac.compare_digest`` is used so a wrong key of the correct length
    doesn't leak prefix information via string-comparison timing.

    Returns True if ``api_key`` is unset (no auth configured) or any
    supported header contains the configured key. Never logs the
    presented value; callers get False and a generic 401.
    """
    if not api_key:
        return True
    expected = api_key.encode("utf-8")
    for name, value in headers or []:
        name_lower = name.lower() if isinstance(name, bytes) else str(name).encode().lower()
        raw = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        if name_lower == b"authorization":
            if raw.lower().startswith("bearer "):
                candidate = raw[7:].strip().encode("utf-8")
                if hmac.compare_digest(candidate, expected):
                    return True
        elif name_lower == b"x-api-key":
            candidate = raw.strip().encode("utf-8")
            if hmac.compare_digest(candidate, expected):
                return True
    return False


class EWSMCPServer:
    """MCP Server for Exchange Web Services with comprehensive logging."""

    def __init__(self):
        # Get settings (lazy loading)
        self.settings = get_settings()

        # Set timezone
        os.environ['TZ'] = self.settings.timezone
        try:
            import time
            time.tzset()
        except AttributeError:
            # tzset not available on Windows
            pass

        # Set up logging
        setup_logging(self.settings.log_level)
        self.logger = logging.getLogger(__name__)

        # Initialize comprehensive logging system
        self.log_manager = get_logger()

        # Initialize server
        self.server = Server(self.settings.mcp_server_name)

        # Initialize components
        self.auth_handler = AuthHandler(self.settings)
        self.ews_client = EWSClient(self.settings, self.auth_handler)
        self.error_handler = ErrorHandler()
        self.audit_logger = AuditLogger()

        # Rate limiter (if enabled)
        self.rate_limiter = None
        if self.settings.rate_limit_enabled:
            self.rate_limiter = RateLimiter(self.settings.rate_limit_requests_per_minute)

        # Tool registry
        self.tools = {}

        # OpenAPI adapter (initialized after tools are registered)
        self.openapi_adapter = None

        # Register handlers
        self._register_handlers()

        # Log server initialization
        self.log_manager.log_activity(
            level="INFO",
            module="main",
            action="SERVER_INIT",
            data={
                "version": "3.4.0",
                "user": self.settings.ews_email,
                "auth_type": self.settings.ews_auth_type,
                "server_url": self.settings.ews_server_url or "autodiscover"
            },
            result={"status": "initializing"},
            context={
                "timezone": self.settings.timezone,
                "transport": self.settings.mcp_transport,
                "features": {
                    "email": self.settings.enable_email,
                    "calendar": self.settings.enable_calendar,
                    "contacts": self.settings.enable_contacts,
                    "tasks": self.settings.enable_tasks
                }
            }
        )

    def _register_handlers(self):
        """Register MCP protocol handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """List all available tools."""
            return [
                Tool(
                    name=tool.get_schema()["name"],
                    description=tool.get_schema()["description"],
                    inputSchema=tool.get_schema()["inputSchema"]
                )
                for tool in self.tools.values()
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            """Execute a tool."""
            # Check rate limit
            if self.rate_limiter:
                try:
                    self.rate_limiter.check_and_raise()
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=safe_json_dumps(self.error_handler.handle_exception(e, f"Rate limit"))
                    )]

            # Check if tool exists
            if name not in self.tools:
                error_response = {
                    "success": False,
                    "error": f"Unknown tool: {name}",
                    "available_tools": list(self.tools.keys())
                }
                return [TextContent(
                    type="text",
                    text=safe_json_dumps(error_response)
                )]

            # Execute tool
            tool = self.tools[name]
            self.logger.info(f"Executing tool: {name}")

            # Optional MCP ``notifications/progress`` stream. Keeps
            # client-side session watchdogs happy during long tool
            # executions (distinct from the SSE byte-stream keepalive).
            # Only runs when the client supplied ``_meta.progressToken``
            # — we never synthesise a token.
            progress_task: Optional[asyncio.Task[None]] = None
            if getattr(self.settings, "progress_notification_enabled", True):
                progress_task = self._maybe_start_progress_ticker(
                    tool_name=name,
                    interval_seconds=int(
                        getattr(
                            self.settings,
                            "progress_notification_interval_seconds",
                            10,
                        )
                    ),
                )

            try:
                result = await tool.safe_execute(**arguments)

                # Audit log — AuditLogger.log_operation redacts sensitive
                # fields (body/token/attachment content) before writing.
                if self.settings.enable_audit_log:
                    self.audit_logger.log_operation(
                        operation=name,
                        user=self.settings.ews_email,
                        success=result.get("success", False),
                        details={"arguments": arguments}
                    )

                return [TextContent(
                    type="text",
                    text=safe_json_dumps(result)
                )]

            except Exception as e:
                self.logger.exception(f"Tool execution failed: {name}")
                error_response = self.error_handler.handle_exception(e, f"Tool: {name}")
                return [TextContent(
                    type="text",
                    text=safe_json_dumps(error_response)
                )]
            finally:
                if progress_task is not None and not progress_task.done():
                    progress_task.cancel()
                    try:
                        await progress_task
                    except (asyncio.CancelledError, Exception):
                        pass

    def _maybe_start_progress_ticker(
        self, *, tool_name: str, interval_seconds: int,
    ) -> Optional[asyncio.Task[None]]:
        """Start a background progress-notification ticker for this call.

        Returns None when:
          * the client didn't include ``_meta.progressToken`` on the request
          * the MCP SDK doesn't expose the request context in this
            version (attribute missing)
          * the session object doesn't have ``send_progress_notification``

        Never raises — worst case we fall back to SSE keepalive only.
        Never logs the progress token (security rule 2).
        """
        # The MCP SDK exposes the active request via a contextvar
        # attribute on the server. The exact name has varied between
        # releases, so probe defensively.
        request_ctx = getattr(self.server, "request_context", None)
        if request_ctx is None:
            return None

        session = getattr(request_ctx, "session", None)
        send_progress = getattr(session, "send_progress_notification", None)
        if send_progress is None:
            return None

        # The progress token lives under request.params._meta.progressToken.
        progress_token = None
        try:
            meta = getattr(getattr(request_ctx, "meta", None), "progressToken", None)
            if meta is not None:
                progress_token = meta
            else:
                req = getattr(request_ctx, "request", None)
                params = getattr(req, "params", None) if req is not None else None
                meta = getattr(params, "meta", None) if params is not None else None
                progress_token = getattr(meta, "progressToken", None) if meta is not None else None
        except Exception:
            progress_token = None

        if progress_token is None:
            return None

        # Bound the interval here too, independent of the Settings
        # validator. Zero/negative would spin-loop the event loop.
        interval = max(5, min(60, int(interval_seconds)))
        logger = self.logger

        async def _ticker() -> None:
            progress = 0
            try:
                while True:
                    try:
                        await asyncio.sleep(interval)
                    except asyncio.CancelledError:
                        raise
                    progress += 1
                    try:
                        await send_progress(
                            progress_token=progress_token,
                            progress=float(progress),
                            total=None,
                        )
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:
                        if _peer_gone(exc):
                            logger.debug(
                                "progress ticker for %s: peer closed (%s)",
                                tool_name, type(exc).__name__,
                            )
                            return
                        # Don't kill the tool if notifications fail —
                        # just stop the ticker.
                        logger.debug(
                            "progress ticker for %s: %s: %s; exiting loop",
                            tool_name, type(exc).__name__, exc,
                        )
                        return
            except asyncio.CancelledError:
                raise

        return asyncio.create_task(_ticker(), name=f"progress-{tool_name}")

    def register_tools(self):
        """Register all enabled tools (42 base tools, up to 46 with AI)."""
        tool_classes = []

        # Email tools (10 core + 3 drafts = 13)
        if self.settings.enable_email:
            tool_classes.extend([
                CreateDraftTool,
                CreateReplyDraftTool,
                CreateForwardDraftTool,
                SendEmailTool,
                ReadEmailsTool,
                SearchEmailsTool,
                GetEmailDetailsTool,
                DeleteEmailTool,
                MoveEmailTool,
                UpdateEmailTool,
                CopyEmailTool,
                ReplyEmailTool,
                ForwardEmailTool
            ])
            self.logger.info("Email tools enabled (13 tools)")

        # Attachment tools (7 tools)
        if self.settings.enable_email:
            tool_classes.extend([
                ListAttachmentsTool,
                DownloadAttachmentTool,
                AddAttachmentTool,
                DeleteAttachmentTool,
                ReadAttachmentTool,
                GetEmailMimeTool,
                AttachEmailToDraftTool
            ])
            self.logger.info("Attachment tools enabled (7 tools)")

        # Calendar tools (7 tools)
        if self.settings.enable_calendar:
            tool_classes.extend([
                CreateAppointmentTool,
                GetCalendarTool,
                UpdateAppointmentTool,
                DeleteAppointmentTool,
                RespondToMeetingTool,
                CheckAvailabilityTool,
                FindMeetingTimesTool
            ])
            self.logger.info("Calendar tools enabled (7 tools)")

        # Contact tools (3 tools — search/get/resolve merged into find_person)
        if self.settings.enable_contacts:
            tool_classes.extend([
                CreateContactTool,
                UpdateContactTool,
                DeleteContactTool
            ])
            self.logger.info("Contact tools enabled (3 tools)")

        # Contact Intelligence tools (2 tools — find_person + analyze_contacts)
        if self.settings.enable_contacts:
            tool_classes.extend([
                FindPersonTool,
                AnalyzeContactsTool
            ])
            self.logger.info("Contact Intelligence tools enabled (2 tools)")

        # Task tools (5 tools)
        if self.settings.enable_tasks:
            tool_classes.extend([
                CreateTaskTool,
                GetTasksTool,
                UpdateTaskTool,
                CompleteTaskTool,
                DeleteTaskTool
            ])
            self.logger.info("Task tools enabled (5 tools)")

        # Search tools (1 tool — search_by_conversation)
        tool_classes.extend([
            SearchByConversationTool
        ])
        self.logger.info("Search tools enabled (1 tool)")

        # Folder tools (3 tools — list_folders + find_folder + manage_folder)
        tool_classes.extend([
            ListFoldersTool,
            FindFolderTool,
            ManageFolderTool
        ])
        self.logger.info("Folder tools enabled (3 tools)")

        # Out-of-Office tools (1 tool — oof_settings with get/set)
        tool_classes.extend([
            OofSettingsTool
        ])
        self.logger.info("Out-of-Office tools enabled (1 tool)")

        # AI tools (4 tools - conditionally enabled)
        if self.settings.enable_ai:
            ai_tools = []
            if self.settings.enable_semantic_search:
                ai_tools.append(SemanticSearchEmailsTool)
            if self.settings.enable_email_classification:
                ai_tools.append(ClassifyEmailTool)
            if self.settings.enable_email_summarization:
                ai_tools.append(SummarizeEmailTool)
            if self.settings.enable_smart_replies:
                ai_tools.append(SuggestRepliesTool)

            tool_classes.extend(ai_tools)
            self.logger.info(f"AI tools enabled ({len(ai_tools)} tools)")

        # Agent-secretary tools (memory + commitments + approvals + rules +
        # voice + OOF policy + briefing + meeting prep). Opt-in via
        # ENABLE_AGENT (default True).
        if getattr(self.settings, "enable_agent", True):
            tool_classes.extend([
                # Memory primitives
                MemorySetTool, MemoryGetTool, MemoryListTool, MemoryDeleteTool,
                # Commitments
                TrackCommitmentTool, ListCommitmentsTool, ResolveCommitmentTool,
                ExtractCommitmentsTool,
                # Approvals
                SubmitForApprovalTool, ListPendingApprovalsTool, ApproveTool, RejectTool,
                # Voice profile
                BuildVoiceProfileTool, GetVoiceProfileTool,
                # Rule engine
                RuleCreateTool, RuleListTool, RuleDeleteTool, RuleSimulateTool,
                EvaluateRulesOnMessageTool,
                # OOF policy
                ConfigureOOFPolicyTool, GetOOFPolicyTool, ApplyOOFPolicyTool,
                # Compound tools
                GenerateBriefingTool, PrepareMeetingTool,
            ])
            self.logger.info("Agent-secretary tools enabled (24 tools)")

        # Instantiate and register tools
        for tool_class in tool_classes:
            tool = tool_class(self.ews_client)
            schema = tool.get_schema()
            self.tools[schema["name"]] = tool

        # ExecuteApprovedActionTool needs the tool registry, so wire it after
        # every other tool is in place. This is why it isn't in the block
        # above.
        if getattr(self.settings, "enable_agent", True):
            executor = ExecuteApprovedActionTool(self.ews_client, self.tools)
            executor_schema = executor.get_schema()
            self.tools[executor_schema["name"]] = executor

        self.logger.info(f"Registered {len(self.tools)} tools: {', '.join(self.tools.keys())}")

        # Initialize OpenAPI adapter with settings for configurable URLs
        self.openapi_adapter = OpenAPIAdapter(self.server, self.tools, self.settings)
        self.logger.info("OpenAPI adapter initialized with dynamic configuration")

    async def run(self):
        """Run the MCP server with comprehensive logging."""
        try:
            self.logger.info(f"Starting {self.settings.mcp_server_name}")
            self.logger.info(f"Server: {self.settings.ews_server_url or 'autodiscover'}")
            self.logger.info(f"User: {self.settings.ews_email}")
            self.logger.info(f"Auth: {self.settings.ews_auth_type}")

            # Test connection
            self.logger.info("Testing Exchange connection...")
            if not self.ews_client.test_connection():
                self.logger.error("Failed to connect to Exchange server")
                self.logger.error("Please check your configuration and credentials")

                # Log connection failure
                self.log_manager.log_activity(
                    level="ERROR",
                    module="main",
                    action="CONNECTION_FAILED",
                    data={"server": self.settings.ews_server_url or "autodiscover"},
                    result={"status": "failed"},
                    context={"auth_type": self.settings.ews_auth_type}
                )
                return

            self.logger.info("✓ Successfully connected to Exchange")

            # Log successful connection
            self.log_manager.log_activity(
                level="INFO",
                module="main",
                action="CONNECTION_SUCCESS",
                data={"server": self.settings.ews_server_url or "autodiscover"},
                result={"status": "connected"},
                context={"auth_type": self.settings.ews_auth_type}
            )

            # Register tools
            self.register_tools()

            # Bug 4: kick off the semantic-search warmup as a background
            # task so the server accepts traffic immediately. The warmup
            # pre-fills the embedding cache from Inbox + Sent top-N so the
            # first few semantic_search_emails calls don't spend 45-76s
            # embedding on demand.
            if (
                getattr(self.settings, "enable_ai", False)
                and getattr(self.settings, "enable_semantic_search", False)
                and getattr(self.settings, "enable_embedding_warmup", True)
            ):
                try:
                    asyncio.create_task(self._run_embedding_warmup())
                except Exception as exc:
                    self.logger.warning(f"Failed to schedule embedding warmup: {exc}")

            # Log server ready
            self.log_manager.log_activity(
                level="INFO",
                module="main",
                action="SERVER_READY",
                data={"registered_tools": len(self.tools)},
                result={"status": "ready"},
                context={
                    "tool_list": list(self.tools.keys()),
                    "transport": self.settings.mcp_transport
                }
            )

            # Start server based on transport type
            if self.settings.mcp_transport == "stdio":
                self.logger.info(f"Server ready - listening on stdio")
                async with stdio_server() as (read_stream, write_stream):
                    await self.server.run(
                        read_stream,
                        write_stream,
                        self.server.create_initialization_options()
                    )
            elif self.settings.mcp_transport == "sse":
                self.logger.info(f"Server ready - listening on http://{self.settings.mcp_host}:{self.settings.mcp_port}")
                if not self.settings.mcp_api_key:
                    self.logger.warning(
                        "SSE transport is running without MCP_API_KEY. Only /health is public; "
                        "all other endpoints accept unauthenticated requests. Bind to 127.0.0.1 "
                        "or set MCP_API_KEY before exposing the port beyond localhost."
                    )
                await self.run_sse()

        except KeyboardInterrupt:
            self.logger.info("Shutting down...")
        except Exception as e:
            self.logger.exception(f"Server error: {e}")
            raise
        finally:
            # Cleanup
            self.ews_client.close()
            self.logger.info("Server stopped")

    async def _run_sse_connection(
        self,
        sse: "SseServerTransport",
        scope: Dict[str, Any],
        receive: Callable[[], Awaitable[Dict[str, Any]]],
        wrapped_send: ASGISend,
        headers_sent: asyncio.Event,
        send_lock: asyncio.Lock,
        *,
        keepalive_enabled: bool,
        keepalive_interval: int,
    ) -> None:
        """Drive the MCP server loop for one SSE connection.

        Kept separate from ``handle_sse`` so the caller's try/finally
        stays short (<80 lines). Raises ``BaseExceptionGroup`` when the
        TaskGroup path wraps child failures; the caller classifies.
        """
        async with sse.connect_sse(scope, receive, wrapped_send) as streams:
            run_mcp = self.server.run(
                streams[0],
                streams[1],
                self.server.create_initialization_options(),
            )
            if not keepalive_enabled:
                await run_mcp
                return
            async with asyncio.TaskGroup() as tg:
                mcp_task = tg.create_task(run_mcp, name="mcp-serve")
                keepalive_task = tg.create_task(
                    _keepalive_loop(
                        wrapped_send, send_lock, headers_sent,
                        keepalive_interval, self.logger,
                    ),
                    name="sse-keepalive",
                )
                # When the MCP task finishes, stop the keepalive so the
                # TaskGroup exits. TaskGroup cancels outstanding siblings
                # if ANY raises, so we only need to explicitly cancel on
                # normal mcp_task completion.
                await mcp_task
                if not keepalive_task.done():
                    keepalive_task.cancel()

    async def _run_embedding_warmup(self) -> None:
        """Background task: pre-embed recent Inbox + Sent items.

        Controlled by env:
          * EMBEDDING_WARMUP_FOLDERS (default "inbox,sent")
          * EMBEDDING_WARMUP_PER_FOLDER (default 500)
          * EMBEDDING_WARMUP_BATCH_SIZE (default 32)

        Silently logs and returns on any failure — the warmup is a
        best-effort optimisation; the server must still run without it.
        """
        try:
            from .ai import get_embedding_provider, EmbeddingService
        except Exception as exc:
            self.logger.warning(f"warmup: AI imports unavailable: {exc}")
            return
        try:
            provider = get_embedding_provider(self.settings)
        except Exception as exc:
            self.logger.warning(f"warmup: embedding provider not available: {exc}")
            return
        if provider is None:
            self.logger.info("warmup: no embedding provider configured; skipping")
            return

        folders_env = os.environ.get("EMBEDDING_WARMUP_FOLDERS", "inbox,sent")
        per_folder = int(os.environ.get("EMBEDDING_WARMUP_PER_FOLDER", "500"))
        batch_size = int(os.environ.get("EMBEDDING_WARMUP_BATCH_SIZE", "32"))
        folder_names = [f.strip().lower() for f in folders_env.split(",") if f.strip()]
        if not folder_names:
            return

        # Collect texts in a thread so the blocking iteration doesn't
        # stall the event loop. Returns (texts, per_folder_errors)
        # so the retry loop below can tell "transient connection broke
        # on all folders" from "some folders are legitimately empty".
        def _collect() -> tuple[list, list]:
            from .utils import safe_get
            texts: list = []
            errors: list = []
            account = self.ews_client.account
            folder_map = {
                "inbox": getattr(account, "inbox", None),
                "sent": getattr(account, "sent", None),
                "drafts": getattr(account, "drafts", None),
                "archive": getattr(account, "archive", None),
            }
            for name in folder_names:
                folder = folder_map.get(name)
                if folder is None:
                    continue
                try:
                    items = list(
                        folder.all().order_by("-datetime_received")[:per_folder]
                    )
                except Exception as exc:
                    errors.append((name, exc))
                    continue
                for item in items:
                    subject = safe_get(item, "subject", "") or ""
                    body = safe_get(item, "text_body", "") or ""
                    # Match the format used by SemanticSearchEmailsTool so
                    # the cache key is identical and the at-query-time
                    # lookup hits.
                    texts.append(f"{subject} {body[:500]}")
            return texts, errors

        # Retry collection with exponential backoff. The warmup races
        # container startup against the Exchange connection pool; early
        # attempts can hit ``RemoteDisconnected`` / ``ConnectionError``
        # before the pool is ready.
        attempts = [5, 15, 45]  # seconds between retries (3 tries total)
        texts: list = []
        last_errors: list = []
        for attempt_idx in range(len(attempts) + 1):
            try:
                texts, errors = await asyncio.to_thread(_collect)
            except Exception as exc:
                texts, errors = [], [("<top-level>", exc)]

            if texts:
                if errors:
                    self.logger.info(
                        "warmup: collected %d texts with %d folder errors: %s",
                        len(texts), len(errors),
                        [(n, type(e).__name__) for n, e in errors],
                    )
                break

            # Nothing collected — decide whether to retry.
            last_errors = errors
            if attempt_idx >= len(attempts):
                break
            if errors and _is_transient_error(errors):
                delay = attempts[attempt_idx]
                self.logger.info(
                    "warmup: collection hit transient errors "
                    "(%s); retrying in %ds (attempt %d/%d)",
                    [(n, type(e).__name__) for n, e in errors],
                    delay, attempt_idx + 2, len(attempts) + 1,
                )
                try:
                    await asyncio.sleep(delay)
                except Exception:
                    break
                continue
            # Non-transient error or truly empty — don't retry.
            break

        if not texts:
            if last_errors:
                self.logger.warning(
                    "warmup: collection failed after retries: %s",
                    [(n, f"{type(e).__name__}: {e}") for n, e in last_errors],
                )
            else:
                self.logger.info("warmup: nothing to embed")
            return

        self.logger.info(
            "warmup: starting with %d texts across %s (batch_size=%d)",
            len(texts), folder_names, batch_size,
        )

        service = EmbeddingService(provider, cache_dir="data/embeddings")
        try:
            stats = await service.warmup(
                texts,
                batch_size=batch_size,
                max_items=per_folder * len(folder_names),
            )
            self.logger.info(
                "warmup: finished — %s",
                ", ".join(f"{k}={v}" for k, v in stats.items()),
            )
        except Exception as exc:
            self.logger.warning(f"warmup: failed: {exc}")

    async def run_sse(self):
        """Run the MCP server with SSE (HTTP) transport."""
        import uvicorn

        sse = SseServerTransport("/messages")
        keepalive_enabled = bool(
            getattr(self.settings, "sse_keepalive_enabled", True)
        )
        keepalive_interval = int(
            getattr(self.settings, "sse_keepalive_interval_seconds", 15)
        )
        if keepalive_enabled:
            self.logger.info(
                "sse keepalive enabled: interval=%ds", keepalive_interval,
            )

        async def handle_sse(
            scope: Dict[str, Any], receive: Callable[[], Awaitable[Dict[str, Any]]], send: ASGISend,
        ) -> None:
            """Handle SSE connection endpoint.

            Transport-layer responsibilities only:
              * Inject SSE proxy-buster headers on the first response.
              * Run a per-connection keepalive task (comment frames).
              * Serialise every ASGI send through a single lock so the
                SDK's event writes and our keepalives don't interleave
                inside one HTTP/1.1 chunk.
              * Increment/decrement the active-connection counter used
                by /health.
            """
            wrapped_send, headers_sent, send_lock = _wrap_send_with_sse_headers(send)
            _sse_active_inc()
            try:
                await self._run_sse_connection(
                    sse, scope, receive, wrapped_send,
                    headers_sent, send_lock,
                    keepalive_enabled=keepalive_enabled,
                    keepalive_interval=keepalive_interval,
                )
            except BaseExceptionGroup as eg:
                # TaskGroup wraps child exceptions in ExceptionGroup.
                non_peer = [e for e in eg.exceptions if not _peer_gone(e)]
                if not non_peer:
                    self.logger.debug(
                        "SSE connection closed by peer (%s)",
                        ", ".join(sorted({type(e).__name__ for e in eg.exceptions})),
                    )
                else:
                    self.logger.warning(
                        "SSE connection ended with error(s): %s",
                        "; ".join(f"{type(e).__name__}: {e}" for e in non_peer),
                    )
            except Exception as exc:
                # Pre-TaskGroup failure (e.g. connect_sse raising) —
                # plain, not wrapped. Classify the same way.
                if _peer_gone(exc):
                    self.logger.debug(
                        "SSE connection closed by peer (%s)", type(exc).__name__,
                    )
                else:
                    self.logger.warning(
                        "SSE connection ended: %s: %s", type(exc).__name__, exc,
                    )
            finally:
                _sse_active_dec()

        # Create raw ASGI handler for messages endpoint
        async def handle_messages(scope, receive, send):
            """Handle POST messages endpoint."""
            try:
                await sse.handle_post_message(scope, receive, send)
            except Exception as e:
                # Log but don't crash - client may have disconnected
                self.logger.warning(f"Message handling failed: {type(e).__name__}: {e}")
                # Don't try to send error response if connection is broken
                if "BrokenResource" not in str(type(e).__name__):
                    try:
                        await send({
                            "type": "http.response.start",
                            "status": 500,
                            "headers": [[b"content-type", b"application/json"]],
                        })
                        await send({
                            "type": "http.response.body",
                            "body": b'{"error": "Internal Server Error"}',
                        })
                    except Exception:
                        pass  # Connection already broken

        # API key verification for non-health endpoints.
        # When MCP_API_KEY is set, every request (except /health) must include
        # either `Authorization: Bearer <key>` or `X-API-Key: <key>`.
        api_key = self.settings.mcp_api_key

        async def _send_401(send, reason: str = "Unauthorized") -> None:
            body = b'{"success":false,"error":"' + reason.encode("utf-8") + b'"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                    [b"www-authenticate", b'Bearer realm="ews-mcp"'],
                ],
            })
            await send({"type": "http.response.body", "body": body})

        def _authorized(headers: list) -> bool:
            """Backward-compatible wrapper around ``_authorized_request``.

            Kept as a closure (rather than inlining) so the caller in
            ``app`` reads the same way as before — only the auth
            algorithm changed (constant-time compare via hmac).
            """
            return _authorized_request(headers, api_key)

        # Create a simple ASGI router that handles both MCP and REST endpoints
        async def app(scope, receive, send):
            """ASGI router for MCP SSE transport + REST API."""
            if scope["type"] == "http":
                path = scope["path"]
                method = scope["method"]

                # Health check is always public. Exposes the active SSE
                # connection counter so a leak is observable without
                # needing to attach to the process.
                if path == "/health" and method == "GET":
                    health_body = safe_json_dumps({
                        "status": "ok",
                        "tools": len(self.tools),
                        "sse_active_connections": _sse_active_count(),
                    }).encode("utf-8")
                    await send({
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"content-length", str(len(health_body)).encode()],
                        ],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": health_body,
                    })
                    return

                # All other endpoints require auth when MCP_API_KEY is set
                if not _authorized(scope.get("headers") or []):
                    await _send_401(send)
                    return

                # MCP SSE endpoints
                if path == "/sse" and method == "GET":
                    await handle_sse(scope, receive, send)
                elif path == "/messages" and method == "POST":
                    await handle_messages(scope, receive, send)

                # OpenAPI/REST endpoints
                elif path == "/openapi.json" and method == "GET":
                    # Return OpenAPI schema
                    schema = self.openapi_adapter.generate_openapi_schema()
                    body = safe_json_dumps(schema, indent=2).encode('utf-8')
                    await send({
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"content-length", str(len(body)).encode()],
                        ],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": body,
                    })

                elif path.startswith("/api/tools/") and method == "POST":
                    # Execute tool via REST API
                    tool_name = path.replace("/api/tools/", "")

                    # Read request body
                    body_parts = []
                    while True:
                        message = await receive()
                        if message["type"] == "http.request":
                            body_parts.append(message.get("body", b""))
                            if not message.get("more_body", False):
                                break
                    body = b"".join(body_parts)

                    # Execute tool
                    result = await self.openapi_adapter.handle_rest_request(tool_name, body)
                    status = result.pop("status", 200)

                    # Send response. Use safe_json_dumps so exchangelib
                    # types (CalendarEventDetails, Decimal, ItemId, naive
                    # datetimes) don't crash the serialiser (CAL-006 /
                    # TSK-004 root cause).
                    response_body = safe_json_dumps(result).encode('utf-8')
                    await send({
                        "type": "http.response.start",
                        "status": status,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"content-length", str(len(response_body)).encode()],
                        ],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": response_body,
                    })

                else:
                    # Return 404 for unknown routes
                    await send({
                        "type": "http.response.start",
                        "status": 404,
                        "headers": [[b"content-type", b"text/plain"]],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": b"Not Found",
                    })
            elif scope["type"] == "lifespan":
                # Handle lifespan events
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return

        # Run with uvicorn. ``timeout_keep_alive`` bounds the idle
        # HTTP/1.1 keep-alive window so stale connections (e.g.
        # abandoned clients) don't linger forever. We pair that with a
        # generous graceful-shutdown window so in-flight SSE responses
        # can flush on container stop.
        http_keep_alive = int(
            getattr(self.settings, "http_keep_alive_timeout_seconds", 300)
        )
        config = uvicorn.Config(
            app,
            host=self.settings.mcp_host,
            port=self.settings.mcp_port,
            log_level=self.settings.log_level.lower(),
            http="h11",
            timeout_keep_alive=http_keep_alive,
            timeout_graceful_shutdown=30,
        )
        server = uvicorn.Server(config)

        # Enable SO_KEEPALIVE + Linux TCP_KEEPIDLE on listening sockets
        # *after* startup has produced ``server.servers``. This catches
        # connections accepted on every listening socket without having
        # to subclass uvicorn's Config. Failure is non-fatal — tune is
        # a best-effort optimisation.
        tcp_keepalive = bool(getattr(self.settings, "tcp_keepalive_enabled", True))
        tcp_idle = int(getattr(self.settings, "tcp_keepalive_idle_seconds", 60))

        async def _serve_with_tcp_keepalive() -> None:
            if not tcp_keepalive:
                await server.serve()
                return
            # Drive uvicorn as a background task so we can iterate its
            # listening sockets once startup has completed. If we wait
            # on ``server.serve()`` directly the coroutine blocks until
            # shutdown.
            serve_task = asyncio.create_task(server.serve(), name="uvicorn-serve")
            # Poll briefly for startup. ``server.started`` flips True
            # once all listeners are bound.
            for _ in range(100):  # ~10s budget
                if getattr(server, "started", False):
                    break
                await asyncio.sleep(0.1)
            try:
                for srv in getattr(server, "servers", ()) or ():
                    for sock in getattr(srv, "sockets", ()) or ():
                        _enable_tcp_keepalive(sock, tcp_idle, self.logger)
                self.logger.info(
                    "tcp keepalive enabled on listening socket(s): idle=%ds", tcp_idle,
                )
            except Exception as exc:  # pragma: no cover - best effort
                self.logger.debug("TCP keepalive setup skipped: %s", exc)
            await serve_task

        await _serve_with_tcp_keepalive()


def main():
    """Entry point."""
    try:
        server = EWSMCPServer()
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
