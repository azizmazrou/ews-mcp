# Security policy

This page lists which server lines are supported, how to report a vulnerability without exposing
details in public, and what falls within the server's security model.

## Supported versions

Two server lines live in this repository. New deployments should run the 4.5 line.

| Line | Directory | Container tags | Support status |
|---|---|---|---|
| **4.5** | [`v5/`](v5/) | `ghcr.io/azizmazrou/ews-mcp:v4.5*` | Supported; fixes land on `main` and the latest `v4.5.*` release |
| 4.0 (legacy) | [`src/`](src/) | `ghcr.io/azizmazrou/ews-mcp:latest` and the version tags of the legacy line | Maintenance only |
| 3.x and older | — | Older version tags of the legacy line | Not supported; see [`v5/docs/UPGRADING.md`](v5/docs/UPGRADING.md) |

## Reporting a vulnerability

Do not post vulnerability details, proof-of-concept payloads, credentials, internal hostnames, or
mailbox data in a public issue or pull request.

Private vulnerability reporting on GitHub is not enabled today, though it may be enabled later.
Check the repository's **Security** tab first:

1. If the **Security** tab offers **Report a vulnerability**, submit your report there.
2. Otherwise, open a public issue stating only that you have a security report to share, with no
   technical details, component names, or reproduction steps. A private channel will be arranged.

Once you have a private channel, include:

- Which line is affected: `4.5` under `v5/` or legacy `4.0` under `src/`.
- The release tag or commit hash, transport mode `stdio` or `http`, and relevant safety settings
  such as `EWS_CAPABILITY_TIER` and `SEND_ENABLED`.
- Minimal reproduction steps using `example.com` addresses and synthetic inputs.

## What to expect

- The maintainer will reply to confirm receipt or arrange a private channel.
- Valid reports are reproduced and fixed by the maintainer on `main`, following the workflow in
  [`CONTRIBUTING.md`](CONTRIBUTING.md), and shipped in a tagged release.
- Public details are shared only after a fix is published, with credit to the reporter on request.

## Scope

A security vulnerability is any defect that lets the server send, delete, leak, or run something
that its configured policy or boundaries should prevent. In-scope findings include:

- Bypassing `SEND_ENABLED=false`, `EWS_CAPABILITY_TIER`, `EWS_RECIPIENT_ALLOWLIST`,
  `EWS_RECIPIENT_DENYLIST`, `EWS_MAX_SENDS_PER_HOUR`, or two-phase `confirm_token` content binding
  on drafts, calendar events, or inbox rules.
- Creating or editing an inbox rule so that it sends, forwards, redirects, or deletes mail, or
  silently overwrites a rule marked `editable: false`.
- Path traversal or file access outside `DATA_DIR/attachments` and `EWS_ATTACHMENT_DIR`, or
  bypassing the cloud-synced folder check when `DATA_DIR_ALLOW_SYNCED=false`.
- Authentication bypass on protected HTTP routes such as `/mcp`, `/api/tools/{name}`,
  `/openapi.json`, and `/metrics` when `MCP_API_KEY` is set, leaking credentials or tokens into
  logs, tool responses, or audit files, or bypassing the HTTP `401` lockout guard.
- Code execution, XML entity expansion, server-side request forgery, or unbounded CPU and memory
  exhaustion while parsing untrusted HTML, MIME, XML, or message attachments.

Out of scope are actions permitted by settings the operator chose to enable, such as saving drafts
at `draft` tier, sending confirmed invitations or update notices at `draft` tier when
`SEND_ENABLED=true`, sending confirmed mail, meeting responses, cancellations, or automatic replies
at `full` tier when `SEND_ENABLED=true`, exposing HTTP mode without `MCP_API_KEY` beyond loopback,
or setting `EWS_INSECURE_SKIP_VERIFY=true` or `DATA_DIR_ALLOW_SYNCED=true`. Prompt injection inside
email content is in scope only if it defeats a server-enforced gate or preview guarantee rather than
persuading the calling model to make an allowed call.

For the full 4.5 safety design and explicit non-promises, read
[`v5/docs/SAFETY.md`](v5/docs/SAFETY.md) and its
[What is not promised](v5/docs/SAFETY.md#what-is-not-promised) section.
