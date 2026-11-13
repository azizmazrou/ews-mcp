"""SyncEngine: delta application, token persistence, degrade-not-die."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from conftest import FIXED_OFFSET_TZ, make_settings
from exchangelib import EWSDate

from ewsmcp.cache.store import CacheStore
from ewsmcp.cache.sync import SyncEngine, row_from_event, row_from_message

TZ = ZoneInfo(FIXED_OFFSET_TZ)
NOW = datetime(2026, 7, 10, 9, 0, tzinfo=TZ)


class FakeGateway:
    async def call(self, fn):
        return fn(self.account)

    def __init__(self, account):
        self.account = account


class FakeFolder:
    """Scripted sync_items: yields the queued changes, then exposes the
    new token via item_sync_state (exchangelib contract)."""

    def __init__(self, name):
        self.name = name
        self.batches = []
        self.item_sync_state = None
        self.seen_tokens = []

    def queue(self, changes, new_token):
        self.batches.append((changes, new_token))

    def sync_items(self, sync_state=None, only_fields=None, **kw):
        self.seen_tokens.append(sync_state)
        if not self.batches:
            return
        changes, new_token = self.batches.pop(0)
        yield from changes
        self.item_sync_state = new_token


def _msg(raw_id, *, subject="Subj", body="Body text", dt=None, is_read=False,
         conv="CONV-1"):
    return SimpleNamespace(
        id=raw_id, changekey="CK", subject=subject,
        sender=SimpleNamespace(name="Sender", email_address="sender@corp.example"),
        datetime_received=dt or NOW,
        is_read=is_read, has_attachments=False, importance="Normal",
        categories=None, conversation_id=SimpleNamespace(id=conv),
        message_id=f"<{raw_id}@corp.example>",
        to_recipients=[SimpleNamespace(email_address="exec@corp.example")],
        text_body=body,
    )


def _account():
    account = SimpleNamespace()
    account.inbox = FakeFolder("inbox")
    account.sent = FakeFolder("sent")
    account.tasks = FakeFolder("tasks")
    return account


def _engine(tmp_path, account, **overrides):
    overrides.setdefault("ews_tz", FIXED_OFFSET_TZ)
    settings = make_settings(**overrides)
    store = CacheStore(tmp_path / "mirror.db")
    return SyncEngine(settings, FakeGateway(account), store), store


def test_cycle_applies_creates_updates_deletes_and_read_flags(tmp_path):
    account = _account()
    account.inbox.queue([
        ("create", _msg("M1", subject="First", is_read=False)),
        ("create", _msg("M2", subject="Second")),
    ], "TOK-1")
    engine, store = _engine(tmp_path, account)
    asyncio.run(engine._cycle())
    assert store.get_message("M1")["subject"] == "First"
    assert store.get_sync_state("item:inbox") == "TOK-1"

    account.inbox.queue([
        ("update", _msg("M1", subject="First (edited)")),
        ("delete", SimpleNamespace(id="M2")),
        ("read_flag_change", (SimpleNamespace(id="M1"), True)),
    ], "TOK-2")
    asyncio.run(engine._cycle())
    row = store.get_message("M1")
    assert row["subject"] == "First (edited)"
    assert row["is_read"] == 1
    assert store.get_message("M2") is None
    assert store.get_sync_state("item:inbox") == "TOK-2"
    # the second sync resumed FROM the first token
    assert account.inbox.seen_tokens[-1] == "TOK-1"


def test_window_floor_skips_ancient_backfill(tmp_path):
    account = _account()
    ancient = NOW - timedelta(days=4000)
    account.inbox.queue([
        ("create", _msg("OLD", dt=ancient)),
        ("create", _msg("NEW")),
    ], "TOK-1")
    engine, store = _engine(tmp_path, account, ews_cache_window_days=365)
    asyncio.run(engine._cycle())
    assert store.get_message("OLD") is None
    assert store.get_message("NEW") is not None


def test_cycle_failure_degrades_not_dies(tmp_path):
    class BoomGateway:
        async def call(self, fn):
            raise ConnectionError("exchange gone")

    settings = make_settings()
    store = CacheStore(tmp_path / "mirror.db")
    engine = SyncEngine(settings, BoomGateway(), store)

    async def one_iteration():
        try:
            await engine._cycle()
        except Exception as exc:
            engine.last_error = f"{type(exc).__name__}: {exc}"

    asyncio.run(one_iteration())
    assert "exchange gone" in engine.last_error
    assert engine.status()["last_error"]


def test_row_from_message_cleans_body_once(tmp_path):
    quoted = ("Latest reply only.\n\nFrom: Someone <s@corp.example>\n"
              "Sent: Monday\nTo: Exec\nSubject: Re: X\n\nOLD QUOTED TEXT")
    row = row_from_message(_msg("M1", body=quoted), "inbox", FIXED_OFFSET_TZ)
    assert "OLD QUOTED" not in row["body_clean"]
    assert row["body_clean"].startswith("Latest reply only.")
    assert row["norm_text"]  # normalized shadow present for FTS
    assert row["internet_message_id"] == "<M1@corp.example>"


def test_slow_lane_syncs_folders_calendar_tasks(tmp_path):
    account = _account()

    def folder_node(fid, name, children=(), total=0, unread=0,
                    folder_class=None):
        return SimpleNamespace(id=fid, name=name, children=list(children),
                               total_count=total, unread_count=unread,
                               folder_class=folder_class)

    inbox_node = folder_node("F-IN", "Inbox", total=5, unread=2,
                             folder_class="IPF.Note")
    account.msg_folder_root = folder_node("F-ROOT", "root", [inbox_node])
    account.inbox = FakeFolder("inbox")
    account.inbox.id = "F-IN"

    class CalendarStub:
        def __init__(self):
            self.calls = []

        def view(self, *, start, end, max_items=None):
            self.calls.append((start, end, max_items))
            return [SimpleNamespace(
                id="EV1", changekey=None, subject="Standup",
                start=NOW, end=NOW + timedelta(minutes=30),
                location=None, organizer=None, is_recurring=False,
                recurrence=None, my_response_type=None,
            )]

    account.calendar = CalendarStub()
    account.tasks.queue([("create", SimpleNamespace(
        id="T1", changekey=None, subject="File report",
        due_date=None, is_complete=False, status="NotStarted"))], "TT-1")

    engine, store = _engine(tmp_path, account)
    engine._sync_slow_lane(account)
    folders = {r["path"]: r for r in store.folder_rows()}
    assert folders["Inbox"]["unread"] == 2
    assert folders["Inbox"]["folder_class"] == "IPF.Note"
    assert store.events_window(0, 2**40)
    rows, total = store.task_rows()
    assert total == 1
    assert store.get_sync_state("item:tasks") == "TT-1"


def test_all_day_one_day_and_three_day_events_appear_on_every_covered_day(tmp_path):
    one_day = SimpleNamespace(
        id="EV-1D",
        changekey="CK-1",
        subject="Company holiday",
        start=EWSDate(2026, 7, 10),
        end=EWSDate(2026, 7, 10),
        location=None,
        organizer=None,
        is_recurring=False,
        recurrence=None,
        my_response_type=None,
    )
    three_day = SimpleNamespace(
        id="EV-3D",
        changekey="CK-3",
        subject="Annual summit",
        start=EWSDate(2026, 7, 10),
        end=EWSDate(2026, 7, 12),
        location=None,
        organizer=None,
        is_recurring=False,
        recurrence=None,
        my_response_type=None,
    )
    timed = SimpleNamespace(
        id="EV-TIMED",
        changekey="CK-T",
        subject="Morning check-in",
        start=datetime(2026, 7, 10, 9, 0, tzinfo=TZ),
        end=datetime(2026, 7, 10, 9, 30, tzinfo=TZ),
        location=None,
        organizer=None,
        is_recurring=False,
        recurrence=None,
        my_response_type=None,
    )

    row_1d = row_from_event(one_day, FIXED_OFFSET_TZ)
    row_3d = row_from_event(three_day, FIXED_OFFSET_TZ)
    row_timed = row_from_event(timed, FIXED_OFFSET_TZ)

    # end_iso keeps the date Exchange reported, while end_ts advances to next-day midnight.
    assert row_1d["start_iso"] == "2026-07-10"
    assert row_1d["end_iso"] == "2026-07-10"
    assert row_1d["end_ts"] - row_1d["start_ts"] == 86400

    assert row_3d["start_iso"] == "2026-07-10"
    assert row_3d["end_iso"] == "2026-07-12"
    assert row_3d["end_ts"] - row_3d["start_ts"] == 3 * 86400

    assert row_timed["end_ts"] - row_timed["start_ts"] == 1800

    store = CacheStore(tmp_path / "mirror.db")
    try:
        store.replace_events([row_1d, row_3d])

        def subjects_on(day_num):
            day_start = datetime(2026, 7, day_num, 0, 0, tzinfo=TZ)
            day_end = day_start + timedelta(days=1)
            rows = store.events_window(
                int(day_start.timestamp()), int(day_end.timestamp()), limit=10,
            )
            return [r["subject"] for r in rows]

        assert subjects_on(9) == []
        assert set(subjects_on(10)) == {"Company holiday", "Annual summit"}
        assert subjects_on(11) == ["Annual summit"]
        assert subjects_on(12) == ["Annual summit"]
        assert subjects_on(13) == []
    finally:
        store.close()
