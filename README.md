# EWS MCP Server

**A Model Context Protocol server that lets an LLM agent run a real Microsoft Exchange / Office 365 mailbox.**

Plug it into Claude Desktop, Open WebUI, your in-house agent, or any
MCP-aware client, and the LLM gets 67 typed tools for email, calendar,
contacts, tasks, folders, attachments, threads, and follow-up flags —
plus a small set of agent-side primitives (memory KV, commitments,
approvals, declarative rules, voice profile, OOF policy, briefing,
meeting-prep) for building autonomous workflows.

The server speaks Exchange Web Services natively (no Graph proxy) and
ships as a multi-arch container image (`linux/amd64` + `linux/arm64`)
on GitHub Container Registry. Pull and run:

```bash
docker pull ghcr.io/azizmazrou/ews-mcp:latest
docker run -d --name ews-mcp --env-file .env --network host \
  ghcr.io/azizmazrou/ews-mcp:latest
```

---

## What it does well

### Bidirectional Markdown ⇄ HTML body format (~12× cheaper LLM I/O)

Outlook bodies are MSO HTML — typically 5–10× more bytes than the
information they carry. Reading and writing email through this MCP, the
LLM can opt into Markdown on either side:

| Direction | Tools | Parameter | Effect |
|---|---|---|---|
| Read | `get_email_details` | `format=html\|markdown\|text` (default `html`) | The MCP runs the Outlook MSO body through `markdownify` server-side, caches the result in SQLite, and returns clean GFM markdown. ~12× fewer tokens for the same content. |
| Read | `get_email_details` | `trim_quoted=true` | Strips the `On …, X wrote:` history glued to the bottom of every reply. Massive savings on long threads. |
| Read | `get_email_details` | `include_body=false` | Drop the body entirely for envelope-only / list-style calls. |
| Write | `send_email`, `reply_email`, `forward_email` | `body_format=html\|markdown\|text` (default `html`) | The LLM produces clean markdown; the MCP converts to HTML before EWS. The user's Outlook signature with its inline `cid:` image refs is preserved end-to-end (`inline_attachments_preserved` returned in the response). |

Concrete numbers on a representative 25 KB Outlook reply:

```
HTML  (default):  25,446 bytes  →  ~5,500 tokens   (baseline)
markdown:          3,799 bytes  →    ~450 tokens   (~12× cheaper)
text:              ~3,200 bytes →    ~330 tokens   (lossy — drops links)
```

Defaults are unchanged from the v3.x line, so existing callers don't
break — opt in to `markdown` when you want the saving.

### Document text extraction for everything an email actually carries

`read_attachment` returns ready-to-reason text from binary attachments,
so the LLM never has to download and parse an Excel file itself:

| Format | Library | What you get |
|---|---|---|
| PDF | `pdfplumber` | text + tables, page-bounded |
| DOCX | `python-docx` | text + optional tables |
| XLSX / XLS | `openpyxl` / `xlrd` | sheet-by-sheet rows |
| PPTX | `python-pptx` | slide-by-slide text + speaker notes + embedded tables |
| **MSG** | `extract-msg` | Outlook compound-file: subject / from / to / cc / date envelope **plus** the body (HTML→markdown via the same pipeline as `get_email_details`) **plus** a recursive nested-attachment listing. Ideal for forwarded email threads. |
| EML | `email` (stdlib) | RFC-822: same shape as `.msg`, used by non-Outlook exporters |
| HTML / HTM | `markdownify` | GFM markdown, RTL-safe (Arabic / Hebrew) |
| CSV / LOG / JSON / XML / MD | stdlib | BOM-aware UTF-8 / UTF-16 decode |
| TXT | stdlib | UTF-8 / UTF-16, BOM-aware |

### Reliable EWS write path with signature + thread preservation

`reply_email`, `forward_email`, `create_reply_draft`, and
`create_forward_draft` build a fresh Outlook-compatible HTML body
manually instead of calling EWS `create_reply()` / `create_forward()`,
which have unpredictable signature placement under Exclaimer. The
result:

- Original conversation thread preserved with the standard Outlook
  border-top separator and `From: / Sent: / To: / Cc: / Subject:`
  header block
- Inline images from the original message (signature graphics, embedded
  logos, screenshots inside the thread) copied to the new message
  before send / save
- Threading headers (`In-Reply-To` / `References`) set so the reply
  stays in the same Outlook conversation

Same path is used whether `body_format` is HTML, markdown, or text —
markdown gets converted upstream of the wrap; the wrap is preserved.

### Multi-mailbox impersonation

Every base tool accepts `target_mailbox=<smtp>` when
`EWS_IMPERSONATION_ENABLED=true`, so a single service account can read
the CEO's calendar, send on behalf of `support@`, search across
multiple shared mailboxes, etc. Per-target accounts are cached.

### Search that scales with the mailbox

`search_emails` has three modes: `quick` (one or two filters,
direct EWS), `advanced` (every filter combinable with sort / scope), and
`full_text` (Exchange's `searchquery` engine for prose). All three honor
a unified parameter vocabulary including `is_read`, `is_flagged`,
`importance`, `categories`, `from_address`, `body_contains`,
`has_attachments`, `start_date`, `end_date`, paginated via
`offset` / `next_offset`.

`search_by_conversation` walks every mail folder in the mailbox by
default so archived / custom-label messages don't disappear, and
classifies skipped folders with reasons (`permission_denied`, etc.).

`semantic_search_emails` runs vector cosine over a SQLite-backed
embedding cache. No external vector database needed; the cache lives
next to the per-mailbox data file. Embeddings are computed on the
configured AI provider (OpenAI / Anthropic / OpenAI-compatible local
endpoint such as Ollama, LM Studio, llama.cpp).

### A clean MCP / skill boundary

The MCP does **deterministic data work** — fetch, transform, embed,
extract, persist. The consuming skill / agent does the **reasoning** —
classify, summarise, decide, compose. So the AI surface on the server
is intentionally small:

- `semantic_search_emails` (vector math + cache, can't be done from
  the skill efficiently)
- `read_attachment` text extraction (binary parsing belongs server-side)
- `compose_body` / `render_body` (deterministic transformations)

LLM-reasoning tools that just round-tripped to a model — `classify_email`,
`summarize_email`, `suggest_replies`, `extract_commitments`,
`build_voice_profile` — are intentionally **not** exposed. The skill
already has an LLM running, and doing those tasks in-prompt with the
data this MCP returns is faster, cheaper, and produces better results
than a second LLM hop.

This means `AI_MODEL` (chat) is **optional** in the env — only the
embedding model is required, and only when semantic search is enabled.

### Per-mailbox SQLite cache

A single SQLite file at `data/ews_mcp_<mailbox>.sqlite` holds three
tables, all populated lazily:

- `body_format_cache` — converted markdown bodies. Exchange messages
  are immutable post-send, so we never invalidate.
- `attachment_text_cache` — extracted PDF / DOCX / XLSX / PPTX / MSG /
  EML / HTML / CSV text, keyed by attachment ID.
- `embedding_cache` — packed `float32` vectors for semantic search,
  keyed by `(text_hash, model)`.

No vector database to stand up. Backups are a single-file copy.

---

## Quick start

### 1 — Get the image

The published multi-arch image lives at
`ghcr.io/azizmazrou/ews-mcp` and supports both `linux/amd64`
(Intel / AMD) and `linux/arm64` (Apple Silicon, Raspberry Pi 4/5,
Ampere). Choose a tag:

| Tag | Use this if you want… |
|---|---|
| `latest` | Always-current build of `main` |
| `4.0.0` (or any `v*.*.*` tag) | A specific semver release |
| `4.0` | The latest patch on a minor line |
| `sha-<7chars>` | An exact commit |

```bash
docker pull ghcr.io/azizmazrou/ews-mcp:latest
```

GHCR images are anonymously pullable — no `docker login` required for
public images.

### 2 — Configure

```bash
cat > .env <<'EOF'
EWS_SERVER_URL=outlook.office365.com
EWS_EMAIL=user@company.com
TIMEZONE=UTC

# Auth — pick ONE of the three blocks below
# OAuth2 (Office 365)
EWS_AUTH_TYPE=oauth2
EWS_CLIENT_ID=00000000-0000-0000-0000-000000000000
EWS_CLIENT_SECRET=...
EWS_TENANT_ID=00000000-0000-0000-0000-000000000000

# OR Basic
# EWS_AUTH_TYPE=basic
# EWS_USERNAME=user@company.com
# EWS_PASSWORD=...

# OR NTLM (corporate Exchange behind ADFS)
# EWS_AUTH_TYPE=ntlm
# EWS_USERNAME=DOMAIN\\user
# EWS_PASSWORD=...

# Optional — only needed for semantic_search_emails
ENABLE_AI=true
AI_PROVIDER=local            # or openai, anthropic
AI_BASE_URL=http://ollama:11434/v1
AI_EMBEDDING_MODEL=nomic-embed-text

# Optional — SSE transport for remote MCP clients
# MCP_TRANSPORT=sse
# MCP_HOST=0.0.0.0
# MCP_API_KEY=$(openssl rand -hex 32)
EOF
```

### 3 — Run

```bash
docker run -d \
  --name ews-mcp \
  --restart unless-stopped \
  --env-file .env \
  --network host \
  -v ./data:/app/data \
  -v ./logs:/app/logs \
  ghcr.io/azizmazrou/ews-mcp:latest
```

The two volume mounts are recommended so the per-mailbox SQLite cache
(body / attachment / embedding) and structured logs survive container
recreation. Default transport is stdio (Claude Desktop / Claude Code /
any MCP-stdio client). For SSE / HTTP, set `MCP_TRANSPORT=sse` and
bind a non-loopback `MCP_HOST` only with `MCP_API_KEY` set — the
server refuses to listen on `0.0.0.0` without an API key.

### Claude Desktop config

```json
{
  "mcpServers": {
    "ews": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "--env-file", "/path/to/.env",
        "ghcr.io/azizmazrou/ews-mcp:latest"
      ]
    }
  }
}
```

`%APPDATA%\Claude\claude_desktop_config.json` on Windows;
`~/Library/Application Support/Claude/claude_desktop_config.json` on
macOS; `~/.config/Claude/claude_desktop_config.json` on Linux.

### docker-compose

A reference `docker-compose.yml` ships in the repo. After cloning
(see "From source" below) you can:

```bash
cp .env.example .env  # edit credentials
docker compose up -d
```

### From source (development / forks)

If you want to modify the code, fork-and-PR, or run a custom build:

```bash
git clone https://github.com/azizmazrou/ews-mcp.git
cd ews-mcp
pip install -r requirements.txt
cp .env.example .env
python -m src.main
```

Or container-build:

```bash
docker build -t ews-mcp:dev .
docker run -d --name ews-mcp --env-file .env --network host ews-mcp:dev
```

### Releasing your own fork

The repo's CI (`.github/workflows/docker-publish.yml`) publishes a
multi-arch image to GHCR on every push to `main` and on every
`v*.*.*` tag. To cut a release on a fork:

```bash
# 1. Make sure your fork has GHCR write permissions enabled in
#    Settings → Actions → General → Workflow permissions:
#    "Read and write permissions"
# 2. Tag and push
git tag v4.0.1
git push origin v4.0.1
```

The workflow runs, builds for `amd64` + `arm64`, and publishes
`ghcr.io/<your-fork>/ews-mcp:4.0.1`, `:4.0`, `:4`, `:sha-<…>`, and
updates `:latest` if the tag is on the default branch.

---

## Tool surface — 67 tools

| Category | Tools |
|---|---|
| **Email (14)** | `send_email`, `read_emails`, `search_emails` (quick / advanced / full_text), `get_email_details`, `get_emails_bulk`, `delete_email`, `move_email`, `copy_email`, `update_email`, `reply_email`, `forward_email`, `create_draft`, `create_reply_draft`, `create_forward_draft` |
| **Attachments (7)** | `list_attachments`, `download_attachment`, `add_attachment`, `delete_attachment`, `read_attachment` (PDF / DOCX / XLSX / PPTX / MSG / EML / HTML / CSV / TXT / LOG / JSON / XML / MD), `get_email_mime`, `attach_email_to_draft` |
| **Calendar (7)** | `create_appointment`, `get_calendar`, `update_appointment`, `delete_appointment`, `respond_to_meeting`, `check_availability`, `find_meeting_times` |
| **Contacts (5)** | `create_contact`, `update_contact`, `delete_contact`, `find_person` (GAL + contacts + email-history), `analyze_contacts` |
| **Tasks (5)** | `create_task`, `get_tasks`, `update_task`, `complete_task`, `delete_task` |
| **Search (1)** | `search_by_conversation` |
| **Folders (3)** | `list_folders`, `find_folder`, `manage_folder` (create / delete / rename / move) |
| **Out-of-Office (4)** | `oof_settings`, `configure_oof_policy`, `get_oof_policy`, `apply_oof_policy` |
| **AI (1, optional)** | `semantic_search_emails` |
| **Memory KV (4)** | `memory_set`, `memory_get`, `memory_list`, `memory_delete` |
| **Commitments (3)** | `track_commitment`, `list_commitments`, `resolve_commitment` |
| **Approvals (5)** | `submit_for_approval`, `list_pending_approvals`, `approve`, `reject`, `execute_approved_action` |
| **Voice profile (1)** | `get_voice_profile` |
| **Rules (5)** | `rule_create`, `rule_list`, `rule_delete`, `rule_simulate`, `evaluate_rules_on_message` |
| **Compound (2)** | `generate_briefing`, `prepare_meeting` (return structured context — composition runs on the skill side) |

Full per-tool input / output schemas in [`docs/API.md`](docs/API.md).

---

## Usage examples

### Read an email cheaply

```python
get_email_details(
    message_id="AAMk...",
    format="markdown",         # 12× fewer tokens than HTML
    trim_quoted=True,          # drop the "On X wrote:" history
)
```

### Compose a reply in markdown — signature preserved

```python
reply_email(
    message_id="AAMk...",
    body_format="markdown",
    body="""
    Approved on the Q1 budget. Two changes:

    - Move line 4 (cloud spend) up by 8%
    - Defer the contractor budget to Q2

    See attached for the revised numbers.
    """.strip(),
    attachments=["/path/to/q1-budget-rev2.xlsx"],
)
```

The MCP converts the markdown to HTML, hands it to EWS, and the
existing reply pipeline preserves the user's Outlook signature with its
inline images at the bottom of the rendered email.

### Read an entire forwarded thread that came as a `.msg` attachment

```python
read_attachment(
    message_id="AAMk...",
    attachment_name="Re_ Q1 Performance Review.msg",
)
```

Returns the envelope (subject / from / to / cc / date) plus the body
converted to markdown plus a listing of any nested attachments inside
the `.msg`. No manual download / parse step required.

### Find a person across every signal

```python
find_person(
    query="Ahmed",
    source="all",              # GAL + Contacts + email history
    include_stats=True,
)
```

### Free-busy across multiple attendees

```python
find_meeting_times(
    attendees=["alice@company.com", "bob@company.com"],
    duration_minutes=60,
    date_range_start="2026-04-20",
    date_range_end="2026-04-22",
    max_suggestions=3,
)
```

### Reply draft for human review before send

```python
draft = create_reply_draft(
    message_id="AAMk...",
    body="<p>Will review by Friday.</p>",
    cc=["manager@company.com"],
    categories=["Follow-up"],
)
# Open in OWA / Outlook, edit if needed, send manually.
```

### Filter on Outlook follow-up flag

```python
search_emails(is_flagged=True, max_results=20)
```

### Operate on a shared mailbox

```python
read_emails(folder="inbox", target_mailbox="support@company.com")
```

(requires `EWS_IMPERSONATION_ENABLED=true` and an account with
delegate / impersonation rights on the target)

---

## Configuration reference

All settings are pydantic `Settings` parsed from env (or `.env`).
See `.env.example`, `.env.basic.example`, `.env.oauth2.example`,
`.env.ai.example`.

### Required

| Variable | Description |
|----------|-------------|
| `EWS_EMAIL` | Primary mailbox SMTP address |
| `EWS_AUTH_TYPE` | `oauth2`, `basic`, or `ntlm` |

### Connection

| Variable | Default | Description |
|----------|---------|-------------|
| `EWS_SERVER_URL` | autodiscover | Explicit EWS endpoint (skip autodiscover) |
| `EWS_AUTODISCOVER` | `true` | |
| `EWS_INSECURE_SKIP_VERIFY` | `false` | TLS verification off — only for internal CA setups |
| `EWS_DOWNLOAD_DIR` | `downloads` | Jail dir for `download_attachment` |

### Auth — OAuth2

| Variable | Description |
|----------|-------------|
| `EWS_CLIENT_ID` | Azure AD app client ID |
| `EWS_CLIENT_SECRET` | Azure AD app client secret |
| `EWS_TENANT_ID` | Azure AD tenant ID |

### Auth — Basic / NTLM

| Variable | Description |
|----------|-------------|
| `EWS_USERNAME` | SMTP address (basic) or `DOMAIN\user` (NTLM) |
| `EWS_PASSWORD` | |

### Multi-mailbox

| Variable | Default | Description |
|----------|---------|-------------|
| `EWS_IMPERSONATION_ENABLED` | `false` | Enable `target_mailbox=` parameter on every tool |

### AI (optional)

| Variable | Description |
|----------|-------------|
| `ENABLE_AI` | Master switch for the AI category |
| `ENABLE_SEMANTIC_SEARCH` | Enable `semantic_search_emails` |
| `AI_PROVIDER` | `openai`, `anthropic`, or `local` |
| `AI_BASE_URL` | Endpoint (required for `local`) |
| `AI_EMBEDDING_MODEL` | e.g. `nomic-embed-text`, `text-embedding-3-small` |
| `AI_API_KEY` | Required for non-`local` providers |

`AI_MODEL` (chat) is optional — only `AI_EMBEDDING_MODEL` is needed for
the AI tool that actually ships on this MCP.

### Transport

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `sse` |
| `MCP_HOST` | `127.0.0.1` | SSE bind address — non-loopback requires `MCP_API_KEY` |
| `MCP_PORT` | `8000` | SSE port |
| `MCP_API_KEY` | — | Bearer token for SSE clients |

---

## Architecture

```
                ┌──────────────────────┐
                │   MCP client (LLM)   │   stdio or SSE
                └──────────┬───────────┘
                           │  JSON-RPC over MCP
                ┌──────────▼───────────┐
                │   EWS MCP Server     │   ──→  src/tools/* (67 tool classes)
                │                      │   ──→  src/body_format.py  (HTML ⇄ Markdown)
                │                      │   ──→  src/cache/sqlite_cache.py
                └──────────┬───────────┘            ▲ │
                           │                        │ │
                           │  exchangelib SOAP      │ │  per-mailbox
                ┌──────────▼───────────┐            │ │  ews_mcp_<email>.sqlite
                │  Microsoft Exchange  │            │ │  (3 tables)
                │  / Office 365        │            │ │
                └──────────────────────┘            │ │
                                                    │ │
                Optional AI provider for embeddings │ │
                  (OpenAI / Anthropic / Ollama / ──┘ │
                   LM Studio / llama.cpp / ...)      │
                                                     │
                Read/write the cache:                │
                  body_format_cache (markdown ----->─┘
                  attachment_text_cache             ─┘
                  embedding_cache                   ─┘
```

Detailed component design in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Documentation

- **[API.md](docs/API.md)** — every tool with input / output schemas + examples
- **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** — component design + data flow
- **[DEPLOYMENT.md](docs/DEPLOYMENT.md)** — production deployment patterns
- **[REPLY_FORWARD.md](docs/REPLY_FORWARD.md)** — signature preservation deep-dive
- **[IMPERSONATION.md](docs/IMPERSONATION.md)** — multi-mailbox / delegate access
- **[AGENT_SECRETARY.md](docs/AGENT_SECRETARY.md)** — memory / commitments / approvals / rules / voice / OOF policy / briefing
- **[CONNECTION_GUIDE.md](docs/CONNECTION_GUIDE.md)** — Claude Desktop and other MCP-client setup
- **[SETUP.md](docs/SETUP.md)** — getting started in detail
- **[TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — diagnostic playbook
- **[COMMON_PITFALLS.md](docs/COMMON_PITFALLS.md)** — recurring foot-guns
- **[CHANGELOG.md](CHANGELOG.md)** — version history

---

## License

MIT — see [LICENSE](LICENSE).
