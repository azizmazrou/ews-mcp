# Contributing to ews-mcp

Thanks for your interest in this project. Here's how it's organised
and how to engage with it productively.

## Repository layout

```
src/                     production source
docs/                    user-facing documentation
scripts/                 build / deploy / setup scripts
.github/workflows/       CI: docker-publish.yml + docker-build-test.yml
Dockerfile               container build
docker-compose.yml       reference docker-compose for local dev
requirements.txt         runtime Python deps
setup.py                 distribution metadata
README.md                product landing page
CHANGELOG.md             version history
```

There is no `tests/` directory in this repository. Test development
happens out-of-tree against the maintainer's live mailbox; the public
repo intentionally ships only the production product. The CI workflows
verify Dockerfile build + Python import correctness — that's the gate
PRs need to pass.

## Filing issues

- **Bug reports**: please include the Exchange auth type (`oauth2` /
  `basic` / `ntlm`), the exchangelib version, the relevant tool name,
  and the verbatim error message + a redacted version of the input
  arguments. **Do not paste real email addresses, internal hostnames,
  or message bodies** — the maintainer will reproduce against their
  own mailbox once the shape of the bug is clear.
- **Feature requests**: describe the workflow first, the proposed API
  second. The maintainer's bias is to keep the MCP doing deterministic
  data work and push reasoning to the consuming agent — see the README
  "MCP / skill boundary" section for context. Reasoning-shaped tools
  ("classify this", "summarise that") are unlikely to be added because
  the consuming LLM does them better in-prompt.

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

If you'd rather your code merged verbatim, the bar is higher:
- The PR must apply cleanly on top of `main`
- The Dockerfile + import check (`docker-build-test.yml`) must pass
- The change must not introduce a new external service dependency
  (e.g. a vector database) without prior discussion in an issue
- The change must not re-add LLM-reasoning tools removed in v4.0
  (see CHANGELOG for the rationale)

## Local development

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
pip install -r requirements.txt
cp .env.example .env  # edit with your credentials
python -m src.main
```

Or via Docker:

```bash
docker build -t ews-mcp:dev .
docker run -i --rm --env-file .env ews-mcp:dev
```

## Code style

- Plain Python, no specific formatter enforced. Match surrounding style.
- Comments should explain *why*, not *what*.
- Don't add docstrings that just restate the function name.
- Avoid try/except that swallows failures silently — log and re-raise
  with context.
- Don't introduce a new dependency without justifying it in the PR
  description. Pure-stdlib solutions are preferred for everything
  except the document-extraction libraries already in `requirements.txt`.

## Security

If you find a vulnerability that affects production deployments
(credential leak, RCE, AuthZ bypass, etc.), **please don't open a
public issue**. Email the maintainer directly via the address in
`setup.py` and they'll coordinate a fix.

## License

By contributing you agree your contribution is licensed under the
project's MIT license.
