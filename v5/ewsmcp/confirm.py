"""Two-phase confirm tokens for irreversible operations.

Stateless HMAC tokens bind a *previewed* action to its execution: the second
call must echo the token, and the token is rejected if the underlying content
changed (``content_hash``) or the TTL lapsed. This is the defense that holds
even when the calling LLM is prompt-injected by hostile mail content — the
human / policy layer sees the preview before the token is minted, and a
"send the contract to attacker@evil" instruction still has to survive a
preview that says exactly that.

Stateless by design: nothing is persisted, so tokens survive process restarts
only while the secret is stable. Set ``SEND_CONFIRM_SECRET`` to keep tokens
valid across restarts; otherwise a per-process secret is used (fine given the
10-minute default TTL).

Single-use: a verified token must also be *consumed* exactly once
(``consume_token``). The consumption record is in-process only — after a
restart an unexpired token could in principle be replayed, which is why the
TTL stays short and the record is a defense-in-depth layer on top of the
content binding, not a substitute for it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

DEFAULT_TTL_SECONDS = 600

# sig -> expiry_ts of tokens already spent on a successful phase-2.
_CONSUMED: Dict[str, float] = {}
_CONSUMED_LOCK = threading.Lock()


def consume_token(token: str) -> bool:
    """Mark a (previously verified) token as spent.

    Returns False when the token was already consumed. Expired records are
    pruned opportunistically so the registry stays bounded by the TTL.
    """
    sig = token.rsplit(".", 1)[-1]
    expiry = time.time() + DEFAULT_TTL_SECONDS
    try:  # prefer the token's own expiry for the prune horizon
        raw = base64.urlsafe_b64decode(token.rsplit(".", 1)[0].encode("ascii"))
        expiry = float(json.loads(raw).get("e", expiry))
    except Exception:
        pass
    now = time.time()
    with _CONSUMED_LOCK:
        for stale in [s for s, exp in _CONSUMED.items() if exp < now]:
            del _CONSUMED[stale]
        if sig in _CONSUMED:
            return False
        _CONSUMED[sig] = expiry
    return True


def reset_consumed_tokens() -> None:
    """Tests only."""
    with _CONSUMED_LOCK:
        _CONSUMED.clear()

# Per-process fallback secret when SEND_CONFIRM_SECRET is unset. Tokens then
# do not survive a restart — acceptable given the short TTL.
_PROCESS_SECRET = secrets.token_hex(32)


def _secret(explicit: Optional[str] = None) -> bytes:
    val = explicit or os.environ.get("SEND_CONFIRM_SECRET") or _PROCESS_SECRET
    return val.encode("utf-8")


def content_hash(*parts: Any) -> str:
    """Stable hash over the previewed content (subject, recipients, body…).

    Each part is JSON-serialized with sorted keys so dict ordering doesn't
    change the hash. Callers that want order-independence for lists (e.g.
    recipients) must sort the list before passing it in.
    """
    h = hashlib.sha256()
    for p in parts:
        h.update(b"\x1f")
        h.update(
            json.dumps(p, default=str, sort_keys=True, ensure_ascii=False).encode("utf-8")
        )
    return h.hexdigest()


def make_token(
    *,
    mailbox: str,
    subject: str,
    action: str,
    target_id: str,
    chash: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    secret: Optional[str] = None,
) -> dict:
    """Mint a confirm token bound to (subject, mailbox, action, target_id, chash).

    ``subject`` is the CALLER, and it is inside the HMAC input — so one caller
    can neither forge nor redeem another's token, and since ``_CONSUMED`` is
    keyed on the signature (which now covers the subject), single-use stays
    correct per caller rather than globally.

    It is the token subject rather than the address on purpose: a mailbox
    rename mid-TTL should invalidate the token, and a reassigned address must
    never let a successor redeem their predecessor's pending confirmation.
    """
    exp = int(time.time()) + int(ttl_seconds)
    payload = {"m": mailbox, "u": subject, "a": action, "t": target_id,
               "c": chash, "e": exp}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_secret(secret), raw, hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(raw).decode("ascii") + "." + sig
    return {"confirm_token": token, "expires_at": exp, "ttl_seconds": int(ttl_seconds)}


def verify_token(
    token: str,
    *,
    mailbox: str,
    subject: str,
    action: str,
    target_id: str,
    chash: str,
    secret: Optional[str] = None,
) -> Tuple[bool, str]:
    """Validate a confirm token.

    Returns ``(ok, reason)`` where reason ∈
    {``ok``, ``malformed``, ``bad_signature``, ``expired``, ``stale``}.
    ``stale`` means the token is authentic but the action/target/content no
    longer matches what was previewed.
    """
    if not token or "." not in token:
        return False, "malformed"
    b64, sig = token.rsplit(".", 1)
    try:
        raw = base64.urlsafe_b64decode(b64.encode("ascii"))
        payload = json.loads(raw)
    except Exception:
        return False, "malformed"
    expected = hmac.new(_secret(secret), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, "bad_signature"
    if int(payload.get("e", 0)) < int(time.time()):
        return False, "expired"
    if (
        payload.get("m"),
        payload.get("u"),
        payload.get("a"),
        payload.get("t"),
        payload.get("c"),
    ) != (mailbox, subject, action, target_id, chash):
        # Includes the caller mismatch, and tokens minted before ``u`` existed
        # (``None`` never equals a subject) — both are "re-preview, please".
        return False, "stale"
    return True, "ok"
