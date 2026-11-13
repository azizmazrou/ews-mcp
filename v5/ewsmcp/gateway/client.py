"""EWS gateway — one Account per process, EWS off the event loop.

Authentication auto-negotiation is the default because it adapts to what the
Exchange server offers; forcing a scheme via ``ews_auth_type_force`` skips the
unauthenticated probe request and is appropriate when the scheme is known.

HTTP 401 credential rejections fail fast via ``RejectingFaultTolerance`` and
trip the gateway's authentication circuit breaker so a stale password can
never be resubmitted in a loop or lock out the directory account.
"""

import asyncio
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from exchangelib import Account, Configuration, Credentials, DELEGATE, EWSTimeZone
from exchangelib.errors import UnauthorizedError
from exchangelib.protocol import (
    BaseProtocol,
    CachingProtocol,
    FaultTolerance,
    NoVerifyHTTPAdapter,
)

from ..config import Settings
from ..errors import AUTH_FAILED_HINT, ToolError

logger = logging.getLogger(__name__)

WELL_KNOWN = {
    "f:inbox": "inbox", "f:sent": "sent", "f:drafts": "drafts",
    "f:trash": "trash", "f:junk": "junk", "f:outbox": "outbox",
    "f:calendar": "calendar", "f:contacts": "contacts", "f:tasks": "tasks",
    "f:sentitems": "sent", "f:deleteditems": "trash", "f:junkemail": "junk",
}

_QUOTED_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_SCHEME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")


def _www_authenticate_schemes(response: Any) -> List[str]:
    """Extract bare scheme tokens from a response's WWW-Authenticate header(s).

    Strips quoted auth-param values and skips key=value pairs and challenge
    payloads so realms, host identifiers and NTLM/Negotiate tokens never enter
    logs.
    """
    headers = getattr(response, "headers", None)
    if headers is None:
        return []
    raw_values: List[str] = []
    if hasattr(headers, "get_all"):
        raw_values.extend(str(v) for v in (headers.get_all("WWW-Authenticate") or []))
    elif hasattr(headers, "getlist"):
        raw_values.extend(str(v) for v in (headers.getlist("WWW-Authenticate") or []))
    elif hasattr(headers, "items"):
        for k, v in headers.items():
            key = k.decode("latin-1") if isinstance(k, bytes) else str(k)
            if key.lower() == "www-authenticate" and v is not None:
                val = v.decode("latin-1", "replace") if isinstance(v, bytes) else str(v)
                raw_values.append(val)
    elif isinstance(headers, (list, tuple)):
        for item in headers:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                k, v = item
                key = k.decode("latin-1") if isinstance(k, bytes) else str(k)
                if key.lower() == "www-authenticate" and v is not None:
                    val = v.decode("latin-1", "replace") if isinstance(v, bytes) else str(v)
                    raw_values.append(val)
    schemes: List[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        scrubbed = _QUOTED_RE.sub('""', raw)
        for segment in scrubbed.split(","):
            part = segment.strip()
            if not part:
                continue
            token = part.split()[0]
            if "=" in token or not _SCHEME_RE.match(token):
                continue
            lowered = token.lower()
            if lowered not in seen:
                seen.add(lowered)
                schemes.append(token)
    return schemes


class RejectingFaultTolerance(FaultTolerance):
    """FaultTolerance policy that raises UnauthorizedError immediately on 401.

    exchangelib 5.0.3's ``FaultTolerance`` maps HTTP 401 onto
    ``ErrorServerBusy("Caused by HTTP 401 response")`` and retries with
    exponential back-off up to ``max_wait``. Any 401 whose body does not
    contain ``b"currently locked out"`` raises ``UnauthorizedError`` at once;
    locked-out 401s and all other statuses delegate to ``super()`` unchanged.
    """

    def __init__(
        self,
        *args: Any,
        configured_auth_type: Optional[str] = None,
        on_rejection: Optional[Callable[[], None]] = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.configured_auth_type = configured_auth_type
        self._on_rejection = on_rejection

    def raise_response_errors(self, response: Any) -> Any:
        if getattr(response, "status_code", None) == 401:
            raw_body = getattr(response, "content", b"") or b""
            body = (
                raw_body.encode("utf-8", "replace")
                if isinstance(raw_body, str)
                else bytes(raw_body)
            )
            if b"currently locked out" not in body:
                schemes = _www_authenticate_schemes(response)
                offered = ", ".join(schemes) if schemes else "none"
                configured = self.configured_auth_type or "auto-negotiated"
                logger.warning(
                    "Exchange HTTP 401 rejection: WWW-Authenticate schemes [%s]; "
                    "ews_auth_type_force=%s",
                    offered,
                    configured,
                )
                err = UnauthorizedError("Exchange rejected credentials (HTTP 401)")
                if self._on_rejection is not None:
                    self._on_rejection()
                raise err
        return super().raise_response_errors(response)


class AuthBreaker:
    """Thread-safe circuit breaker guarding all Exchange credential use."""

    def __init__(
        self,
        cooldown_seconds: int = 900,
        max_rejections: int = 3,
        clock: Optional[Callable[[], float]] = None,
    ):
        self._cooldown = max(0, int(cooldown_seconds))
        self._max_rejections = max(1, int(max_rejections))
        self._clock = clock
        self._lock = threading.Lock()
        self._rejections = 0
        self._open_until_ts: Optional[float] = None
        self._permanent = False
        self._probe_in_flight = False
        self._seq = 0

    def _now(self) -> float:
        return float(self._clock()) if self._clock is not None else time.time()

    def _error_locked(self, now: float) -> ToolError:
        if self._permanent:
            return ToolError(
                "auth_failed",
                f"Exchange rejected credentials {self._rejections} time(s); "
                "authentication circuit breaker is permanently open until restart.",
                hint=AUTH_FAILED_HINT,
                retry_after_s=None,
            )
        remaining = 0
        if self._open_until_ts is not None and now < self._open_until_ts:
            remaining = max(1, math.ceil(self._open_until_ts - now))
        return ToolError(
            "auth_failed",
            f"Exchange rejected credentials ({self._rejections}/{self._max_rejections}); "
            f"authentication circuit breaker is open for {remaining}s.",
            hint=AUTH_FAILED_HINT,
            retry_after_s=remaining,
        )

    def check_open(self) -> None:
        with self._lock:
            now = self._now()
            if self._permanent:
                raise self._error_locked(now)
            if self._rejections > 0:
                if self._open_until_ts is not None and now < self._open_until_ts:
                    raise self._error_locked(now)
                if self._probe_in_flight:
                    raise self._error_locked(now)

    def before_call(self) -> Tuple[int, bool]:
        """Gate an Exchange operation; returns (rejection_seq, claimed_half_open)."""
        with self._lock:
            now = self._now()
            if self._permanent:
                raise self._error_locked(now)
            if self._rejections > 0:
                if self._open_until_ts is not None and now < self._open_until_ts:
                    raise self._error_locked(now)
                if self._probe_in_flight:
                    raise self._error_locked(now)
                self._probe_in_flight = True
                return self._seq, True
            return self._seq, False

    def release_half_open(self) -> None:
        with self._lock:
            self._probe_in_flight = False

    def record_rejection(self) -> ToolError:
        with self._lock:
            now = self._now()
            self._rejections += 1
            self._seq += 1
            self._probe_in_flight = False
            if self._rejections >= self._max_rejections:
                self._permanent = True
                self._open_until_ts = None
            else:
                self._open_until_ts = now + self._cooldown
            return self._error_locked(now)

    def record_success(self) -> None:
        with self._lock:
            self._rejections = 0
            self._open_until_ts = None
            self._permanent = False
            self._probe_in_flight = False

    def current_error(self) -> ToolError:
        with self._lock:
            return self._error_locked(self._now())

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def status(self) -> Dict[str, Any]:
        with self._lock:
            now = self._now()
            if self._permanent:
                next_retry_in_s: Optional[int] = None
                cooldown_remaining_s: Optional[float] = None
                open_now = True
            elif self._rejections > 0:
                if self._open_until_ts is not None and now < self._open_until_ts:
                    cooldown_remaining_s = self._open_until_ts - now
                    next_retry_in_s = max(1, math.ceil(cooldown_remaining_s))
                    open_now = True
                else:
                    cooldown_remaining_s = 0.0
                    next_retry_in_s = 0
                    open_now = self._probe_in_flight
            else:
                next_retry_in_s = None
                cooldown_remaining_s = None
                open_now = False
            return {
                "rejections": self._rejections,
                "max_rejections": self._max_rejections,
                "permanent": self._permanent,
                "open": open_now,
                "next_retry_in_s": next_retry_in_s,
                "cooldown_remaining_s": cooldown_remaining_s,
            }


class EWSGateway:
    def __init__(
        self,
        settings: Settings,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.settings = settings
        self._clock = clock
        self._breaker = AuthBreaker(
            cooldown_seconds=settings.ews_auth_failure_cooldown_seconds,
            max_rejections=settings.ews_auth_max_rejections,
            clock=clock,
        )
        self._account: Optional[Account] = None
        self._account_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, settings.ews_max_concurrency),
            thread_name_prefix="ews",
        )
        self.last_connection_error: Optional[str] = None
        self._folder_cache: Dict[str, Any] = {}
        self._folder_cache_ts = 0.0
        if settings.ews_insecure_skip_verify:
            BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
            logger.warning("TLS verification DISABLED for Exchange traffic")

    def _now(self) -> float:
        return float(self._clock()) if self._clock is not None else time.time()

    def auth_status(self) -> Dict[str, Any]:
        """Thread-safe snapshot of the authentication circuit breaker."""
        return self._breaker.status()

    # ------------------------------------------------------------- account

    def _get_or_build_account(self) -> Account:
        with self._account_lock:
            if self._account is None:
                self._account = self._build_account()
            return self._account

    @property
    def account(self) -> Account:
        self._breaker.check_open()
        return self._get_or_build_account()

    def _build_account(self) -> Account:
        s = self.settings
        BaseProtocol.TIMEOUT = s.request_timeout
        policy = RejectingFaultTolerance(
            max_wait=s.ews_retry_max_wait_seconds,
            configured_auth_type=s.ews_auth_type_force,
            on_rejection=lambda: self._breaker.record_rejection(),
        )
        kwargs: Dict[str, Any] = dict(
            service_endpoint=s.ews_server_url,
            credentials=Credentials(s.ews_username or s.ews_email, s.ews_password or ""),
            retry_policy=policy,
        )
        if s.ews_auth_type_force:
            logger.info("Using forced Exchange auth scheme: %s", s.ews_auth_type_force)
            kwargs["auth_type"] = s.ews_auth_type_force
        config = Configuration(**kwargs)
        account = Account(
            primary_smtp_address=s.ews_email,
            config=config,
            autodiscover=False,
            access_type=DELEGATE,
            default_timezone=EWSTimeZone(s.ews_tz),
        )
        # CachingProtocol keys on (service_endpoint, credentials), so a second
        # gateway sharing those gets back the cached Protocol carrying the
        # earlier gateway's retry_policy. Rebind it to this gateway's policy.
        protocol = getattr(account, "protocol", None)
        if protocol is not None and getattr(protocol, "retry_policy", None) is not policy:
            protocol.config.retry_policy = policy
        return account

    def reset(self) -> None:
        """Drop the cached account AND exchangelib's protocol-cache entry.

        Dropping only our Account is not enough: ``CachingProtocol`` hands
        the same wedged Protocol (with its already-negotiated auth type)
        right back on the next build, so a session that died mid-outage
        would never renegotiate. Clearing the cache forces a genuinely
        fresh session + auth negotiation on the next access.
        """
        with self._account_lock:
            if self._account is not None:
                try:
                    self._account.protocol.close()
                except Exception:
                    pass
                self._account = None
        try:
            CachingProtocol.clear_cache()
        except Exception as e:
            logger.debug("protocol cache clear failed: %s", e)
        self._folder_cache.clear()
        self._folder_cache_ts = 0.0

    def _handle_auth_rejection(
        self, exc: UnauthorizedError, seq_before: int
    ) -> ToolError:
        if self._breaker.seq != seq_before and self._breaker.status()["rejections"] > 0:
            err = self._breaker.current_error()
        else:
            err = self._breaker.record_rejection()
        self.last_connection_error = f"{type(exc).__name__}: {exc}"
        return err

    def test_connection(self) -> bool:
        """Real network probe — must round-trip on EVERY call.

        ``inbox.total_count`` is a cached property after its first read, so
        probing it reported warm forever once it had succeeded once (the
        false-warm bug: /readyz lied through outages and the reset ladder
        never ran). ``root.refresh()`` issues a GetFolder request each time.

        While the auth breaker is open, or when Exchange rejects credentials
        with HTTP 401, raises ``ToolError("auth_failed", ...)`` without
        retrying.
        """
        seq_before, claimed = self._breaker.before_call()
        try:
            account = self._get_or_build_account()
            account.root.refresh()
            if self._breaker.seq != seq_before:
                raise self._breaker.current_error()
            self._breaker.record_success()
            self.last_connection_error = None
            return True
        except ToolError as err:
            if err.code == "auth_failed":
                self.last_connection_error = f"ToolError: {err.message}"
                raise
            self.last_connection_error = f"{type(err).__name__}: {err}"
            logger.error("connection test failed: %s", self.last_connection_error)
            return False
        except UnauthorizedError as exc:
            err = self._handle_auth_rejection(exc, seq_before)
            logger.error("connection test rejected credentials: %s", err.message)
            raise err from exc
        except Exception as e:
            self.last_connection_error = f"{type(e).__name__}: {e}"
            logger.error("connection test failed: %s", self.last_connection_error)
            return False
        finally:
            if claimed:
                self._breaker.release_half_open()

    # ------------------------------------------------------------- calling

    async def call(self, fn: Callable[[Account], Any]) -> Any:
        """Run blocking EWS work on the bounded pool; the pool size IS the
        EWS concurrency cap (polite guest on the per-user throttle budget)."""
        _seq_gate, claimed = self._breaker.before_call()

        def run_on_pool() -> Any:
            if not claimed:
                self._breaker.check_open()
            call_seq = self._breaker.seq
            try:
                account = self._get_or_build_account()
                out = fn(account)
            except UnauthorizedError as exc:
                raise self._handle_auth_rejection(exc, call_seq) from exc
            if self._breaker.seq != call_seq:
                raise self._breaker.current_error()
            self._breaker.record_success()
            return out

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._pool, run_on_pool)
        finally:
            if claimed:
                self._breaker.release_half_open()

    # ------------------------------------------------------------- folders

    def _folder_map(self, account: Account) -> Dict[str, Any]:
        """{raw_id|lower_path: Folder} cache, rebuilt every 300s (sync)."""
        now = self._now()
        if self._folder_cache and now - self._folder_cache_ts < 300:
            return self._folder_cache
        cache: Dict[str, Any] = {}
        try:
            for folder in account.msg_folder_root.walk():
                if getattr(folder, "id", None):
                    cache[folder.id] = folder
                try:
                    path = "/".join(
                        p.name for p in folder.parts[2:]  # drop root/Top of Info Store
                    ) or folder.name
                except Exception:
                    path = folder.name
                cache[path.lower()] = folder
        except UnauthorizedError:
            raise
        except Exception as e:
            logger.warning("folder walk failed: %s", e)
        if cache:
            self._folder_cache = cache
            self._folder_cache_ts = now
        return cache

    def mail_folders(self, account: Account) -> List[Tuple[str, Any]]:
        """(path, folder) of every mail folder, without deleted items, junk,
        drafts, outbox and what lies below them (sync)."""
        skip: set[str] = set()
        for attr in ("trash", "junk", "drafts", "outbox"):
            try:
                wf = getattr(account, attr, None)
                fid = getattr(wf, "id", None) if wf is not None else None
            except Exception:
                fid = None
            if isinstance(fid, str) and fid:
                skip.add(fid)
        out: List[Tuple[str, Any]] = []
        for key, folder in self._folder_map(account).items():
            if key != getattr(folder, "id", None):
                continue  # the same folder under its path key
            if not str(getattr(folder, "folder_class", "") or "").startswith("IPF.Note"):
                continue
            parts = list(folder.parts)
            if any(getattr(p, "id", None) in skip for p in parts):
                continue
            out.append(("/".join(p.name for p in parts[2:]) or folder.name, folder))
        return out

    def resolve_folder(self, account: Account, ref: Optional[str], aliaser) -> Any:
        """well-known alias | folder alias (f12) | path | raw id → Folder (sync)."""
        if not ref:
            return account.inbox
        key = ref.strip()
        attr = WELL_KNOWN.get(key.lower()) or WELL_KNOWN.get(f"f:{key.lower()}")
        if attr:
            return getattr(account, attr)
        try:
            key = aliaser.resolve(key)  # f12 → raw id; raw/path pass through
        except KeyError as e:
            raise ToolError("validation", str(e.args[0] if e.args else e))
        cache = self._folder_map(account)
        folder = cache.get(key) or cache.get(key.lower())
        if folder is None:
            raise ToolError(
                "not_found", f"No folder matches {ref!r}.",
                hint="Use list_folders and pass one of its ids or paths.",
            )
        return folder


def paginate(query: Any, *, offset: int, limit: int,
             chunk: int = 50) -> Tuple[List[Any], Optional[int]]:
    """Materialize query[offset:offset+limit] in chunks (sync, raises on
    mid-iteration failure — the caller's error mapper classifies it).

    Returns ``(items, next_offset)``. NEVER calls ``QuerySet.count()`` —
    in exchangelib that iterates every matching id server-side, so a 20k
    inbox paid ~20k ids of round trips on every "read 10 emails". Whether
    another page exists comes from a one-item lookahead instead; callers
    that want an exact total use a refreshed ``folder.total_count`` (only
    valid for unfiltered listings) or a local mirror count.
    """
    offset = max(0, offset)
    limit = max(0, limit)
    lookahead = limit + 1
    items: List[Any] = []
    cursor = offset
    chunk = max(1, min(chunk, 250))
    while len(items) < lookahead:
        want = min(chunk, lookahead - len(items))
        batch = list(query[cursor:cursor + want])
        if not batch:
            break
        items.extend(batch)
        cursor += len(batch)
        if len(batch) < want:
            break
    next_offset = offset + limit if len(items) > limit else None
    return items[:limit], next_offset
