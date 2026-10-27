"""Read-only ticketing revenue to date, as Infomaniak returns the amounts.

Only totals and counts: no buyer, no email, no name.

    python -m ticketing_sync --club clubs/foxes.json --revenue

Revenue counts orders whose status is ``paid`` (``free`` orders count 0):
tickets that are not cancelled, plus season tickets / member cards
(``/passes``) that are not cancelled. Tickets booked with a pass are shown
apart so they are not counted twice. The order totals (``/orders``) are
given as a cross-check.
"""
from collections import defaultdict
from typing import Any, Dict, List

from ticketing_sync.event_lists import classify_event
from ticketing_sync.infomaniak import _first, _fold, _is_cancelled, _number


def _shops(ticketing) -> List[Any]:
    return list(getattr(ticketing, "shops", {}).values()) or [ticketing]


def _month(value: Any) -> str:
    return str(value or "")[:7] or "inconnu"


def _bucket() -> Dict[str, float]:
    return {"amount": 0.0, "count": 0}


def _add(bucket: Dict[str, float], amount: float) -> None:
    bucket["amount"] = round(bucket["amount"] + amount, 2)
    bucket["count"] += 1


def revenue(ticketing, config: Dict[str, Any]) -> Dict[str, Any]:
    event_config = config.get("event_lists") or {}
    by_status: Dict[str, Dict[str, float]] = defaultdict(_bucket)
    by_event: Dict[str, Dict[str, Any]] = {}
    by_type: Dict[str, Dict[str, float]] = defaultdict(_bucket)
    by_pass: Dict[str, Dict[str, float]] = defaultdict(_bucket)
    by_month: Dict[str, Dict[str, float]] = defaultdict(_bucket)
    pass_seats = _bucket()
    cancelled = _bucket()
    tickets_total = passes_total = 0.0

    for shop in _shops(ticketing):
        orders = shop.orders_by_id()
        for order in orders.values():
            _add(by_status[_fold(order.get("status") or "inconnu")], _number(order.get("amount")) or 0.0)

        def paid(order_id) -> Any:
            order = orders.get(order_id)
            return order if order and _fold(order.get("status") or "") == "paid" else None

        for ticket in shop.iter_tickets():
            amount = _number(ticket.get("amount")) or 0.0
            order = paid(_first(ticket, "order_id"))
            if not order:
                continue
            if _is_cancelled(ticket.get("status")):
                _add(cancelled, amount)
                continue
            if ticket.get("pass_id"):
                _add(pass_seats, amount)
                continue
            name = ticket.get("event_name") or "inconnu"
            event_type = classify_event(name, event_config)
            entry = by_event.setdefault(name, {"date": ticket.get("event_date"), "type": event_type,
                                               "amount": 0.0, "count": 0})
            _add(entry, amount)
            _add(by_type[event_type], amount)
            _add(by_month[_month(order.get("date"))], amount)
            tickets_total += amount

        if getattr(shop, "passes_path", None):
            for item in shop._iter_pages(shop.passes_path, "passes"):
                order = paid(_first(item, "order_id"))
                if not order or _is_cancelled(item.get("status")):
                    continue
                amount = _number(item.get("amount")) or 0.0
                _add(by_pass[item.get("category_name") or "inconnu"], amount)
                _add(by_month[_month(order.get("date"))], amount)
                passes_total += amount

    events = sorted(({"event": k, **v} for k, v in by_event.items()), key=lambda e: str(e["date"] or ""))
    return {
        "currency": "CHF",
        "total": round(tickets_total + passes_total, 2),
        "tickets_total": round(tickets_total, 2),
        "passes_total": round(passes_total, 2),
        "by_type": dict(sorted(by_type.items())),
        "by_event": events,
        "by_pass_product": dict(sorted(by_pass.items())),
        "by_order_month": dict(sorted(by_month.items())),
        "seats_booked_with_a_pass": pass_seats,
        "cancelled_tickets_in_paid_orders": cancelled,
        "cross_check_orders_by_status": dict(sorted(by_status.items())),
    }
