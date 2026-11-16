"""Attachment selector tests for get_attachment (alias, raw id, index, name, paging)."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from conftest import FIXED_OFFSET_TZ, make_settings

from ewsmcp.audit import AuditLog
from ewsmcp.ids import NullAliaser, get_aliaser
from ewsmcp.tools import mail_read
from ewsmcp.tools.base import Context, dispatch

TZ = ZoneInfo(FIXED_OFFSET_TZ)
SPECS = {spec.name: spec for spec in mail_read.TOOLS}


class _FakeGateway:
    def __init__(self, account):
        self.account = account

    async def call(self, fn):
        return fn(self.account)


class _CountingAttachment:
    """Attachment double whose lazy .content property counts byte reads."""

    def __init__(
        self,
        att_id,
        name,
        content,
        content_type="text/plain",
        *,
        size=None,
    ):
        self.attachment_id = SimpleNamespace(id=att_id) if att_id is not None else None
        self.name = name
        self._content = content
        self.content_type = content_type
        self.size = len(content) if size is None and content is not None else size
        self.reads = 0

    @property
    def content(self):
        self.reads += 1
        return self._content


def _msg(raw_id, attachments, *, subject="Draft packet"):
    return SimpleNamespace(
        id=raw_id,
        subject=subject,
        sender=SimpleNamespace(name="Ops", email_address="ops@example.com"),
        datetime_received=datetime(2026, 6, 10, 9, 0, tzinfo=TZ),
        is_read=True,
        has_attachments=bool(attachments),
        text_body="See attached.",
        message_id=f"<{raw_id}@example.com>",
        conversation_id=SimpleNamespace(id="CONV-1="),
        to_recipients=[],
        cc_recipients=[],
        attachments=list(attachments),
        importance="Normal",
        body=None,
        categories=None,
        flag_status=None,
    )


def _ctx(tmp_path, by_id, *, aliaser=None):
    account = MagicMock(name="account")
    account.fetch = MagicMock(
        side_effect=lambda ids, **_kw: [by_id[ids[0][0]]]
    )
    settings = make_settings(
        ews_server_url="https://mail.example.com/EWS/Exchange.asmx",
        ews_email="ops@example.com",
        data_dir=str(tmp_path / "data"),
        ews_tz=FIXED_OFFSET_TZ,
    )
    return (
        Context(
            settings=settings,
            gateway=_FakeGateway(account),
            manager=None,
            aliaser=aliaser if aliaser is not None else get_aliaser(str(tmp_path / "alias")),
            audit=AuditLog(str(tmp_path / "audit")),
            cache=None,
        ),
        account,
    )


def _run(ctx, **kwargs):
    return asyncio.run(dispatch(ctx, SPECS["get_attachment"], dict(kwargs)))


def test_alias_selects_attachment_it_was_issued_for_regardless_of_position(tmp_path):
    first = _CountingAttachment("AAMk-ATT-FIRST=", "first.txt", b"first-body")
    second = _CountingAttachment("AAMk-ATT-SECOND=", "second.txt", b"second-body")
    msg = _msg("RAW-DRAFT-1=", [first, second])
    ctx, _account = _ctx(tmp_path, {"RAW-DRAFT-1=": msg})

    # Issue a1 for the first attachment (index 0), then bump the counter so the
    # second attachment (index 1) receives a7: neither alias number matches its
    # zero-based position, and a1 must NOT select index 1.
    alias_first = ctx.aliaser.alias_for("AAMk-ATT-FIRST=", "a")
    for idx in range(2, 7):
        ctx.aliaser.alias_for(f"AAMk-PRIOR-{idx}=", "a")
    alias_second = ctx.aliaser.alias_for("AAMk-ATT-SECOND=", "a")
    assert alias_first == "a1"
    assert alias_second == "a7"

    draft_alias = ctx.aliaser.alias_for("RAW-DRAFT-1=", "d")

    res_first = _run(ctx, message_id=draft_alias, attachment=alias_first, mode="text")
    assert res_first["ok"] is True
    assert res_first["name"] == "first.txt"
    assert res_first["text"] == "first-body"
    assert first.reads == 1 and second.reads == 0

    res_second = _run(ctx, message_id=draft_alias, attachment=alias_second, mode="text")
    assert res_second["ok"] is True
    assert res_second["name"] == "second.txt"
    assert res_second["text"] == "second-body"
    assert first.reads == 1 and second.reads == 1


def test_alias_from_another_message_returns_not_found_without_reading_content(tmp_path):
    other_att = _CountingAttachment("AAMk-OTHER-ATT=", "other.txt", b"other-secret")
    here_first = _CountingAttachment("AAMk-HERE-0=", "alpha.txt", b"alpha-body")
    here_second = _CountingAttachment("AAMk-HERE-1=", "beta.txt", b"beta-body")
    other_msg = _msg("RAW-MSG-OTHER=", [other_att])
    here_msg = _msg("RAW-MSG-HERE=", [here_first, here_second])
    ctx, _account = _ctx(
        tmp_path,
        {"RAW-MSG-OTHER=": other_msg, "RAW-MSG-HERE=": here_msg},
    )

    # a1 has digits "1" < len(here_msg.attachments), so falling back to an index
    # would wrongly return beta.txt.
    foreign_a1 = ctx.aliaser.alias_for("AAMk-OTHER-ATT=", "a")
    assert foreign_a1 == "a1"
    m_here = ctx.aliaser.alias_for("RAW-MSG-HERE=", "m")

    res = _run(ctx, message_id=m_here, attachment=foreign_a1, mode="text")
    assert res["ok"] is False
    assert res["error"]["code"] == "not_found"
    err_msg = res["error"]["message"]
    assert "another message" in err_msg.lower()
    assert "alpha.txt" in err_msg and "beta.txt" in err_msg
    assert "AAMk-OTHER-ATT=" not in err_msg
    assert other_att.reads == 0
    assert here_first.reads == 0
    assert here_second.reads == 0


def test_a0_and_a1_without_alias_select_zero_based_indices(tmp_path):
    first = _CountingAttachment("AAMk-IDX-0=", "zero.txt", b"index-zero")
    second = _CountingAttachment("AAMk-IDX-1=", "one.txt", b"index-one")
    msg = _msg("RAW-IDX=", [first, second])
    ctx, _account = _ctx(tmp_path, {"RAW-IDX=": msg})

    res_a0 = _run(ctx, message_id="RAW-IDX=", attachment="a0", mode="text")
    assert res_a0["ok"] is True
    assert res_a0["name"] == "zero.txt"
    assert res_a0["text"] == "index-zero"

    res_a1 = _run(ctx, message_id="RAW-IDX=", attachment="a1", mode="text")
    assert res_a1["ok"] is True
    assert res_a1["name"] == "one.txt"
    assert res_a1["text"] == "index-one"

    res_oob = _run(ctx, message_id="RAW-IDX=", attachment="a2", mode="text")
    assert res_oob["ok"] is False
    assert res_oob["error"]["code"] == "not_found"


def test_plain_digits_and_filenames_work_and_literal_a1_filename_wins(tmp_path):
    first = _CountingAttachment("AAMk-NAME-0=", "Report.txt", b"report-zero")
    second = _CountingAttachment("AAMk-NAME-1=", "Summary.csv", b"col1,col2\n1,2")
    literal_a1 = _CountingAttachment("AAMk-NAME-2=", "a1", b"named-a1-content")
    msg = _msg("RAW-NAMES=", [first, second, literal_a1])
    ctx, _account = _ctx(tmp_path, {"RAW-NAMES=": msg})

    # Even when a1 is a minted alias for another attachment, the literal file
    # name "a1" wins over alias and index readings.
    minted = ctx.aliaser.alias_for("AAMk-NAME-0=", "a")
    assert minted == "a1"

    r_zero = _run(ctx, message_id="RAW-NAMES=", attachment="0", mode="text")
    assert r_zero["ok"] is True and r_zero["text"] == "report-zero"

    r_one = _run(ctx, message_id="RAW-NAMES=", attachment="1", mode="text")
    assert r_one["ok"] is True and r_one["text"] == "col1,col2\n1,2"

    r_by_name = _run(ctx, message_id="RAW-NAMES=", attachment="report.TXT", mode="text")
    assert r_by_name["ok"] is True and r_by_name["text"] == "report-zero"

    r_lit = _run(ctx, message_id="RAW-NAMES=", attachment="a1", mode="text")
    assert r_lit["ok"] is True
    assert r_lit["name"] == "a1"
    assert r_lit["text"] == "named-a1-content"

    r_lit_upper = _run(ctx, message_id="RAW-NAMES=", attachment="A1", mode="text")
    assert r_lit_upper["ok"] is True
    assert r_lit_upper["name"] == "a1"
    assert r_lit_upper["text"] == "named-a1-content"


def test_raw_attachment_id_selects_its_attachment(tmp_path):
    first = _CountingAttachment("AAMkAGI2-RAW-FIRST=", "memo.txt", b"memo-body")
    second = _CountingAttachment("AAMkAGI2-RAW-SECOND=", "annex.txt", b"annex-body")
    msg = _msg("RAW-RAWID=", [first, second])

    # Works with the normal SQLite aliaser.
    ctx, _account = _ctx(tmp_path / "sqlite", {"RAW-RAWID=": msg})
    res = _run(
        ctx,
        message_id="RAW-RAWID=",
        attachment="AAMkAGI2-RAW-SECOND=",
        mode="text",
    )
    assert res["ok"] is True
    assert res["name"] == "annex.txt"
    assert res["text"] == "annex-body"
    assert first.reads == 0 and second.reads == 1

    # Also works when the aliaser is unavailable (NullAliaser returns raw ids).
    ctx_null, _ = _ctx(
        tmp_path / "null",
        {"RAW-RAWID=": msg},
        aliaser=NullAliaser(),
    )
    raw_from_draft = ctx_null.aliaser.alias_for("AAMkAGI2-RAW-FIRST=", "a")
    assert raw_from_draft == "AAMkAGI2-RAW-FIRST="
    res_null = _run(
        ctx_null,
        message_id="RAW-RAWID=",
        attachment=raw_from_draft,
        mode="text",
    )
    assert res_null["ok"] is True
    assert res_null["name"] == "memo.txt"
    assert res_null["text"] == "memo-body"

    res_missing = _run(
        ctx,
        message_id="RAW-RAWID=",
        attachment="AAMkAGI2-NO-SUCH-ID=",
        mode="text",
    )
    assert res_missing["ok"] is False
    assert res_missing["error"]["code"] == "not_found"


def test_paging_with_offset_and_same_alias_returns_next_part_of_same_document(tmp_path):
    doc_first_text = "FIRST-DOC-CHUNK-" * 2_800  # 44,800 chars -> 3 parts
    doc_second_text = "SECOND-DOC-PART-" * 2_800  # 44,800 chars -> 3 parts
    att_first = _CountingAttachment(
        "AAMk-PAGE-FIRST=", "first_long.txt", doc_first_text.encode("utf-8")
    )
    att_second = _CountingAttachment(
        "AAMk-PAGE-SECOND=", "second_long.txt", doc_second_text.encode("utf-8")
    )
    msg = _msg("RAW-PAGE=", [att_first, att_second])
    ctx, account = _ctx(tmp_path, {"RAW-PAGE=": msg})

    # Mint a1 for the SECOND attachment (index 1) and a2 for the FIRST (index 0).
    alias_for_second = ctx.aliaser.alias_for("AAMk-PAGE-SECOND=", "a")
    alias_for_first = ctx.aliaser.alias_for("AAMk-PAGE-FIRST=", "a")
    assert alias_for_second == "a1"
    assert alias_for_first == "a2"
    m_alias = ctx.aliaser.alias_for("RAW-PAGE=", "m")

    p1 = _run(ctx, message_id=m_alias, attachment=alias_for_first, mode="text", offset=0)
    assert p1["ok"] is True
    assert p1["name"] == "first_long.txt"
    assert p1["offset"] == 0
    assert p1["next_offset"] == 20_000
    assert p1["truncated"] is True
    assert att_first.reads == 1 and att_second.reads == 0
    fetches_after_p1 = account.fetch.call_count

    p2 = _run(
        ctx,
        message_id=m_alias,
        attachment=alias_for_first,
        mode="text",
        offset=p1["next_offset"],
    )
    assert p2["ok"] is True
    assert p2["name"] == "first_long.txt"
    assert p2["offset"] == 20_000
    assert p2["next_offset"] == 40_000
    assert att_first.reads == 1 and att_second.reads == 0
    assert account.fetch.call_count == fetches_after_p1

    p3 = _run(
        ctx,
        message_id=m_alias,
        attachment=alias_for_first,
        mode="text",
        offset=p2["next_offset"],
    )
    assert p3["ok"] is True
    assert p3["name"] == "first_long.txt"
    assert "next_offset" not in p3
    assert p1["text"] + p2["text"] + p3["text"] == doc_first_text
    assert att_first.reads == 1 and att_second.reads == 0
    assert account.fetch.call_count == fetches_after_p1

    # Selecting the same attachment via file name, index, or raw id hits the
    # same cached document without re-reading attachment bytes.
    for alt_selector in ("first_long.txt", "0", "AAMk-PAGE-FIRST="):
        alt = _run(
            ctx,
            message_id=m_alias,
            attachment=alt_selector,
            mode="text",
            offset=20_000,
        )
        assert alt["ok"] is True
        assert alt["text"] == p2["text"]
    assert att_first.reads == 1


def test_get_attachment_description_names_all_three_selector_forms():
    desc = SPECS["get_attachment"].description
    assert "file name" in desc
    assert "zero-based index" in desc
    assert "`id` returned by the draft tools" in desc
