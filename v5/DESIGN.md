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
5. **Never pin `auth_type`.** Only exchangelib auto-negotiation works
   against the target Exchange (verified live). All exchangelib imports
   are module-top; every kwarg-bearing call has a signature pin.
6. **No footprints.** No personal names, real addresses, employer
   identifiers, personal skill names, mailbox content, or tokens in any
   tracked file, comment, commit message, or doc. Fixtures use
   example.com and neutral wording.

## §Tools — the surface

Five packs (see the generated table in `docs/API.md`):

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
- **rules** (4): `list_rules`, `create_rule`, `update_rule`, `delete_rule`
  — Inbox rules (mail filters) via EWS's GetInboxRules/UpdateInboxRules,
  which exchangelib does not implement (`gateway/rules.py` talks raw EWS
  XML). Conditions cover every predicate Outlook/OWA's own rule editor
  exposes — everything under its "Contains these words / Was sent or
  received / My name is / Is flagged with / Is / Size / Received" menus
  — except the four that have no OWA UI at all (`Categories`,
  `ItemClasses`, `MessageClassifications`, `FromConnectedAccounts`);
  `unsupported_fields` on a listed rule flags those four if present, so
  a rule using one is never silently misrepresented. `forward_to`/
  `redirect_to` re-check `SEND_ENABLED` themselves (same exception as
  `create_event`'s invitations, §Safety) since a rule is a standing
  auto-send order, not a one-off.
- **semantic** (+1, only when enabled): `find_similar`.

Every list-shaped result ships exactly the canonical envelope
`{items, count, total_available, next_offset}` (contract-tested).

## §Safety — one gate chain

Dispatch order (policy precedes connectivity; nothing irreversible
without two model decisions):

    kill-switch → tier → circuit → cold gate → recipient guard →
    two-phase confirm → send rate cap → alias resolution → handler → audit

- **Kill-switch** `SEND_ENABLED=false` (default) refuses every send-class
  call before anything else.
- **Tiers** `EWS_CAPABILITY_TIER=read|draft|full` (default `draft`)
  remove above-tier tools from the registry AND refuse them at dispatch.
- **Recipient guard** (allow/denylist globs) fires on every tool whose
  arguments carry recipients (drafts, events, rule `forward_to`/
  `redirect_to`) and on the draft's RESOLVED recipients inside
  `send_draft`'s confirm gate.
- **Two-phase confirm**: phase 1 returns a preview + HMAC token; phase 2
  must echo it. For `send_draft` the token binds the draft's CONTENT
  (subject + sorted recipients + full body, refetched and re-verified at
  phase 2), so editing the draft between preview and confirm kills the
  token. Tokens are single-use; idempotent replays (same
  `idempotency_key` + draft) return the cached receipt without a fresh
  token — that is what makes retry-after-timeout safe (Stripe semantics).
- **Send rate cap** `EWS_MAX_SENDS_PER_HOUR`.
- The documented handler-side checks: `create_event`/`update_event` are
  write-class for tier purposes, but invitations leave the org, so they
  re-check the kill-switch when (and only when) they would notify; same
  for `create_rule`/`update_rule` when `forward_to`/`redirect_to` is set
  — a rule is a standing auto-send order, not a one-off.

## §Ids — aliases only

The model never sees a raw EWS id: outputs carry short aliases (`m12`,
`e3`, `d1`, `t4`, `p2`, `k1`, `f7`, `r3`), inputs accept aliases or raw ids.
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

## §Errors — a taxonomy, not tracebacks

`validation | auth_failed | tier_blocked | kill_switch |
recipient_blocked | confirm_invalid | not_found | throttled | rate_capped
| upstream_unavailable | upstream_error | internal` — each with an
LLM-directed `hint` and `retry_after_s` where meaningful. Handler
`TypeError`/`ValueError` map to `validation`, never 502.

## §Transports

stdio MCP, Streamable HTTP `/mcp`, a REST shim `/api/tools/<name>`
(jsonschema-validated against the public tool schema, 1 MiB body cap),
`/openapi.json`, public health (`/livez`, `/readyz`, `/health`,
`/version`) and `/metrics` (Prometheus, behind the API key).
**Never-exit boot**: tools register and transports bind before any
Exchange contact; a background warmup loop owns connection recovery
(exponential backoff + jitter, protocol-cache eviction every 3 failures,
heartbeat re-probe with a REAL network round trip).

## §Multi-user

`EWS_MULTI_USER=true` (HTTP transport only) drops the single shared
mailbox: every call instead carries its own `X-EWS-Email` /
`X-EWS-Password` headers, resolved to a per-user `Context` with its own
`EWSGateway` (`multiuser.UserContextCache`, bounded + idle-evicting).
Alias DB, audit log, cache mirror, semantic index and the connection
manager are all per-user state that would otherwise leak between
identities, so they stay out entirely in this mode — reads go straight
to EWS. The one thing shared is the tool `registry` itself (stateless,
built once from the tier-filtered config).

## §Audit

Hash-chained JSONL per tool call (no bodies; recipients/subject only for
send/destructive). The chain head persists across restarts
(`audit/chain.state`); `scripts/verify_audit_chain.py` re-derives every
link and catches edits, deletions and truncation.

## Structural guards (scar tissue, encoded)

- `test_exchangelib_signatures.py`: signature pins for every
  kwarg-bearing exchangelib call + behavior contracts for the three lies
  that caused the v5 criticals (string `conversation_id` raises; stored
  `total_count` is not a probe; the protocol cache must be evictable).
- AST sentinel: no exchangelib imports inside function bodies, no
  exemptions.
- Envelope contract test; north-star budget test (≤2 calls, <2k tokens);
  Arabic-search gate suite.
