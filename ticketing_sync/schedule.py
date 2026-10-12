"""When to sync: H-24 before each match and H+24 after it.

The scheduler (GitHub Actions) wakes up every hour and asks ``due_runs``
whether a match crosses one of those two marks within the last
``lookback`` window. GitHub skips many hourly runs (5 runs in 25 hours
were seen on 10-11.10.2026), so the window is 12 hours: the next run that
does happen catches a mark a skipped run missed. Every run inside the
window syncs again; the sync is idempotent, so this is harmless.

* H-24: buyers of the next match are in Brevo the day before (pre-match
  campaigns, matchday SMS).
* H+24: scans are final, so attendance, DERNIER_MATCH and DERNIER_IMPORT
  (post-match automations) are up to date the day after.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from ticketing_sync.event_lists import classify_event, date_override

DEFAULT_OFFSET_HOURS = 24
DEFAULT_LOOKBACK_MINUTES = 720
DEFAULT_TIMEZONE = "Europe/Zurich"
DEFAULT_EVENT_TYPES = ("match",)

_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                 "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M")


def parse_event_datetime(value: Any, tz: ZoneInfo) -> Optional[datetime]:
    """Event start as an aware datetime. Naive values are local club time
    (Infomaniak shows local times); a date without time is not usable."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None or (len(text) <= 10 and parsed.hour == 0 and parsed.minute == 0):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def due_runs(events: Iterable[Dict[str, Any]], config: Dict[str, Any],
             now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Marks (H-24 / H+24) reached in ``(now - lookback, now]``.

    ``config`` is the club's ``schedule`` section (``offset_hours``,
    ``lookback_minutes``, ``timezone``, ``event_types``) merged with its
    ``event_lists`` keys used to classify events (``club_keywords``,
    ``event_types``...).
    """
    tz = ZoneInfo(config.get("timezone") or DEFAULT_TIMEZONE)
    now = now or datetime.now(timezone.utc)
    offset = timedelta(hours=float(config.get("offset_hours") or DEFAULT_OFFSET_HOURS))
    lookback = timedelta(minutes=float(config.get("lookback_minutes") or DEFAULT_LOOKBACK_MINUTES))
    wanted = set(config.get("sync_event_types") or DEFAULT_EVENT_TYPES)
    classify_config = config.get("event_lists") or {}

    due = []
    for event in events:
        name = event.get("name") or event.get("title")
        start = parse_event_datetime(event.get("date") or event.get("start") or event.get("start_date"), tz)
        if not name or not start or classify_event(name, classify_config) not in wanted:
            continue
        corrected = date_override(name, classify_config)
        if corrected:
            start = start.replace(year=corrected.year, month=corrected.month, day=corrected.day)
        for label, mark in (("H-24", start - offset), ("H+24", start + offset)):
            if now - lookback < mark <= now:
                due.append({"event": name, "start": start.isoformat(), "mark": label})
    return due
