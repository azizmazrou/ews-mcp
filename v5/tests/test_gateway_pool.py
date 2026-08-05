"""Per-caller Exchange sessions: isolation, recycling, and the leak guard.

The pool is where "one process, many mailboxes" actually happens, so the
tests below are mostly about resources rather than features: a server that
serves the right mailbox but leaks a connection pool every hour is not a
server anyone can run.
"""

import asyncio

import pytest
from conftest import ISSUER, oidc_settings

from ewsmcp.auth import Principal
from ewsmcp.auth.obo import ExchangeToken
from ewsmcp.gateway.pool import GatewayPool

ALICE = Principal(subject="user-alice", smtp="alice@corp.example", issuer=ISSUER,
                  expires_at=9e9, raw_token="alice-token")
BOB = Principal(subject="user-bob", smtp="bob@corp.example", issuer=ISSUER,
                expires_at=9e9, raw_token="bob-token")


def _token(value="ews-token", ttl=3600):
    import time
    return ExchangeToken(access_token=value, expires_at=time.time() + ttl)


def _pool(**overrides):
    return GatewayPool(oidc_settings(**overrides))


def _run(factory):
    return asyncio.run(factory())


# ------------------------------------------------------------------ isolation

def test_each_caller_gets_their_own_mailbox():
    pool = _pool()

    async def run():
        alice = await pool.acquire(ALICE, _token())
        bob = await pool.acquire(BOB, _token())
        await pool.aclose()
        return alice, bob

    alice, bob = _run(run)
    assert alice is not bob
    assert alice.mailbox == "alice@corp.example"
    assert bob.mailbox == "bob@corp.example"


def test_the_same_caller_reuses_one_session():
    pool = _pool()

    async def run():
        first = await pool.acquire(ALICE, _token())
        second = await pool.acquire(ALICE, _token())
        await pool.aclose()
        return first, second

    first, second = _run(run)
    assert first is second


def test_the_account_is_built_for_the_caller_with_delegate_access(monkeypatch):
    """The requirement, encoded: the caller's own token opens the caller's own
    mailbox. No impersonation, and no service account that could open every
    mailbox — so what matters is exactly these three arguments."""
    from exchangelib import DELEGATE, OAUTH2

    from ewsmcp.gateway import client as client_module
    captured = {}

    def fake_account(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(client_module, "Account", fake_account)
    pool = _pool()

    async def run():
        gateway = await pool.acquire(ALICE, _token())
        gateway._build_account()
        await pool.aclose()

    _run(run)
    assert captured["primary_smtp_address"] == "alice@corp.example"
    assert captured["access_type"] is DELEGATE
    assert captured["config"].auth_type == OAUTH2
    assert captured["autodiscover"] is False


def test_no_code_in_the_package_uses_impersonation():
    """A sentinel, deliberately: `ApplicationImpersonation` — a credential
    that can open every mailbox — is the capability this whole design exists
    to avoid, and it must not reappear by accident.

    Comments and docstrings are stripped first: prose EXPLAINING the ban is
    exactly what we want to keep, so a blunt grep would fight the docs.
    """
    import io
    import tokenize
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "ewsmcp"
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        code = "".join(
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.COMMENT, tokenize.STRING)
        )
        if "IMPERSONATION" in code.upper():
            offenders.append(path.relative_to(package.parent).as_posix())
    assert offenders == []


def test_oauth_credentials_are_pinned_to_the_oauth2_auth_type():
    """exchangelib cannot probe for Bearer, so oidc is the one place
    DESIGN.md law #5 permits pinning auth_type."""
    from exchangelib import OAUTH2
    pool = _pool()

    async def run():
        gateway = await pool.acquire(ALICE, _token())
        await pool.aclose()
        return gateway

    credentials, auth_type = _run(run)._cred_provider()
    assert auth_type == OAUTH2
    assert credentials.access_token["access_token"] == "ews-token"


# ------------------------------------------------------------- token refresh

def test_refresh_mutates_the_credentials_in_place():
    """THE leak guard. A fresh credentials object per refresh would be a
    protocol-cache miss and would mint (and strand) a new session pool every
    hour, per caller. See test_exchangelib_signatures.py for the pin."""
    pool = _pool()

    async def run():
        await pool.acquire(ALICE, _token("tok-1"))
        entry = pool._entries[ALICE.key]
        before = entry.credentials
        await pool.acquire(ALICE, _token("tok-2"))
        after = pool._entries[ALICE.key].credentials
        await pool.aclose()
        return before, after

    before, after = _run(run)
    assert before is after, "the credentials OBJECT must survive the refresh"
    assert after.access_token["access_token"] == "tok-2"


def test_refresh_does_not_rebuild_the_gateway():
    pool = _pool()

    async def run():
        first = await pool.acquire(ALICE, _token("tok-1"))
        second = await pool.acquire(ALICE, _token("tok-2"))
        await pool.aclose()
        return first, second

    first, second = _run(run)
    assert first is second


# ----------------------------------------------------------------- eviction

def test_least_recently_used_is_evicted_over_capacity():
    pool = _pool(auth_gateway_pool_max=2)

    async def run():
        for name in ("a", "b"):
            await pool.acquire(
                Principal(subject=name, smtp=f"{name}@corp.example",
                          issuer=ISSUER, expires_at=9e9), _token())
        await pool.acquire(ALICE, _token())  # third caller, over capacity
        mailboxes = pool.mailboxes()
        await pool.aclose()
        return mailboxes

    mailboxes = _run(run)
    assert len(mailboxes) == 2
    assert "a@corp.example" not in mailboxes  # the oldest went
    assert "alice@corp.example" in mailboxes


def test_recent_use_protects_a_caller_from_eviction():
    pool = _pool(auth_gateway_pool_max=2)

    async def run():
        await pool.acquire(ALICE, _token())
        await pool.acquire(BOB, _token())
        await pool.acquire(ALICE, _token())  # alice is now the most recent
        await pool.acquire(
            Principal(subject="c", smtp="c@corp.example", issuer=ISSUER,
                      expires_at=9e9), _token())
        mailboxes = pool.mailboxes()
        await pool.aclose()
        return mailboxes

    mailboxes = _run(run)
    assert "alice@corp.example" in mailboxes
    assert "bob@corp.example" not in mailboxes


def test_idle_callers_are_swept():
    pool = _pool(auth_gateway_idle_ttl_seconds=1)

    async def run():
        await pool.acquire(ALICE, _token())
        pool._entries[ALICE.key].last_used -= 3600  # pretend an hour passed
        await pool.acquire(BOB, _token())  # any acquire sweeps
        mailboxes = pool.mailboxes()
        await pool.aclose()
        return mailboxes

    assert _run(run) == ("bob@corp.example",)


def test_eviction_never_wipes_the_global_protocol_cache():
    """THE cross-caller hazard: CachingProtocol's cache is process-wide, so
    clearing it to recycle one caller would drop every other caller's live
    Protocol — and their in-flight work with it."""
    from exchangelib.protocol import CachingProtocol
    pool = _pool()
    calls = []
    original = CachingProtocol.clear_cache
    CachingProtocol.clear_cache = staticmethod(lambda: calls.append(1))
    try:
        async def run():
            await pool.acquire(ALICE, _token())
            pool.evict(ALICE.key)
            await pool.aclose()

        _run(run)
    finally:
        CachingProtocol.clear_cache = original
    assert calls == []


def test_evictions_are_counted():
    pool = _pool(auth_gateway_pool_max=1)

    async def run():
        await pool.acquire(ALICE, _token())
        await pool.acquire(BOB, _token())
        stats = pool.stats()
        await pool.aclose()
        return stats

    stats = _run(run)
    assert stats["evictions"] >= 1
    assert stats["active_principals"] == 1


# ------------------------------------------------------------- concurrency

def test_the_executor_is_shared_across_callers():
    """One executor per caller would multiply the thread count — and the
    politeness budget against Exchange's throttling — by the user count."""
    pool = _pool()

    async def run():
        alice = await pool.acquire(ALICE, _token())
        bob = await pool.acquire(BOB, _token())
        await pool.aclose()
        return alice, bob

    alice, bob = _run(run)
    assert alice._pool is bob._pool


def test_each_caller_has_a_private_share_of_the_shared_budget():
    pool = _pool(ews_max_concurrency_per_user=2)

    async def run():
        alice = await pool.acquire(ALICE, _token())
        bob = await pool.acquire(BOB, _token())
        await pool.aclose()
        return alice, bob

    alice, bob = _run(run)
    assert alice._semaphore is not bob._semaphore
    assert alice._semaphore._value == 2


def test_a_busy_caller_cannot_exceed_their_share():
    pool = _pool(ews_max_concurrency_per_user=2)

    async def run():
        gateway = await pool.acquire(ALICE, _token())
        peak = {"now": 0, "max": 0}
        gate = asyncio.Event()

        def work(account):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            import time as _t
            _t.sleep(0.02)
            peak["now"] -= 1
            return True

        object.__setattr__(gateway, "_account", object())  # skip the real build
        gate.set()
        await asyncio.gather(*(gateway.call(work) for _ in range(8)))
        await pool.aclose()
        return peak["max"]

    assert _run(run) <= 2


# ------------------------------------------------------------------ sweeping

def test_close_is_idempotent_and_safe_on_an_empty_pool():
    pool = _pool()
    _run(pool.aclose)
    _run(pool.aclose)


@pytest.mark.parametrize("ttl", [0, -1])
def test_a_disabled_idle_ttl_keeps_everyone(ttl):
    pool = _pool(auth_gateway_idle_ttl_seconds=ttl)

    async def run():
        await pool.acquire(ALICE, _token())
        pool._entries[ALICE.key].last_used -= 10_000
        await pool.acquire(BOB, _token())
        mailboxes = pool.mailboxes()
        await pool.aclose()
        return mailboxes

    assert len(_run(run)) == 2
