"""H-24 / H+24 scheduling around matches."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ticketing_sync.schedule import due_runs, parse_event_datetime

ZURICH = ZoneInfo("Europe/Zurich")
CONFIG = {"timezone": "Europe/Zurich", "event_lists": {"club_keywords": ["foxes", "pully"]}}
LIONS = {"name": "Pully Lausanne Foxes vs Lions de Genève - 10.10.26 - championnat SBL",
         "date": "2026-10-10 19:30:00"}          # local time, 17:30 UTC
CAMP = {"name": "Camp Foxes - 12 au 16 octobre 2026", "date": "2026-10-12 09:00:00"}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_parse_event_datetime_local_and_aware():
    assert parse_event_datetime("2026-10-10 19:30:00", ZURICH) == datetime(2026, 10, 10, 19, 30, tzinfo=ZURICH)
    assert parse_event_datetime("2026-10-10T17:30:00Z", ZURICH) == utc(2026, 10, 10, 17, 30)
    assert parse_event_datetime("2026-10-10", ZURICH) is None          # no time: no mark
    assert parse_event_datetime(None, ZURICH) is None


def test_h_minus_24_and_h_plus_24():
    # H-24 = 2026-10-09 17:30 UTC, H+24 = 2026-10-11 17:30 UTC
    assert due_runs([LIONS], CONFIG, utc(2026, 10, 9, 18, 7)) == [
        {"event": LIONS["name"], "start": "2026-10-10T19:30:00+02:00", "mark": "H-24"}]
    assert [d["mark"] for d in due_runs([LIONS], CONFIG, utc(2026, 10, 11, 18, 7))] == ["H+24"]


def test_nothing_due_outside_the_windows():
    for now in (utc(2026, 10, 9, 17, 0), utc(2026, 10, 9, 19, 30), utc(2026, 10, 10, 18, 0),
                utc(2026, 10, 11, 17, 0), utc(2026, 10, 11, 19, 30)):
        assert due_runs([LIONS], CONFIG, now) == [], now


def test_delayed_hourly_run_still_catches_the_mark():
    # Lookback is 90 min: a run 80 min late still sees the H+24 mark.
    assert [d["mark"] for d in due_runs([LIONS], CONFIG, utc(2026, 10, 11, 18, 50))] == ["H+24"]


def test_only_matches_trigger_by_default():
    camp_h24 = utc(2026, 10, 11, 7, 30)                  # 09:00 local - 24 h = 07:00 UTC
    assert due_runs([CAMP], CONFIG, camp_h24) == []
    config = {**CONFIG, "sync_event_types": ["match", "camp"]}
    assert [d["mark"] for d in due_runs([CAMP], config, camp_h24)] == ["H-24"]


def test_custom_offset():
    config = {**CONFIG, "offset_hours": 48}
    assert [d["mark"] for d in due_runs([LIONS], config, utc(2026, 10, 8, 18, 0))] == ["H-24"]
