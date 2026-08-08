# Design — ews-mcp v5 (release line 4.5.x)

The architecture the code enforces. Module docstrings cite the sections
below (§Tools, §Safety, §Ids, §DTOs, §Errors, §Transports, §Audit, §Cache).

## The law

1. **MCP = data plane only.** Fast, deterministic Exchange access plus
   safety gates. Judgment — summaries, briefings, prioritization,
   commitments, voice — belongs to the CALLING assistant. The server never
   makes an LLM call and never ships a "judgment tool". Tool count stays
   lean (≤ 29) and is generated into the docs, never hand-counted.
2. **Indexing = SQLite + FTS5 in core.** The optional semantic tier hides
   behind an adapter (`EWS_SEMANTIC_INDEX=none|pgvector`, default `none`);
   the public server runs with zero dependencies beyond Exchange
   credentials.
3. **Reads are cache-first with provenance** (`source`, `as_of`,
   `fresh:true` escape hatch); **writes go straight to EWS** and then
   write-through to the mirror.
4. **Safety gates live ONLY in the dispatcher** (`tools/base.py`).
   Handlers declare `side_effect_class` and `confirm`; they contain no
   policy.
5. **Never pin `auth_type` in `AUTH_MODE=static`.** Only exchangelib
   auto-negotiation works against the target Exchange (verified live);
   pinning BASIC or NTLM both fail. `AUTH_MODE=oidc` is the single
   exception: OAuth2 *requires* `auth_type=OAUTH2` because exchangelib's
   probe cannot discover Bearer, and the negotiation failure mode that
   motivated this law does not apply to it. The `EWS_AUTH_TYPE_FORCE`
   escape hatch (a *different* server only) is unchanged. All exchangelib
   imports are module-top; every kwarg-bearing call has a signature pin.
6. **No footprints.** No personal names, real addresses, employer
   identifiers, personal skill names, mailbox content, or tokens in any
   tracked file, comment, commit message, or doc. Fixtures use
   example.com and neutral wording.

## §Tools — the surface

Four packs (see the generated table in `docs/API.md`):

- **mail-read** (6): `list_folders`, `search_messages`, `get_message`,
  `get_thread`, `get_attachment`, `get_mailbox_overview`.
- **calendar / people / status** (7): `list_events`, `get_event`,
  `check_availability`, `find_people`, `get_contact`, `get_oof_settings`,
  `get_server_status`.
- **tasks / waiting-on** (3): `list_tasks`, `update_task`, `waiting_on`.
- **writes** (12): draft lifecycle (`create_draft`, `update_draft`,
  `delete_draft`, `send_draft`), bulk ops (`update_messages`,
  `move_messages`, `delete_messages`), calendar writes (`create_event`,
  `update_event`, `respond_to_event`, `cancel_event`), `set_oof`.
- **semantic** (+1, only when enabled): `find_similar`.

Every list-shaped result ships exactly the canonical envelope
`{items, count, total_available, next_offset}` (contract-tested).

## §Safety — one gate chain

Dispatch order (policy precedes connectivity; nothing irreversible
without two model decisions):

    identity → kill-switch → tier → circuit → cold gate → recipient guard →
    two-phase confirm → send rate cap → alias resolution → handler → audit

**Every gate is evaluated per principal.** In `static` mode there is one
principal (the configured mailbox) and this is invisible. In `oidc` mode
the confirm-token binding, the send rate window, the `idempotency_key`
namespace, the alias store and the cache mirror are all per caller —
otherwise one caller's send budget throttles another, and replaying
another caller's `idempotency_key` returns *their* send receipt. The
circuit breaker is the deliberate exception: it stays global, because the
three codes that feed it describe Exchange's health, not a caller's.

- **Kill-switch** `SEND_ENABLED=false` (default) refuses every send-class
  call before anything else.
- **Tiers** `EWS_CAPABILITY_TIER=read|draft|full` (default `draft`)
  remove above-tier tools from the registry AND refuse them at dispatch.
- **Recipient guard** (allow/denylist globs) fires on every tool whose
  arguments carry recipients (drafts, events) and on the draft's RESOLVED
  recipients inside `send_draft`'s confirm gate.
- **Two-phase confirm**: phase 1 returns a preview + HMAC token; phase 2
  must echo it. For `send_draft` the token binds the draft's CONTENT
  (subject + sorted recipients + full body, refetched and re-verified at
  phase 2), so editing the draft between preview and confirm kills the
  token. Tokens are single-use; idempotent replays (same
  `idempotency_key` + draft) return the cached receipt without a fresh
  token — that is what makes retry-after-timeout safe (Stripe semantics).
- **Send rate cap** `EWS_MAX_SENDS_PER_HOUR`.
- The one documented handler-side check: `create_event`/`update_event`
  are write-class for tier purposes, but invitations leave the org, so
  they re-check the kill-switch when (and only when) they would notify.

## §Ids — aliases only

The model never sees a raw EWS id: outputs carry short aliases (`m12`,
`e3`, `d1`, `t4`, `p2`, `k1`, `f7`), inputs accept aliases or raw ids.
The SQLite-backed aliaser survives restarts, rebinds on moves, and keeps
`internet_message_id` as a secondary key. Stale alias → clean re-search
hint, never an upstream error. Page-sized mints batch into one
transaction and run off the event loop.

## §DTOs — token economy

`MsgCard` (~60 tokens): id, from, subject, date, 200-char snippet, flags.
`MsgFull`: card + recipients + CLEANED body (bilingual quoted-history +
signature stripping) + attachment inventory. Raw HTML only on explicit
`include_html=true`. The measured pathology this kills: one v3 detail
call shipped 115,457 chars for a ~150-char message.

## §Cache — the mirror (see `cache/`)

- `store.py`: per-mailbox SQLite (WAL, owner-only, absolute non-synced
  `DATA_DIR`); messages with bodies cleaned ONCE at sync time; FTS5
  external-content index over a normalized shadow text; events, tasks,
  folders, sync tokens; per-sender learned-signature table. SINGLE
  WRITER; tools read via `mode=ro` connections.
- `normalize.py`: ONE `normalize_ar()` for index and query — diacritics/
  tatweel stripped, alef/hamza-carrier/teh-marbuta/alef-maqsura folded,
  bidi marks removed, Arabic-Indic digits folded. This is what makes
  "الاحاطه" find "تمت الإحاطة".
- `sync.py`: background engine started on the first warm connection;
  resumable `SyncFolderItems` deltas every `EWS_CACHE_SYNC_SECONDS` (45)
  for `EWS_CACHE_FOLDERS` (inbox,sent); a slow lane every ~10 min
  refreshes the folder tree (honest unread/total counts), the expanded
  14-day calendar window and the tasks folder. Failures degrade — reads
  fall back to live EWS, the server never gates on the mirror.
- Provenance contract: every read is stamped `source: cache|live`
  (+ `as_of` for cache); `fresh:true` forces live. `EWS_CACHE_ENABLED=
  false` = pure EWS reads, fully functional.
- **The background engine cannot run under a per-caller upstream**, and
  the mirror is per caller there (`cache/percaller.py`). A user's access
  token lives about an hour and the design deliberately stores no per-user
  refresh token, so there is no credential a background loop could use.
  Instead each caller's mirror is warmed from *inside their own requests*:
  after a tool call returns, if their mirror has gone stale, one bounded
  cycle runs in the background with the token already in hand. The mirror
  is therefore warm for people actually using the server and costs nothing
  for the rest, and nobody's mailbox is ever synced without them asking
  for something first. Four properties are load-bearing: it never fails a
  tool call (the answer has already been sent), never adds latency to one,
  runs at most one cycle per caller at a time, and closes each store's
  SQLite writer on LRU eviction.

## §Errors — a taxonomy, not tracebacks

`validation | auth_failed | identity_blocked | tier_blocked | kill_switch
| recipient_blocked | confirm_invalid | not_found | throttled |
rate_capped | upstream_unavailable | upstream_error | internal` — each with an
LLM-directed `hint` and `retry_after_s` where meaningful. Handler
`TypeError`/`ValueError` map to `validation`, never 502.

`auth_failed` (401) is *"we do not know who you are"* — missing/invalid
API key, an unverifiable bearer token, or Exchange rejecting our
credentials; the caller should re-acquire a token and retry.
`identity_blocked` (403) is *"we know who you are and you may not"* — a
cryptographically valid token whose mailbox domain or scope is refused.
Retrying is pointless, so they are distinct codes rather than one.
Neither counts toward the circuit breaker: they are caller problems, not
Exchange health.

## §Transports

stdio MCP, Streamable HTTP `/mcp`, a REST shim `/api/tools/<name>`
(jsonschema-validated against the public tool schema, 1 MiB body cap),
`/openapi.json`, public health (`/livez`, `/readyz`, `/health`,
`/version`) and `/metrics` (Prometheus, behind the API key).
**Never-exit boot**: tools register and transports bind before any
Exchange contact; a background warmup loop owns connection recovery
(exponential backoff + jitter, protocol-cache eviction every 3 failures,
heartbeat re-probe with a REAL network round trip).

**Readiness depends on the mode.** With a shared credential, `/readyz`
reports the connection manager's state. With a per-caller upstream there
is no shared connection to warm — and probing an arbitrary caller's
mailbox to manufacture one would mean issuing EWS requests nobody asked
for, on somebody's real mail. So `readiness.py` probes only what needs no
identity: the IdP's key set is reachable and non-empty, its token endpoint
answers, and an UNAUTHENTICATED request to the EWS endpoint comes back
`401` with a `Bearer` challenge. That last header is the valuable one — it
proves Exchange is up *and* still OAuth-enabled, which is the one upstream
assumption the whole deployment rests on. A `401` offering only
`Negotiate`/`NTLM` means every caller is about to fail, so readiness goes
red and says why. Results are cached for a few seconds so a liveness poll
cannot become a load generator.

**Inbound auth is a mode, not a flag** (`AUTH_MODE`, default `static`).

- `static` — one shared `MCP_API_KEY`, one mailbox, no identity anywhere.
  Every path is exactly as it has always been; stdio stays here.
- `oidc` — the server is an OAuth2 **resource server**. It validates the
  caller's access token against the IdP's JWKS (`iss`, `aud` = *this*
  server's resource id, `exp`/`nbf`, an algorithm allowlist that excludes
  `HS*`) and derives the mailbox from a claim. HTTP only: stdio has no
  layer to carry a token, so `oidc` + stdio refuses to boot. A shared
  `MCP_API_KEY` may be set *as well*, and is then required in addition —
  a leaked token alone must not reach the mailbox.

`Settings.per_caller_upstream` is the second half of that mode. The verified
token is exchanged **on behalf of** the caller for an Exchange-audience one,
which opens that caller's own mailbox with `access_type=DELEGATE` — never
impersonation, never a service account that could open every mailbox, and no
tool anywhere takes a mailbox argument. It also gates the multi-tenant rules:
per-caller Exchange sessions and alias namespaces, no shared cache mirror, no
shared embeddings.

The pool (`gateway/pool.py`) is LRU + idle-TTL, keyed on `issuer|sub`. Two
constraints shape it: the access token is mutated **in place** on refresh,
because exchangelib caches Protocol objects on `(endpoint, credentials)` and a
fresh credentials object every hour would strand one session pool per caller
per refresh; and the EWS executor is **shared** (it is the server-wide
politeness budget against Exchange throttling) with a per-caller semaphore as
each caller's share, so threads do not multiply by the number of users.

Identity crosses into the tools through the ASGI **scope**: `build_app`
verifies the token once and writes the principal into `scope`, and the
Streamable HTTP transport hands that same scope dict to `call_tool` as
`request_context.request.scope`. No contextvar, no shared mutable slot,
so no request can observe another's identity. The hops it relies on are
SDK internals, pinned by `test_mcp_sdk_pins.py`.

**Fail closed.** If `oidc` is on and no verified principal reaches the
dispatcher, the call is refused. There is no fallback to `EWS_EMAIL` —
that fallback would hand the configured mailbox to an anonymous caller.

## §Audit

Hash-chained JSONL per tool call (no bodies; recipients/subject only for
send/destructive). The chain head persists across restarts
(`audit/chain.state`); `scripts/verify_audit_chain.py` re-derives every
link and catches edits, deletions and truncation.

The audit is the one deliberate exception to "namespace everything per
principal": splitting the chain per caller would destroy the property it
exists for, since a whole caller's file could then be deleted without
breaking any link. Instead there is ONE chain, and the caller's identity
(a salted hash by default, `AUDIT_IDENTITY=smtp` for the real address)
goes into the record *before* hashing — so who did it is as
tamper-evident as what they did. No tool reads the audit, so one chain
creates no cross-caller read path.

## Structural guards (scar tissue, encoded)

- `test_exchangelib_signatures.py`: signature pins for every
  kwarg-bearing exchangelib call + behavior contracts for the three lies
  that caused the v5 criticals (string `conversation_id` raises; stored
  `total_count` is not a probe; the protocol cache must be evictable).
- AST sentinel: no exchangelib imports inside function bodies, no
  exemptions.
- Envelope contract test; north-star budget test (≤2 calls, <2k tokens);
  Arabic-search gate suite.
