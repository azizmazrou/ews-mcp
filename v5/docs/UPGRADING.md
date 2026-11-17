# Upgrading

This page explains which version line you are running, how to pin images and packages, what changes
between 4.5 releases, and what to adjust when moving from the legacy 3.x / 4.0 server.

## Versions and images

Two server lines share this repository:

| Line | Source tree | Status | Container tags on `ghcr.io/azizmazrou/ews-mcp` |
|---|---|---|---|
| **4.5 — current** | `v5/`, package `ews-mcp`, command `ewsmcp`, Python `>=3.11` | Active development; recommended for all new setups | Exact release tags `:v4.5.0a4` and `:v4.5.*`, plus moving line tag `:v4.5`; never `:latest` |
| **4.0 / 3.x — legacy** | `src/`, documented in [`../../docs/legacy/`](../../docs/legacy/) | Maintenance only | `:latest` and the version tags of the legacy line |

- **Pin an exact `v4.5.*` tag in Docker.** Pulling `:latest` runs the legacy 4.0 server, not 4.5.
  Pin a release tag such as `ghcr.io/azizmazrou/ews-mcp:v4.5.0a4` in `docker run` or
  [`../docker-compose.example.yml`](../docker-compose.example.yml).
- **Build from `v5/` when building locally.** From the repository root, run
  `docker build -t ews-mcp:dev v5`. Running `docker build .` at the repository root builds the
  legacy 4.0 `Dockerfile` instead.
- **Update a local virtual environment from the repository root.** `ews-mcp` is installed from a
  local checkout rather than PyPI:

```bash
# Run from the repository root with your virtual environment activated
git pull
pip install -U "./v5[extract]"   # or `pip install -U ./v5` without PDF extraction
```

Restart every running `ewsmcp` process after upgrading so new code, schemas, and dependencies take
effect; see [DEPLOYMENT.md](DEPLOYMENT.md#updating).

## To 4.5.0a4 (from 4.5.0a3)

Changes introduced in `v4.5.0a4` on 2026-11-17:

### What is new

| Change | Tool or setting | What to do |
|---|---|---|
| Allow-listed HTML draft bodies via `body_format="html"` with `sanitised` removal counts | `create_draft`, `update_draft` | Optional; `body_format="text"` stays the default. Pass `body_format="html"` for structured or right-to-left mail up to `200,000` characters; see [API.md](API.md#draft-bodies). |
| Stored-HTML link and host inspection across URL attributes and eight raw-text readings: `links`, `link_hosts`, `links_unverified`, and mismatch `warnings` | `send_draft` | Automatic on phase-1 previews; review `links`, `link_hosts`, and `warnings` before confirming. |
| Remove attachments from an existing draft via `remove_attachments=[…]` | `update_draft` | Pass attachment names, `a`-alias ids, or raw attachment ids; matching is all-or-nothing and invalidates prior `send_draft` confirm tokens. See [API.md](API.md#draft-attachments). |
| Attach an existing mail message as `.eml` via `message_id="…"` | `add_draft_attachment` | Pass an `m`-alias or raw message id; mutually exclusive with `content_base64` and `path`. `Bcc` and `Resent-Bcc` headers are stripped automatically. |
| Isolated-worker document text extraction and `offset` paging for PDF, Word, Excel, PowerPoint, `.eml`, attached messages, and HTML | `get_attachment`, `ews-mcp[extract]` | Reinstall from the repository root with `pip install -U "./v5[extract]"` to install `pypdf>=6.21,<7` for PDFs; `v5/Dockerfile` pre-installs it. Use `offset` and `next_offset` to page 20,000-character slices; see [API.md](API.md#reading-attachments). |
| Batch message fetch: up to 20 ids in order, per-id `failed`, and `source="mixed"` | `get_messages` | New `read`-tier tool; use instead of calling `get_message` in a loop. |
| Save raw message MIME under `DATA_DIR/attachments` via `format="eml"` | `get_message` | Returns `saved_path` and `size_bytes`, capped by `EWS_MAX_ATTACHMENT_MB`. Sent Items exports keep `Bcc` headers as stored in the mailbox. |
| Whole-tree folder search via `query="…"` | `list_folders` | Pass a non-empty substring to match folder names or paths across the full tree; `depth` is ignored when `query` is set. |
| Filter and display message importance: `high`, `normal`, or `low` | `search_messages`, `get_message`, `get_messages` | Filter by `importance` on mirror or live reads without `query`; cards and full messages now also emit `"low"`. |
| Directory `department` and `office` fields | `find_people`, `get_contact` | Included automatically on person results when present in the GAL or contact item. |
| Linux process dumpability override | `EWS_KEEP_DUMPABLE` | Defaults to `false`, which calls `prctl(PR_SET_DUMPABLE, 0)` on Linux before starting attachment workers; set `true` only when debugging with `ptrace` or core dumps. |
| Custom linear-time HTML-to-text converter without `html.parser` | `get_message`, `get_messages`, `get_event`, `get_oof_settings`, `get_attachment` | Automatic; see behaviour notes below. |

### What behaves differently

- **Tool counts increase by one with `get_messages`.** The default registry grows from **33** to
  **34** tools:

  | Tier | `v4.5.0a3` | `v4.5.0a4` |
  |---|---|---|
  | `read` | 17 | 18 |
  | `draft` | 28 | 29 |
  | `full` | 33 | 34 |
  | `full` + `EWS_SEMANTIC_INDEX=pgvector` (with `EWS_SEMANTIC_PG_DSN`) | 34 | 35 |

  Update any smoke tests or monitors that assert exact tool counts on `/health` or
  `get_server_status.tools`.
- **Kill-switch at phase one and hourly send cap across all sending calls.** While
  `SEND_ENABLED=false`, `create_event` with `send_invitations=true` and `update_event` with
  `notify_attendees=true` are now refused with `kill_switch` at phase one before any
  `confirm_token` is issued (in `v4.5.0a3` they were refused only at phase two). `cancel_event` on
  a meeting with attendees is now also refused at phase one with `kill_switch` and hint
  `"Cancel the meeting from Outlook, or have the operator flip SEND_ENABLED."` (in `v4.5.0a3`
  `SEND_ENABLED=false` did not stop `cancel_event`). Silent calendar edits and `cancel_event` on
  appointments without attendees are unaffected. Confirmed invitations, update notices, and meeting
  cancellations now count toward the in-memory per-process `EWS_MAX_SENDS_PER_HOUR` cap alongside
  `send_draft`, `respond_to_event`, and `set_oof`. At phase two, the token is verified, spent
  tokens are rejected with `confirm_invalid`, `EWS_MAX_SENDS_PER_HOUR` is checked, and the token is
  consumed last. A `rate_capped` refusal (`retry_after_s` at least `1`) therefore keeps its
  `confirm_token` for retry once the hour has room, whereas in `v4.5.0a3` the refused call lost its
  token.
- **`update_event(notify_attendees=true)` returns an event-bound preview and checks stored
  attendees.** Phase one now fetches the event and checks its stored required, optional, and
  resource `attendees` against `EWS_RECIPIENT_DENYLIST` and `EWS_RECIPIENT_ALLOWLIST`, failing with
  `recipient_blocked` at phase one with nothing saved (`v4.5.0a3` sent update notices without
  checking stored attendees against the lists). On both `update_event(notify_attendees=true)` and
  `cancel_event`, stored attendees with no address at all (such as personal distribution lists;
  `v4.5.0a3` did not list or check stored attendees at all) now appear by display name under
  `attendees_without_address` and fail at phase one with `recipient_blocked` when either list is
  set. Unchanged: a stored `send_draft` recipient without an SMTP address, and a directory-name
  address on a draft or event, fail with `validation` when a list is set. Phase one lists external
  attendees under `warnings` and returns `preview` holding `event_id`, current `subject`, `start`,
  and `end`, `type`, `scope` when given, `attendee_count`, stored `attendees` (plus
  `attendees_without_address` when present), and `changes` (showing a new `body` as 200-character
  `body_snippet`). The token binds the event's `subject`, `location`, `start`, `end`, `type`, and
  `attendees`, plus `scope`, `notify_attendees`, and every requested change including the full
  `body`, failing with `confirm_invalid` (`stale`) at phase two if the event changed. Without
  `notify_attendees=true`, `update_event` stays a one-step silent edit.
- **`cancel_event` fails closed on non-organizer and ambiguous calendar items.** `cancel_event`
  fetches the item at each phase and fails with `validation` on non-`CalendarItem` ids, meetings
  where your response is `Accept`, `Decline`, `Tentative`, or `NoResponseReceived` — use
  `respond_to_event` instead — foreign or unverified non-SMTP organizers, and entries with missing
  `is_meeting`. Plain appointments without attendees move to Deleted Items with `action: "delete"`
  and `deleted: true` without sending mail, while recurring series masters
  (`type="RecurringMaster"`) flag `series: true` and `scope: "series"`. Stored meeting `attendees`
  pass the recipient lists and external-recipient warning, and `start`, `end`, `type`, `series`,
  `action`, `is_meeting`, `attendees`, `subject`, and `message` are bound into `confirm_token`.
- **PDF extraction, `get_attachment` selector order, and circuit-breaker exemptions.** A plain
  `pip install -U ./v5` installs `defusedxml>=0.7` in core for Office and XML parsing, while PDF
  text in `get_attachment` requires `pip install -U "./v5[extract]"` (`pypdf>=6.21,<7`). Without
  `[extract]`, reading PDF text returns `validation`; the Docker image already installs
  `.[extract]`. Supported document formats — `docx`, `xlsx`, `pptx`, `eml`, `message`, `html`, and
  `pdf` when installed — now extract text in `mode="auto"` instead of returning `mode: "info"`,
  while Outlook `.msg` files return `mode: "info"` with a `mode='save'` hint in both `mode="auto"`
  and `mode="text"`. In `get_attachment`, `attachment` is matched in order: file name
  (case-insensitive; a matching name always wins), an `a<digits>` id from the draft tools resolved
  to the attachment it was issued for, a raw Exchange attachment id, or a zero-based digit index.
  An `a<digits>` id issued for another message's attachment now fails with `not_found` rather than
  selecting by index as in `v4.5.0a3`, while `a<digits>` with no matching alias (or when
  `aliases.db` cannot be read) still acts as a zero-based index. Also, while the dispatcher circuit
  breaker is open, `get_server_status`, `waiting_on`, and `find_similar` now continue to answer
  (`v4.5.0a3` refused `get_server_status` with `upstream_unavailable`), and
  `get_server_status.connection` adds `circuit_open: true` and `retry_after_s` while open (absent
  otherwise).
- **HTML attachment and fallback body conversion no longer uses `html.parser`.**
  `bodyclean.html_to_text_bounded` avoids multi-second stalls caused by thousands of unclosed tags
  on Python older than `3.11.14`, `3.12.12`, or `3.13.6`. Clean HTML converts identically; Outlook
  `<head>` conditional comments (`<![endif]-->`) and `<title>` text no longer leak into output; and
  text after unclosed `<head>`, `<![CDATA[`, `<textarea>`, `<iframe>`, `<!--<script>`, or `<svg>`
  and `<math>` scripts and styles is preserved. Unterminated trailing tags are dropped, and for
  HTML attachments, `partial: true` is reported only when text was really cut.

## From 4.5.0a2

Changes introduced in `v4.5.0a3` on 2026-11-16 — apply the
[To 4.5.0a4 (from 4.5.0a3)](#to-450a4-from-450a3) steps as well:

- **Dependency update to `exchangelib==5.6.0`:** `exchangelib` moved from `5.0.3` to `5.6.0`. From
  the repository root, reinstall with `pip install -U ./v5` (or `pip install -U "./v5[extract]"`
  from `v4.5.0a4`).
- **Inbox rules and tool counts:** `list_rules` at `read` tier plus `save_rule` and `delete_rule`
  at `draft` tier were added, raising the `4.5.0a3` default surface from `30` to **33** tools:
  **17** at `read`, **28** at `draft`, and **33** at `full`. Modifying or deleting a rule via EWS
  sends `RemoveOutlookRuleBlob=true` and discards Outlook-only client rules; `list_rules` reports
  `outlook_only_rules`, and `save_rule` and `delete_rule` warn in their phase-1 previews before
  acting. See [API.md](API.md#inbox-rules).
- **Leader/follower sync lease on shared `DATA_DIR`:** when several `ewsmcp` processes share one
  `DATA_DIR`, only the process holding `DATA_DIR/cache/sync.lock` reports `cache.sync.role:
  "leader"` and runs `SyncEngine`. Followers read the shared SQLite mirror without duplicating
  Exchange delta syncs, skip `EWS_CACHE_PURGE_ON_BOOT` with a warning, and retry the lock every 30
  seconds to take over if the leader exits. Note that sharing `DATA_DIR` across processes still
  interleaves writes to `DATA_DIR/audit` and breaks `verify_audit_chain.py`; see
  [DEPLOYMENT.md](DEPLOYMENT.md#several-clients-on-one-machine).

## From 4.5.0a1

Changes introduced in `v4.5.0a2` on 2026-11-13 — apply the [From 4.5.0a2](#from-450a2) steps as
well:

- **Dependencies:** `mcp>=1.30,<2` is required; reinstall from the repository root with
  `pip install -U ./v5` (or `pip install -U "./v5[extract]"` from `v4.5.0a4`).
- **Default timezone:** `EWS_TZ` now defaults to `UTC`. Set `EWS_TZ` to an IANA timezone name if
  your setup relied on the earlier default.
- **Compose and `.env` templates:** the host-specific Compose file and legacy helper scripts were
  replaced by [`../docker-compose.example.yml`](../docker-compose.example.yml) and
  [`../.env.example`](../.env.example).
- **Tools and attachment settings:** `check_messages` and `add_draft_attachment` raised the
  `4.5.0a2` surface from `28` to `30` tools. `4.5.0a2` also added `EWS_ATTACHMENT_DIR`, which
  defaults to `""`; `EWS_MAX_ATTACHMENT_MB`, which defaults to `25` and accepts `1` to `50`; S/MIME
  `smime_type` (`signed` or `enveloped`) detection; `folder_class` on `list_folders`; recurring
  series (`recurrence`, `scope="series"`) and `reminder_minutes` on calendar tools; and folder
  aliases `sentitems`, `deleteditems`, and `junkemail`. `send_draft` also binds attachment `id`
  values into `confirm_token` and serialises operations per draft.
- **Search and `flag_complete` bounds:**
  - Multi-folder `search_messages` (`folder="f:all"` and `recursive=true`) scans at most `200`
    folders per call, reporting extras in `folders_skipped`, and caps `offset + limit` at `500`.
  - `search_messages` with `flag_complete=false` returns `total_available: null`; use `next_offset`
    to check for more pages.
  - `get_message` includes `flag_complete` only on live reads (`fresh=true` or uncached).
- **Lockout guard and OAuth probe paths:** HTTP `401` credential rejections stop immediately
  without retry and report `state: "auth_failed"` on `/readyz`, `/metrics`, and
  `get_server_status`, controlled by `EWS_AUTH_FAILURE_COOLDOWN_SECONDS=900` and
  `EWS_AUTH_MAX_REJECTIONS=3` rejections since the last successful login. `EWS_AUTH_TYPE_FORCE=ntlm`
  and empty `EWS_AUTH_TYPE_FORCE` were also fixed, and OAuth discovery paths (`/.well-known/*`,
  `/register`) return `404` instead of `401`.

## From the legacy line (3.x and 4.0)

4.5 (`v5/`) is a clean-slate rewrite rather than an in-place flag update over `src/`. Four design
choices changed:

1. **Fewer, data-plane-only tools.** The legacy surface — 70 tools in 3.x, reduced to 67 in 4.0
   when the five LLM-reasoning tools were dropped — shrinks to **34** tools (35 when semantic
   search is active). Both the 3.x LLM-proxying tools and the 4.0 agent-secretary stack — memory
   store, commitment tracking, approval queue, voice profiles, OOF policy, briefings, and meeting
   prep — are absent because reasoning and workflow state belong in the calling assistant.
   Overlapping search, read, and draft tools were consolidated behind uniform list envelopes and
   compact message cards instead of full HTML or Markdown bodies.
2. **Short aliases instead of raw Exchange ids.** Tool outputs emit persistent short handles — `m`,
   `d`, `a`, `e`, `t`, `p`, `k`, `f`, and `r` prefixes — stored in `DATA_DIR/memory/aliases.db` and
   rebound automatically by `move_messages`. Inputs still accept aliases or raw EWS ids.
3. **Drafts before mail sends, gated in one place.** One-shot send tools (`send_email`,
   `reply_email`, `forward_email`) were removed. Ordinary mail leaves the mailbox only through
   `create_draft` followed by `send_draft`, while meeting mail — invitations, update notices,
   responses, and cancellations — goes through the calendar tools, guarded by `SEND_ENABLED=false`
   by default, recipient globs on addressed recipients, two-phase confirmation, and
   `EWS_MAX_SENDS_PER_HOUR`; see [SAFETY.md](SAFETY.md).
4. **Capability tiers and lockout-safe auth.** `EWS_CAPABILITY_TIER` defaults to `draft` and leaves
   tools above `read`, `draft`, or `full` unregistered. A local SQLite + FTS5 mirror serves
   cache-first reads with Arabic normalization, and HTTP `401` rejections trip `AuthBreaker` on the
   first failure instead of retrying as throttling like 4.0 did.

Update your configuration and client wiring using [`../.env.example`](../.env.example) and
[CONFIGURATION.md](CONFIGURATION.md):

| Area | Legacy 3.x / 4.0 (`src/`) | 4.5 (`v5/`) |
|---|---|---|
| Image and command | `ghcr.io/azizmazrou/ews-mcp:latest`, root package | Pin `ghcr.io/azizmazrou/ews-mcp:v4.5.0a4`, never `:latest`; or `pip install "./v5[extract]"` and run `ewsmcp` |
| `EWS_SERVER_URL` | Accepted bare hostname `mail.example.com` or `EWS_AUTODISCOVER` | Full EWS endpoint URL required, such as `https://mail.example.com/EWS/Exchange.asmx`; Autodiscover is off |
| Authentication | `EWS_AUTH_TYPE` (`oauth2`, `basic`, `ntlm`) and `EWS_CLIENT_ID`, `EWS_CLIENT_SECRET`, `EWS_TENANT_ID` | Auto-negotiated by default; optional `EWS_AUTH_TYPE_FORCE` (`basic`, `ntlm`, `digest`); OAuth2 dropped |
| Timezone | `TIMEZONE` | `EWS_TZ`, default `UTC` |
| HTTP transport and API key | `MCP_TRANSPORT=sse` at `/sse`; accepted `?api_key=` query parameter in 4.0.1 | `MCP_TRANSPORT=http` at `/mcp` and `/api/tools/<name>`; `Authorization: Bearer <key>` or `X-API-Key: <key>` headers only |
| Local state on disk | `data/ews_mcp_<mailbox>.sqlite` and `data/embeddings/` | Fresh `DATA_DIR`, default `~/.ewsmcp`, cloud-synced paths refused; legacy SQLite and JSON caches are not imported and resync automatically from Exchange |
| Attachment settings | `MAX_ATTACHMENT_SIZE` in bytes; `.msg` parsed via `extract-msg` | `EWS_MAX_ATTACHMENT_MB` (`1` to `50` MiB, default `25`) and `EWS_ATTACHMENT_DIR`; `.msg` returns `info` with a `mode='save'` hint |

For tool-by-tool replacements and features intentionally left out of 4.5 — one-shot sends,
impersonation, OAuth2, contact and folder CRUD, inline raw MIME responses, and the agent-secretary
stack — see [API.md — v3 → 4.5 tool rename map](API.md#v3--45-tool-rename-map) and
[API.md — Intentionally dropped vs v3](API.md#intentionally-dropped-vs-v3). If you maintain an
existing 4.0 deployment, its guides remain under [`../../docs/legacy/`](../../docs/legacy/).
