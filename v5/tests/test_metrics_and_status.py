"""Observability that does not become a staff directory.

Cardinality is the whole design constraint here. Labelling a metric by
mailbox or subject would mean one time series per person per tool — thousands
of series that grow with headcount, and an employee list readable by anyone
who can scrape /metrics. Per-caller attribution belongs in the audit log,
which is hash-chained and identity-stamped; Prometheus gets aggregates.
"""

import asyncio

from conftest import ISSUER, make_binder, make_verifier, oidc_settings

from ewsmcp.audit import AuditLog
from ewsmcp.auth import Principal
from ewsmcp.http import _metrics_text
from ewsmcp.ids import get_aliaser
from ewsmcp.tools.base import Context, for_principal
from ewsmcp.tools.calendar_people import _get_server_status

ALICE = Principal(subject="user-alice", smtp="alice@corp.example", issuer=ISSUER,
                  expires_at=9e9, raw_token="alice-token")
BOB = Principal(subject="user-bob", smtp="bob@corp.example", issuer=ISSUER,
                expires_at=9e9, raw_token="bob-token")


def _ctx(tmp_path, settings=None, *, with_binder=True) -> Context:
    settings = settings or oidc_settings()
    ctx = Context(settings=settings, gateway=None, manager=None,
                  aliaser=get_aliaser(str(tmp_path / "alias")),
                  audit=AuditLog(str(tmp_path / "audit")))
    ctx.registry = {}
    if with_binder:
        ctx.binder = make_binder(settings)
    return ctx


def _with_callers(ctx, *principals):
    async def run():
        for principal in principals:
            await ctx.binder.bind(ctx, principal)

    asyncio.run(run())
    return ctx


# ------------------------------------------------------------------ metrics

def test_pool_and_exchange_activity_is_exported(tmp_path, rsa_keys):
    ctx = _with_callers(_ctx(tmp_path), ALICE, BOB)
    text = _metrics_text(ctx, make_verifier(rsa_keys))
    assert "ewsmcp_active_principals 2" in text
    assert "ewsmcp_obo_exchanges_total 2" in text
    assert "ewsmcp_gateway_pool_evictions_total 0" in text


def test_no_metric_is_labelled_by_who_is_calling(tmp_path, rsa_keys):
    """The cardinality guard, and the privacy one: /metrics must not name a
    single human being."""
    ctx = _with_callers(_ctx(tmp_path), ALICE, BOB)
    text = _metrics_text(ctx, make_verifier(rsa_keys))
    for forbidden in ("alice", "bob", "corp.example", "user-alice", ISSUER):
        assert forbidden not in text, f"{forbidden!r} leaked into /metrics"


def test_rejection_reasons_come_from_a_bounded_vocabulary(tmp_path, rsa_keys):
    """Labels are safe only because the reason set is small and fixed — never
    a message, never anything derived from a token."""
    verifier = make_verifier(rsa_keys)
    verifier.rejections.update({"expired": 3, "bad_signature": 1})
    text = _metrics_text(_ctx(tmp_path, with_binder=False), verifier)
    assert 'ewsmcp_jwt_rejections_total{reason="expired"} 3' in text
    assert 'ewsmcp_jwt_rejections_total{reason="bad_signature"} 1' in text


def test_a_stale_key_set_is_visible(tmp_path, rsa_keys):
    """An IdP outage is survivable (we serve the last good keys) but must not
    be invisible."""
    verifier = make_verifier(rsa_keys)
    verifier.jwks.stale = True
    assert "ewsmcp_jwks_stale 1" in _metrics_text(_ctx(tmp_path, with_binder=False),
                                                  verifier)


def test_static_mode_metrics_are_unchanged(tmp_path):
    """The canary: an existing dashboard must not break."""
    from conftest import make_settings
    text = _metrics_text(_ctx(tmp_path, make_settings(), with_binder=False))
    assert "ewsmcp_uptime_seconds" in text
    assert "ewsmcp_active_principals" not in text


# -------------------------------------------------- reaching /metrics at all

def _drive(app, path, headers):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "http", "path": path, "method": "GET",
                     "headers": list(headers)}, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _app(tmp_path, rsa_keys, **overrides):
    from ewsmcp.http import build_app
    settings = oidc_settings(**overrides)
    ctx = _ctx(tmp_path, settings)
    return build_app(ctx, settings, verifier=make_verifier(rsa_keys, settings))


def test_a_scraper_reaches_metrics_with_the_operator_key(tmp_path, rsa_keys):
    """Found by running the real server: a Prometheus scraper has no user
    identity and must not need one. /metrics carries no mailbox data, so the
    operator key is the right credential for it — demanding a user JWT made
    the endpoint unscrapable in oidc mode."""
    app = _app(tmp_path, rsa_keys, mcp_api_key="operator-key")
    assert _drive(app, "/metrics",
                  [(b"authorization", b"Bearer operator-key")]) == 200
    assert _drive(app, "/openapi.json",
                  [(b"authorization", b"Bearer operator-key")]) == 200


def test_metrics_still_refuses_an_anonymous_scraper(tmp_path, rsa_keys):
    app = _app(tmp_path, rsa_keys, mcp_api_key="operator-key")
    assert _drive(app, "/metrics", []) == 401


def test_without_an_operator_key_metrics_needs_a_verified_token(tmp_path, rsa_keys):
    """Fail closed: no key configured must never mean "open to anyone"."""
    app = _app(tmp_path, rsa_keys)
    assert _drive(app, "/metrics", []) == 401


def test_a_tool_call_is_never_reachable_with_the_operator_key_alone(tmp_path, rsa_keys):
    """The exemption is scoped to endpoints with no mailbox data — mail must
    still require a verified caller."""
    app = _app(tmp_path, rsa_keys, mcp_api_key="operator-key")
    assert _drive(app, "/api/tools", [(b"authorization", b"Bearer operator-key")]) == 401


# ------------------------------------------------------------ server status

def test_status_answers_who_am_i_without_a_29th_tool(tmp_path):
    """Adding a `whoami` tool would trip the generated tool table and its
    count assertions; get_server_status already runs without Exchange."""
    ctx = _ctx(tmp_path)
    status = asyncio.run(_get_server_status(for_principal(ctx, ALICE)))
    assert status["caller"]["mailbox"] == "alice@corp.example"
    assert status["caller"]["authenticated"] is True
    assert status["caller"]["subject_hash"]
    assert "user-alice" not in status["caller"]["subject_hash"]


def test_status_reports_the_pool(tmp_path):
    ctx = _with_callers(_ctx(tmp_path), ALICE, BOB)
    status = asyncio.run(_get_server_status(for_principal(ctx, ALICE)))
    assert status["pool"]["active_principals"] == 2


def test_status_counters_come_from_the_root_not_the_clone(tmp_path):
    """Counters live on the shared root; reading them off a per-request clone
    would report zero for everything."""
    ctx = _ctx(tmp_path)
    ctx.bump("tool.search_messages")
    status = asyncio.run(_get_server_status(for_principal(ctx, ALICE)))
    assert status["counters"]["tool.search_messages"] == 1


def test_status_in_static_mode_names_the_configured_mailbox(tmp_path):
    from conftest import make_settings
    ctx = _ctx(tmp_path, make_settings(), with_binder=False)
    status = asyncio.run(_get_server_status(ctx))
    assert status["caller"]["authenticated"] is False
    assert status["caller"]["subject_hash"] is None
    assert "pool" not in status
