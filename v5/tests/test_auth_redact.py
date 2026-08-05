"""Tokens must not survive a log line (DESIGN.md law #6).

Our own code is careful, but httpx, uvicorn and exchangelib are not: they
log URLs, header dumps and exception reprs. A filter on the root handlers is
the one place that covers all of them, so it has to hold for records this
package never wrote.
"""

import logging

from conftest import make_token

from ewsmcp.auth.redact import TokenRedactingFilter, install, redact

TOKEN = ("eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0"
         ".eyJzdWIiOiJ1c2VyIn0"
         ".c2lnbmF0dXJlLWJ5dGVz")


def test_bearer_header_dump_is_redacted():
    line = redact(f"POST /mcp headers={{'authorization': 'Bearer {TOKEN}'}}")
    assert TOKEN not in line
    assert "[redacted]" in line


def test_bare_jwt_is_redacted_anywhere():
    assert TOKEN not in redact(f"upstream said: token {TOKEN} is invalid")


def test_a_real_signed_token_is_redacted(rsa_keys):
    """Not just the synthetic sample — a genuine PyJWT output too."""
    token = make_token(rsa_keys)
    assert token not in redact(f"Authorization: Bearer {token}")


def test_ordinary_text_is_untouched():
    line = "connection test failed: ErrorServerBusy after 3 retries"
    assert redact(line) == line


def test_the_word_bearer_alone_is_not_mangled():
    assert redact("no bearer token was supplied") == "no bearer token was supplied"


def test_filter_redacts_the_message_and_the_arguments(caplog):
    """Interpolation happens after filtering, so `%s` arguments — where a
    token usually hides — must be cleaned too, not just the format string."""
    logger = logging.getLogger("ewsmcp.test.redact")
    logger.addFilter(TokenRedactingFilter())
    with caplog.at_level(logging.INFO, logger="ewsmcp.test.redact"):
        logger.info("calling upstream with %s", f"Bearer {TOKEN}")
        logger.info("raw token %s in the message", TOKEN)
    logger.filters.clear()
    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert TOKEN not in joined
    assert joined.count("[redacted]") == 2


def test_install_attaches_to_handlers_not_the_logger():
    """A filter on a logger only sees records logged directly to it — records
    propagated up from httpx/uvicorn child loggers would slip past."""
    root = logging.getLogger("ewsmcp.test.install")
    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        install(root)
        install(root)  # idempotent
        assert sum(isinstance(f, TokenRedactingFilter) for f in handler.filters) == 1
    finally:
        root.removeHandler(handler)


def test_a_third_party_logger_is_covered(caplog):
    """The whole point: a record this package never wrote."""
    root = logging.getLogger()
    install(root)
    with caplog.at_level(logging.WARNING):
        logging.getLogger("httpx").warning("GET /keys failed, sent Bearer %s", TOKEN)
    assert TOKEN not in "\n".join(r.getMessage() for r in caplog.records)
