# Agent Secretary Guide

The EWS MCP Server ships a set of **agentic** tools that let an LLM act as
an executive secretary: remember things across sessions, track
commitments, hold side-effects for human approval, apply rules, keep a
policy for when the user is out of office, and produce deterministic
briefings and meeting prep.

This guide walks through the 24 new tools, the data model they share, and
the security boundaries that keep them safe for production.

- [Architecture](#architecture)
- [Memory primitives](#memory-primitives)
- [Commitments](#commitments)
- [Approval queue (human-in-the-loop)](#approval-queue-human-in-the-loop)
- [Voice profile](#voice-profile)
- [Rule engine](#rule-engine)
- [OOF policy](#oof-policy)
- [Briefing](#briefing)
- [Meeting prep](#meeting-prep)
- [Security model](#security-model)
- [Configuration](#configuration)
- [FAQ](#faq)

## Architecture

The agent features share a single foundation: a **per-mailbox SQLite
memory store** (`src/memory/store.py`). Every new tool talks to this store
via typed repositories (`src/memory/models.py`) — no tool reaches into
SQLite directly, so the data model is centrally described and validated.

```
┌─────────────────────────────────────────────────────────────┐
│ MCP client  (Claude Desktop / Open WebUI / custom)          │
└──────────────┬──────────────────────────────────────────────┘
               │  stdio | SSE (bearer auth)
┌──────────────▼──────────────────────────────────────────────┐
│  Agent-secretary tools                                      │
│                                                             │
│  memory_*              commitments_*      approvals_*       │
│  rule_*                voice_*            oof_policy_*      │
│  generate_briefing    prepare_meeting                      │
│                                                             │
│           ▼ typed repositories (src/memory/models.py) ▼     │
│                                                             │
│  CommitmentRepo  ApprovalRepo  RuleRepo  VoiceRepo          │
│  OOFPolicyRepo                                              │
│                                                             │
│           ▼           MemoryStore  (SQLite, WAL)            │
│                                                             │
│  data/memory/mailbox-<sha256_prefix>.sqlite3                │
│  (owner-only, 1 MiB value cap, 50 MiB namespace cap)        │
└─────────────────────────────────────────────────────────────┘
```

Every record is keyed by the *primary authenticated* mailbox — impersonated
target mailboxes do **not** get their own state (by design: the operator
is the primary account, and per-target state would either leak across
service accounts or balloon into a per-target-mailbox DB per call).

## Memory primitives

Four MCP tools expose the KV directly for ad-hoc agent state (use the
typed tools below for structured data).

| Tool | Purpose |
|------|---------|
| `memory_set` | Store a JSON value under `(namespace, key)`, optional TTL |
| `memory_get` | Fetch by namespace + key |
| `memory_list` | List entries in a namespace, optional key prefix |
| `memory_delete` | Remove an entry |

Reserved namespaces — `approval`, `rule`, `oof.policy` — are refused from
the generic tools so the repositories remain the only writers.

### Example

```jsonc
// The agent remembers how a colleague likes meetings.
memory_set {
  "namespace": "person.note",
  "key": "alice.al-rashid",
  "value": {
    "prefers": "morning meetings",
    "lang": "Arabic",
    "allergy": "shellfish"
  }
}

// Later, when drafting an invite:
memory_get { "namespace": "person.note", "key": "alice.al-rashid" }
```

## Commitments

A commitment is a tracked promise — "I owe X to Y by Z", or "Y owes me
X". Typical flow:

1. The agent (or user) creates a commitment with `track_commitment`.
2. `list_commitments` surfaces what's open, overdue, or done.
3. `resolve_commitment` closes one out when complete.
4. `extract_commitments` uses the configured AI provider to pull
   candidate commitments out of an email thread.

Stored fields include the message/thread ID, the counterparty, a due
timestamp, a short excerpt, and a `source` flag (`manual` | `extracted`)
so AI-generated items are distinguishable.

### Example

```jsonc
track_commitment {
  "description": "Send Alice the Q1 budget numbers",
  "owner": "me",
  "counterparty": "alice@corp.com",
  "due_at": "2026-04-25T17:00:00Z"
}

list_commitments { "scope": "overdue" }
resolve_commitment {
  "commitment_id": "…", "outcome": "done", "note": "sent via email"
}

extract_commitments {
  "message_id": "AAMkAGI…", "save": true, "max_extractions": 5
}
```

## Approval queue (human-in-the-loop)

For side-effectful actions the agent **submits instead of executes**:

1. `submit_for_approval` queues an action + arguments, returns an
   approval id.
2. A human sees it via `list_pending_approvals`.
3. `approve` or `reject` decides.
4. `execute_approved_action` atomically consumes the approval and runs
   the underlying tool.

Approval IDs are UUID4 and single-use — the `MemoryStore.consume`
primitive guarantees no double-spend even under concurrent calls.

Allow-listed actions (see `src/memory/models.py:ApprovalRepo`):

```
send_email, reply_email, forward_email, delete_email, move_email,
create_appointment, update_appointment, delete_appointment,
create_contact, update_contact, delete_contact,
create_task, update_task, delete_task, complete_task,
manage_folder, add_attachment, delete_attachment,
oof_settings, configure_oof_policy
```

### Example

```jsonc
submit_for_approval {
  "action": "send_email",
  "arguments": {
    "to": ["ceo@corp.com"],
    "subject": "Re: quarterly outlook",
    "body": "<p>Attached is the revised deck.</p>"
  },
  "ttl_seconds": 1800,
  "reason": "AI drafted reply — needs review before send"
}
// → { approval_id: "8a…" }

approve { "approval_id": "8a…", "reason": "LGTM" }

execute_approved_action { "approval_id": "8a…" }
// → actually sends the mail; approval record consumed.
```

### Dry-run flag on `send_email`

`send_email` accepts `dry_run: true`; it validates, builds the
`Message` object, and returns a preview without calling
`message.send()`. Useful for AI agents that want "what would this send"
before committing. The Drafts folder is *not* touched either — that's
what `create_draft` is for.

## Voice profile

`build_voice_profile` samples up to 200 messages from the user's Sent
folder, strips quoted replies/signatures, and asks the AI provider to
emit a JSON style card (formality, typical greetings, typical sign-offs,
structure notes, 3 short examples).

The card is stored once under `NS.VOICE="voice.profile"` / key
`"current"`. `get_voice_profile` retrieves it; draft-generation tools
can feed it into prompts as few-shot material to produce text that
"sounds like you".

Safety:

- Samples are capped at 200 messages × 1500 chars, prompt at ~30 KB.
- The AI is asked to **redact PII** (names, emails, phone numbers) from
  the stored examples.
- Only the primary mailbox's Sent folder is read. No impersonation.

### Example

```jsonc
build_voice_profile { "sample_count": 100, "min_words": 20 }
// → profile stored; returns formality, greetings, signoffs, 3 examples

get_voice_profile
```

## Rule engine

Declarative automations: "when X, do Y". Rules have:

- **Match**: allow-listed keys — `from`, `to`, `subject_contains`,
  `body_contains`, `has_attachment`, `is_unread`, `categories_any`,
  `categories_all`, `importance`. `from`/`to` support `fnmatch` wildcards.
- **Actions**: allow-listed types — `flag_importance`, `categorize`,
  `move_to_folder`, `mark_read`, `track_commitment`, `notify_agent`.

There is **no background watcher** today — rules fire when the agent
invokes `evaluate_rules_on_message` on a specific message. `rule_simulate`
reports which rules *would* fire, without mutating anything.

### Example

```jsonc
rule_create {
  "name": "CEO urgent handler",
  "match": {
    "from": "ceo@corp.com",
    "subject_contains": "urgent",
    "is_unread": true
  },
  "actions": [
    { "type": "flag_importance", "importance": "High" },
    { "type": "categorize", "categories": ["CEO", "Action"] },
    { "type": "notify_agent", "note": "Draft acknowledgement reply" },
    {
      "type": "track_commitment",
      "description": "Acknowledge CEO urgent message",
      "owner": "me"
    }
  ]
}

rule_simulate { "message_id": "AAMkAGI…" }
evaluate_rules_on_message { "message_id": "AAMkAGI…", "dry_run": false }
```

## OOF policy

`configure_oof_policy` stores a policy alongside the native Exchange OOF:
internal / external reply templates, a `vip_passthrough` flag, and
**forward rules** — `{match, to, reason}` triples that reuse the rule
engine's match vocabulary.

`apply_oof_policy` evaluates the forward rules against one message. When a
match fires and `dry_run=false`, it creates a **draft** forward (never
sends). The user reviews drafts on return.

### Example

```jsonc
configure_oof_policy {
  "internal_template": "I'm out until Monday. For urgent items contact Alice.",
  "external_template": "Thanks for your note — I'm out of office until Monday.",
  "vip_passthrough": true,
  "forward_rules": [
    { "match": { "from": "*@bigcustomer.com" }, "to": "alice@corp.com", "reason": "Key account" },
    { "match": { "categories_any": ["Finance"] }, "to": "finance-ops@corp.com" }
  ]
}

apply_oof_policy { "message_id": "AAMkAGI…", "dry_run": true }
```

## Briefing

`generate_briefing` composes a single JSON payload the LLM renders for the
user. It is **compound** — the tool calls several primitives and returns
the aggregate. Sections (each independently toggle-able via `include`):

| Section | Source |
|---------|--------|
| `inbox_delta` | unread messages since `since` (or "today") |
| `meetings` | calendar view for the briefing window |
| `commitments` | open commitments due in the window + overdue |
| `overdue_tasks` | Exchange tasks with `due_date < today` |
| `vip_activity` | messages from frequent recent senders |

Fully deterministic; no AI call. Truncates preview fields to 200 chars so
payloads stay small.

### Example

```jsonc
generate_briefing { "scope": "today" }
generate_briefing { "scope": "weekly", "include": ["meetings", "commitments"] }
```

## Meeting prep

`prepare_meeting` takes an appointment ID and returns a brief:

- Meeting metadata (subject, start/end, location, organizer, attendees)
- Per attendee — stored `person.note` + last emails with them (in both
  directions) + open commitments involving them
- Related thread — a recent subject-match search
- Attachments on the invite — name + size; optionally extracted text
  snippet for PDF/DOCX/XLSX when `extract_attachment_text=true`

### Example

```jsonc
prepare_meeting {
  "appointment_id": "AAMkAGI…",
  "depth": "deep",
  "history_per_attendee": 10,
  "extract_attachment_text": true
}
```

## Security model

Every design choice in the agent stack maps to a threat:

| Threat | Mitigation |
|--------|------------|
| Cross-mailbox data leak | Per-mailbox DB file; hash-derived filename; primary-key includes mailbox |
| SQL injection | All queries use `?` placeholders only |
| Path traversal in DB filename | File path resolved and verified inside `EWS_MEMORY_DIR` |
| Path traversal in namespace/key | Alphabet restricted to `[A-Za-z0-9._:-]{1,128}` |
| Unbounded value growth | 1 MiB per value, 50 MiB per namespace (LRU prune) |
| Approval double-spend | `MemoryStore.consume` is atomic and re-checks status |
| Arbitrary code in rule actions | Explicit allow-list in `_ALLOWED_ACTION_TYPES` — no `eval` |
| Arbitrary match keys (SQL bait) | `RuleRepo.validate_match` allow-list |
| Sensitive fields in audit log | Reuses `redact_sensitive` from logging middleware |
| AI cost explosion | Voice: 200 samples × ~1.5 KB; extraction: ~6 KB body cap |
| Forward-rule spoofing | Forwards create DRAFTS (never send); basic email regex on destination |

Tools inherit the existing middleware: rate limit, circuit breaker,
structured audit log, and `json` serialisation over the MCP transport.
See [Security & Ops](../README.md#security--operations-notes) for the
transport-level defaults (bearer auth, TLS verification, jailed
downloads).

## Configuration

All agent features are on by default; toggle with `ENABLE_AGENT=false` to
exclude the 24 new tools from the tool registry entirely.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENABLE_AGENT` | `true` | Register the agent-secretary tools |
| `EWS_MEMORY_DIR` | `data/memory` | Jail for per-mailbox SQLite files |
| `ENABLE_AI` | `false` | Required for `extract_commitments` and `build_voice_profile` |
| `AI_PROVIDER` | `openai` | `openai` / `anthropic` / `local` |

## FAQ

**Q: Can the agent act on my mailbox without me seeing it?**
By default the rule engine only fires when you explicitly call
`evaluate_rules_on_message`. There is no background watcher yet, so
auto-actions require a prompt or a scheduled job outside ews-mcp. All
side-effectful tools go through the audit log.

**Q: Where are my secrets / API keys / tokens?**
Not in the memory store. `redact_sensitive` in the logging middleware
replaces `password`/`token`/`secret`/`body`/`file_content` fields with
placeholders before they reach the audit log. Keys live in env vars only.

**Q: Can I wipe state for a single mailbox?**
Delete `data/memory/mailbox-<prefix>.sqlite3` (the file name is a
SHA-256 prefix of the mailbox, not the email itself). There's no
cross-mailbox data in a single file.

**Q: Is this Exchange Online only?**
No — the memory and rule layers are Exchange-agnostic. They talk to
`exchangelib`'s account object, which handles both on-prem EWS and
Exchange Online.

**Q: How do I call an approved action twice?**
You don't. `consume()` is atomic and `execute_approved_action` rejects
any redemption of a consumed approval. To retry after a transient error,
submit a fresh approval.
