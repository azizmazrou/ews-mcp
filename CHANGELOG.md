# Changelog

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

### Tool Count
- **Before:** 46 tools (42 base + 4 AI)
- **After:** 36 tools (32 base + 4 AI)
- **Reduction:** -10 tools

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
