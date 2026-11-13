"""Connection lifecycle manager — lazy, self-healing, bounded on 401.

Some Exchange deployments fail or time out on exchangelib's unauthenticated
auth-type probe for *fresh* connections ("Failed to get auth type from
service") while warm, long-lived sessions keep working because exchangelib
probes once per process and caches the negotiated auth scheme. Startup
therefore never gates on a successful connection. Tools register and
transports bind immediately; this manager keeps trying in the background
with exponential backoff + full jitter for transient network faults, while
deferring to the gateway's authentication circuit breaker on HTTP 401
credential rejections and stopping once the rejection cap is reached.

States:
    connecting  — never connected since process start; warmup loop running
    warm        — last probe/connect succeeded
    degraded    — was warm, a later heartbeat hit a transient fault; re-armed
    auth_failed — Exchange rejected our credentials (HTTP 401); waiting on
                  the auth breaker cooldown or stopped permanently
"""

import asyncio
import logging
import random
import threading
import time
from typing import Any, Awaitable, Callable, Coroutine, Dict, Optional

from ..errors import ToolError

STATE_CONNECTING = "connecting"
STATE_WARM = "warm"
STATE_DEGRADED = "degraded"
STATE_AUTH_FAILED = "auth_failed"

# After this many consecutive transient failures, escalate the recovery ladder:
# drop the cached Account/Protocol so the next attempt re-runs the full
# auth negotiation on a fresh session instead of reusing a wedged one.
# Never run on HTTP 401 rejections.
_RESET_EVERY_N_FAILURES = 3


class ConnectionManager:
    """Owns the background warmup/heartbeat lifecycle for one EWSGateway."""

    def __init__(
        self,
        ews_client: Any,
        initial_backoff: float = 1.0,
        max_backoff: float = 300.0,
        heartbeat_seconds: int = 600,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ):
        self._client = ews_client
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._heartbeat_seconds = heartbeat_seconds
        if clock is None:
            clock = getattr(ews_client, "_clock", None)
        self._clock = clock
        self._sleep = sleep
        self.logger = logging.getLogger(__name__)

        # Status fields are read from the event loop (readyz/get_server_status)
        # and written from worker threads — guard with a plain lock.
        self._lock = threading.Lock()
        self._state = STATE_CONNECTING
        self._attempts = 0
        self._transient_failures = 0
        self._last_error: Optional[str] = None
        self._last_success_ts: Optional[float] = None
        self._next_retry_ts: Optional[float] = None

        self._task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._started = False
        self._stopped = False
        self._on_warm: Optional[Callable[[], Coroutine[Any, Any, None]]] = None
        self._on_warm_fired = False

        breaker = getattr(ews_client, "_breaker", None)
        add_listener = getattr(breaker, "add_rejection_listener", None)
        if callable(add_listener):
            add_listener(self._on_breaker_rejection)

    def _now(self) -> float:
        return float(self._clock()) if self._clock is not None else time.time()

    async def _sleep_for(self, delay: float) -> None:
        if self._sleep is not None:
            await self._sleep(delay)
            return
        if self._clock is None:
            await asyncio.sleep(delay)
            return
        if delay <= 0:
            await asyncio.sleep(0)
            return
        deadline = self._now() + delay
        while not self._stopped:
            rem = deadline - self._now()
            if rem <= 0:
                return
            await asyncio.sleep(min(rem, 0.01))

    def _auth_cooldown_floor(self) -> float:
        """Minimum wait after any auth failure, read from the gateway settings."""
        settings = getattr(self._client, "settings", None)
        configured = getattr(settings, "ews_auth_failure_cooldown_seconds", None)
        if configured is not None:
            return float(max(60, int(configured)))
        return 900.0

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> str:
        auth = self._client.auth_status()
        if auth["rejections"] > 0:
            return STATE_AUTH_FAILED
        with self._lock:
            return self._state

    @property
    def is_warm(self) -> bool:
        return self.state == STATE_WARM

    def status(self) -> Dict[str, Any]:
        """Snapshot for /readyz and get_server_status. Never blocks on EWS."""
        auth = self._client.auth_status()
        with self._lock:
            now = self._now()
            effective_state = (
                STATE_AUTH_FAILED if auth["rejections"] > 0 else self._state
            )
            if effective_state == STATE_AUTH_FAILED:
                next_retry_in_s = (
                    None if auth["permanent"] else auth["next_retry_in_s"]
                )
            elif self._next_retry_ts is not None and effective_state != STATE_WARM:
                next_retry_in_s = max(0, int(self._next_retry_ts - now))
            else:
                next_retry_in_s = None
            return {
                "state": effective_state,
                "attempts": self._attempts,
                "last_error": self._last_error,
                "last_success_age_s": (
                    int(now - self._last_success_ts)
                    if self._last_success_ts is not None
                    else None
                ),
                "next_retry_in_s": next_retry_in_s,
                "auth_rejections": auth["rejections"],
                "auth_permanent": auth["permanent"],
            }

    # ------------------------------------------------------------- lifecycle

    async def start(
        self,
        on_warm: Optional[Callable[[], Coroutine[Any, Any, None]]] = None,
    ) -> None:
        """Begin the background warmup loop. Returns immediately."""
        self._loop = asyncio.get_running_loop()
        self._started = True
        self._on_warm = on_warm
        self._task = asyncio.create_task(self._warmup_loop(), name="ews-warmup")

    async def stop(self) -> None:
        self._stopped = True
        breaker = getattr(self._client, "_breaker", None)
        remove_listener = getattr(breaker, "remove_rejection_listener", None)
        if callable(remove_listener):
            remove_listener(self._on_breaker_rejection)
        for task in (self._task, self._heartbeat_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    def _on_breaker_rejection(self) -> None:
        """Arm cooldown recovery when a 401 trips the breaker off the warmup loop."""
        if not self._started or self._stopped:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._ensure_auth_warmup()
            return
        try:
            loop.call_soon_threadsafe(self._ensure_auth_warmup)
        except RuntimeError:
            pass

    def _ensure_auth_warmup(self) -> None:
        """Ensure a warmup loop is armed to wait out cooldown and probe once."""
        if not self._started or self._stopped:
            return
        if self._task is not None and not self._task.done():
            return
        auth = self._client.auth_status()
        if auth["rejections"] <= 0:
            return
        last_err = getattr(self._client, "last_connection_error", None)
        with self._lock:
            if self._state == STATE_WARM:
                self._state = STATE_DEGRADED
            if last_err:
                self._last_error = str(last_err)[:500]
            if auth["permanent"]:
                self._next_retry_ts = None
                return
        self._task = asyncio.create_task(
            self._warmup_loop(initial_auth_wait=True),
            name="ews-warmup",
        )

    async def _warmup_loop(self, *, initial_auth_wait: bool = False) -> None:
        backoff = self._initial_backoff
        while not self._stopped:
            pre_auth = self._client.auth_status()
            floor = self._auth_cooldown_floor()
            with self._lock:
                pre_now = self._now()
                if pre_auth["permanent"]:
                    self._next_retry_ts = None
                    return
                raw_pre_wait = pre_auth.get("cooldown_remaining_s")
                must_wait = (
                    bool(pre_auth.get("open"))
                    or initial_auth_wait
                    or (raw_pre_wait is not None and raw_pre_wait > 0)
                )
                if must_wait:
                    pre_wait: Optional[float] = max(
                        floor,
                        float(raw_pre_wait) if raw_pre_wait is not None else 0.0,
                    )
                    self._next_retry_ts = pre_now + pre_wait
                else:
                    pre_wait = None
            if pre_wait is not None:
                initial_auth_wait = False
                await self._sleep_for(pre_wait)
                continue

            outcome = await asyncio.to_thread(self._attempt_probe)
            if outcome == "ok":
                self._mark_warm()
                await self._fire_on_warm()
                self._start_heartbeat()
                return

            if outcome == "auth_failed":
                auth = self._client.auth_status()
                floor = self._auth_cooldown_floor()
                with self._lock:
                    now = self._now()
                    if auth["permanent"]:
                        self._next_retry_ts = None
                        self.logger.error(
                            "warmup: Exchange rejected credentials %d time(s); "
                            "stopping background probes until restart",
                            auth["rejections"],
                        )
                        return
                    rem = auth.get("cooldown_remaining_s")
                    delay = max(floor, float(rem) if rem is not None else 0.0)
                    self._next_retry_ts = now + delay
                await self._sleep_for(delay)
                continue

            with self._lock:
                transient = self._transient_failures
            # Recovery ladder for transient faults ONLY: every Nth failure,
            # drop the cached Account/Protocol so the next try renegotiates
            # auth on a genuinely fresh session.
            if transient > 0 and transient % _RESET_EVERY_N_FAILURES == 0:
                try:
                    await asyncio.to_thread(self._client.reset)
                    self.logger.info(
                        "warmup: reset cached EWS session after %d failed attempts",
                        transient,
                    )
                except Exception as exc:
                    self.logger.debug("warmup: session reset failed: %s", exc)
            # Full jitter: sleep U(0, backoff); cap growth at max_backoff.
            delay = random.uniform(0, backoff)
            with self._lock:
                self._next_retry_ts = self._now() + delay
            await self._sleep_for(delay)
            backoff = min(backoff * 2, self._max_backoff)

    def _attempt_probe(self) -> str:
        """Blocking connect/probe attempt; runs in a worker thread.

        Returns ``"ok"``, ``"auth_failed"``, or ``"transient"``.
        """
        try:
            ok = self._client.test_connection()
        except ToolError as err:
            if err.code == "auth_failed":
                self._mark_auth_failure(f"{type(err).__name__}: {err.message}")
                return "auth_failed"
            self._mark_transient_failure(f"{type(err).__name__}: {err}")
            return "transient"
        except Exception as exc:
            self._mark_transient_failure(f"{type(exc).__name__}: {exc}")
            return "transient"
        if ok:
            return "ok"
        self._mark_transient_failure(
            getattr(self._client, "last_connection_error", None)
            or "connection test returned False"
        )
        return "transient"

    def _try_connect(self) -> bool:
        """Compatibility wrapper returning True only on a warm probe."""
        return self._attempt_probe() == "ok"

    # ------------------------------------------------------------- heartbeat

    def _start_heartbeat(self) -> None:
        if self._heartbeat_seconds <= 0 or self._stopped:
            return
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="ews-heartbeat"
        )

    async def _heartbeat_loop(self) -> None:
        while not self._stopped:
            await self._sleep_for(float(self._heartbeat_seconds))
            if self._stopped or (self._task is not None and not self._task.done()):
                return
            if self._client.auth_status()["rejections"] > 0:
                self._ensure_auth_warmup()
                return
            outcome = await asyncio.to_thread(self._attempt_probe)
            if outcome == "ok":
                self._mark_warm()
                continue
            if outcome == "auth_failed":
                auth = self._client.auth_status()
                with self._lock:
                    self._state = STATE_DEGRADED
                    if auth["permanent"]:
                        self._next_retry_ts = None
                self.logger.warning(
                    "heartbeat: Exchange rejected credentials — state=auth_failed"
                )
                self._ensure_auth_warmup()
                return
            with self._lock:
                self._state = STATE_DEGRADED
            self.logger.warning(
                "heartbeat: EWS probe failed — state=degraded, re-arming warmup"
            )
            # Re-enter the warmup loop (which restarts the heartbeat on
            # success). Exit this heartbeat; warmup owns recovery now.
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(
                    self._warmup_loop(), name="ews-warmup"
                )
            return

    def _probe(self) -> bool:
        """Cheap liveness probe routed through the gateway."""
        return self._attempt_probe() == "ok"

    # --------------------------------------------------------------- helpers

    def _mark_warm(self) -> None:
        with self._lock:
            previous = self._state
            self._state = STATE_WARM
            self._transient_failures = 0
            self._last_success_ts = self._now()
            self._next_retry_ts = None
            self._last_error = None
        if previous != STATE_WARM:
            self.logger.info("EWS connection established (state=warm)")

    def _mark_transient_failure(self, error: str) -> None:
        with self._lock:
            self._attempts += 1
            self._transient_failures += 1
            self._last_error = error[:500]

    def _mark_auth_failure(self, error: str) -> None:
        with self._lock:
            self._attempts += 1
            self._transient_failures = 0
            self._last_error = error[:500]

    async def _fire_on_warm(self) -> None:
        """Run the on-warm callback exactly once per process."""
        if self._on_warm is None or self._on_warm_fired:
            return
        self._on_warm_fired = True
        try:
            await self._on_warm()
        except Exception as exc:
            self.logger.warning("on_warm callback failed: %s", exc)
