"""ToolSpec + the dispatcher: every gate from DESIGN.md §Safety in one chain.

Gate order (policy before connectivity, nothing irreversible without two
model decisions): kill-switch → tier → circuit → cold gate → recipient
guard (argument-borne recipients on EVERY tool, resolved recipients via
the preview hook) → two-phase confirm (content-bound + single-use when
the spec supplies a preview hook) → send rate cap → alias resolution →
handler (on the EWS pool) → audit.
"""

import asyncio
import fnmatch
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from ..confirm import consume_token, content_hash, make_token, verify_token
from ..errors import ToolError, map_exception
from ..identity import Caller, audit_identity, caller_of

logger = logging.getLogger(__name__)

TIER_RANK = {"read": 0, "draft": 1, "full": 2}
CLASS_TIER = {"read": "read", "write": "draft", "send": "full", "destructive": "full"}
ID_KEYS = frozenset({
    "id", "message_id", "draft_id", "event_id", "thread_id", "reply_to", "folder",
})
ID_LIST_KEYS = frozenset({"ids"})
RECIPIENT_KEYS = ("to", "cc", "bcc", "attendees")

CONFIRM_TOKEN_PROPERTY = {
    "type": "string",
    "description": (
        "Two-phase confirm: omit on the first call to get a preview + token "
        "(nothing executes). Call again with identical arguments plus this "
        "token to execute. Tokens are argument-bound and expire."
    ),
}

# Send timestamps PER CALLER. A single shared deque meant one caller's sends
# consumed everybody else's hourly budget — invisible with one mailbox, a
# denial of service between colleagues with several.
_SEND_TIMES: Dict[str, deque] = {}
_SEND_LOCK = threading.Lock()
_RATE_WINDOW_MAX_CALLERS = 500  # prune threshold, not a cap on who may send


def _prune_rate_windows(now: float) -> None:
    """Drop callers with nothing left inside the hour. Caller-held lock."""
    for subject in [s for s, w in _SEND_TIMES.items()
                    if not w or now - w[-1] > 3600]:
        del _SEND_TIMES[subject]


def reset_send_rate_window() -> None:
    with _SEND_LOCK:
        _SEND_TIMES.clear()


@dataclass
class ToolSpec:
    name: str
    description: str
    side_effect_class: str  # read | write | send | destructive
    input_schema: Dict[str, Any]
    handler: Callable[..., Awaitable[Dict[str, Any]]]  # handler(ctx, **kwargs)
    requires_ews: bool = True
    confirm: Union[bool, Callable[[Any, Dict[str, Any]], bool]] = False
    output_schema: Optional[Dict[str, Any]] = None
    # Optional async hook ``preview(ctx, kwargs) -> content dict`` that
    # resolves the REAL content the confirm token must bind (e.g. fetch the
    # draft and return its subject/recipients/body_text). When present the
    # gate hashes that content — not just the caller's literal arguments —
    # so an update_draft between preview and confirm invalidates the token
    # (TOCTOU defense), and the resolved recipients pass the recipient
    # guard. Reserved key "body_text" is hashed in full but previewed as a
    # snippet.
    preview: Optional[Callable[[Any, Dict[str, Any]], Awaitable[Dict[str, Any]]]] = None

    def confirm_needed(self, ctx: Any, kwargs: Dict[str, Any]) -> bool:
        return (self.confirm(ctx, kwargs) if callable(self.confirm)
                else bool(self.confirm))

    def public_schema(self) -> Dict[str, Any]:
        schema = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.confirm is not False:
            props = schema["inputSchema"].setdefault("properties", {})
            props.setdefault("confirm_token", dict(CONFIRM_TOKEN_PROPERTY))
        return schema


@dataclass
class Context:
    settings: Any
    gateway: Any
    manager: Any  # ConnectionManager | None
    aliaser: Any
    audit: Any
    cache: Any = None  # CacheStore | None (None = cache disabled/broken)
    sync: Any = None  # SyncEngine | None
    semantic: Any = None  # SemanticIndex adapter | None
    registry: Dict[str, ToolSpec] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    counters: Dict[str, int] = field(default_factory=dict)
    principal: Any = None  # Principal | None (None = static mode)
    binder: Any = None  # CallerBinder | None (oidc: per-caller gateway/aliaser)
    _root: Any = None  # the shared boot Context, when this is a per-request clone
    _circuit_failures: int = 0
    _circuit_open_until: float = 0.0

    @property
    def root(self) -> "Context":
        """The process-wide Context that owns the genuinely shared state.

        Counters and the circuit breaker must NOT be cloned per request:
        counters would vanish, and each caller would get a private circuit
        that never opens. Everything else on a clone is per-caller by design.
        """
        return self._root or self

    def bump(self, key: str) -> None:
        root = self.root
        root.counters[key] = root.counters.get(key, 0) + 1


def for_principal(root: Context, principal: Any) -> Context:
    """A per-request view of the boot Context, bound to one caller.

    A shallow copy, so concurrent callers never alias each other — and no
    handler signature changes, because handlers only ever reach identity
    through ``ctx``. The stores (gateway, aliaser, cache) are still the
    shared ones: they become per-caller with the gateway pool, once each
    caller opens their own mailbox.
    """
    return replace(root, principal=principal, _root=root.root)


def _split(raw: str) -> List[str]:
    return [p.strip().lower() for p in (raw or "").split(",") if p.strip()]


def _recipients(kwargs: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for key in RECIPIENT_KEYS:
        value = kwargs.get(key)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            out.extend(r.lower() for r in value if isinstance(r, str) and "@" in r)
    return out


def _recipient_guard(ctx: Context, kwargs: Dict[str, Any]) -> None:
    deny = _split(ctx.settings.ews_recipient_denylist)
    allow = _split(ctx.settings.ews_recipient_allowlist)
    if not deny and not allow:
        return
    for r in _recipients(kwargs):
        if any(fnmatch.fnmatch(r, p) for p in deny):
            raise ToolError("recipient_blocked",
                            f"recipient '{r}' is denylisted (EWS_RECIPIENT_DENYLIST)")
        if allow and not any(fnmatch.fnmatch(r, p) for p in allow):
            raise ToolError("recipient_blocked",
                            f"recipient '{r}' is not allowlisted (EWS_RECIPIENT_ALLOWLIST)")


def _rate_guard(ctx: Context, caller: Caller) -> None:
    cap = int(ctx.settings.ews_max_sends_per_hour or 0)
    if cap <= 0:
        return
    now = time.time()
    with _SEND_LOCK:
        if len(_SEND_TIMES) > _RATE_WINDOW_MAX_CALLERS:
            _prune_rate_windows(now)
        window = _SEND_TIMES.setdefault(caller.subject, deque())
        while window and now - window[0] > 3600:
            window.popleft()
        if len(window) >= cap:
            retry = int(3600 - (now - window[0]))
            raise ToolError("rate_capped",
                            f"send rate cap reached ({cap}/hour)",
                            retry_after_s=retry)
        window.append(now)


def _external_recipients(ctx: Context, caller: Caller,
                        source: Dict[str, Any]) -> List[str]:
    """External relative to the CALLER's own domain, not the server's."""
    own = caller.smtp.rsplit("@", 1)[-1].lower()
    return sorted({
        r for r in _recipients(source) if r.rsplit("@", 1)[-1] != own
    })


_CONFIRM_HINTS = {
    "expired": "Token expired — call again WITHOUT confirm_token for a fresh preview.",
    "stale": ("The content changed since the preview (or the arguments differ) — "
              "re-preview to get a token bound to the current content."),
    "consumed": "This token was already used once — re-preview for a fresh one.",
    "bad_signature": "Invalid confirmation token.",
    "malformed": "Malformed confirmation token.",
}


async def _confirm_gate(ctx: Context, spec: ToolSpec, caller: Caller,
                        kwargs: Dict[str, Any],
                        token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Returns the phase-1 response, or None when execution may proceed.

    With ``spec.preview`` the token binds the RESOLVED content (refetched and
    re-verified at phase 2 — the TOCTOU defense) and the resolved recipients
    pass the recipient guard; without it the caller's literal arguments are
    hashed. Verified tokens are single-use.
    """
    secret = ctx.settings.send_confirm_secret
    content: Optional[Dict[str, Any]] = None
    if spec.preview is not None:
        content = await spec.preview(ctx, dict(kwargs))
        _recipient_guard(ctx, content)  # the draft's REAL recipients
        body_text = content.get("body_text")
        chash = content_hash(
            content.get("subject") or "",
            sorted(content.get("to") or []),
            sorted(content.get("cc") or []),
            sorted(content.get("bcc") or []),
            body_text if isinstance(body_text, str) else "",
        )
        target_id = str(kwargs.get("draft_id") or kwargs.get("event_id")
                        or kwargs.get("id") or "-")
    else:
        chash = content_hash(dict(kwargs))
        target_id = "-"
    if not token:
        tok = make_token(
            mailbox=caller.smtp, subject=caller.subject,
            action=spec.name, target_id=target_id,
            chash=chash, ttl_seconds=ctx.settings.confirm_ttl_seconds, secret=secret,
        )
        if content is not None:
            preview = {k: v for k, v in content.items() if k != "body_text"}
            body_text = content.get("body_text")
            if isinstance(body_text, str):
                preview["body_snippet"] = body_text[:1000]
        else:
            preview = {
                k: (v[:200] if k in ("body", "message", "comment") and isinstance(v, str) else v)
                for k, v in kwargs.items()
            }
        external = _external_recipients(
            ctx, caller, content if content is not None else kwargs)
        response: Dict[str, Any] = {
            "ok": True,
            "requires_confirmation": True,
            "message": (f"{spec.name} previewed — NOTHING executed. Call again "
                        "with the same arguments plus confirm_token to proceed."),
            "preview": preview,
            **tok,
        }
        if external:
            response["warnings"] = [f"external recipients: {', '.join(external)}"]
        return response
    ok, reason = verify_token(
        token, mailbox=caller.smtp, subject=caller.subject, action=spec.name,
        target_id=target_id, chash=chash, secret=secret,
    )
    if ok and not consume_token(token):
        ok, reason = False, "consumed"
    if not ok:
        raise ToolError(
            "confirm_invalid",
            f"confirm_token rejected ({reason})",
            hint=_CONFIRM_HINTS.get(
                reason,
                f"Call {spec.name} again WITHOUT confirm_token for a fresh preview."),
        )
    return None


async def mint_token(ctx: Context, spec: ToolSpec, kwargs: Dict[str, Any]) -> str:
    """Server-side pre-confirmation (e.g. a human-approved queue item).

    Mirrors ``_confirm_gate``'s binding exactly: preview-hook specs get a
    token bound to the resolved content, others to the literal arguments —
    and, like the gate, the token is bound to the caller on ``ctx``.
    """
    caller = caller_of(ctx)
    if spec.preview is not None:
        content = await spec.preview(ctx, dict(kwargs))
        body_text = content.get("body_text")
        chash = content_hash(
            content.get("subject") or "",
            sorted(content.get("to") or []),
            sorted(content.get("cc") or []),
            sorted(content.get("bcc") or []),
            body_text if isinstance(body_text, str) else "",
        )
        target_id = str(kwargs.get("draft_id") or kwargs.get("event_id")
                        or kwargs.get("id") or "-")
    else:
        chash = content_hash(dict(kwargs))
        target_id = "-"
    return make_token(
        mailbox=caller.smtp, subject=caller.subject,
        action=spec.name, target_id=target_id,
        chash=chash,
        ttl_seconds=ctx.settings.confirm_ttl_seconds,
        secret=ctx.settings.send_confirm_secret,
    )["confirm_token"]


def _resolve_ids(ctx: Context, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(kwargs)
    try:
        for key, value in kwargs.items():
            if key in ID_KEYS and isinstance(value, str) and key != "folder":
                out[key] = ctx.aliaser.resolve(value)
            elif key in ID_LIST_KEYS and isinstance(value, list):
                out[key] = [ctx.aliaser.resolve(v) if isinstance(v, str) else v
                            for v in value]
    except KeyError as e:
        raise ToolError("validation", str(e.args[0] if e.args else e))
    return out


async def _run_handler(ctx: Context, spec: ToolSpec,
                       kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Run the handler, retrying ONCE if Exchange rejected our token.

    A caller's access token can be revoked, or their clock can drift, between
    the expiry-margin check and the actual request. Re-exchanging and retrying
    turns that into a hiccup instead of a failed call.

    Reads only, deliberately. On a send or a delete we cannot know whether
    Exchange rejected the request or merely the response, so a silent retry
    risks sending twice. Those callers get the error and retry themselves —
    which is exactly what ``idempotency_key`` exists for.
    """
    try:
        return await spec.handler(ctx, **kwargs)
    except Exception as exc:
        binder = ctx.binder
        if (binder is None or ctx.principal is None
                or spec.side_effect_class != "read"
                or map_exception(exc).code != "auth_failed"):
            raise
        logger.info("upstream rejected the caller's token mid-flight — "
                    "re-exchanging and retrying once")
        binder.on_upstream_auth_failure(ctx.principal)
        ctx.gateway = await binder.pool.acquire(
            ctx.principal, await binder.exchanger.token_for(ctx.principal))
        return await spec.handler(ctx, **kwargs)


async def dispatch(ctx: Context, spec: ToolSpec, kwargs: Dict[str, Any],
                   transport: str = "-") -> Dict[str, Any]:
    start = time.time()
    outcome = "ok"
    # confirm_token is dispatcher vocabulary, never a handler argument —
    # strip it unconditionally so non-confirm handlers can't receive it
    # (a leak here used to surface as TypeError → misleading 502).
    kwargs = dict(kwargs)
    confirm_token = kwargs.pop("confirm_token", None)
    caller = caller_of(ctx)
    try:
        # Identity — before every other gate, and FAIL CLOSED. In oidc mode a
        # missing principal means the ASGI layer's verification did not reach
        # us (an SDK change, a new transport, a wiring mistake). Falling back
        # to the configured mailbox here would hand it to an anonymous
        # caller, so the only safe answer is to refuse.
        if ctx.settings.auth_mode == "oidc" and ctx.principal is None:
            raise ToolError(
                "auth_failed",
                "no verified caller identity reached the dispatcher",
                hint="The server is in AUTH_MODE=oidc; every call must carry "
                     "a verified bearer token.",
            )
        # Kill-switch (policy precedes connectivity)
        if spec.side_effect_class == "send" and not ctx.settings.send_enabled:
            raise ToolError(
                "kill_switch",
                f"{spec.name} is blocked: SEND_ENABLED=false on this server.",
                hint="Create a draft instead; sending requires the operator "
                     "to flip SEND_ENABLED.",
            )
        # Tier
        tier = ctx.settings.ews_capability_tier
        need = CLASS_TIER.get(spec.side_effect_class, "draft")
        if TIER_RANK[need] > TIER_RANK.get(tier, 2):
            raise ToolError(
                "tier_blocked",
                f"{spec.name} needs capability tier '{need}'; server runs '{tier}'.",
            )
        # Circuit breaker
        now = time.time()
        # On the ROOT: the circuit tracks Exchange's health, which is a
        # property of the server, not of whoever happens to be calling.
        root = ctx.root
        if root._circuit_open_until > now:
            raise ToolError(
                "upstream_unavailable",
                "circuit open after repeated upstream failures",
                retry_after_s=int(root._circuit_open_until - now),
            )
        # Cold gate
        if spec.requires_ews and ctx.manager is not None and ctx.manager.state == "connecting":
            st = ctx.manager.status()
            raise ToolError(
                "upstream_unavailable",
                f"Exchange connection still warming up (attempt {st['attempts']}; "
                f"last error: {st['last_error'] or 'none yet'})",
                hint="Check /readyz or call get_server_status.",
                retry_after_s=st.get("next_retry_in_s"),
            )
        # Recipient policy — every tool whose arguments carry recipients
        # (create_draft/update_draft/create_event/…), not just send class:
        # drafts and events are how mail actually acquires recipients.
        _recipient_guard(ctx, kwargs)
        # Two-phase confirm (content-bound via spec.preview when present,
        # otherwise the caller's literal args; single-use either way)
        if spec.confirm_needed(ctx, kwargs):
            phase1 = await _confirm_gate(ctx, spec, caller, kwargs, confirm_token)
            if phase1 is not None:
                outcome = "phase1"
                return phase1
        # Send rate cap — this call WILL execute
        if spec.side_effect_class == "send":
            _rate_guard(ctx, caller)
        # Alias → raw ids
        kwargs = _resolve_ids(ctx, kwargs)
        result = await _run_handler(ctx, spec, kwargs)
        ctx.root._circuit_failures = 0
        if isinstance(result, dict):
            result.setdefault("ok", True)
        return result
    except ToolError as err:
        outcome = err.code
        return err.to_dict()
    except (TypeError, ValueError) as exc:
        # Handler contract violations (wrong kwarg names/types) are caller
        # errors, not upstream failures — never report them as 502s.
        outcome = "validation"
        return ToolError(
            "validation", f"{type(exc).__name__}: {exc}",
            hint="Check the argument names and types against the tool schema.",
        ).to_dict()
    except Exception as exc:
        err = map_exception(exc)
        outcome = err.code
        if err.code in ("upstream_unavailable", "upstream_error", "throttled"):
            root = ctx.root
            root._circuit_failures += 1
            if root._circuit_failures >= ctx.settings.circuit_failure_threshold:
                root._circuit_open_until = time.time() + ctx.settings.circuit_open_seconds
                root._circuit_failures = 0
        return err.to_dict()
    finally:
        ctx.bump(f"tool.{spec.name}")
        if outcome != "ok":
            ctx.bump(f"err.{outcome}")
        detail = None
        if spec.side_effect_class in ("send", "destructive"):
            detail = {k: kwargs.get(k)
                      for k in ("to", "cc", "subject", "id", "ids",
                                "draft_id", "event_id")
                      if kwargs.get(k) is not None}
        # Audit writes (file append + chain hash) run off the event loop —
        # one slow disk must not stall every concurrent request.
        await asyncio.to_thread(
            ctx.audit.record,
            tool=spec.name, side_effect_class=spec.side_effect_class,
            outcome=outcome, latency_ms=int((time.time() - start) * 1000),
            transport=transport, detail=detail,
            principal=audit_identity(ctx, caller),
        )
        # Opportunistic mirror warm-up, AFTER the answer is on its way, using
        # the token already in hand. Fire-and-forget by design: it must never
        # add latency to this call, and never fail it.
        if ctx.binder is not None and ctx.principal is not None:
            ctx.binder.schedule_sync(ctx)
