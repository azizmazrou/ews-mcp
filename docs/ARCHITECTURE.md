# Architecture — v4.0

This document describes how the v4.0 server is put together: the
runtime topology, the layers inside the process, the caching strategy,
the data flow on a typical call, the boundary between MCP and the
consuming skill / agent, and the design constraints that shape the
codebase.

It's targeted at people who want to fork, extend, or audit the
server. End-user usage lives in the [README](../README.md) and the
per-tool reference is in [API.md](API.md).

---

## Runtime topology

```
   ┌─────────────────────────┐
   │ MCP client (LLM agent)  │
   │  Claude Desktop /       │
   │  Open WebUI /           │
   │  custom MCP runtime     │
   └────────────┬────────────┘
                │ JSON-RPC over MCP
                │  (stdio | SSE/HTTP)
   ┌────────────▼────────────┐
   │   EWS MCP Server (this) │
   │   single Python process │
   │   single Docker image   │
   └────┬──────────────┬─────┘
        │              │
        │ exchangelib  │ optional embedding HTTP
        │ SOAP / OAuth │ (OpenAI / Anthropic / local OpenAI-compat)
        │              │
   ┌────▼────────┐  ┌──▼───────────────┐
   │  Microsoft  │  │  AI provider     │
   │  Exchange / │  │  (only used by   │
   │  Office 365 │  │  semantic_search)│
   └─────────────┘  └──────────────────┘
                        ▲
                        │
        ┌───────────────┘
        │
   ┌────┴───────────────────────┐
   │  Per-mailbox SQLite cache  │
   │  data/ews_mcp_<email>.db   │
   │  - body_format_cache       │
   │  - attachment_text_cache   │
   │  - embedding_cache         │
   └────────────────────────────┘
```

Default transport is stdio (the MCP runtime spawns the container as a
subprocess and pipes JSON-RPC over stdin/stdout). For remote MCP
clients the server also speaks SSE/HTTP — see
[CONNECTION_GUIDE.md](CONNECTION_GUIDE.md) for details.

---

## Layers inside the process

```
src/
├── main.py                     MCP plumbing (tool registry, dispatch)
├── config.py                   Pydantic Settings (env vars)
├── auth.py                     OAuth2 / Basic / NTLM credential factory
├── ews_client.py               exchangelib Account wrapper, FaultTolerance retry
├── exceptions.py               ToolExecutionError / ValidationError
├── models.py                   pydantic input request models
├── utils.py                    safe_get, ews_id_to_str, format_*, …
├── logging_system.py           structured logger
├── log_rotation.py             file log rotation
├── log_analyzer.py             optional log post-processing
├── openapi_adapter.py          dynamic OpenAPI 3.0 schema for SSE clients
├── body_format.py              ▌ NEW v4 ▐ HTML ⇄ Markdown converters
├── cache/
│   └── sqlite_cache.py         ▌ NEW v4 ▐ unified per-mailbox cache
├── ai/
│   ├── base.py                 EmbeddingProvider / ChatProvider abstract
│   ├── openai_provider.py      OpenAI + OpenAI-compatible local
│   ├── anthropic_provider.py   Anthropic Claude
│   ├── provider_factory.py     `get_embedding_provider`, `get_ai_provider`
│   ├── embedding_service.py    SQLite-aware semantic search
│   └── classification_service.py  (used by AI provider helpers only)
├── memory/
│   └── store.py                JSON-on-disk memory KV (per-mailbox)
├── middleware/
│   ├── circuit_breaker.py      per-tool failure-rate breaker
│   ├── rate_limiter.py         token-bucket per-tool
│   └── audit_logger.py         redacted structured audit log
├── adapters/
│   ├── gal_adapter.py          GAL search strategies for find_person
│   └── cache_adapter.py        memo for GAL hits
├── core/
│   ├── person.py               Person model
│   ├── email_message.py        EmailMessage model
│   └── thread.py               ConversationThread model
├── services/
│   ├── person_service.py       multi-strategy find_person orchestration
│   ├── email_service.py        thin wrapper over exchangelib Inbox
│   ├── thread_service.py       conversation walker
│   └── attachment_service.py   download + inline copy helpers
└── tools/
    ├── base.py                 BaseTool — every tool subclasses
    ├── email_tools.py          send / read / search / details / move / copy / update / reply / forward
    ├── email_tools_draft.py    create_draft, create_reply_draft, create_forward_draft
    ├── attachment_tools.py     list / download / add / delete / read_attachment / get_email_mime / attach_email_to_draft
    ├── calendar_tools.py       7 calendar tools
    ├── contact_tools.py        create / update / delete contact
    ├── contact_intelligence_tools.py  find_person, analyze_contacts
    ├── task_tools.py           5 task tools
    ├── folder_tools.py         list / find / manage folder
    ├── search_tools.py         search_by_conversation
    ├── oof_tools.py            oof_settings (get + set)
    ├── oof_policy_tools.py     configure / get / apply OOF policy
    ├── ai_tools.py             SemanticSearchEmailsTool (only — v4)
    ├── memory_tools.py         memory_set / get / list / delete
    ├── commitment_tools.py     track / list / resolve commitment (no auto-extract)
    ├── approval_tools.py       submit / list / approve / reject / execute
    ├── voice_tools.py          get_voice_profile (read-only)
    ├── rule_tools.py           rule_create / list / delete / simulate / evaluate
    ├── briefing_tools.py       generate_briefing (structured JSON, no LLM)
    └── meeting_prep_tools.py   prepare_meeting (structured JSON, no LLM)
```

### Layer boundaries

```
   tools/         <─── MCP-facing input/output schemas. One class
                       per tool, instantiated once at registration.
        │
        ▼
   services/      <─── orchestration: multi-strategy find_person,
                       thread walker, attachment copy.
        │
        ▼
   adapters/      <─── EWS-specific helpers (GAL search variants,
                       per-mailbox cache shim).
        │
        ▼
   ews_client.py  <─── exchangelib Account factory.
   auth.py            credential builders.

   utils.py / models.py / exceptions.py — used at every layer.
   middleware/ — wraps tool dispatch (circuit breaker, rate limit,
                 audit). Sits in front of `tools/`.
   body_format.py / cache/sqlite_cache.py / memory/ — cross-cutting
                 concerns reachable from `tools/` directly.
```

A tool method always takes pure dicts in and returns pure dicts out.
The MCP layer (main.py) translates JSON-RPC frames; the tool layer
sees Python dicts. Anything that comes off `exchangelib` is unwrapped
into JSON-friendly shapes via `ews_id_to_str`, `format_datetime`, and
the projection helpers in `utils.py`.

---

## The v4 core: body_format

`src/body_format.py` is a single-file module that does both
directions of the Outlook MSO HTML ⇄ Markdown conversion.

```
                                READ DIRECTION
 ┌─────────────────────────────────────────────────────────────┐
 │ get_email_details(format=markdown)                          │
 │    │                                                        │
 │    ▼                                                        │
 │ check SQLite body_format_cache[(message_id, "markdown")]    │
 │    │                                                        │
 │    ▼ miss                                                   │
 │ render_body(html=item.body, plain=item.text_body, fmt=md)   │
 │    │  uses markdownify with MSO strip list:                 │
 │    │    o:p, v:shape, v:imagedata, w:wordDocument,          │
 │    │    script, style, meta, xml, …                         │
 │    ▼                                                        │
 │ write back to SQLite (forever — Exchange msgs are immutable)│
 │    │                                                        │
 │    ▼                                                        │
 │ optional trim_quoted: strip "On … wrote:" / Arabic equiv    │
 │    │                                                        │
 │    ▼                                                        │
 │ return body, body_format=markdown                           │
 │ (body_html is dropped from the response when fmt != html)   │
 └─────────────────────────────────────────────────────────────┘

                                WRITE DIRECTION
 ┌─────────────────────────────────────────────────────────────┐
 │ send_email(body_format=markdown, body="# hi …")             │
 │    │                                                        │
 │    ▼                                                        │
 │ compose_body(body, fmt=markdown)                            │
 │    │  python-markdown w/ extra+nl2br+sane_lists extensions  │
 │    ▼                                                        │
 │ HTML produced                                               │
 │    │                                                        │
 │    ▼                                                        │
 │ existing send_email pipeline: HTMLBody(...), exchangelib    │
 │ Message.send() → EWS                                        │
 │ Outlook signature appended downstream by transport rule     │
 │    │                                                        │
 │    ▼                                                        │
 │ caller's signature with inline cid: image refs is intact    │
 └─────────────────────────────────────────────────────────────┘
```

Reply / forward path is identical except that the converted HTML is
emitted as a direct child of `WordSection1` (avoiding an invalid
`<p class="MsoNormal">`-wraps-block-elements situation), and the
existing `format_body_for_html`/`sanitize_html` second pass is skipped
for `body_format != "html"` (the markdown library output is already
trusted markup).

The schema fragments `READ_FORMAT_SCHEMA` and `WRITE_FORMAT_SCHEMA`
in `body_format.py` are imported by `email_tools.py` so every tool
that exposes the `format` / `body_format` parameter declares them
identically.

---

## The v4 core: SQLite cache

`src/cache/sqlite_cache.py` is a thin wrapper over `sqlite3` from the
stdlib. One file per mailbox, three tables.

```sql
CREATE TABLE body_format_cache (
    message_id  TEXT NOT NULL,
    format      TEXT NOT NULL,         -- 'markdown' | 'text'
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (message_id, format)
);

CREATE TABLE attachment_text_cache (
    attachment_id  TEXT PRIMARY KEY,
    file_name      TEXT,
    content_type   TEXT,
    extracted_text TEXT NOT NULL,
    extractor      TEXT NOT NULL,        -- 'pdfplumber', 'extract-msg', ...
    bytes_in       INTEGER,
    chars_out      INTEGER,
    created_at     REAL NOT NULL
);

CREATE TABLE embedding_cache (
    text_hash   TEXT NOT NULL,           -- sha256(subject+first 500 chars body)
    model       TEXT NOT NULL,           -- e.g. 'nomic-embed-text'
    vector      BLOB NOT NULL,           -- packed float32, len = dim*4
    dim         INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (text_hash, model)
);
```

Why three caches in one file:

- **Single backup unit.** A user wanting to migrate machines
  or restore from a snapshot copies one `.sqlite` file.
- **Single connection pool.** Each tool reaches for
  `self.ews_client.sqlite_cache` and gets the same instance.
  Per-call `sqlite3.connect` with `isolation_level=None` and
  `PRAGMA journal_mode=WAL` keeps writes from blocking concurrent
  reads.
- **Why not a vector database?** A real vector DB (pgvector, Qdrant,
  Chroma, Milvus) would force every operator to stand up another
  service. Cosine similarity over ≤ 100 K vectors in numpy is < 50 ms
  and a personal mailbox rarely exceeds that. The opt-in escape hatch
  for huge mailboxes is a future `AI_VECTOR_BACKEND=qdrant` env knob;
  not implemented in v4.

### Legacy migration

On first instantiation, the cache reads `data/embeddings/embeddings.json`
(the v3.x flat-dict cache) once and INSERTs every entry into
`embedding_cache`, attributing them to `AI_EMBEDDING_MODEL` (defaults
to `nomic-embed-text` if the env var is unset). Idempotent — repeat
runs INSERT-OR-REPLACE and don't duplicate. The legacy file is left
in place as a fallback so the existing JSON read path keeps working
during the rollout.

The `EmbeddingService` (`src/ai/embedding_service.py`) checks
`SQLiteCache.get_embedding(text_hash, model)` before going to the
network. `embed_batch` calls `get_embeddings_bulk` to avoid N+1
SQLite hits when ranking 50-200 candidates for a semantic search.

---

## Data flow: a typical call

`get_email_details(message_id="…", format="markdown")`:

```
 1. MCP client sends a JSON-RPC tools/call frame
 2. main.py dispatches to GetEmailDetailsTool.safe_execute
       → middleware (rate limit, circuit breaker, audit)
       → tool.execute(**kwargs)
 3. tool.execute:
       a. resolve account (impersonation if target_mailbox set)
       b. find_message_for_account(account, message_id)
          (walks every mail folder until found)
       c. extract sender, recipients, attachments metadata
       d. cache lookup:
             cache.get_body(message_id, "markdown")
             ── hit ──> use cached markdown
             ── miss ─> render_body(html, plain, "markdown")
                          markdownify → strip MSO tags →
                          collapse whitespace
                       cache.put_body(message_id, "markdown", md)
       e. assemble email_details dict; gate body_html on fmt=="html"
       f. apply optional trim_quoted
       g. apply optional fields= projection
       h. wrap in format_success_response(...)
 4. main.py wraps in MCP envelope → JSON-RPC response over the
    transport
 5. middleware records audit log
```

Total time on a warm cache: ~30 ms (mostly EWS GetItem).
On a cold cache: GetItem + markdownify (~20 ms) + SQLite write
(< 1 ms). Negligible.

---

## MCP / skill boundary

The v4 design draws an explicit line between **what the MCP does** and
**what the consuming skill / agent does**:

```
                        MCP                   |             skill / agent
                  (this server)               |          (Claude / GPT / …)
   ─────────────────────────────────────────  |  ──────────────────────────
   Deterministic data work                    |   Reasoning
                                              |
   • fetch / store / move / delete / search   |   • classify / summarise
   • embed / cosine-rank                      |   • generate / draft
   • extract text from PDF / DOCX / MSG / …   |   • decide / plan
   • convert HTML ⇄ Markdown                  |   • compose prose
   • cache, index, deduplicate                |   • follow up on tasks
   • persist commitments, voice cards,        |   • detect commitments in
     approvals, rules, OOF policies             email bodies
   • run typed transactions (approval,        |   • produce style cards
     rule evaluation)                         |     from sample sent items
```

If a tool's output is *deterministic given its inputs* (the same call
always returns the same thing), it belongs in MCP. If a tool's output
is *a judgement, an opinion, or freshly composed prose*, it belongs in
the skill — the LLM is already running there for free.

This is why the v4 server intentionally **does not expose**:

- `classify_email` — the consuming agent classifies in-prompt
- `summarize_email` — the agent summarises in-prompt
- `suggest_replies` — the agent generates replies in-prompt
- `extract_commitments` — the agent detects, then calls `track_commitment`
- `build_voice_profile` — the agent analyses, then `memory_set` persists

…even though those tools existed in v3.x. The result: cheaper
workflows (one fewer LLM hop), better reasoning (the consuming agent
is usually a stronger model than what's wired into the MCP), and a
smaller surface for the tool-discoverer to learn.

The 67-tool surface is thus the maximum the MCP exposes. A v4.x will
not grow LLM-reasoning tools.

---

## Concurrency model

`exchangelib` is synchronous. Each tool's `execute()` is `async` for
MCP compatibility but the EWS calls are blocking inside. The MCP
server runs them on a thread pool via uvicorn / starlette's default
async machinery — this is fine because:

- A typical EWS call is 50-500 ms; 67 tools won't saturate.
- exchangelib's connection pool is per-account; concurrent calls to
  the same mailbox reuse HTTP keep-alive sessions.
- The SQLite cache's WAL journal mode allows concurrent readers + 1
  writer without lock contention.

Tools that issue many EWS calls in a loop (semantic_search's `.only()`
fetch of ~200 candidates, conversation walker, briefing) batch
explicitly via exchangelib's `account.fetch(ids=[…])` rather than
issuing per-item `GetItem` SOAP calls.

---

## Failure modes & resilience

### Transient SOAP failures

`ews_client.py` configures exchangelib's `FaultTolerance(max_wait=30)`
retry policy for in-call transients (load-balancer hiccups,
"No Body element in SOAP response" 5xx errors). The outer tenacity
retry on `_create_account` covers auth-time failures. Together they
absorb the rare per-call transients without surfacing them to the
caller.

### EWS GetItem missing fields

Most tools use `safe_get(item, "field", default)` which catches
`AttributeError` and `DoesNotExist` from exchangelib. EWS responses
sometimes omit fields the schema declares; the MCP returns sensible
defaults instead of 500-ing.

### Partial folder access

`search_by_conversation` walks every mail folder in the user's
mailbox by default. Folders that fail (`ErrorAccessDenied`,
permission misconfig, bad EWS shape) are added to a `skipped_folders`
array in the response with a classified `reason`, rather than failing
the whole call.

### Embedding provider down

`semantic_search_emails` raises `ToolExecutionError` with the upstream
provider's verbatim error message + a hint about
`AI_EMBEDDING_MODEL` mismatch. Tested provider error shapes: Ollama
"model not found", OpenAI 401/429, Anthropic invalid model.

### SQLite cache corrupt

If the per-mailbox `.sqlite` file is corrupt at startup, `SQLiteCache`
logs a warning and proceeds with an in-memory dict. Performance
degrades but the server keeps running. Recovery: stop the container,
remove the corrupt `.sqlite` file, restart — caches rebuild lazily
from the legacy JSON cache (if present) and from new EWS calls.

### Container exit

The `Dockerfile` runs the server as PID 1; the entrypoint shell
script handles `SIGTERM` cleanly so `docker stop` doesn't interrupt
in-flight EWS writes. Auto-restart via `docker run --restart
unless-stopped` covers crashes.

---

## Auth methods

`src/auth.py` builds an `exchangelib.Credentials` according to
`EWS_AUTH_TYPE`:

| Method | Production fit | Notes |
|---|---|---|
| `oauth2` | Office 365 production (recommended) | Requires Azure AD app with `EWS.AccessAsUser.All` (delegate) or full impersonation scope. Token refresh handled by `msal`. |
| `basic` | Internal Exchange or O365 with basic enabled | Username + password. Plan to migrate off; basic auth is being deprecated by Microsoft. |
| `ntlm` | Corporate Exchange behind ADFS | `DOMAIN\user` username. Uses `requests-ntlm`. |

OAuth2 client credentials flow is the future-proof path. Detailed
setup in [SETUP.md](SETUP.md) and [DEPLOYMENT.md](DEPLOYMENT.md).

---

## Security model

- **Credentials** are read from env at startup, held in memory, never
  logged. The audit log redacts every key matching
  `password|token|secret|api[_-]?key|bearer|client[_-]?secret`
  (case-insensitive, hyphen-aware).
- **Inbound tool args** are validated against pydantic models or
  explicit type checks. Unknown params return `ValidationError` (HTTP
  400 over SSE), not a 500.
- **Attacker-controlled email fields** (subject, sender display,
  body) are HTML-escaped before being interpolated into the
  reply/forward header block in `email_tools.py` — see the
  `escape_html` calls around `safe_from`/`safe_to`/`safe_subject`.
- **`download_attachment`** writes only into the `EWS_DOWNLOAD_DIR`
  jail. The `save_path` parameter is reduced to a basename hint and
  joined onto the jail dir; `..` and absolute paths are rejected.
- **SSE transport** refuses to bind a non-loopback `MCP_HOST` without
  `MCP_API_KEY` set. The bearer is verified on every request before
  the SSE event stream opens.
- **TLS to EWS** is verified by default. `EWS_INSECURE_SKIP_VERIFY=true`
  is opt-in only and logs a loud warning on every connect.

---

## v4 dependencies (pure-Python, MIT-licensed)

New in v4:

| Package | Purpose | Licence |
|---|---|---|
| `markdownify` | HTML → Markdown for read-direction | MIT |
| `markdown` | Markdown → HTML for write-direction | BSD |
| `extract-msg` | Outlook .msg compound-file reading | BSD |

Pre-existing (kept):

| Package | Purpose |
|---|---|
| `exchangelib` | EWS SOAP client |
| `pydantic` / `pydantic-settings` | input validation + env config |
| `mcp` | MCP protocol primitives |
| `starlette` / `uvicorn` | SSE/HTTP transport |
| `msal` | OAuth2 token refresh |
| `tenacity` | outer retry on `_create_account` |
| `httpx` | embedding provider HTTP |
| `pdfplumber` / `python-docx` / `openpyxl` / `python-pptx` | document text extraction |

No native dependencies, no compiled C extensions, no vector
database, no external service requirements (beyond Exchange itself
and an optional embedding endpoint for `semantic_search_emails`).

---

## Where to extend

If you want to add a new tool:

1. Create the tool class in the appropriate `src/tools/<area>_tools.py`.
2. Subclass `BaseTool`, implement `get_schema()` (returns the JSON
   Schema for the input) and `execute(**kwargs)` (returns a dict).
3. Add the import + class to the registration block in `src/main.py`
   under the right feature flag.
4. Re-export from `src/tools/__init__.py` if you want it in `from
   src.tools import *` consumers.
5. Document it in [`API.md`](API.md). The dynamic `tools/list` will
   pick it up automatically — you only need to write prose.

If you want to swap the cache backend (e.g. to Redis or pgvector):

1. Subclass / re-implement `SQLiteCache` in `src/cache/`.
2. Wire it in `EWSClient.sqlite_cache` lazy property.
3. The `EmbeddingService` only depends on `get_embedding` /
   `put_embedding` / `get_embeddings_bulk` / `count_embeddings`, so
   any backend implementing those four methods slots in.

If you want to add a new attachment extractor:

1. Add the `elif file_ext == 'xyz':` branch in
   `ReadAttachmentTool.execute` in `src/tools/attachment_tools.py`.
2. Implement `_read_xyz(content: bytes) -> str` on the same class.
3. Add the new file extension to the schema's `description` field
   and to the supported-formats table in [`API.md`](API.md) and the
   README.

---

## See also

- [README.md](../README.md) — product description + quick start
- [API.md](API.md) — per-tool reference
- [DEPLOYMENT.md](DEPLOYMENT.md) — production deployment
- [SETUP.md](SETUP.md) — first-time configuration
- [CONNECTION_GUIDE.md](CONNECTION_GUIDE.md) — MCP-client integration
- [REPLY_FORWARD.md](REPLY_FORWARD.md) — signature preservation
- [IMPERSONATION.md](IMPERSONATION.md) — multi-mailbox setup
- [AGENT_SECRETARY.md](AGENT_SECRETARY.md) — memory / commitments / approvals / rules
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — diagnostic playbook
- [COMMON_PITFALLS.md](COMMON_PITFALLS.md) — recurring foot-guns
- [CHANGELOG.md](../CHANGELOG.md) — version history
