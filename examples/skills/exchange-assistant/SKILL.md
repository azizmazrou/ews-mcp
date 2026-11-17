---
name: exchange-assistant
description: >
  Generic example of composing the ews-mcp 4.5 tool surface into a daily
  mailbox routine: morning overview, triage, and a reply drafted through
  the two-phase confirm flow. Use as a template — real assistants add
  their own judgment, tone and priorities on top of these calls.
---

# Exchange assistant (example skill)

This skill demonstrates the intended division of labor: the MCP server is
a fast, safe DATA PLANE; everything that needs judgment (what matters,
what to say, when to escalate) lives here, in the calling assistant.

## Morning overview

1. Call `get_mailbox_overview` once. It returns `unread_total`,
   `recent_unread` (up to ten unread message cards), and `today_events`
   (calendar events across `horizon_days`, default `1`) in a single
   compact payload (`source: "cache"` when the mirror is warm).
2. Summarize for the user in their language. Lead with meetings that
   start soon, then unread mail grouped by sender. Card ids (`m12`,
   `e3`) are stable handles — keep them for follow-up calls.
3. If the user tracks follow-ups, add `waiting_on` (sent threads with no
   reply after `days`, default `5`; requires a warm `sent` mirror) and
   `list_tasks` to the brief.

## Triage

- To act on a specific message: `get_message {id: "m12"}` (`format: "full"`,
  the default, returns the cleaned body with quoted history stripped; use
  `get_thread {id: "m12"}` when context from the conversation is needed).
- Searching: `search_messages` with either structured filters (`sender`,
  `subject`, `is_unread`, `since` / `until` — date arguments accept `today`,
  `+Nd`, `YYYY-MM-DD`, and an ISO date-time) or an AQS `query` — never
  combine `query` with structured filters in the same call. On the warm
  local mirror, Arabic queries match across spelling variants automatically.
- Bulk hygiene: `update_messages` (`ids` plus `set_read`, `categories_add`,
  `categories_remove`, or `flag_complete`) and `move_messages` (`ids`,
  `to_folder`) accept up to 50 ids per call.

## Replying (draft-first, always)

1. Compose the reply text yourself — tone and content are your job.
2. `create_draft {mode: "reply", reply_to: "m12", body: "..."}` (or
   `mode: "reply_all"`; `mode: "new"` requires `to` and `body`) — the
   server saves a draft and returns `draft_id` plus a `preview`. Nothing
   has been sent.
3. Show the preview to the user. Only after they approve:
4. `send_draft {draft_id: "d1"}` (requires `full` tier and
   `SEND_ENABLED=true`) — phase 1 returns the draft's REAL `to`, `cc`,
   `bcc`, `subject`, `body_snippet`, and `attachments` in `preview`, plus
   a `confirm_token`.
5. Re-check that preview (it is the server's ground truth, not your
   memory), then `send_draft {draft_id: "d1", confirm_token: "..."}`.
   If anyone edited the draft in between, the token is rejected with
   `confirm_invalid` and you must re-preview — that is the tamper
   protection working.

## Scheduling

- `list_events {start: "today", end: "+7d"}` for the week;
  `check_availability {attendees: [...], start: "today", end: "+7d"}`
  proposes concrete free slots for everyone (`duration_minutes` defaults
  to `30`, `working_hours_only` to `true`).
- `create_event {subject, start, end, ...}` with `send_invitations: false`
  (the default) saves the event without emailing anyone; flipping
  `send_invitations` to `true` requires `SEND_ENABLED=true` and triggers
  the same two-phase confirm as sending mail.

## Ground rules baked into the server (do not fight them)

- While `SEND_ENABLED=false` (the default), every call that sends mail or
  meeting notices (`send_draft`, `respond_to_event`, `set_oof`,
  `create_event` with `send_invitations: true`, `update_event` with
  `notify_attendees: true`, and `cancel_event` on a meeting with attendees)
  is refused at phase 1 with `kill_switch` — draft or save without
  notifying instead.
- Tools above the server's `EWS_CAPABILITY_TIER` are not registered: they
  are absent from the tool list, and calling one returns `validation`
  (`Unknown tool: <name>`) — tell the user this deployment's tier does not
  expose that action, and do not retry.
- A `not_found` (or `validation` on an unknown alias) saying the id is
  stale means items moved or came from a previous store; re-run the
  search or list tool and use the fresh id.
- Errors return `{code, message}` plus optional `hint` and `retry_after_s`
  when actionable (on `rate_capped`, `confirm_token` stays unspent until it
  expires) — read `message` and `hint` before retrying.
