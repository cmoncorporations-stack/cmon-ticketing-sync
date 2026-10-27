import json
from unittest.mock import MagicMock

from ticketing_sync.revenue import revenue


def test_revenue_counts_paid_tickets_and_passes_once_without_personal_data():
    shop = MagicMock(spec=["orders_by_id", "iter_tickets", "passes_path", "_iter_pages"])
    shop.passes_path = "/passes"
    shop.orders_by_id.return_value = {
        1: {"order_id": 1, "status": "paid", "amount": "50.00", "date": "2026-09-10",
            "customer": {"email": "fan@example.com"}},
        2: {"order_id": 2, "status": "paid", "amount": "300.00", "date": "2026-08-01"},
        3: {"order_id": 3, "status": "free", "amount": "0", "date": "2026-09-11"},
        4: {"order_id": 4, "status": "unbooked", "amount": "20.00", "date": "2026-09-12"},
    }
    shop.iter_tickets.return_value = [
        {"order_id": 1, "event_name": "Foxes - Lions", "event_date": "2026-10-04", "amount": "30.00",
         "status": "valid", "customer": {"email": "fan@example.com"}},
        {"order_id": 1, "event_name": "Foxes - Lions", "amount": "20.00", "status": "scanned"},
        {"order_id": 1, "event_name": "Foxes - Lions", "amount": "20.00", "status": "cancelled"},
        {"order_id": 2, "event_name": "Foxes - Lions", "amount": "0", "status": "valid", "pass_id": 9},
        {"order_id": 3, "event_name": "Foxes - Lions", "amount": "0", "status": "valid"},
        {"order_id": 4, "event_name": "Foxes - Lions", "amount": "20.00", "status": "valid"},
    ]
    shop._iter_pages.return_value = [
        {"order_id": 2, "category_name": "Abonnement", "amount": "300.00", "status": "valid",
         "form": {"firstname": "Ana", "lastname": "Fox"}},
    ]
    report = revenue(shop, {"event_lists": {"club_code": "FOXES"}})
    assert report["tickets_total"] == 50.0
    assert report["passes_total"] == 300.0
    assert report["total"] == 350.0
    assert report["by_pass_product"]["Abonnement"] == {"amount": 300.0, "count": 1}
    assert report["seats_booked_with_a_pass"]["count"] == 1
    assert report["cancelled_tickets_in_paid_orders"] == {"amount": 20.0, "count": 1}
    assert report["by_order_month"] == {"2026-08": {"amount": 300.0, "count": 1},
                                        "2026-09": {"amount": 50.0, "count": 2}}
    text = json.dumps(report)
    assert "example.com" not in text and "Ana" not in text
