# Design — ews-mcp v5 (release line 4.5.x)

The architecture the code enforces. Module docstrings cite the sections
below (§Tools, §Safety, §Ids, §DTOs, §Errors, §Transports, §Audit, §Cache).

## The law

1. **MCP = data plane only.** Fast, deterministic Exchange access plus
   safety gates. Judgment — summaries, briefings, prioritization,
   commitments, voice — belongs to the CALLING assistant. The server never
   makes an LLM call and never ships a "judgment tool". Tool count stays
   lean (≤ 35) and is generated into the docs, never hand-counted.
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
5. **Default to auto-negotiated `auth_type`, never retry rejected credentials.**
   exchangelib auto-negotiation is the default because it adapts to the
   authentication schemes the server offers, and blank `EWS_AUTH_TYPE_FORCE`
   values load as unset.
   `RejectingFaultTolerance` counts and raises `UnauthorizedError` on every
   HTTP 401, locked-out responses included, and `EWSGateway`'s
   authentication circuit breaker enforces a cooldown
   (`EWS_AUTH_FAILURE_COOLDOWN_SECONDS`, default 900) and a per-process cap
   (`EWS_AUTH_MAX_REJECTIONS`, default 3) so a stale secret never locks out
   the directory account. `EWS_AUTH_MAX_REJECTIONS` counts rejected
   credential submissions (matching directory lockout counters), so
   concurrent requests already in flight when the first 401 arrives are each
   counted and a burst can reach the cap at once. All exchangelib imports
   are module-top; every kwarg-bearing call and overridden hook has a
   signature pin.
6. **No footprints.** No personal names, real addresses, employer
   identifiers, personal skill names, mailbox content, or tokens in any
   tracked file, comment, commit message, or doc. Fixtures use
   example.com and neutral wording.

## §Tools — the surface

Five packs (see the generated table in `docs/API.md`):

- **mail-read** (7): `list_folders`, `search_messages`, `check_messages`,
  `get_message`, `get_thread`, `get_attachment`, `get_mailbox_overview`.
- **calendar / people / status** (7): `list_events`, `get_event`,
  `check_availability`, `find_people`, `get_contact`, `get_oof_settings`,
  `get_server_status`.
- **tasks / waiting-on** (3): `list_tasks`, `update_task`, `waiting_on`.
- **writes** (13): draft lifecycle (`create_draft`, `update_draft`,
  `add_draft_attachment`, `delete_draft`, `send_draft`), bulk ops
  (`update_messages`, `move_messages`, `delete_messages`), calendar writes
  (`create_event`, `update_event`, `respond_to_event`, `cancel_event`),
  `set_oof`.
- **inbox rules** (3): `list_rules`, `save_rule`, `delete_rule`.
- **semantic** (+1, only when enabled): `find_similar`.

Every list-shaped result ships exactly the canonical envelope
`{items, count, total_available, next_offset}` (contract-tested).
Multi-folder `search_messages` calls (`folder="f:all"` and
`recursive=true`) cap fan-out at `200` folders per call (extras reported
as `folders_skipped`) and `offset + limit <= 500`.

## §Safety — one gate chain

Dispatch order (policy precedes connectivity; nothing irreversible
without two model decisions):

    kill-switch → tier → circuit → cold / auth gate → recipient guard →
    two-phase confirm → send rate cap → alias resolution → handler → audit

- **Kill-switch** `SEND_ENABLED=false` (default) refuses every send-class
  call before anything else.
- **Tiers** `EWS_CAPABILITY_TIER=read|draft|full` (default `draft`)
  remove above-tier tools from the registry AND refuse them at dispatch.
- **Recipient guard** (allow/denylist globs) fires on every tool whose
  arguments carry recipients (drafts, events) and on the RESOLVED
  recipients inside `send_draft`'s and `cancel_event`'s confirm gates
  (cancellation `attendees` are checked and trigger external-recipient
  warnings just like mail recipients).
- **Two-phase confirm**: phase 1 returns a preview + HMAC token; phase 2
  must echo it. For `send_draft` the token binds the draft's CONTENT
  (subject + sorted recipients + full text and HTML body + attachments
  bound by `id`, `name`, and `size` — not by a hash of attachment byte
  content), and the phase-1 preview inspects stored
  HTML off the event loop. For `cancel_event`, each phase makes one
  projected fetch (`_EVENT_CANCEL_FIELDS`), fails closed on non-calendar
  items and foreign or unverified meeting organizers, marks recurring
  series masters (`type="RecurringMaster"`) in the preview, and binds
  `event_id`, `action` (`"cancel"` vs `"delete"`), `subject`, `start`,
  `end`, `type`, `series`, `is_meeting`, sorted `attendees`, and
  `message` into the token. Rather than chasing every client HTML parser
  quirk on foreign markup, draft HTML sanitisation and preview make **four
  promises, and only these**:
  1. **A body that went through `sanitise_html` is exact**: its links,
     their text, and mismatch warnings are right because we wrote that
     markup.
  2. **For any stored body, every host that appears in URL form anywhere
     in its text is listed** in `link_hosts` (marked `unparsed: true` when
     not reported by the tokenizer walk), or `links_unverified` is `true`.
     The host list includes the **union over six readings of the raw text**
     (with no quote, tag, attribute, or comment state, so adding a reading
     can only add hosts): three character-reference readings (**none**
     decoded; **as a browser decodes** — numeric always with or without `;`,
     named with `;`, and the legacy HTML5 names without `;` only; and
     **everything decoded to a fixed point of three passes**) crossed with
     two whitespace readings (tab, CR, and LF **deleted**, and tab, CR, and
     LF **replaced by a space**).
  3. **Critical notices are never cut**: warnings are ordered as:
     unverified and size notices; then the singular hosts-omitted notice and
     outside-normal-markup notice (each exists at most once); then mismatch
     warnings (including `mailto:` links with multiple recipients or query
     parameters), shown up to 100 and followed beyond 100 by one count line
     saying how many more mismatching links exist and setting
     `links_unverified: true`; then everything else (more than three
     warnings of one kind folded into a single count line). `links` lists
     mismatching links first; `link_hosts` lists hosts of mismatching links
     first, then other link hosts, then `embedded`, then `unparsed`, and
     sets `links_unverified: true` when hosts exceed 50.
  4. **Cost is bounded at a stated size**: `sanitise_html` finishes in
     under 2 s for any input up to 200,000 characters; `inspect_draft_links`
     finishes in under 5 s for any stored body up to 2,000,000 characters,
     above which it lists hosts from the raw-text scan only and sets
     `links_unverified: true`. Runs of more than 30 combining marks are
     cut to 30 before normalisation, hosts longer than 253 characters are
     cut to 80 characters with a warning and never passed to IDNA, and
     neither function holds the Python GIL for more than ~0.2 s in one
     library call.
  **Not promised**: pairing link text with link target inside quoted or
  otherwise foreign HTML that this server did not write; and catching
  look-alike characters in link text beyond removing whitespace around dots
  and warning when a whitespace-free link-text token mixes ASCII letters
  with any non-ASCII character (pure non-Latin text such as Arabic link
  text gets no warning). Phase 2 fetches the draft once, re-verifies the
  hash on that object, and sends that same object, so any edit between
  preview and confirm fails with `confirm_invalid`. Draft tools
  (`update_draft`, `add_draft_attachment`, `delete_draft`, `send_draft`)
  are serialised per draft (`_draft_lock`). Tokens are single-use;
  idempotent replays (same `idempotency_key` + draft) return the cached
  receipt without a fresh token — that is what makes retry-after-timeout
  safe (Stripe semantics).
- **Draft HTML sanitisation** (`htmlsafe.py`): `create_draft` and
  `update_draft` default to `body_format="text"` (plain text escaped and
  wrapped) and also accept `body_format="html"` (calendar event bodies
  stay plain text; `update_draft` omits `body_format` when `body` is not
  supplied). HTML inputs are tokenised in one linear-time non-raising pass
  (stdlib-only custom tokenizer, never `html.parser`, executed off the
  event loop; trailing `/` is ignored on non-void HTML elements; numeric
  character references in attributes decode with or without `;`, while
  named character references in attributes decode only when terminated
  with `;` so `&region=sa` stays verbatim; runs of more than 30 combining
  marks are cut to 30; and `drop_stack` closes in O(1) amortised time) and
  rebuilt from scratch as `<html><body…>…</body></html>` (`dir`, `lang`,
  `align`, and `style` on input `<html>`/`<body>` carried to `<body>`,
  `body` winning) with browser-matching implied end tags and every text
  node and attribute value escaped, then re-sanitised until the output is a
  fixed point (reporting first-pass removal counts). Anchors with no
  visible text anywhere in their descendants (table cells included) are
  removed and counted under `removed_links`. Only allow-listed
  structural/typography/table/list/link elements, element attributes,
  `http`/`https` and address-only `mailto` links (`?query`, `#fragment`,
  and extra or percent-smuggled recipients stripped and counted), and safe
  typography/layout CSS declarations (`unicode-bidi` restricted to
  `normal|embed|isolate|plaintext`, `font-size` of `0` or `< 6px/pt`
  dropped) pass; bidi override controls (`U+202D`/`U+202E`), scripts,
  styles, forms, embeds, `noembed`, `noframes`, SVG/MathML,
  comments/CDATA/PI/doctype, and all `img` elements are dropped. Inputs
  over 200,000 characters (applied to the input; escaped output may be
  longer) or with no visible non-blank text after sanitising fail with
  `validation` before touching Exchange; any stripped elements,
  attributes, or link parts are reported in `sanitised`.
- **Send rate cap** `EWS_MAX_SENDS_PER_HOUR`.
- **Inbox rules safety**: `save_rule` builds `Actions` strictly from an
  allow-list of mailbox-internal actions (`move_to_folder`,
  `copy_to_folder`, `assign_categories`, `mark_as_read`, `mark_importance`,
  `stop_processing_rules`) — no argument path can create a rule that sends,
  forwards, redirects, replies, or deletes. Move and copy targets are
  verified fail-closed against Deleted Items and Junk Email (by folder
  class, distinguished id, well-known name, raw id, and live `GetFolder`
  ancestry up to `msg_folder_root`, bounded to 25 levels) and always carry a
  real `FolderId`. Existing rules using any non-writable field, unreadable
  XML element, or unnameable target folder are reported `editable: false`
  and refused on replace. Both `save_rule` and `delete_rule` stay `write`
  class (`draft` tier, so a draft-tier caller can undo its own rule), are
  serialised in-process under `_rules_lock`, and are always two-phase
  confirmed with tokens bound to the planned rule (including raw target
  folder ids), the replaced or deleted rule (including its serialised XML
  SHA-256 fingerprint), and the `outlook_only_rules` flag.
- The one documented handler-side check: `create_event`/`update_event`
  are write-class for tier purposes, but invitations leave the org, so
  they re-check the kill-switch when (and only when) they would notify.

## §Ids — aliases only

The model never sees a raw EWS id: outputs carry short aliases (`m12`,
`e3`, `d1`, `a1`, `t4`, `p2`, `k1`, `f7`, `r1`), inputs accept aliases or
raw ids.
The SQLite-backed aliaser survives restarts, rebinds on moves, and keeps
`internet_message_id` as a secondary key. Stale alias → clean re-search
hint, never an upstream error. Page-sized mints batch into one
transaction and run off the event loop.

## §DTOs — token economy

`MsgCard` (~60 tokens): id, from, subject, date, 200-char snippet, flags.
`MsgFull`: card + recipients + CLEANED body (bilingual quoted-history +
signature stripping) + attachment inventory. Raw HTML only on explicit
`include_html=true`. Full HTML bodies routinely exceed 100 kB for a short
message.

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
- `lease.py`: non-blocking OS file lock on `cache/sync.lock`
  (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) held for the life
  of the process so at most one `SyncEngine` runs per `DATA_DIR`.
  Followers serve every tool from the shared mirror, skip
  `EWS_CACHE_PURGE_ON_BOOT`, report `cache.sync.role: "follower"`, and
  retry acquisition every 30 s to take over when the leader exits. If the
  lock file cannot be opened or locked, `ewsmcp` logs once and runs sync
  as before (never-exit rule).
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
(jsonschema-validated against the public tool schema), `/openapi.json`,
public health (`/livez`, `/readyz`, `/health`, `/version`) and `/metrics`
(Prometheus, behind the API key). Both `/api/tools/<name>` and `/mcp`
cap request bodies at `max_body_bytes(settings)` —
`(EWS_MAX_ATTACHMENT_MB * 1024 * 1024) * 4 // 3 + 1_048_576` bytes
(base64 expansion of the attachment ceiling plus 1 MiB for JSON
arguments).
**Never-exit boot**: tools register and transports bind before any
Exchange contact; a background warmup loop owns connection recovery
(exponential backoff + jitter and protocol-cache eviction every 3
transient failures, heartbeat re-probe with a REAL network round trip, and
a bounded `auth_failed` state on HTTP 401 rejections that respects the
gateway's auth breaker cooldown and stops after `EWS_AUTH_MAX_REJECTIONS`).

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
