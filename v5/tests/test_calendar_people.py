"""calendar_people pack tests — tools 7-12, driven through dispatch().

Same harness as test_dispatcher: make_settings + Context with a fake
gateway whose ``call`` runs the work function against a MagicMock
account synchronously. All datetimes are tz-aware Asia/Riyadh.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from exchangelib.recurrence import (
    AbsoluteMonthlyPattern,
    AbsoluteYearlyPattern,
    DailyPattern,
    Recurrence,
    WeeklyPattern,
)

from conftest import make_settings
from ewsmcp import __version__
from ewsmcp.audit import AuditLog
from ewsmcp.ids import get_aliaser
from ewsmcp.tools import calendar_people
from ewsmcp.tools.base import Context, dispatch
from ewsmcp.tools.calendar_people import merge_busy_and_find_slots

TZ = ZoneInfo("Asia/Riyadh")
SPEC = {s.name: s for s in calendar_people.TOOLS}


class _FakeGateway:
    def __init__(self, account):
        self.account = account

    async def call(self, fn):
        return fn(self.account)


def _ctx(tmp_path, account, **settings_overrides) -> Context:
    return Context(
        settings=make_settings(**settings_overrides),
        gateway=_FakeGateway(account),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "mem")),
        audit=AuditLog(str(tmp_path / "data")),
    )


def _run(ctx, name, args=None):
    return asyncio.run(dispatch(ctx, SPEC[name], dict(args or {})))


def _dt(hour, minute=0, day=15):
    return datetime(2026, 6, day, hour, minute, tzinfo=TZ)


def _event(raw_id="RAW-EV-1", subject="Standup", start=None, end=None):
    return SimpleNamespace(
        id=raw_id, subject=subject,
        start=start or _dt(9), end=end or _dt(9, 30),
        location=None, organizer=None, my_response_type=None,
        is_recurring=False, recurrence=None,
    )


# --- pack contract ----------------------------------------------------------


def test_pack_exports_seven_read_specs():
    assert [s.name for s in calendar_people.TOOLS] == [
        "list_events", "get_event", "check_availability",
        "find_people", "get_contact", "get_oof_settings", "get_server_status",
    ]
    for spec in calendar_people.TOOLS:
        assert spec.side_effect_class == "read"
        assert spec.input_schema["additionalProperties"] is False
        assert isinstance(spec.input_schema["required"], list)
    assert SPEC["get_server_status"].requires_ews is False
    assert all(s.requires_ews for n, s in SPEC.items() if n != "get_server_status")


# --- list_events --------------------------------------------------------------


def test_list_events_uses_view_not_filter(tmp_path):
    account = MagicMock()
    account.calendar.view.return_value = [_event("RAW-1"), _event("RAW-2", "Review")]
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "list_events", {"start": "2026-06-15", "end": "2026-06-16"})
    assert res["ok"] is True
    account.calendar.view.assert_called_once()
    account.calendar.filter.assert_not_called()
    kwargs = account.calendar.view.call_args.kwargs
    assert kwargs["start"] == datetime(2026, 6, 15, tzinfo=TZ)
    assert kwargs["end"] == datetime(2026, 6, 16, tzinfo=TZ)
    assert kwargs["start"].tzinfo is not None
    assert res["count"] == 2 and res["total_available"] == 2
    assert res["next_offset"] is None
    assert res["items"][0]["id"] == "e1"
    assert res["items"][0]["subject"] == "Standup"
    assert res["items"][0]["start"] == "2026-06-15T09:00+03:00"
    assert res["items"][1]["id"] == "e2"


def test_list_events_pagination_caps_the_view_server_side(tmp_path):
    """The window expansion is capped via view(max_items=offset+limit+1) —
    a year of recurrences is never materialized to render one page. A
    truncated view means the exact total is unknown (None) but the
    lookahead proves another page exists."""
    events = [_event(f"RAW-{i}") for i in range(5)]
    account = MagicMock()
    account.calendar.view.side_effect = (
        lambda *, start, end, max_items=None: events[:max_items])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "list_events",
               {"start": "2026-06-15", "end": "2026-06-22", "offset": 1, "limit": 2})
    assert account.calendar.view.call_args.kwargs["max_items"] == 4  # 1+2+1
    assert res["count"] == 2
    assert res["total_available"] is None  # view was truncated at the cap
    assert res["next_offset"] == 3

    # Uncapped window (fewer events than the cap): exact total, no next page.
    res2 = _run(ctx, "list_events",
                {"start": "2026-06-15", "end": "2026-06-22", "offset": 0, "limit": 10})
    assert res2["total_available"] == 5
    assert res2["next_offset"] is None


def test_list_events_rejects_absurd_windows(tmp_path):
    account = MagicMock()
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "list_events", {"start": "today", "end": "+9999d"})
    assert res["error"]["code"] == "validation"
    assert "narrow" in res["error"]["message"]
    account.calendar.view.assert_not_called()


def test_list_events_relative_dates_today_plus_7d(tmp_path):
    account = MagicMock()
    account.calendar.view.return_value = []
    ctx = _ctx(tmp_path, account)
    before = datetime.now(TZ)
    res = _run(ctx, "list_events", {})  # defaults: start="today", end="+7d"
    after = datetime.now(TZ)
    assert res["ok"] is True and res["items"] == []
    kwargs = account.calendar.view.call_args.kwargs
    start = kwargs["start"]
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert start.date() in {before.date(), after.date()}
    assert str(start.tzinfo) == "Asia/Riyadh"
    assert kwargs["end"] - start == timedelta(days=7)


def test_list_events_bad_date_is_validation_error(tmp_path):
    account = MagicMock()
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "list_events", {"start": "next tuesday"})
    assert res["ok"] is False and res["error"]["code"] == "validation"
    account.calendar.view.assert_not_called()


# --- get_event ----------------------------------------------------------------


def test_get_event_attendees_body_and_flags(tmp_path):
    raw = "RAW-EVENT-FULL"
    ev = _event(raw, subject="Budget review")
    ev.required_attendees = [SimpleNamespace(
        mailbox=SimpleNamespace(name="Ahmed", email_address="ahmed@corp.example"),
        response_type="Accept")]
    ev.optional_attendees = [SimpleNamespace(
        mailbox=SimpleNamespace(name=None, email_address="sara@corp.example"),
        response_type="Decline")]
    ev.organizer = SimpleNamespace(name="Boss", email_address="boss@corp.example")
    ev.text_body = "Agenda attached."
    ev.my_response_type = "Organizer"
    ev.is_recurring = True
    account = MagicMock()
    account.fetch.return_value = iter([ev])
    ctx = _ctx(tmp_path, account)
    alias = ctx.aliaser.alias_for(raw, "e")  # dispatcher will resolve alias→raw
    res = _run(ctx, "get_event", {"id": alias})
    assert res["ok"] is True
    account.fetch.assert_called_once_with(ids=[(raw, None)])  # handler saw RAW id
    event = res["event"]
    assert event["id"] == alias
    assert event["organizer"] == "Boss <boss@corp.example>"
    assert event["attendees"] == [
        {"email": "ahmed@corp.example", "response": "Accept", "name": "Ahmed"},
        {"email": "sara@corp.example", "response": "Decline"},
    ]
    assert event["body"] == "Agenda attached."
    assert event["recurring"] is True
    assert event["my_response"] == "Organizer"


def test_all_day_event_keeps_its_dates(tmp_path):
    # exchangelib hands back dates, not datetimes, for all-day items.
    from datetime import date
    from ewsmcp.cache.sync import row_from_event
    from ewsmcp.dto import event_card
    ev = _event("RAW-ALLDAY", subject="Holiday",
                start=date(2026, 11, 3), end=date(2026, 11, 4))
    ev.is_all_day = True
    card = event_card(ev, get_aliaser(str(tmp_path / "mem")), "Europe/Berlin")
    assert (card["start"], card["end"]) == ("2026-11-03", "2026-11-04")
    assert card["all_day"] is True
    row = row_from_event(ev, "Europe/Berlin")
    assert row["start_iso"] == "2026-11-03"
    assert row["start_ts"] == int(
        datetime(2026, 11, 3, tzinfo=ZoneInfo("Europe/Berlin")).timestamp())
    assert row["end_ts"] > row["start_ts"]


def _get(tmp_path, ev):
    account = MagicMock()
    account.fetch.return_value = iter([ev])
    ctx = _ctx(tmp_path, account)
    return _run(ctx, "get_event", {"id": ctx.aliaser.alias_for("RAW-S", "e")})["event"]


def _series_event(kind):
    ev = MagicMock()
    ev.id = "RAW-S"
    ev.subject = "Sync"
    ev.type = kind
    ev.required_attendees = ev.optional_attendees = None
    ev.organizer = None
    ev.text_body = ""
    ev.my_response_type = "Organizer"
    return ev


def test_get_event_reads_the_rule_from_the_master_of_an_occurrence(tmp_path):
    ev = _series_event("Occurrence")
    master = ev.recurring_master.return_value
    master.recurrence = Recurrence(pattern=WeeklyPattern(interval=1, weekdays=[4]),
                                   start=date(2026, 11, 12))
    event = _get(tmp_path, ev)
    master.refresh.assert_called_once_with()
    assert event["type"] == "Occurrence"
    assert event["recurrence"] == {"frequency": "weekly", "interval": 1,
                                   "weekdays": ["thursday"], "first_date": "2026-11-12"}


def test_get_event_describes_series_boundaries(tmp_path):
    ev = _series_event("RecurringMaster")
    ev.recurrence = Recurrence(pattern=AbsoluteMonthlyPattern(interval=2, day_of_month=12),
                               start=date(2026, 11, 12), end=date(2027, 6, 30))
    assert _get(tmp_path, ev)["recurrence"] == {
        "frequency": "monthly", "interval": 2, "day_of_month": 12,
        "first_date": "2026-11-12", "until": "2027-06-30"}

    ev = _series_event("RecurringMaster")
    ev.recurrence = Recurrence(pattern=DailyPattern(interval=1), start=date(2026, 11, 12), number=5)
    assert _get(tmp_path, ev)["recurrence"] == {
        "frequency": "daily", "interval": 1, "first_date": "2026-11-12", "count": 5}


def test_get_event_reports_a_pattern_it_cannot_write(tmp_path):
    ev = _series_event("RecurringMaster")
    ev.recurrence = Recurrence(pattern=AbsoluteYearlyPattern(day_of_month=12, month=11),
                               start=date(2026, 11, 12))
    rule = _get(tmp_path, ev)["recurrence"]
    assert rule["frequency"] == "other" and rule["pattern"]


def test_get_event_single_event_has_no_rule(tmp_path):
    event = _get(tmp_path, _series_event("Single"))
    assert "recurrence" not in event and event["type"] == "Single"


@pytest.mark.parametrize("sent", [True, False])
def test_get_event_reports_invitation_status_to_the_organizer(tmp_path, sent):
    ev = _series_event("Single")
    ev.required_attendees = [SimpleNamespace(
        mailbox=SimpleNamespace(name=None, email_address="a@corp.example"), response_type="Unknown")]
    ev.meeting_request_was_sent = sent
    assert _get(tmp_path, ev)["invitations_sent"] is sent


def test_get_event_hides_invitation_status_from_an_attendee(tmp_path):
    ev = _series_event("Single")
    ev.required_attendees = [SimpleNamespace(
        mailbox=SimpleNamespace(name=None, email_address="a@corp.example"), response_type="Accept")]
    ev.my_response_type = "Accept"
    assert "invitations_sent" not in _get(tmp_path, ev)


def test_get_event_per_item_exception_maps_to_not_found(tmp_path):
    class ErrorItemNotFound(Exception):
        pass

    account = MagicMock()
    account.fetch.return_value = iter([ErrorItemNotFound("object not found")])
    ctx = _ctx(tmp_path, account)
    alias = ctx.aliaser.alias_for("RAW-GONE", "e")
    res = _run(ctx, "get_event", {"id": alias})
    assert res["ok"] is False
    assert res["error"]["code"] == "not_found"
    assert res["error"]["hint"]


# --- slot synthesis (pure helper) ----------------------------------------------


def test_slots_overlapping_busy_blocks_merge():
    busy = {"a@x": [(_dt(9), _dt(10)), (_dt(9, 30), _dt(10, 30))]}
    slots = merge_busy_and_find_slots(busy, _dt(9), _dt(12), 30, None)
    assert slots == [(_dt(10, 30), _dt(11)), (_dt(11), _dt(11, 30)),
                     (_dt(11, 30), _dt(12))]


def test_slots_working_hours_clipping():
    slots = merge_busy_and_find_slots({"a@x": []}, _dt(8), _dt(10), 30,
                                      (dtime(9), dtime(17)))
    assert slots == [(_dt(9), _dt(9, 30)), (_dt(9, 30), _dt(10))]


def test_slots_no_free_window():
    busy = {"a@x": [(_dt(9), _dt(10))]}
    assert merge_busy_and_find_slots(busy, _dt(9), _dt(10), 30, None) == []


def test_slots_multi_attendee_intersection():
    busy = {"a@x": [(_dt(9), _dt(10))], "b@x": [(_dt(10, 30), _dt(11))]}
    slots = merge_busy_and_find_slots(busy, _dt(9), _dt(11, 30), 30, None)
    assert slots == [(_dt(10), _dt(10, 30)), (_dt(11), _dt(11, 30))]


def test_slots_long_duration_and_touching_edges_are_free():
    busy = {"a@x": [(_dt(10), _dt(10, 30))]}
    slots = merge_busy_and_find_slots(busy, _dt(9), _dt(12), 60, None)
    # busy block touching a slot boundary does not block it
    assert slots == [(_dt(9), _dt(10)), (_dt(10, 30), _dt(11, 30)),
                     (_dt(11), _dt(12))]


# --- check_availability ----------------------------------------------------------


def test_check_availability_end_to_end(tmp_path):
    busy_view = SimpleNamespace(calendar_events=[
        SimpleNamespace(start=_dt(9), end=_dt(10), busy_type="Busy"),
        SimpleNamespace(start=_dt(13), end=_dt(14), busy_type="Free"),
    ])
    free_view = SimpleNamespace(calendar_events=[])
    account = MagicMock()
    account.protocol.get_free_busy_info.return_value = iter([busy_view, free_view])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "check_availability", {
        "attendees": ["a@corp.example", "b@corp.example"],
        "start": "2026-06-15T09:00", "end": "2026-06-15T11:00",
    })
    assert res["ok"] is True
    kwargs = account.protocol.get_free_busy_info.call_args.kwargs
    assert kwargs["accounts"] == [("a@corp.example", "Required", False),
                                  ("b@corp.example", "Required", False)]
    assert kwargs["start"] == _dt(9) and kwargs["end"] == _dt(11)
    assert res["slots"] == [
        {"start": "2026-06-15T10:00+03:00", "end": "2026-06-15T10:30+03:00"},
        {"start": "2026-06-15T10:30+03:00", "end": "2026-06-15T11:00+03:00"},
    ]
    per = res["per_attendee"]
    assert [e["status"] for e in per["a@corp.example"]] == ["Busy", "Free"]
    assert per["a@corp.example"][0]["start"] == "2026-06-15T09:00+03:00"
    assert per["b@corp.example"] == []


def test_check_availability_degrades_per_attendee(tmp_path):
    """One unresolvable attendee (external address, hidden calendar) gets an
    {'error': ...} entry and a warning; the call itself succeeds and slots
    are computed from the attendees that DID resolve."""
    class ErrorMailRecipientNotFound(Exception):
        pass

    good_view = SimpleNamespace(calendar_events=[
        SimpleNamespace(start=_dt(9), end=_dt(10), busy_type="Busy"),
    ])
    account = MagicMock()
    account.protocol.get_free_busy_info.return_value = iter([
        good_view, ErrorMailRecipientNotFound("no such mailbox"),
    ])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "check_availability", {
        "attendees": ["a@corp.example", "ghost@external.example"],
        "start": "2026-06-15T09:00", "end": "2026-06-15T11:00",
    })
    assert res["ok"] is True
    assert res["per_attendee"]["ghost@external.example"]["error"].startswith(
        "ErrorMailRecipientNotFound")
    assert [e["status"] for e in res["per_attendee"]["a@corp.example"]] == ["Busy"]
    assert res["slots"]  # computed from the resolvable attendee
    assert any("ghost@external.example" in w for w in res["warnings"])


def test_check_availability_all_attendees_failing_is_an_error(tmp_path):
    account = MagicMock()
    account.protocol.get_free_busy_info.return_value = iter([
        RuntimeError("boom"),
    ])
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "check_availability", {
        "attendees": ["ghost@external.example"],
        "start": "2026-06-15T09:00", "end": "2026-06-15T11:00",
    })
    assert res["ok"] is False
    assert res["error"]["code"] == "upstream_error"


# --- find_people ------------------------------------------------------------------


def _contact(raw_id, name, email, title=None, company=None, phone=None):
    return SimpleNamespace(
        id=raw_id, display_name=name,
        email_addresses=[SimpleNamespace(email=email)],
        job_title=title, company_name=company,
        phone_numbers=([SimpleNamespace(phone_number=phone)] if phone else []),
    )


def test_find_people_auto_merges_and_gal_wins_duplicates(tmp_path):
    gal_entry = (
        SimpleNamespace(name="Ahmed Al-X", email_address="ahmed@corp.example"),
        SimpleNamespace(display_name="Ahmed Al-X", job_title="Director",
                        company_name="Acme",
                        phone_numbers=[SimpleNamespace(phone_number="+966500000001")]),
    )
    account = MagicMock()
    account.protocol.resolve_names.return_value = [gal_entry]
    account.contacts.all.return_value = [
        _contact("C-RAW-1", "Ahmed (personal)", "AHMED@corp.example"),  # dup of GAL
        _contact("C-RAW-2", "Ahmed Junior", "junior@else.example",
                 title="Analyst", company="Elsewhere", phone="+966500000002"),
        _contact("C-RAW-3", "Sara", "sara@x.example"),  # no query match
    ]
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "find_people", {"query": "ahmed"})
    assert res["ok"] is True
    account.protocol.resolve_names.assert_called_once_with(
        ["ahmed"], return_full_contact_data=True)
    assert [p["email"] for p in res["items"]] == ["ahmed@corp.example",
                                                  "junior@else.example"]
    gal_person = res["items"][0]
    assert gal_person["source"] == "gal"
    assert gal_person["name"] == "Ahmed Al-X"  # GAL won the duplicate
    assert gal_person["title"] == "Director"
    assert gal_person["company"] == "Acme"
    assert gal_person["phone"] == "+966500000001"
    assert gal_person["id"] == "p1"
    contact_person = res["items"][1]
    assert contact_person["source"] == "contacts"
    assert contact_person["id"] == "p2"
    assert res["count"] == 2 and res["total_available"] == 2


def test_find_people_gal_no_results_exception_is_skipped(tmp_path):
    class ErrorNameResolutionNoResults(Exception):
        pass

    account = MagicMock()
    account.protocol.resolve_names.return_value = [
        ErrorNameResolutionNoResults("no results")]
    account.contacts.all.return_value = [
        _contact("C-RAW-9", "Ahmed Junior", "junior@else.example")]
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "find_people", {"query": "junior"})
    assert res["ok"] is True
    assert [p["source"] for p in res["items"]] == ["contacts"]


def test_find_people_source_contacts_never_hits_gal(tmp_path):
    account = MagicMock()
    account.contacts.all.return_value = [
        _contact("C-RAW-5", "Ahmed Junior", "junior@else.example")]
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "find_people", {"query": "ahmed", "source": "contacts"})
    assert [p["email"] for p in res["items"]] == ["junior@else.example"]
    account.protocol.resolve_names.assert_not_called()


# --- get_oof_settings ----------------------------------------------------------


def test_get_oof_settings_html_stripped_and_capped(tmp_path):
    account = MagicMock()
    account.oof_settings = SimpleNamespace(
        state="Scheduled",
        start=datetime(2026, 6, 20, 8, 0, tzinfo=TZ),
        end=datetime(2026, 6, 25, 17, 0, tzinfo=TZ),
        internal_reply="<p>سأكون خارج المكتب <b>حتى الخميس</b></p>",
        external_reply="<div>" + "external reply word " * 60 + "</div>",
    )
    ctx = _ctx(tmp_path, account)
    res = _run(ctx, "get_oof_settings", {})
    assert res["ok"] is True
    assert res["state"] == "scheduled"
    assert res["internal_reply_text"] == "سأكون خارج المكتب حتى الخميس"
    assert "<" not in res["external_reply_text"]
    assert len(res["external_reply_text"]) <= 500
    assert res["start"] == "2026-06-20T08:00+03:00"
    assert res["end"] == "2026-06-25T17:00+03:00"


# --- get_server_status -----------------------------------------------------------


def test_get_server_status_with_manager_none(tmp_path):
    account = MagicMock()
    ctx = _ctx(tmp_path, account)
    ctx.registry = dict(SPEC)
    ctx.counters["tool.list_events"] = 3
    ctx.aliaser.alias_for("RAW-X", "m")
    res = _run(ctx, "get_server_status", {})
    assert res["ok"] is True
    assert res["version"] == __version__
    assert res["uptime_s"] >= 0
    assert res["connection"] == {"state": "unmanaged"}
    assert res["tier"] == "draft"
    assert res["send_kill_switch"] is True  # send_enabled defaults False
    assert res["tools"] == 7
    assert res["counters"]["tool.list_events"] == 3
    assert res["alias_stats"] == {"m": 1}
    assert res["cache"]["enabled"] is True  # default posture
    assert res["cache"]["ready"] is False  # no CacheStore in this ctx


def test_get_server_status_not_cold_gated_while_connecting(tmp_path):
    class _ColdManager:
        state = "connecting"

        def status(self):
            return {"state": "connecting", "attempts": 4,
                    "last_error": "TransportError: Failed to get auth type",
                    "next_retry_in_s": 30, "last_success_age_s": None}

    account = MagicMock()
    ctx = _ctx(tmp_path, account)
    ctx.manager = _ColdManager()
    ctx.registry = dict(SPEC)
    res = _run(ctx, "get_server_status", {})
    # requires_ews=False must bypass the dispatcher's cold gate entirely
    assert res["ok"] is True
    assert res["connection"]["state"] == "connecting"
    assert res["connection"]["attempts"] == 4


def test_get_event_reports_the_reminder(tmp_path):
    def get(ev):
        account = MagicMock()
        account.fetch.return_value = iter([ev])
        ctx = _ctx(tmp_path, account)
        return _run(ctx, "get_event", {"id": ctx.aliaser.alias_for("RAW-R", "e")})["event"]

    def event(reminder_is_set):
        ev = MagicMock()
        ev.id = "RAW-R"
        ev.required_attendees = ev.optional_attendees = None
        ev.organizer = None
        ev.text_body = ""
        ev.reminder_is_set = reminder_is_set
        ev.reminder_minutes_before_start = 15
        return ev

    assert get(event(True))["reminder_minutes"] == 15
    assert get(event(False))["reminder_minutes"] is None


def test_check_availability_passes_ewstimezone_for_named_zone_and_fixed_offset(tmp_path):
    from exchangelib import EWSTimeZone

    account_named = MagicMock()
    account_named.protocol.get_free_busy_info.return_value = iter([
        SimpleNamespace(calendar_events=[]),
    ])
    ctx_named = _ctx(tmp_path / "named", account_named, ews_tz="Europe/Berlin")
    res_named = _run(ctx_named, "check_availability", {
        "attendees": ["ops@example.com"],
        "start": "2026-06-15T09:00",
        "end": "2026-06-15T11:00",
    })
    assert res_named["ok"] is True
    kw_named = account_named.protocol.get_free_busy_info.call_args.kwargs
    assert isinstance(kw_named["start"].tzinfo, EWSTimeZone)
    assert isinstance(kw_named["end"].tzinfo, EWSTimeZone)
    assert kw_named["start"].tzinfo.ms_id
    assert (kw_named["start"].hour, kw_named["end"].hour) == (9, 11)

    account_fixed = MagicMock()
    account_fixed.protocol.get_free_busy_info.return_value = iter([
        SimpleNamespace(calendar_events=[]),
    ])
    ctx_fixed = _ctx(tmp_path / "fixed", account_fixed, ews_tz="Asia/Tokyo")
    res_fixed = _run(ctx_fixed, "check_availability", {
        "attendees": ["ops@example.com"],
        "start": "2026-06-15T01:00+01:00",
        "end": "2026-06-15T03:00+01:00",
    })
    assert res_fixed["ok"] is True
    kw_fixed = account_fixed.protocol.get_free_busy_info.call_args.kwargs
    assert isinstance(kw_fixed["start"].tzinfo, EWSTimeZone)
    assert isinstance(kw_fixed["end"].tzinfo, EWSTimeZone)
    assert kw_fixed["start"].tzinfo.ms_id
    assert (kw_fixed["start"].hour, kw_fixed["end"].hour) == (9, 11)


@pytest.mark.parametrize("tz_name, expected_offset", [
    ("Asia/Tokyo", "+09:00"),
    ("America/New_York", "-04:00"),
])
def test_check_availability_treats_naive_busy_events_as_requested_zone(
    tmp_path, tz_name, expected_offset,
):
    # Free/busy CalendarEvent boundaries arrive naive from exchangelib; testing
    # two distant zones guarantees at least one differs from any host timezone.
    naive_busy = SimpleNamespace(
        start=datetime(2026, 6, 15, 9, 0),
        end=datetime(2026, 6, 15, 10, 0),
        busy_type="Busy",
    )
    assert naive_busy.start.tzinfo is None
    account = MagicMock()
    account.protocol.get_free_busy_info.return_value = iter([
        SimpleNamespace(calendar_events=[naive_busy]),
    ])
    ctx = _ctx(tmp_path, account, ews_tz=tz_name)
    res = _run(ctx, "check_availability", {
        "attendees": ["ops@example.com"],
        "start": "2026-06-15T09:00",
        "end": "2026-06-15T10:30",
    })
    assert res["ok"] is True
    assert res["per_attendee"]["ops@example.com"] == [{
        "start": f"2026-06-15T09:00{expected_offset}",
        "end": f"2026-06-15T10:00{expected_offset}",
        "status": "Busy",
    }]
    assert res["slots"] == [{
        "start": f"2026-06-15T10:00{expected_offset}",
        "end": f"2026-06-15T10:30{expected_offset}",
    }]


@pytest.mark.parametrize("kind, fail_mode", [
    ("Occurrence", "refresh"),
    ("Exception", "refresh"),
    ("Occurrence", "master_call"),
])
def test_get_event_returns_occurrence_when_master_cannot_be_loaded(
    tmp_path, kind, fail_mode,
):
    ev = _series_event(kind)
    ev.is_recurring = True
    ev.recurrence = None
    if fail_mode == "refresh":
        ev.recurring_master.return_value.refresh.side_effect = RuntimeError(
            "ErrorItemNotFound: recurring master not in attendee mailbox"
        )
    else:
        ev.recurring_master.side_effect = RuntimeError("ErrorAccessDenied")
    account = MagicMock()
    account.fetch.return_value = iter([ev])
    ctx = _ctx(tmp_path, account)
    alias = ctx.aliaser.alias_for("RAW-OCC", "e")
    res = _run(ctx, "get_event", {"id": alias})
    assert res["ok"] is True
    event = res["event"]
    assert event["recurring"] is True
    assert event["type"] == kind
    assert "recurrence" not in event

