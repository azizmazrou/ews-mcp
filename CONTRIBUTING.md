# Contributing to ews-mcp

Thanks for your interest in this project. Here's how it's organised
and how to engage with it productively.

## Repository layout

```
v5/                      the 4.5 server (current line)
  ewsmcp/                Python package (`ewsmcp` entry point)
  tests/                 offline unit, contract, and structural suite
  scripts/               boot_smoke.py, dump_tool_table.py, live_smoke.py, verify_audit_chain.py
  docs/                  API.md (generated tool tables + hand-written contracts)
src/                     legacy 4.0 source (ships as :latest)
docs/                    documentation map; docs/legacy/ = 4.0 docs
examples/                example assistant skill on top of 4.5
.github/workflows/       CI: v5-tests.yml, v5-publish.yml,
                             docker-build-test.yml, docker-publish.yml
Dockerfile               legacy 4.0 container build (4.5: v5/Dockerfile)
docker-compose.yml       legacy 4.0 compose file (4.5: v5/docker-compose.example.yml)
requirements.txt         legacy 4.0 runtime deps (4.5: v5/pyproject.toml)
setup.py                 legacy 4.0 distribution metadata
README.md                product landing page
CHANGELOG.md             version history
```

The 4.5 line ships a full offline test suite under `v5/tests` that runs
without an Exchange server, gated in CI by `v5-tests.yml` (ruff,
`pytest` on Python 3.11 and 3.12, `dump_tool_table.py --check`,
`boot_smoke.py`, and a Docker import smoke). Only the legacy 4.0 tree
(`src/`) has no in-repo test suite and relies on `docker-build-test.yml`
for Dockerfile build and Python import checks.

## Filing issues

- **Bug reports**: please include the server line (`4.5` or legacy
  `4.0`), the Exchange auth type (auto-negotiated / `basic` / `ntlm` /
  `digest`, or `oauth2` on legacy 4.0), the exchangelib version, the
  relevant tool name, and the verbatim error message + a redacted
  version of the input arguments. **Do not paste real email addresses,
  internal hostnames, or message bodies** — the maintainer will
  reproduce against their own mailbox once the shape of the bug is
  clear.
- **Feature requests**: describe the workflow first, the proposed API
  second. The maintainer's bias is to keep the MCP doing deterministic
  data work and push reasoning to the consuming agent — see
  [`v5/DESIGN.md`](v5/DESIGN.md) ("The law" #1) for context.
  Reasoning-shaped tools ("classify this", "summarise that") are
  unlikely to be added because the consuming LLM does them better
  in-prompt.

## Submitting pull requests

PRs are welcome but the maintainer's pattern is:

1. **Read the issue / PR for the problem statement and approach**
2. **Reproduce the bug against a real mailbox** (not against the PR's diff)
3. **Apply a fix in the maintainer's own style** rather than merging the
   PR directly

This keeps the codebase consistent and avoids hidden assumptions in
contributor changes. So **please don't be offended if a PR is closed
without merge** — it usually means the bug was real and got fixed in
a separate commit by the maintainer; the issue stays linked so you can
verify the fix.

A pull request is therefore read as a proposal: it shows the problem and
one way to solve it, and the change itself is then written by the
maintainer. A proposal is quickest to assess when:
- It applies cleanly on top of `main`
- For `v5/`, all four checks from the root README's Development section
  pass when run from `v5/` (`python -m pytest tests -q`,
  `python -m ruff check .`, `python scripts/boot_smoke.py full`, and
  `python scripts/dump_tool_table.py --check`) and new behaviour
  comes with a test under `v5/tests` (for legacy `src/` changes,
  `docker-build-test.yml` passes)
- It does not introduce a new external service dependency
  (e.g. a vector database) without prior discussion in an issue
- It does not re-add LLM-reasoning tools removed in v4.0
  (see CHANGELOG for the rationale)

## Local development

### 4.5 (`v5/`, current line)

Clone the repository and create a virtual environment in the repository root:

```bash
git clone https://github.com/azizmazrou/ews-mcp.git
cd ews-mcp
python3 -m venv .venv        # Windows: python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
```

Install the development dependencies and run all four checks from `v5/`:

```bash
cd v5
pip install -e ".[dev]"
python -m pytest tests -q
python -m ruff check .
python scripts/boot_smoke.py full
python scripts/dump_tool_table.py --check
```

To run against a mailbox from the repository root, copy `v5/.env.example` to `.env`, fill in
credentials, and run `ewsmcp`. To build the 4.5 container image, run
`docker build -t ews-mcp:dev v5` from the repository root.

### Legacy 4.0 (`src/`)

```bash
pip install -r requirements.txt
cp .env.example .env  # edit with your credentials
python -m src.main
```

Or via the legacy Docker image:

```bash
docker build -t ews-mcp:legacy .
docker run -i --rm --env-file .env ews-mcp:legacy
```

## Code style

- For `v5/`, `python -m ruff check .` from `v5/` with the rule set pinned
  in `v5/pyproject.toml` is a blocking CI gate. Match surrounding style in
  both trees.
- Comments should explain *why*, not *what*.
- Don't add docstrings that just restate the function name.
- Avoid try/except that swallows failures silently — log and re-raise
  with context.
- Don't introduce a new dependency without justifying it in the PR
  description. Pure-stdlib solutions are preferred beyond the packages
  already listed in `v5/pyproject.toml` (or `requirements.txt` for
  legacy 4.0).

## Security

Do not post vulnerability details in a public issue or pull request. Check the repository's
**Security** tab and use **Report a vulnerability** if it is offered; otherwise open an issue that
states only that you have a security report, with no details, so a private channel can be arranged.
Supported versions and security scope are in [`SECURITY.md`](SECURITY.md).

## License

By contributing you agree your contribution is licensed under the
project's MIT license.
