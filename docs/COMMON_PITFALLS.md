# Common pitfalls — patterns that caused recurring regressions

Six months of bug-fixes converged on a small number of patterns. Each
has a structural test in `tests/` that fires before code can ship. Read
this list before diagnosing a "weird" bug — odds are it's one of these.

## 1. Mock-drift: `MagicMock` accepts any kwarg

`MagicMock().delete(disposal_type=X)` silently records the kwarg and
returns happily. Production calls the real `exchangelib.Item.delete()`
in 5.x, which has no such kwarg → `TypeError` → 500 on every delete.
The same pattern hit `OofSettings`/`OofReply` (wrapper class removed
in 5.x) and `disposal_type` independently.

**Guard:** `tests/test_exchangelib_signatures.py` pins the real
`inspect.signature` of every `exchangelib` API the codebase calls.
Adding a new call site? Add the corresponding signature pin.

## 2. Lazy `from exchangelib import X` inside a function body

When `X` disappears in an upstream upgrade, the `ImportError` fires
only at call time — never at load time, never under unit tests
(because tests patch the same lazy path). Both the OOF and the
inline-attachment outages were this exact pattern.

**Guard:** `tests/test_no_lazy_exchangelib_imports.py` is an AST-based
sentinel — it fails CI if any `from exchangelib...` appears inside a
function body in `src/`. Module-top `try: from x import y / except`
guards are allowed (they fail loud at import).

The same hazard with intra-`src/tools/` imports: a lazy
`from .other_tool_module import X` masks wrong-module / wrong-kwarg /
missing-`await` mistakes inside an outer `try/except`. The
`rule_tools.py` `move_to_folder` outage stacked all three defects in
three lines. The same sentinel covers that case for files under
`src/tools/`.

## 3. HTML escape cascading

`format_forward_header()` once returned strings with `<` already
escaped to `&lt;`. The reply/forward callers ran the result through
`escape_html()` again, turning every `&` into `&amp;`. After N reply
cycles the thread header showed `&amp;amp;amp;lt;`. The contract has
to be one transform per value; track who owns escaping at each step.

**Guard:** `tests/test_reply_forward_escape.py` and
`tests/test_format_body_for_html.py` pin both the cascade and the
`<` heuristic (plain text `"x < y"` used to be misclassified as HTML
and routed through `sanitize_html`, which does not escape stray `<`).

## 4. Tool-response key drift

Search/list tools used to ship `{results, total, total_results, total_count}`
in five overlapping shapes. Callers learned the wrong key, the source
later renamed, and clients silently broke. Today the canon is
`{items, count, total_available, next_offset}` for paged search, but
several older tools still ship sibling spellings. Don't add a new
list-shaped tool without checking sibling tools first; pick the
canonical envelope.

## 5. Bare `except: pass` swallowing real errors

`semantic_search_emails` returned 0 hits silently because a generic
`except` swallowed the upstream Ollama 404 / `KeyError`. The
embedding service was rewritten to surface the upstream message via
`EmbeddingError`. Watch for `except Exception: pass`,
`except: return []`, and similar in any tool — turn them into
classified error codes the caller can act on.

## 6. `exchangelib` 5.x signature shifts

The library has rewritten several method signatures in 5.x:

- `Item.delete()` — no longer takes `disposal_type` / `delete_type`.
- `OofReply` wrapper class — removed; `OofSettings.internal_reply` /
  `external_reply` take plain strings.
- `Item.save(update_fields=[...])` — kwarg name pinned by signature
  test; previously easy to confuse with QuerySet.only().

**Guard:** every kwarg-bearing exchangelib call has a signature
contract test in `tests/test_exchangelib_signatures.py`. CI fails
the moment a kwarg appears, disappears, or is renamed.

## 7. Audit-log redaction: hyphenated header names

`X-API-Key` and other hyphenated header spellings used to slip past
`redact_sensitive` because the matcher used `_` (e.g. `api_key`) but
the request header used `-`. The matcher now normalises hyphens to
underscores so both spellings hit the same patterns.

**Guard:** `tests/test_audit_redaction.py` parametrizes every
sensitive-key pattern + several substring variants (`client_secret`,
`access_token`, `X-API-Key`, etc.) so a pattern change or matcher
regression fails loudly.
