# Changelog

## Unreleased — Agent-secretary stack (memory, commitments, approvals, rules, briefings)

Adds a persistent, per-mailbox state layer and 24 new MCP tools that
turn the server from a stateless Exchange client into an **agentic
secretary**. See [`docs/AGENT_SECRETARY.md`](docs/AGENT_SECRETARY.md) for
the full guide.

### New infrastructure

- **`src/memory/`** — SQLite-backed, per-mailbox KV store with
  namespaces, TTL, size caps (1 MiB/value, 50 MiB/namespace), atomic
  read-delete (`consume`), and an audit table. Every mailbox gets its
  own file under `EWS_MEMORY_DIR` (default `data/memory/`) with a
  SHA-256-prefix filename — raw emails never touch the filesystem.
- **Typed repositories** — `CommitmentRepo`, `ApprovalRepo`, `RuleRepo`,
  `VoiceRepo`, `OOFPolicyRepo`. Each wraps the KV in a typed API and
  validates inputs.
- **BaseTool.get_memory_store()** — single helper every agent tool uses
  to reach the store for the authenticated primary mailbox.

### New MCP tools (24)

- **Memory** (4): `memory_set`, `memory_get`, `memory_list`, `memory_delete`
- **Commitments** (4): `track_commitment`, `list_commitments`,
  `resolve_commitment`, `extract_commitments` (AI-assisted)
- **Approval queue** (5): `submit_for_approval`,
  `list_pending_approvals`, `approve`, `reject`,
  `execute_approved_action` (atomic, single-use)
- **Voice profile** (2): `build_voice_profile` (samples Sent folder,
  AI-generates a style card), `get_voice_profile`
- **Rule engine** (5): `rule_create`, `rule_list`, `rule_delete`,
  `rule_simulate`, `evaluate_rules_on_message`. Match keys and action
  types are strict allow-lists.
- **OOF policy** (3): `configure_oof_policy`, `get_oof_policy`,
  `apply_oof_policy` (creates drafts, never sends)
- **Compound** (2): `generate_briefing` (inbox delta + meetings +
  commitments + overdue tasks + VIP activity), `prepare_meeting`
  (attendees + history + notes + attachment previews)

### `send_email` gains `dry_run`

`send_email(dry_run=true)` validates inputs, builds the Message object,
and returns a preview without calling `message.send()` or touching the
Drafts folder. Useful for "what would this send" pre-flight checks.

### New config flags

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENABLE_AGENT` | `true` | Registers the 24 agent-secretary tools |
| `EWS_MEMORY_DIR` | `data/memory` | Jail for per-mailbox SQLite files |

### Tool count

- Base tools: 42 → **66** (24 new base tools under `ENABLE_AGENT=true`)
- Optional AI tools: 4 → 4 (unchanged)
- Grand total with everything on: **70**

### Security properties

- Per-mailbox file isolation by design (no shared tables between users)
- SQL placeholder-only queries
- Path jailing on the DB file directory
- Strict alphabet for namespaces and keys
- Value size caps + LRU pruning
- Atomic `consume` for single-use approval tokens
- Allow-lists for rule actions, match keys, and approval-queue actions
- AI prompts enforce PII-redaction instructions for the voice profile
- Forward rules only ever create drafts

### Test coverage

`tests/test_agent_secretary.py` — 23 new tests covering:
- Memory roundtrip, isolation, key/namespace validation, size caps,
  TTL expiry, list filtering, atomic consume, path jailing
- Commitment lifecycle, overdue filter, validation errors
- Approval submit/decide, action allow-list, double-consume refusal,
  TTL validation
- Rule action and match-key allow-lists, `fnmatch` semantics, AND
  combination across multiple match keys
- Voice and OOF repo roundtrips
- Reserved-namespace refusal from generic memory tools

All 23 pass; 18 pre-existing failures unchanged (147 passing total).

### Files added

```
src/memory/__init__.py
src/memory/store.py
src/memory/models.py
src/tools/memory_tools.py
src/tools/commitment_tools.py
src/tools/approval_tools.py
src/tools/voice_tools.py
src/tools/rule_tools.py
src/tools/oof_policy_tools.py
src/tools/briefing_tools.py
src/tools/meeting_prep_tools.py
tests/test_agent_secretary.py
docs/AGENT_SECRETARY.md
```

### Known follow-ups (intentionally deferred)

- Background watcher that fires `evaluate_rules_on_message` on
  inbound mail via `exchangelib` streaming notifications. Manual
  evaluation works today; the watcher is a separate infra change.
- Scheduled/recurring agent tasks (cron-style). Out of scope for this
  PR; plugs into the same memory layer when added.
- Memory-backed voice application inside `suggest_replies` /
  `create_reply_draft` prompts. The profile is stored and fetchable;
  wiring it into each draft prompt is a narrow follow-up.

---

## Unreleased — Security and reliability hardening

This release closes the 6 HIGH-severity findings from the end-to-end security
review and the top code-quality bugs found alongside them. **Behaviour
changes that operators need to know about** are called out under
"Breaking / operator-visible changes".

### Security fixes (HIGH)

- **S1 — Authenticated HTTP/SSE transport.** When `MCP_API_KEY` is set,
  every request to `/sse`, `/messages`, `/openapi.json`, and
  `/api/tools/{tool}` must present `Authorization: Bearer <key>` (or
  `X-API-Key`). Only `/health` remains public. The OpenAPI schema now
  advertises `bearerAuth` instead of the unenforced `basicAuth`.
- **S2 — TLS verification restored by default.** The EWS HTTP adapter
  no longer globally disables certificate verification. Set
  `EWS_INSECURE_SKIP_VERIFY=true` to opt back in for internal Exchange
  servers with private CAs — a `WARNING` log line is emitted when used.
- **S3 — `download_attachment` path jail.** `save_path` is now treated
  as a basename hint only; directory components and `..` are stripped
  and the resolved path is verified to live inside `EWS_DOWNLOAD_DIR`
  (defaults to `./downloads`). This closes the pre-auth
  arbitrary-file-write → RCE chain with S1.
- **S4 — HTML injection in reply/forward drafts fixed.** `reply_email`,
  `forward_email`, `create_reply_draft`, and `create_forward_draft`
  now HTML-escape the original message's From/To/Cc/Subject/Sent
  fields and pass user-supplied bodies through a proper sanitiser
  (`utils.sanitize_html`, which now actually removes `<script>`,
  `<style>`, `on*=` handlers, and `javascript:` URIs). Plain-text
  bodies are escaped and newline→`<br/>` converted.
- **S5 — Audit log redaction.** `AuditLogger.log_operation` now runs
  every `details` payload through a new `redact_sensitive()` helper
  before writing to `audit.log`. Fields matching `password`, `token`,
  `secret`, `api_key`, `authorization`, `body`, `html_body`,
  `text_body`, `file_content`, `content_base64`, `mime_content`, or
  `inline_attachments` are replaced with `[redacted]` / length hints.
- **S6 — Default bind `127.0.0.1`.** `MCP_HOST` defaults to loopback;
  the SSE startup now refuses to bind a non-loopback address without
  `MCP_API_KEY`, and warns when running on loopback with no API key.

### Code-quality fixes (High)

- **C1** `read_attachment` now correctly extracts PDF / DOCX / XLSX.
  The `_read_pdf`, `_read_docx`, `_read_excel` methods were incorrectly
  placed on `AttachEmailToDraftTool` (they were unreachable from
  `ReadAttachmentTool.execute`, which silently fell back to a generic
  "Failed to read attachment" error for every non-TXT extraction).
- **C2** `main.py` now returns **JSON** over the MCP transport.
  Responses were built with `str(result)` (Python repr — single
  quotes, `True/False/None`, opaque `str(datetime(...))`).
- **C3** `find_meeting_times` fixes: slots outside the returned
  `merged_free_busy` range are now treated as **unavailable** (they
  were falsely reported as free), dead buffer-check code now actually
  runs, and accepted slots advance by `duration_minutes` so the tool
  stops emitting N overlapping 15-minute shifts of the same hour.
- **C4** `EmailService.get_message` and `ThreadService.get_thread` now
  use `account.trash` instead of the nonexistent `account.deleted`
  (which previously raised and was swallowed by a bare `except:`,
  silently skipping Deleted Items).
- **C5** OAuth2 credential path simplified. `AuthHandler` no longer
  pre-fetches an MSAL token that was then thrown away; `exchangelib`
  already handles the OAuth2 token lifecycle internally.
- **C6** Advanced search responses now stringify `ItemId` via
  `ews_id_to_str` so `message_id` is a plain string, matching the
  other search modes.

### Code-quality fixes (Medium)

- **C7** `RateLimiter`, `CircuitBreaker`, and `CacheAdapter` are now
  thread-safe; a `threading.Lock` guards every mutating critical
  section so concurrent tool executions don't race on the rate window,
  failure count, or cache dict.
- **C8** Inline-attachment `content_id` values are sanitized (spaces
  → dashes, non-ASCII stripped) and de-duplicated so multiple inlines
  with the same basename don't collide and so `cid:...` references
  render correctly in Outlook/OWA.
- **C9** `parse_datetime_tz_aware` / `parse_date_tz_aware` are now
  annotated `Optional[...]` to match their actual behaviour; bad
  inputs log a DEBUG line so silent None-assignment to exchangelib
  fields stops being invisible.
- **C10** `CreateReplyDraftTool` / `CreateForwardDraftTool` now use
  `add_reply_prefix` / `add_forward_prefix` so threads no longer stack
  "RE: RE: RE: …".
- **C11** Plain-text bodies in reply/forward/draft tools are HTML-escaped
  and newlines converted to `<br/>` (handled by the new
  `utils.format_body_for_html`).
- **C12** `ConnectionError` renamed to `EWSConnectionError` (alias
  kept for one release). The old name shadowed the Python builtin of
  the same name and broke `isinstance(e, ConnectionError)` matching
  for real OS-level socket errors.
- **C13** `GetCalendar` end-date heuristic no longer over-collects the
  day after when the caller explicitly asks for events ending at
  midnight; it now checks whether the input was date-only (no `T`).
- **C14** `EmbeddingService._save_cache` writes atomically
  (`tempfile` + `os.replace`) so a crash mid-write cannot corrupt
  `embeddings.json`.
- **C15** `EmbeddingService.embed_batch` no longer has an O(N²)
  `indices_to_embed.index(i)` lookup; replaced with positional
  iteration.
- **C16** `openapi_adapter.handle_rest_request` now returns a proper
  HTTP status (400 / 401 / 429 / 503 / 500) when a tool fails, matching
  the advertised OpenAPI responses.
- **C17** Tool-count comments corrected in `main.py` (42 base + 4 AI = 46).
- **C19** AI tools (`semantic_search_emails`, `classify_email`,
  `summarize_email`, `suggest_replies`) now accept `target_mailbox`
  for impersonation — they used to be the only four tools that
  ignored it.

### Code-quality fixes (Low)

- **C21** Remaining bare `except:` clauses in `attachment_service.py`
  replaced with logged `except Exception:` blocks.
- **C22** `run_server.py` no longer hardcodes `C:\Tools\ews-mcp`. It
  uses `os.path.dirname(os.path.abspath(__file__))` so the MSIX
  wrapper works from any install location on any OS.
- **C25** Config now logs when `AI_MODEL` / `AI_EMBEDDING_MODEL`
  defaults are applied (previously silent) and warns when semantic
  search is enabled against a local provider without
  `AI_EMBEDDING_MODEL` set.

### New settings

| Variable | Default | Purpose |
|----------|---------|---------|
| `MCP_API_KEY` | — | Bearer token required on every non-`/health` request on the SSE transport |
| `MCP_HOST` | `127.0.0.1` (was `0.0.0.0`) | Bind address for SSE |
| `EWS_INSECURE_SKIP_VERIFY` | `false` | Opt-in for internal Exchange with private CAs |
| `EWS_DOWNLOAD_DIR` | `downloads` | Jail directory for `download_attachment` writes |

### Breaking / operator-visible changes

- **SSE transport binds `127.0.0.1` by default.** Docker-compose files
  that expect the server on `0.0.0.0` must now set `MCP_HOST=0.0.0.0`
  **and** `MCP_API_KEY=<secret>` — startup refuses the combination
  without a key.
- **TLS is verified by default.** Setups that depended on the old
  behaviour must set `EWS_INSECURE_SKIP_VERIFY=true` or install the
  internal CA bundle into the container's trust store.
- **`download_attachment` save path is jailed.** Callers can no
  longer pick an arbitrary filesystem location; only the basename of
  `save_path` is honoured and the file is written under
  `EWS_DOWNLOAD_DIR`. The response `file_path` shows the actual
  location.
- **MCP tool responses are now JSON** rather than Python repr.
  Clients that relied on parsing `True`/`False`/single-quoted dicts
  need to switch to `json.loads`.
- **`ConnectionError` → `EWSConnectionError`.** The old name is
  aliased for one release but should be replaced in any downstream
  `except` / `isinstance` checks.

### Files changed (18)

`src/main.py`, `src/config.py`, `src/auth.py`, `src/ews_client.py`,
`src/exceptions.py`, `src/utils.py`, `src/openapi_adapter.py`,
`src/middleware/logging.py`, `src/middleware/rate_limiter.py`,
`src/middleware/circuit_breaker.py`, `src/middleware/error_handler.py`,
`src/adapters/cache_adapter.py`, `src/tools/attachment_tools.py`,
`src/tools/email_tools.py`, `src/tools/email_tools_draft.py`,
`src/tools/calendar_tools.py`, `src/tools/ai_tools.py`,
`src/services/email_service.py`, `src/services/thread_service.py`,
`src/services/attachment_service.py`, `src/ai/embedding_service.py`,
`run_server.py`, `tests/test_attachment_tools.py`.

---

## Prior to this release (also unreleased) — Drafts, folder discovery, availability fixes

### New Tools (+4)

Base tool count: **42** (38 → 42 with the additions below). Total with AI: **46**.

- `create_draft` — create an email draft in the Drafts folder without sending
- `create_reply_draft` — build a reply draft (quoted original, signature placeholder) for AI preview-before-send
- `create_forward_draft` — build a forward draft for AI preview-before-send
- `find_folder` — locate a folder by name or ID anywhere in the mailbox hierarchy

### New Features

- **HTML reply/forward drafts** (`src/tools/email_tools_draft.py`): preserve the original conversation, inline images, CDATA blocks, and Outlook-style quoted headers when composing a reply or forward.
- **Folder-ID support** on `move_email`, `copy_email`, and `manage_folder`: pass `destination_folder_id` / `parent_folder_id` to resolve by stable Exchange ID instead of display name or path.
- **Email MIME export** (`get_email_mime`): return the raw RFC-822 MIME of a message.
- **Attach email to draft** (`attach_email_to_draft`): attach another message as an `.eml` file to a draft.
- **Windows MSIX wrapper**: new entrypoint script corrects the Claude Desktop MSIX working-directory bug on Windows.

### Bug Fixes

- **Availability parsing** (`check_availability`): correctly parse exchangelib `merged_free_busy` responses.
- **Availability coverage**: include the current authenticated mailbox in availability checks by default.
- **Scheduling responses**: clarify free/busy output so the AI can act on the result without a second round-trip.
- **Reply / forward drafts**: fix threading metadata, signature placement, and duplicate `RE:` / `FW:` prefixes; preserve styles and CDATA in quoted HTML bodies.
- **Draft attachments**: attachment flow on drafts was failing in certain edge cases; fixed as part of the backlog-folder / availability / draft-attachment work.

### Documentation

- README fully refreshed: accurate tool counts (42 base + 4 AI = 46), full tool tables per category, complete environment-variable reference, corrected architecture diagram, new "Known limitations" section.
- New draft-workflow and folder-discovery examples.

### Known Limitations (unchanged from v3.4.0)

- The four AI tools (`semantic_search_emails`, `classify_email`, `summarize_email`, `suggest_replies`) do not honor `target_mailbox`; they always act on the primary authenticated mailbox.
- `read_attachment` extracts PDF / DOCX / XLSX only.
- The SSE/HTTP transport is unauthenticated and binds `0.0.0.0` by default — put it behind an auth-enforcing reverse proxy for any non-local deployment.

---

## v3.4.0 — Phase 3+4: Reliability & Code Quality (2026-03-15)

### New Features

#### Circuit Breaker (`src/middleware/circuit_breaker.py`)
- Trips after 3 consecutive EWS connectivity failures
- Rejects requests immediately for 60s instead of waiting for timeout
- Allows one probe request after timeout to test recovery
- Only trips on connectivity/timeout errors, not user errors (validation, not-found)
- Saves ~30s per request when Exchange is down (no more 3x10s timeout retries)

### Improvements

#### Simplified Error Messages
- `validate_input()` now produces `"to: Input should be a valid list"` instead of multi-line Pydantic internals
- `format_error_response()` returns `{"success": false, "error": "..."}` (removed redundant `error_type` field)
- Error messages truncated to 200 chars max — prevents Claude from processing paragraph-length Exchange error dumps
- `find_message_for_account()` returns `"Message not found: {id}"` instead of a 3-line suggestion paragraph

#### Proper async/await (`asyncio.to_thread`)
- All `resolve_names()` calls in GALAdapter wrapped in `asyncio.to_thread()` — no longer blocks event loop
- PersonService `_search_contacts()` and `_search_email_history()` run blocking iteration in thread pool
- Inbox + Sent scans in `_search_email_history` and `get_communication_history` now run concurrently via `asyncio.gather()`

### Code Quality

#### Removed Dead Code
- Removed `handle_ews_errors` decorator from `utils.py` (~70 lines) — was defined but never used by any tool
- All tools use `BaseTool.safe_execute()` for error handling instead

#### Deduplicated JSON Serialization
- `EWSJSONEncoder.default()` now delegates to `make_json_serializable()` instead of duplicating the same logic
- Single source of truth for datetime/EWS-object serialization

### Token Budget Impact
| Component | v3.3 | v3.4 | Savings |
|---|---|---|---|
| Error responses | ~150 tokens | ~50 tokens | -67% |
| Circuit breaker (Exchange down) | ~5,000 tokens/min wasted | ~200 tokens/min | -96% |
| **Simple operation total** | ~6,700 | ~6,200 | **-7%** |

### Files Changed
| File | Change |
|---|---|
| `src/middleware/circuit_breaker.py` | NEW (87 lines) |
| `src/middleware/__init__.py` | Added CircuitBreaker export |
| `src/tools/base.py` | Circuit breaker integration + simplified validation errors |
| `src/utils.py` | Removed handle_ews_errors, deduplicated JSON encoder, simplified error responses |
| `src/adapters/gal_adapter.py` | asyncio.to_thread for all resolve_names calls |
| `src/services/person_service.py` | asyncio.to_thread + asyncio.gather for blocking EWS operations |

---

## v3.3.0 — Phase 2: Tool Consolidation (2026-03-15)

### Breaking Changes
**10 tools removed** from the MCP surface. AI assistants will automatically adapt via `list_tools`. External automation calling these tools by name will need updating.

**Removed tools and their replacements:**

| Removed Tool | Replacement | How to Migrate |
|---|---|---|
| `advanced_search` | `search_emails` with `mode: "advanced"` | Add `mode: "advanced"` parameter |
| `full_text_search` | `search_emails` with `mode: "full_text"` | Add `mode: "full_text"`, rename `query` param |
| `search_contacts` | `find_person` with `source: "contacts"` | Use `find_person(query="...", source="contacts")` |
| `get_contacts` | `find_person` with `source: "contacts"` | Use `find_person(source="contacts")` (no query = list all) |
| `resolve_names` | `find_person` with `source: "gal"` | Use `find_person(query="...", source="gal")` |
| `create_folder` | `manage_folder` with `action: "create"` | Add `action: "create"` parameter |
| `delete_folder` | `manage_folder` with `action: "delete"` | Add `action: "delete"` parameter |
| `rename_folder` | `manage_folder` with `action: "rename"` | Add `action: "rename"` parameter |
| `move_folder` | `manage_folder` with `action: "move"` | Add `action: "move"`, use `destination` param |
| `get_oof_settings` | `oof_settings` with `action: "get"` | Use `oof_settings(action="get")` |
| `set_oof_settings` | `oof_settings` with `action: "set"` | Use `oof_settings(action="set", state="...")` |
| `get_communication_history` | `analyze_contacts` with `analysis_type: "communication_history"` | Add `analysis_type: "communication_history"` |
| `analyze_network` | `analyze_contacts` with `analysis_type: "overview"` etc. | Use `analyze_contacts(analysis_type="...")` |

### Tool Count (at v3.3 release)
- **Before:** 46 tools (42 base + 4 AI)
- **After v3.3:** 36 tools (32 base + 4 AI)
- **Reduction:** -10 tools

> **Note:** The base tool count has since grown back to 42 with the addition of `create_draft`, `create_reply_draft`, `create_forward_draft`, `find_folder`, `get_email_mime`, and `attach_email_to_draft` in later releases (see the Unreleased section at the top of this file).

### New Merged Tools

#### `search_emails` (unified search)
- `mode: "quick"` (default) — filter by subject, sender, date, read status, attachments
- `mode: "advanced"` — multi-folder search with sort, categories, importance, keywords
- `mode: "full_text"` — full-text search across subject, body, attachment names

#### `find_person` (unified contact lookup)
- `source: "all"` (default) — search GAL + contacts + email history
- `source: "gal"` — Active Directory only
- `source: "contacts"` — personal contacts only (no query = list all)
- `source: "email_history"` — email history only
- `source: "domain"` — domain-based search

#### `manage_folder` (unified folder management)
- `action: "create"` — create new folder
- `action: "delete"` — delete folder (soft or permanent)
- `action: "rename"` — rename folder
- `action: "move"` — move folder to new parent

#### `oof_settings` (unified OOF)
- `action: "get"` — retrieve current OOF settings
- `action: "set"` — configure OOF settings

#### `analyze_contacts` (unified contact analysis)
- `analysis_type: "communication_history"` — history with specific person (uses server-side sender filter)
- `analysis_type: "overview"` — comprehensive network overview
- `analysis_type: "top_contacts"` — most-emailed contacts
- `analysis_type: "by_domain"` — contacts grouped by domain
- `analysis_type: "dormant"` — inactive relationships
- `analysis_type: "vip"` — high-volume recent contacts

### Performance Improvements
- **Token savings:** ~2,200 tokens per `list_tools` call (10 fewer schemas × ~220 tokens each)
- **Wrong-tool retries eliminated:** Claude no longer picks wrong search/contact tool
- **Server-side filtering:** `analyze_contacts(analysis_type="communication_history")` uses `sender__email_address` server-side filter instead of scanning 2,000 items client-side
- **SearchByConversationTool:** `folder_map` moved outside loop (was recreated 3× per call)

### Bug Fixes
- Fixed version string drift: `docker-compose-ghcr.yml`, `docker-entrypoint.sh` now show v3.3
- Fixed `SearchByConversationTool` creating `folder_map` inside loop

### Token Budget Impact
| Component | v3.2 | v3.3 | Savings |
|---|---|---|---|
| Tool schemas (list_tools) | ~10,000 | ~4,500 | -55% |
| Tool selection retries | ~800 | ~200 | -75% |
| **Simple operation total** | ~18,000 | ~6,700 | **-63%** |

---

## v3.2.0 — Phase 1: Token Optimization & Bug Fixes (2026-03-14)

### Bug Fixes
- Fixed autodiscovery ignoring `EWS_SERVER_URL` when `EWS_AUTODISCOVER=true`
- Fixed Docker container unable to reach corporate Exchange (switched to `network_mode: host`)
- Fixed single-day calendar queries returning wrong/missing events (zero-duration window)
- Fixed `format_datetime` not defined in `search_tools.py` (missing import)
- Fixed auth retry loop: `retry_if_not_exception_type(AuthenticationError)` skips retries on auth failures
- Fixed recursive folder search: subfolders + `root.walk()` fallback

### Optimizations
- Trimmed all 46 tool descriptions to 1 line (under 15 words)
- Removed dead `_search_email_history` from FindPersonTool (~140 lines)
- Replaced GAL fuzzy search 8-prefix loop with single query (1 API call instead of 8)
- Removed redundant recipient pre-validation from SendEmailTool (~40 lines, N API calls)
- Deduplicated `INLINE_ATTACHMENTS_SCHEMA` to single definition in `utils.py`
- Added server-side `sender__email_address` filter in PersonService

### New Features
- Base64 `inline_attachments` support on 5 tools (send_email, reply_email, forward_email, create_appointment, update_appointment)
- Person-centric architecture with multi-strategy GAL search (4 fallback strategies)
