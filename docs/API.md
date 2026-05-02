# API Reference — v4.0

This is the per-tool reference for the 67 tools the v4.0 MCP server
advertises over `tools/list`. Every tool's input schema is also
returned dynamically by the server, so an MCP client always sees the
canonical version. This document explains the *intent*, the
*non-obvious parameters*, and the *response shape*.

Sections:

1. [Conventions](#conventions)
2. [Email — read & write (12 tools)](#email--read--write)
3. [Drafts (4 tools)](#drafts)
4. [Attachments (5 tools)](#attachments)
5. [Calendar (7 tools)](#calendar)
6. [Contacts (5 tools)](#contacts)
7. [Tasks (5 tools)](#tasks)
8. [Folders (3 tools)](#folders)
9. [Search (1 tool)](#search)
10. [Out-of-Office (4 tools)](#out-of-office)
11. [AI — semantic search (1 tool)](#ai--semantic-search)
12. [Agent-secretary primitives](#agent-secretary-primitives)
13. [Compound tools (2 tools)](#compound-tools)

---

## Conventions

### `target_mailbox` (multi-mailbox)

Every base tool accepts `target_mailbox: <smtp>` to operate on a
mailbox other than the primary authenticated one. Requires
`EWS_IMPERSONATION_ENABLED=true` in the server env and that the
authenticated account has impersonation or delegate rights on the
target. When omitted (default), the tool operates on `EWS_EMAIL`.

### Body format on read & write

Defaults are `format=html` (read) and `body_format=html` (write) —
identical to v3.x. Opt in to `markdown` or `text` for the v4
conversion pipeline. See sections [`get_email_details`](#get_email_details)
and [`send_email`](#send_email) / [`reply_email`](#reply_email) /
[`forward_email`](#forward_email) for the full effect.

### `categories` (Outlook follow-up labels)

Optional `categories: list[str]` on every create/update tool. On
update, the field is **replaced** with the supplied list (empty list
clears). Strings are matched against the user's category list
case-insensitively by Outlook.

### Field projection

Many list-style and detail-style tools accept a `fields: list[str]`
parameter to return only the named keys in each result. When omitted,
the full backward-compatible response shape is returned.

### Standard response envelope

```jsonc
{
  "success": true,
  "message": "Human-readable summary",
  // tool-specific fields …
  "mailbox": "user@company.com"
}
```

On failure, `success=false` and `error: <string>`. The MCP client also
sees an HTTP 4xx/5xx mapping for ValidationError (400) and
ToolExecutionError (500) when running over SSE/HTTP.

---

## Email — read & write

12 tools covering full email lifecycle. `send_email`, `reply_email`,
and `forward_email` accept the v4 `body_format` for markdown→HTML
server-side conversion.

### `send_email`

Send a new email. Supports CC/BCC, attachments (file paths + inline
base64), `dry_run`, and `body_format`.

**Parameters**

| Name | Type | Required | Notes |
|---|---|---|---|
| `to` | `string[]` | yes | recipient addresses |
| `subject` | `string` | yes | |
| `body` | `string` | yes | shape governed by `body_format` |
| `body_format` | `"html"\|"markdown"\|"text"` | no | default `html`. `markdown` → server converts to HTML; `text` → wrapped in minimal `<p>`. |
| `cc`, `bcc` | `string[]` | no | |
| `importance` | `"Low"\|"Normal"\|"High"` | no | |
| `attachments` | `string[]` | no | server-side file paths |
| `inline_attachments` | `object[]` | no | base64 inline files |
| `dry_run` | `bool` | no | when true, validate + render but do not send |
| `target_mailbox` | `string` | no | impersonation |

**Example**

```python
send_email(
    to=["alice@example.com"],
    subject="Q1 review",
    body_format="markdown",
    body="""
    Hi Alice,

    Please review the **Q1 numbers** before Friday:
    - line 4 (cloud spend) up by 8 %
    - contractor budget moves to Q2

    Thanks
    """.strip(),
)
```

### `read_emails`

Read messages from a folder, newest first. Returns previews (no full
body) by default — use `get_email_details` for the full message.

**Key parameters**

| Name | Type | Default | Notes |
|---|---|---|---|
| `folder` | `string` | `"inbox"` | standard name (`inbox` / `sent` / `drafts` / `deleted` / `junk`) or path (`Inbox/Projects`) or folder ID |
| `max_results` | `int` | 50 | max 1000 |
| `unread_only` | `bool` | false | |
| `format` | `"html"\|"markdown"\|"text"` | `html` | reserved — `read_emails` returns previews only, full conversion happens in `get_email_details` |
| `include_body` | `bool` | true | drop preview entirely |

### `search_emails`

Three modes selected via `mode`:

| Mode | Description | Best for |
|---|---|---|
| `quick` (default) | Direct EWS filter on Inbox. Up to ~3 filters. | `subject_contains` / `from_address` / `is_read` style triage |
| `advanced` | Combinable filters + sort + scope across multiple folders. | Multi-criterion queries |
| `full_text` | Exchange `searchquery` engine. | Prose / phrase search |

**v4-relevant parameters**

| Name | Type | Notes |
|---|---|---|
| `is_flagged` | `bool` | filter by Outlook follow-up flag (`PR_FLAG_STATUS=2`). New in v4. |
| `is_read` | `bool` | |
| `has_attachments` | `bool` | |
| `categories` | `string[]` | one or more must match (advanced/full_text) |
| `importance` | `"Low"\|"Normal"\|"High"` | |
| `subject_contains`, `body_contains`, `from_address`, `to_address` | `string` | |
| `start_date`, `end_date` | ISO 8601 `string` | |
| `offset` / `max_results` | `int` | pagination via `next_offset` in response |
| `search_scope` | `string[]` | folders to search (advanced/full_text) |
| `sort_by`, `sort_order` | enums | `ascending`/`descending` |
| `query`, `search_query`, `keywords`, `exact_phrase`, `search_in` | full_text mode |

### `get_email_details`

Full message including body, attachments metadata, threading.

**v4 parameters (highlight)**

| Name | Type | Default | Notes |
|---|---|---|---|
| `format` | `"html"\|"markdown"\|"text"` | `html` | **the marquee v4 read-side feature.** `markdown` runs the Outlook MSO body through `markdownify` server-side (cached in SQLite forever — Exchange messages are immutable post-send), returning ~12× fewer tokens. |
| `trim_quoted` | `bool` | false | strips `On …, X wrote:` thread history. Big savings on long threads. |
| `include_body` | `bool` | true | when false, both `body` and `body_html` drop from the response |
| `fields` | `string[]` | — | response projection |
| `target_mailbox` | `string` | — | |

**Response shape (relevant fields)**

```jsonc
{
  "email": {
    "message_id": "AAMk...",
    "subject": "...",
    "from": "alice@example.com",
    "to": ["..."],
    "cc": ["..."],
    "body": "# Subject ...",            // shape governed by `format`
    "body_format": "markdown",          // actual format returned
    "body_html": "",                    // empty when format != "html"
    "received_time": "2026-04-30T...",
    "has_attachments": true,
    "attachments": ["report.pdf", "thread.msg"]
    // ...
  }
}
```

### `get_emails_bulk`

Fetch multiple messages by ID in one call. Same per-item shape as
`get_email_details` for each input ID. Useful for the agent's
"give-me-context" pattern after a search.

### `delete_email`

Soft-delete to Trash by default; pass `permanent=true` (or alias
`hard_delete=true`) to bypass.

### `move_email`

Move to another folder. Either `destination_folder` (name / path /
standard name) **or** `destination_folder_id` (Exchange ID — resolved
directly by ID, no path parsing).

### `copy_email`

Same shape as `move_email` but copies. Returns `copied_message_id` of
the new copy in the destination folder.

### `update_email`

Update flags, categories, read status, importance.

| Name | Notes |
|---|---|
| `is_read` | `bool` |
| `flag_status` | `"NotFlagged" \| "Flagged" \| "Complete"` |
| `categories` | `string[]` — replace existing |
| `importance` | `"Low" \| "Normal" \| "High"` |

### `reply_email`

Reply preserving thread + Outlook signature.

| Name | Notes |
|---|---|
| `body_format` | `html` / `markdown` / `text` (v4) |
| `reply_all` | bool, default false |
| `attachments`, `inline_attachments` | optional |

The reply body is wrapped in the standard Outlook `WordSection1` div +
border-top thread separator + `From:/Sent:/To:/Cc:/Subject:` header
block. Inline attachments from the original message (signature
graphics, embedded images) are copied to the reply automatically. The
response includes `inline_attachments_preserved` so the caller can
verify the count.

### `forward_email`

Same shape as `reply_email`, with `to`/`cc`/`bcc` for new recipients
and a body prelude.

---

## Drafts

| Tool | Purpose |
|---|---|
| `create_draft` | Save a new draft to `Drafts` for later review/send |
| `create_reply_draft` | Build a reply draft (no send). Caller can supply extra `cc` / `bcc` (v4 — preserved on save) and `categories`. |
| `create_forward_draft` | Build a forward draft (no send). Same `cc`/`bcc`/`categories` support. |
| `attach_email_to_draft` | Attach an existing message as `.eml` to a draft you already created |

All four accept `body_format` and `categories` parameters. Response
includes the new draft's `message_id` so callers can chain
`add_attachment` / `attach_email_to_draft` against it.

---

## Attachments

5 tools. The marquee v4 change is `read_attachment` text extraction
expansion.

### `read_attachment`

Extract human/LLM-readable text from binary attachments.

**Parameters**

| Name | Type | Required | Notes |
|---|---|---|---|
| `message_id` | `string` | yes | parent message |
| `attachment_name` | `string` | yes | filename on the message |
| `extract_tables` | `bool` | no | PDF/DOCX table extraction |
| `max_pages` | `int` | no | PDF only |
| `target_mailbox` | `string` | no | |

**Supported file types (extension dispatch)**

| Extension | Library | Output |
|---|---|---|
| `pdf` | `pdfplumber` | text + per-page boundaries; tables when `extract_tables=true` |
| `docx` | `python-docx` | text + tables |
| `xlsx`, `xls` | `openpyxl`, `xlrd` | sheet-by-sheet rows |
| `pptx` | `python-pptx` | slide-by-slide text + speaker notes + embedded tables |
| **`msg`** | `extract-msg` | Outlook compound-file: subject / from / to / cc / date envelope + body (HTML→markdown) + recursive nested attachment listing |
| `eml` | stdlib `email` | RFC-822: same shape as `.msg` |
| `html`, `htm` | `markdownify` | RTL-safe GFM markdown |
| `csv` | stdlib | BOM-aware UTF-8 / UTF-16 decode |
| `txt`, `log`, `json`, `xml`, `md` | stdlib | as-is text |

**Response**

```jsonc
{
  "file_name": "thread.msg",
  "file_type": "msg",
  "file_size": 84321,
  "content_length": 12450,
  "content": "Subject: Re: Q1 review\nFrom: ...",
  "supports_arabic": true
}
```

### `list_attachments`

List per-attachment metadata for a message.

```jsonc
{
  "attachments": [
    {
      "id": "AAMk...",
      "name": "thread.msg",
      "size": 84321,
      "content_type": "application/vnd.ms-outlook",
      "is_inline": false,
      "attachment_type": "FileAttachment"
    }
  ],
  "count": 1
}
```

### `download_attachment`

Return an attachment's bytes as base64 (default) or save to a path
under the server's `EWS_DOWNLOAD_DIR` jail. Accepts either
`attachment_id` or `attachment_name`.

### `add_attachment`

Attach a file to a draft. Either `file_path` (server-side) or
`file_content` (base64 inline). Optional `is_inline` + `content_id`
for embedded images. The v4 implementation uses real EWS
`CreateAttachment` (the v3.4 path was a silent no-op).

### `delete_attachment`

Delete an attachment by `attachment_id` or `attachment_name`. Uses
real EWS `DeleteAttachment`.

### `get_email_mime`

Return the raw RFC-822 MIME content of a message as base64. Useful
for archiving, audit, and forensics.

---

## Calendar

7 tools spanning create / read / update / delete + RSVP + free-busy.

| Tool | Highlights |
|---|---|
| `create_appointment` | `subject`, `start_time`, `end_time`, `location`, `body`, `attendees`, `is_all_day`, `reminder_minutes`, **`categories`** (v4) |
| `get_calendar` | range query, returns events with `item_id`, `subject`, `start`, `end`, `location`, `organizer`, `attendees`, `is_all_day` |
| `update_appointment` | `item_id` plus optional fields to update; `categories` v4 |
| `delete_appointment` | optional `send_cancellation: bool` (default true) |
| `respond_to_meeting` | `response: "Accept" / "Decline" / "Tentative"` + optional body |
| `check_availability` | `email_addresses[]`, `start_time`, `end_time`, returns free/busy |
| `find_meeting_times` | `attendees[]`, `duration_minutes`, `date_range_start`, `date_range_end`, scored suggestions |

---

## Contacts

5 tools, including the `find_person` unified lookup.

### `find_person`

Multi-source person search. The v4 `source` selector decides which
strategies run.

| `source` | Searches |
|---|---|
| `all` (default) | GAL + local Contacts + email history |
| `gal` | Global Address List only (GAL fuzzy + exact) |
| `contacts` | Local Contacts folder only |
| `email_history` | Inbox/Sent senders/recipients seen in the last N days |
| `domain` | Everyone at a specified email domain |

`include_stats=true` enriches each result with email counts
(received / sent in window) and a relationship score.

### `analyze_contacts`

Communication-graph analytics. The `analysis_type` enum controls
which pre-built report runs:

| `analysis_type` | What it returns |
|---|---|
| `communication_history` | Per-sender history with one specific person |
| `overview` | Total contacts, email volume, VIP count, dormant count |
| `top_contacts` | Top N by email count over `time_range_days` |
| `by_domain` | Aggregated by `@domain` |
| `dormant` | Senders you used to talk to but haven't recently |
| `vip` | High-frequency, recent senders |

### `create_contact` / `update_contact` / `delete_contact`

Standard Outlook contact CRUD. v4 adds `categories: list[str]` to
both create and update.

---

## Tasks

5 tools — full CRUD plus a quick-mark-complete shortcut.

| Tool | Notes |
|---|---|
| `create_task` | `subject`, `body`, `due_date`, `start_date`, `importance`, `reminder_time`, **`categories`** (v4) |
| `get_tasks` | filter by status / completion |
| `update_task` | partial update; `categories` (v4) |
| `complete_task` | sets status to Completed |
| `delete_task` | |

---

## Folders

3 tools.

### `list_folders`

Folder hierarchy with item counts.

| Name | Notes |
|---|---|
| `parent_folder` | starting point (`root` / `inbox` / etc.) |
| `max_depth` | recursion limit |
| `include_hidden` | system folders |

### `find_folder`

Find a folder by `query` (name fragment or path). Returns the stable
folder ID for use with `move_email` / `copy_email` /
`manage_folder(action="delete")`.

### `manage_folder`

Unified create / delete / rename / move via `action`. Either
`folder_id` (Exchange ID) or `folder_name` resolves the target.
Soft-delete (default) moves to Deleted Items via `Folder.move_to_trash`
when supported by the deployed exchangelib version, else falls back
to permanent delete with an explicit response label
("permanently deleted (no move_to_trash)") so the caller knows.

---

## Search

### `search_by_conversation`

Walk every mail folder by default and gather every message in a
thread. Custom-label / archived messages are not invisible. v4 fix:
`conversation_id` is wrapped in a typed `ConversationId` before
`folder.filter(...)` so the EWS SOAP restriction builds correctly
(was emitting `TypeError` per folder in v3.4 against newer
exchangelib versions).

```jsonc
{
  "items": [
    {
      "message_id": "AAMk...",
      "subject": "Re: Q1 review",
      "from": "alice@example.com",
      "received_time": "2026-04-30T...",
      "folder": "Inbox"
    }
  ],
  "count": 9,
  "conversation_id": "AAQk...",
  "searched_folders": ["Inbox", "Sent Items", "Archive", ...],
  "skipped_folders": [
    {"folder": "Calendar", "reason": "permission_denied", "error_type": "ErrorAccessDenied"}
  ]
}
```

---

## Out-of-Office

| Tool | Purpose |
|---|---|
| `oof_settings` | Get / set the user's OOF state. v4 fixes: preserves omitted reply text, normalises Scheduled `start`/`end` to UTC (fixes `InvalidScheduledOofDuration`), returns `currently_active` in the set response |
| `configure_oof_policy` | Persist a templated policy (auto-replies + forward rules + VIP passthrough) into the agent-side memory store |
| `get_oof_policy` | Retrieve the stored policy |
| `apply_oof_policy` | Evaluate the policy against an inbound message — **creates DRAFTS, never sends**, so the user can review |

---

## AI — semantic search

### `semantic_search_emails`

Vector cosine search over the SQLite-cached embedding store.

| Name | Type | Default | Notes |
|---|---|---|---|
| `query` | `string` | — | natural-language query |
| `folder` | `string` | `inbox` | |
| `max_results` | `int` | 10 | |
| `threshold` | `float` | 0.7 | min cosine similarity, 0.0-1.0 |
| `fields` | `string[]` | — | response projection |
| `exclude_automated` | `bool` | true | filter `noreply@` / `mailer-daemon` / "Automatic reply:" before ranking |
| `target_mailbox` | `string` | — | |

The query is embedded once via the configured `AI_EMBEDDING_MODEL`,
candidate emails are loaded with `.only(...)` to avoid pulling full
bodies, and existing embeddings are bulk-fetched from the SQLite
cache. Per-call cap is 50 freshly embedded items so a single call
doesn't blow up against a huge mailbox.

```jsonc
{
  "query": "Q1 budget approval",
  "result_count": 3,
  "results": [
    {
      "message_id": "AAMk...",
      "subject": "Re: Q1 review",
      "from": "alice@example.com",
      "similarity_score": 0.81,
      "received_time": "2026-04-30T...",
      "snippet": "Approved on Q1 budget. Two changes: ..."
    }
  ]
}
```

---

## Agent-secretary primitives

These are deterministic typed-record CRUD tools that an agent can use
to maintain its own state across conversations. None of them call an
LLM — that's intentional (see README "MCP / skill boundary"). All are
gated behind `ENABLE_AGENT=true` (default).

### Memory KV

| Tool | Purpose |
|---|---|
| `memory_set` | per-mailbox, namespaced JSON KV. `namespace`, `key`, `value`, optional `ttl_seconds`, optional `metadata`. 1 MiB value cap. |
| `memory_get` | by `(namespace, key)` |
| `memory_list` | by `namespace`; pagination |
| `memory_delete` | by `(namespace, key)` |

### Commitments

Manual CRUD only. The auto-extraction tool (`extract_commitments`)
was removed in v4; the consuming agent should detect commitments
in-prompt and call `track_commitment` for each.

| Tool | Purpose |
|---|---|
| `track_commitment` | record "I owe X to Y by Z" or "Y owes me X". `description`, `owner`, `counterparty`, `due_at`, `thread_id`, `message_id`, `excerpt` |
| `list_commitments` | `scope: "open" / "overdue" / "done"`, filter by `owner` |
| `resolve_commitment` | by `commitment_id`, `outcome: "done" / "cancelled"`, optional note |

### Approvals

Two-phase commit for side-effectful actions. The agent submits an
action, a human approves/rejects, then `execute_approved_action`
consumes the approval and runs the original tool.

| Tool | Purpose |
|---|---|
| `submit_for_approval` | `action: <tool_name>`, `arguments: {...}`, `reason`, `ttl_seconds`. Allow-listed actions only. |
| `list_pending_approvals` | |
| `approve` / `reject` | by `approval_id`, atomic single-use |
| `execute_approved_action` | by `approval_id` — runs the tool, consumes the approval |

### Voice profile

`get_voice_profile` returns the stored style card (formality,
greetings, signoffs, sample examples). v4 removed
`build_voice_profile`; the agent builds the profile in-prompt from
sample sent items and persists it via `memory_set`.

### Rule engine

Declarative match → actions automation, evaluated against new mail.

| Tool | Purpose |
|---|---|
| `rule_create` | `name`, `match: {subject_contains, from_contains, ...}`, `actions: [{type: "categorize\|flag_importance\|mark_read\|move_to_folder\|notify_agent\|track_commitment", ...}]`, `enabled` |
| `rule_list` | |
| `rule_delete` | by `rule_id` |
| `rule_simulate` | preview which rules would fire against a `message_id` |
| `evaluate_rules_on_message` | actually apply matching rules; `dry_run` flag |

### OOF policy

See [Out-of-Office](#out-of-office) above. `configure_oof_policy` /
`get_oof_policy` / `apply_oof_policy` operate on the policy stored
in the memory KV.

---

## Compound tools

These two tools return **structured JSON context** so the consuming
agent can compose prose itself. They do not call an LLM. (In v3.x
they did — that's been deliberately moved skill-side per the v4
"MCP / skill boundary" principle.)

### `generate_briefing`

Pulls the structured ingredients for a daily/weekly briefing.

| Name | Notes |
|---|---|
| `scope` | `"today"` / `"week"` |
| `since` | optional ISO timestamp, overrides scope |
| `include` | `string[]` — any of `inbox_delta`, `meetings`, `commitments`, `overdue_tasks`, `vip_activity` |
| `max_per_section` | int |

Response is a structured JSON object the agent renders into prose
in-prompt.

### `prepare_meeting`

Returns the meeting + attendees + per-attendee email history +
attachment text for an upcoming appointment, so the agent has all the
context it needs to brief you on the meeting.

| Name | Notes |
|---|---|
| `appointment_id` | required |
| `depth` | `"quick"` (envelope only) or `"deep"` (recent thread per attendee) |
| `history_per_attendee` | int |
| `extract_attachment_text` | bool — runs `read_attachment` on each meeting attachment |

---

## Removed in v4 (reasoning moved skill-side)

The following tools existed in v3.x and have been deliberately
**removed** — the consuming agent already has an LLM and can do these
tasks in-prompt with the data this MCP returns, which is faster,
cheaper, and produces better results than a second LLM hop:

- `classify_email`
- `summarize_email`
- `suggest_replies`
- `extract_commitments` (the auto-extraction; manual `track_commitment` remains)
- `build_voice_profile` (the LLM tone analysis; storage via `memory_set` remains)

See the [v4.0.0 entry in CHANGELOG.md](../CHANGELOG.md#v400--2026-05-03)
for the rationale and the in-prompt replacement pattern.
