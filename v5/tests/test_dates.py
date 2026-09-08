"""Regression coverage for parse_when's timezone type.

check_availability (and any other tool that feeds a parse_when() result into
an exchangelib EWS call, e.g. get_free_busy_info) needs the returned
datetime's tzinfo to be an exchangelib EWSTimeZone, not a bare
zoneinfo.ZoneInfo - EWS's GetUserAvailability requires the Microsoft
timezone ID exchangelib reads via `.ms_id`, which only EWSTimeZone
provides. A bare ZoneInfo has no `.ms_id` and blows up with
AttributeError on every call, regardless of arguments, since this
resolution happens before any per-call filtering.
"""

from exchangelib import EWSTimeZone

from ewsmcp.dates import parse_when

TZ_NAME = "Europe/Moscow"


def test_parse_when_today_has_ews_timezone():
    dt = parse_when("today", "start", TZ_NAME)
    assert isinstance(dt.tzinfo, EWSTimeZone)
    assert dt.tzinfo.ms_id == "Russian Standard Time"


def test_parse_when_relative_has_ews_timezone():
    dt = parse_when("+7d", "end", TZ_NAME)
    assert isinstance(dt.tzinfo, EWSTimeZone)
    assert hasattr(dt.tzinfo, "ms_id")


def test_parse_when_naive_iso_gets_ews_timezone_attached():
    dt = parse_when("2026-01-15T09:00:00", "start", TZ_NAME)
    assert isinstance(dt.tzinfo, EWSTimeZone)
    assert dt.tzinfo.ms_id == "Russian Standard Time"
