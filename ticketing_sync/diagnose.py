"""Read-only diagnosis of how the sync sees given contacts and events.

Contacts are named by a hash of their email (``email_hash``), never by the
email itself, so the report can sit in a CI log: it shows products, zones,
tariffs, statuses and counts, never names, emails or phone numbers.

    DIAG_HASHES=<hash>,<hash> python -m ticketing_sync --club clubs/foxes.json --diagnose
"""
import hashlib
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from ticketing_sync.event_lists import EventResolver, classify_event, is_vip, is_vip_season_ticket
from ticketing_sync.infomaniak import (
    _first,
    _is_cancelled,
    _order_kept,
    _same_person,
    has_season_ticket_line,
    normalize_order,
)
from ticketing_sync.schedule import DEFAULT_TIMEZONE, parse_event_datetime


def email_hash(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in str(email):
        return None
    return hashlib.sha256(str(email).strip().lower().encode()).hexdigest()[:16]


def _shops(ticketing) -> List[Any]:
    return list(getattr(ticketing, "shops", {}).values()) or [ticketing]


def _raw_passes(shop) -> List[Dict[str, Any]]:
    if not getattr(shop, "passes_path", None):
        return []
    return list(shop._iter_pages(shop.passes_path, "passes"))


def _pass_view(item: Dict[str, Any], order: Dict[str, Any]) -> Dict[str, Any]:
    customer = order.get("customer") or {}
    form = {k: v for k, v in (item.get("form") or {}).items() if v not in (None, "")}
    if _is_cancelled(item.get("status")):
        outcome = "ignoré : pass annulé"
    elif not _order_kept(order):
        outcome = f"ignoré : commande {order.get('status')}"
    elif not (form.get("firstname") or form.get("lastname")):
        outcome = "contact = acheteur (formulaire vide)"
    elif _same_person(form, customer):
        outcome = "contact = acheteur (même nom)"
    elif form.get("phone"):
        outcome = "contact = détenteur, par téléphone (nom différent de l'acheteur)"
    else:
        outcome = "ignoré : détenteur sans email ni téléphone"
    return {"product": item.get("category_name"), "pass_status": item.get("status"),
            "order_status": order.get("status") or "commande introuvable", "outcome": outcome,
            "order_found": bool(order)}


def diagnose(ticketing, config: Dict[str, Any], hashes: Iterable[str]) -> Dict[str, Any]:
    hashes = [h.strip().lower() for h in hashes if h.strip()]
    event_config = config.get("event_lists") or {}
    tz = ZoneInfo((config.get("schedule") or {}).get("timezone") or DEFAULT_TIMEZONE)

    events = ticketing.get_events()
    report: Dict[str, Any] = {"events": [
        {"name": e.get("name"), "date": e.get("date"), "start": e.get("start"),
         "start_read": (lambda d: d.isoformat() if d else None)(
             parse_event_datetime(e.get("date") or e.get("start") or e.get("start_date"), tz)),
         "type": classify_event(e.get("name") or "", event_config)}
        for e in events]}

    orders = ticketing.get_orders()
    passes = ticketing.get_passes()
    resolve = EventResolver(events, event_config)
    rows = [r for r in (normalize_order(o) for o in orders + passes) if r]

    raw = []
    for shop in _shops(ticketing):
        by_id = shop.orders_by_id()
        for item in _raw_passes(shop):
            order = by_id.get(_first(item, "order_id")) or {}
            raw.append((item, order))

    vip = [_pass_view(item, order) for item, order in raw if is_vip(item.get("category_name"))]
    report["vip_passes"] = {
        "total": len(vip),
        "by_product_and_outcome": [{"product": p, "outcome": o, "count": n}
                                   for (p, o), n in sorted(Counter((v["product"], v["outcome"])
                                                                   for v in vip).items())],
    }

    contacts = []
    for index, h in enumerate(hashes, 1):
        buyer_orders = [o for o in orders if email_hash((o.get("customer") or {}).get("email")) == h]
        bought_passes = [_pass_view(item, order) for item, order in raw
                         if email_hash((order.get("customer") or {}).get("email")) == h]
        own_rows = [r for r in rows if email_hash(r.get("email")) == h]
        row_views = []
        for r in own_rows:
            name, day, event_type = resolve(r)
            row_views.append({
                "event": name, "type": event_type, "date": day.isoformat() if day else None,
                "zones": sorted({line["place"] for line in r["lines"] if line["place"]}),
                "tariffs": sorted({line["tariff"] for line in r["lines"] if line["tariff"]}),
                "line_kinds": dict(Counter(line["kind"] for line in r["lines"])),
                "season_ticket_seats": has_season_ticket_line(r),
                "vip_season_ticket": is_vip_season_ticket(r, name, event_type),
            })
        contacts.append({
            "contact": f"#{index}", "email_hash": h,
            "found_as_buyer_orders": len(buyer_orders),
            "order_statuses": dict(Counter(o.get("status") for o in buyer_orders)),
            "passes_bought": bought_passes,
            "rows_synced_under_this_email": row_views,
            "in_vip_list": any(v["vip_season_ticket"] for v in row_views),
        })
    report["contacts"] = contacts
    return report
