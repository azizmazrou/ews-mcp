"""One caller must never reach another caller's state.

These are the tests that justify doing identity threading BEFORE the gateway
pool: every case here is a real defect that already existed in the shared
process, invisible only because one mailbox meant one caller. Fixing them
while a mistake is still harmless is much cheaper than fixing them after the
second mailbox arrives.
"""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict

from conftest import oidc_settings

from ewsmcp.audit import AuditLog
from ewsmcp.auth import Principal
from ewsmcp.identity import caller_of
from ewsmcp.ids import get_aliaser
from ewsmcp.tools import writes
from ewsmcp.tools.base import Context, ToolSpec, dispatch, for_principal

ALICE = Principal(subject="user-alice", smtp="alice@corp.example",
                  issuer="https://idp.corp.example/", expires_at=9_999_999_999,
                  raw_token="alice-token")
BOB = Principal(subject="user-bob", smtp="bob@corp.example",
                issuer="https://idp.corp.example/", expires_at=9_999_999_999,
                raw_token="bob-token")


class _Gateway:
    """Records what ran, so a send can be observed without Exchange."""

    def __init__(self):
        self.calls = 0

    async def call(self, fn):
        self.calls += 1
        return {"sent": True, "internet_message_id": f"<msg-{self.calls}@corp.example>"}


def _root(tmp_path, **overrides) -> Context:
    base = {"send_enabled": True, "ews_capability_tier": "full"}
    base.update(overrides)
    settings = oidc_settings(**base)
    return Context(
        settings=settings,
        gateway=_Gateway(),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "mem")),
        audit=AuditLog(str(tmp_path / "data")),
    )


async def _handler(ctx, **kwargs) -> Dict[str, Any]:
    return {"ran": True}


def _spec(**overrides) -> ToolSpec:
    base = {
        "name": "t", "description": "test", "side_effect_class": "send",
        "input_schema": {"type": "object", "properties": {}},
        "handler": _handler, "requires_ews": False, "confirm": True,
    }
    base.update(overrides)
    return ToolSpec(**base)


def _call(ctx, spec, kwargs):
    return asyncio.run(dispatch(ctx, spec, dict(kwargs)))


# ------------------------------------------------------------- confirm tokens

def test_confirm_token_minted_for_one_caller_is_rejected_for_another(tmp_path):
    """THE test. Alice previews a send and gets a token; Bob replays it. If
    this ever passes, one caller can execute an irreversible action that
    another caller approved."""
    root = _root(tmp_path)
    spec = _spec()
    alice = for_principal(root, ALICE)
    bob = for_principal(root, BOB)

    phase1 = _call(alice, spec, {})
    assert phase1["requires_confirmation"] is True
    token = phase1["confirm_token"]

    stolen = _call(bob, spec, {"confirm_token": token})
    assert stolen["ok"] is False
    assert stolen["error"]["code"] == "confirm_invalid"

    # And Alice's own token still works — the rejection is about identity,
    # not about the token having been burned by Bob's attempt.
    assert _call(alice, spec, {"confirm_token": token}).get("ran") is True


def test_confirm_token_is_still_single_use_for_its_owner(tmp_path):
    root = _root(tmp_path)
    spec = _spec()
    alice = for_principal(root, ALICE)
    token = _call(alice, spec, {})["confirm_token"]
    assert _call(alice, spec, {"confirm_token": token}).get("ran") is True
    replay = _call(alice, spec, {"confirm_token": token})
    assert replay["error"]["code"] == "confirm_invalid"


def test_a_static_mode_token_is_not_redeemable_by_a_verified_caller(tmp_path):
    """Tokens minted before identity existed must not survive the switch."""
    static_root = _root(tmp_path, auth_mode="static", auth_upstream_mode="obo")
    spec = _spec()
    token = _call(static_root, spec, {})["confirm_token"]  # bound to no caller
    oidc_root = _root(tmp_path)
    result = _call(for_principal(oidc_root, ALICE), spec, {"confirm_token": token})
    assert result["error"]["code"] == "confirm_invalid"


# --------------------------------------------------------------- send quota

def test_one_callers_sends_do_not_consume_anothers_quota(tmp_path):
    """A shared window meant a chatty colleague could rate-cap the whole
    company — a denial of service between users, not a safety feature."""
    root = _root(tmp_path, ews_max_sends_per_hour=2)
    spec = _spec(confirm=False)
    alice = for_principal(root, ALICE)
    bob = for_principal(root, BOB)

    assert _call(alice, spec, {}).get("ran") is True
    assert _call(alice, spec, {}).get("ran") is True
    capped = _call(alice, spec, {})
    assert capped["error"]["code"] == "rate_capped"

    # Bob's budget is untouched.
    assert _call(bob, spec, {}).get("ran") is True
    assert _call(bob, spec, {}).get("ran") is True
    assert _call(bob, spec, {})["error"]["code"] == "rate_capped"


# ------------------------------------------------------------- idempotency

def test_replaying_another_callers_idempotency_key_leaks_nothing(tmp_path):
    """The worst of the pre-existing defects: the store was keyed on the
    caller-supplied key alone, so replaying someone else's key returned THEIR
    send receipt — internet_message_id included."""
    root = _root(tmp_path)
    alice = for_principal(root, ALICE)
    bob = for_principal(root, BOB)

    writes._idempotency_put(
        writes._scoped(alice, "shared-key"),
        {"draft_id": "d-alice",
         "result": {"sent": True, "internet_message_id": "<secret@corp.example>"},
         "ts": time.time()},
    )

    assert writes._idempotency_get(writes._scoped(alice, "shared-key")) is not None
    assert writes._idempotency_get(writes._scoped(bob, "shared-key")) is None


def test_idempotent_replay_skips_the_confirm_gate_only_for_its_owner(tmp_path):
    """The replay shortcut exists so a retry-after-timeout does not need a
    fresh single-use token. It must not become a way to skip the gate using
    someone else's key."""
    root = _root(tmp_path)
    alice = for_principal(root, ALICE)
    bob = for_principal(root, BOB)
    kwargs = {"draft_id": "d1", "idempotency_key": "k1"}

    writes._idempotency_put(
        writes._scoped(alice, "k1"),
        {"draft_id": "d1", "result": {"sent": True}, "ts": time.time()},
    )

    assert writes._send_confirm_needed(alice, kwargs) is False
    assert writes._send_confirm_needed(bob, kwargs) is True


# ------------------------------------------------------------------ identity

def test_caller_falls_back_to_the_configured_mailbox_without_a_principal(tmp_path):
    root = _root(tmp_path)
    caller = caller_of(root)
    assert caller.smtp == root.settings.ews_email.lower()
    assert caller.subject == "-"


def test_principal_is_keyed_by_issuer_and_subject_not_by_address(tmp_path):
    """Addresses get reassigned when people leave; `sub` does not. Keying on
    the address would hand a successor the predecessor's namespace."""
    root = _root(tmp_path)
    renamed = Principal(subject=ALICE.subject, smtp="alice.newname@corp.example",
                        issuer=ALICE.issuer, expires_at=ALICE.expires_at)
    assert caller_of(for_principal(root, ALICE)).subject == \
        caller_of(for_principal(root, renamed)).subject
    successor = Principal(subject="user-new-hire", smtp=ALICE.smtp,
                          issuer=ALICE.issuer, expires_at=ALICE.expires_at)
    assert caller_of(for_principal(root, successor)).subject != \
        caller_of(for_principal(root, ALICE)).subject


# --------------------------------------------------- what stays deliberately shared

def test_counters_and_circuit_stay_on_the_root(tmp_path):
    """Per-request clones must not fragment the shared state: counters would
    vanish and each caller would get a private circuit that never opens."""
    root = _root(tmp_path)
    spec = _spec(confirm=False)
    _call(for_principal(root, ALICE), spec, {})
    _call(for_principal(root, BOB), spec, {})
    assert root.counters["tool.t"] == 2

    clone = for_principal(root, ALICE)
    clone.root._circuit_failures = 3
    assert root._circuit_failures == 3


# ------------------------------------------------------ per-caller resources

def _bound(root, principal):
    from conftest import make_binder
    root.binder = make_binder(root.settings)
    try:
        return asyncio.run(root.binder.bind(root, principal))
    finally:
        asyncio.run(root.binder.aclose())


def test_each_caller_gets_a_private_alias_namespace(tmp_path):
    """Aliases map short handles (m12) to raw EWS ids. Sharing the namespace
    across callers would let one caller's handle resolve into another's
    mailbox — the ids are opaque, so it would not even look wrong."""
    root = _root(tmp_path)
    alice = _bound(root, ALICE)
    bob = _bound(root, BOB)
    assert alice.aliaser is not bob.aliaser
    assert alice.aliaser is not root.aliaser


def test_the_alias_namespace_is_keyed_on_the_subject_not_the_address(tmp_path):
    root = _root(tmp_path)
    renamed = Principal(subject=ALICE.subject, smtp="alice.newname@corp.example",
                        issuer=ALICE.issuer, expires_at=ALICE.expires_at)
    assert _bound(root, ALICE).aliaser is _bound(root, renamed).aliaser


def test_alias_directories_do_not_spell_out_who_works_here(tmp_path):
    """DESIGN.md law #6: a listing of DATA_DIR/users must not be a staff
    roster, so the directory name is a salted hash of the subject."""
    root = _root(tmp_path)
    _bound(root, ALICE)
    users = Path(root.settings.data_dir) / "users"
    names = [p.name for p in users.iterdir()] if users.exists() else []
    assert names, "a per-caller namespace should have been created"
    assert not any("alice" in n or "corp.example" in n for n in names)


def test_each_caller_gets_their_own_exchange_session(tmp_path):
    root = _root(tmp_path)
    assert _bound(root, ALICE).gateway.mailbox == "alice@corp.example"
    assert _bound(root, BOB).gateway.mailbox == "bob@corp.example"


def test_a_clone_of_a_clone_still_points_at_the_root(tmp_path):
    root = _root(tmp_path)
    nested = for_principal(for_principal(root, ALICE), BOB)
    assert nested.root is root
    assert nested.principal is BOB
