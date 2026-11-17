# Design — ews-mcp v5 (release line 4.5.x)

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how the server is built; this file holds the rules.

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
   Handlers declare `side_effect_class`, `confirm`, `preview`, and
   `notifies`; they contain no policy beyond second-line kill-switch checks
   in notifying calendar handlers.
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

- **mail-read** (8): `list_folders`, `search_messages`, `check_messages`,
  `get_message`, `get_messages`, `get_thread`, `get_attachment`,
  `get_mailbox_overview`.
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

Dispatch order (argument-known policy precedes connectivity, while
content-dependent policy such as `cancel_event` runs on the preview fetch
after connectivity; nothing irreversible without two model decisions):

    kill-switch (from args) → tier → circuit (requires_ews=True) →
    cold / auth gate (requires_ews=True) → recipient guard (args) →
    two-phase confirm [preview → kill-switch & recipient guard (content) →
    verify token → refuse used token → reserve send slot → consume token
    (release slot if already consumed)] → send rate cap (unconfirmed sends
    only) → alias resolution → handler → audit

- **Kill-switch** `SEND_ENABLED=false` (default) refuses every call that
  sends (`sends_mail`: class `send`, and any call whose tool declares
  `notifies` and for which it answers true — `create_event` with
  `send_invitations`, `update_event` with `notify_attendees`, and
  `cancel_event` when the previewed action is `"cancel"`) at phase 1,
  before any token is issued.
- **Tiers** `EWS_CAPABILITY_TIER=read|draft|full` (default `draft`)
  remove above-tier tools from the registry AND refuse them at dispatch.
- **Recipient guard** (allow/denylist globs) fires on every tool whose
  arguments carry recipients (drafts, events; a recipient argument that is
  neither a string nor a list of strings is refused with `validation`) and
  on the RESOLVED recipients inside `send_draft`'s, notifying
  `update_event`'s, and `cancel_event`'s confirm gates (stored event
  `attendees` are checked and trigger external-recipient warnings just like
  mail recipients; an attendee stored without any address is shown under
  `attendees_without_address` and refused with `recipient_blocked` when a
  list is set).
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
  `message` into the token. For `update_event` with notification, the
  preview hook and handler fetch the event through one helper
  (`_fetch_update_target`) and bind the event's `subject`, `location`,
  `start`, `end`, `type`, `attendees`, `scope`, `notify_attendees`, and
  every change (full `body` included) into the token. Rather than chasing
  every client HTML parser quirk on foreign markup, draft HTML sanitisation
  and preview make **four promises, and only these**:
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
  are serialised per draft (`_draft_lock`). Tokens are single-use
  (spent-token records are kept `CONSUMED_GRACE_SECONDS` past expiry);
  idempotent replays (same `idempotency_key` + raw draft id) return the
  cached receipt without a fresh token — that is what makes
  retry-after-timeout safe (Stripe semantics).
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
- **Send rate cap** `EWS_MAX_SENDS_PER_HOUR`: in-memory, per-process cap
  counting every confirmed call that sends (class `send` and `notifies`,
  plus unconfirmed replays); a call refused with `rate_capped` keeps its
  token.
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
- Second-line handler check: the dispatcher enforces the kill-switch on
  notifying calendar calls through `notifies`; the handlers of
  `create_event`, `update_event`, and `cancel_event` keep a check of their
  own as a second line.

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

Body text comes from the plain-text body Exchange supplies. Only when that is
empty (and for out-of-office texts and HTML attachments) the server converts
the HTML itself, with `bodyclean.html_to_text_bounded`: one linear,
non-raising pass of our own, never the interpreter's `html.parser` (whose
cost and output change between patch releases). It is a reading aid, not a
renderer. What it holds to, each with tests: cost proportional to the input;
mail without malformed markup converts exactly as recorded in
`tests/data/bodyclean_reference_vectors.json`; and it ends hidden regions
where a browser ends them (HTML5 rules for comments, `<head>`, `<title>`,
raw-text elements, the script double escape, CDATA, and leaving `<svg>` /
`<math>`), so that text after such a region is not lost. Where mail programs
disagree it shows rather than hides (`iframe`, `noembed`, `noframes`,
`template` content). Known limits: HTML integration points inside `<svg>` /
`<math>` (`foreignObject`, `desc`, `mtext`) are not modelled; text that is
painted from attributes or style sheets (`alt`, `value`, `content:`) and text
in conditional comments that only one mail program shows is not returned;
nothing is known about what a script would write.

`AttachmentText` (`toolerror.py`, `extract.py`, `extract_child.py`, `extract_worker.py`, `pdftext.py` + `get_attachment`):
isolated per-document subprocess extraction (`python -m ewsmcp.extract_child`,
loading only stdlib, `defusedxml`, and extraction modules — `pypdf` imported
only when a PDF arrives, never `exchangelib`/`mcp`/`requests`; 20 s hard
wall-clock limit with a 75% soft deadline between units of work returning
`partial: true` + `notes`; 1,024 MB memory ceiling self-applied by the child to
its own process before reading `stdin` via Win32 Job Object on Windows or
`RLIMIT_AS` on Linux, reported on `get_server_status.document_memory_ceiling`
when un-enforced on other platforms, plus parent Win32 kill-on-close Job Object
and POSIX `start_new_session` + process-group `SIGKILL` so no child or
grandchild process outlives the call; child JSON validated by the parent as
untrusted hostile input; cancellation kills the child at once and holds the
concurrency slot until cleanup finishes; server temp-folder or launch faults
report `internal` without tripping the circuit breaker; scrubbed env without
`USERNAME`; reduced `PATH`; empty temporary `cwd` removed with retry after child
exit; ≤2 concurrent workers off the mail gateway pool) for document formats
(`pdf`, `docx`, `xlsx`, `pptx`, `eml`, `message`, `html`), while plain `text`
(`.txt`, `.csv`, `.md`, `.log`, `.json`, `text/*`) decodes and slices in-process
(reading only as many bytes as the character limit can require, with no per-line
work or line-end normalisation).
**Boundary (plain words):** the worker protects the server against documents
that consume unbounded time or memory and against parsers that crash; it is
**not** an OS user or filesystem sandbox — code running inside a compromised
parser runs under the server's OS account and could read files accessible to
that account. On Linux, the server marks itself non-dumpable
(`prctl(PR_SET_DUMPABLE, 0)`) before starting its first child so same-user
processes cannot read `/proc/<pid>/environ` or server memory; setting
`EWS_KEEP_DUMPABLE=true` leaves dumpability on for debugging.
Supports `pdf` (optional `ews-mcp[extract]` / `pypdf>=6.21,<7` layout mode via
`pdftext.py`: in-memory `/ToUnicode` RTL ligature reversal including single
start-string `bfrange` expansion up to 256 codes, unpatched plain-mode text for
linear RTL split-word repair up to 12 pieces and for pages with rotated text or
Form XObjects invisible to layout mode, non-RTL layout-visible pages returned
unchanged, whole-word `Word` vs Unicode reading-order recovery up to 5,000
chars/line with single-space separation and no column-tab guessing, Arabic
presentation-form and Persian-twin folding, and `"no text layer"` only when both
layout and plain passes are empty, ≤500 pages), `docx`/`xlsx`/`pptx` (stdlib
`zipfile` stored/deflated only + `defusedxml` iterative walking up to depth 256,
macros untouched, ≤2,000 members checked up front, ≤30 MiB/member read,
≤20 MiB/XML part for non-streamed parts, ≤100 MiB total read, Word
`mc:Choice`-only alternate content, Excel worksheets streamed up to 5,000 rows ×
50 columns with ISO dates/times and percentages formatted from `xl/styles.xml`,
≤2,000 chars/cell, and ≤10-char cell refs), `eml` (`compat32` policy, raw 8-bit
header bytes recovered as UTF-8 falling back to `Windows-1256` before the
≤4,000-char header cut, ≤50 addresses/header), `message` (`ItemAttachment`
fields), `html`, and `text` (lone surrogates replaced with U+FFFD, assembly cap
of 5,000,000 chars; `.msg` files return `mode: "info"` with a save-and-open
note/hint in both `auto` and `text` modes).
Served in 20,000-char slices from `offset` with `total_chars`, `next_offset`
(when more follows), `extracted_from`, `partial: true` whenever the returned
document was cut, `notes` (a list of ≤8 notes ≤300 chars each, cut notes first
and repeated notes folded with a count, repeated on every part alongside
`note`), `pages`/`slides`/`sheets`, and `pages_read`/`slides_read`/`sheets_read`.
An in-memory-only LRU+TTL cache (≤4 documents, ≤8,000,000 characters total,
10 min TTL, keyed by `(attachment_id, size)` or an unambiguous fallback tuple,
with `offset > 0` resolved only through the exact `(message_id, attachment)`
selector binding or a fresh metadata-only picker lookup) serves subsequent parts
without re-downloading or re-extracting and without writing mail to disk.

`Person` (`find_people`, `get_contact`): `p`-alias `id`, `name`, `email`,
`source` (`gal|contacts|mirror`), plus `title`, `department`, `office`,
`company`, `phone`, and mirror `history` when present (omitted when absent).

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

Hash-chained JSONL per tool call (for calls of class `send` or
`destructive` it holds the target ids of the call — `draft_id`, `event_id`,
`ids` — and never recipients, subjects, or bodies). The chain head persists
across restarts (`audit/chain.state`); `scripts/verify_audit_chain.py`
re-derives every link and detects accidental damage and edits that leave
`chain.state` alone. The chain has no key, so someone who can write
`DATA_DIR` can recompute it; several processes sharing one `DATA_DIR` break
it by themselves; audit writes are best effort.

## Structural guards (scar tissue, encoded)

- `test_exchangelib_signatures.py`: signature pins for every
  kwarg-bearing exchangelib call + behavior contracts for the three lies
  that caused the v5 criticals (string `conversation_id` raises; stored
  `total_count` is not a probe; the protocol cache must be evictable).
- AST sentinel: no exchangelib imports inside function bodies, no
  exemptions.
- Envelope contract test; north-star budget test (≤2 calls, <2k tokens);
  Arabic-search gate suite.
