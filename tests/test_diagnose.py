"""Diagnosis report: contacts named by email hash, no personal data."""
import json
from unittest.mock import MagicMock

from ticketing_sync.diagnose import diagnose, email_hash


def test_report_explains_vip_pass_held_by_someone_else_without_personal_data():
    order = {"order_id": 1, "status": "paid", "date": "2026-08-01 10:00:00",
             "customer": {"firstname": "Kim", "lastname": "Favre", "email": "boss@firm.ch"}}
    vip_pass = {"pass_id": 9, "status": "valid", "order_id": 1, "amount": 900, "code": "P9",
                "category_name": "Abonnement VIP Expérience Njørden - 3 Entrées I 2026 - 2027",
                "form": {"firstname": "Léa", "lastname": "Roy", "phone": "079 765 43 21"}}
    shop = MagicMock()
    shop.passes_path = "/passes"
    shop.orders_by_id.return_value = {1: order}
    shop._iter_pages.return_value = [vip_pass]
    shop.get_events.return_value = [{"name": "Pully Lausanne Foxes vs BBC Nyon - 25.10.26",
                                     "date": "2026-10-25 17:30:00", "start": "2026-10-25 17:30:00"}]
    shop.get_orders.return_value = []
    shop.get_passes.return_value = [{"order_id": 1, "date": "2026-08-01 10:00:00", "status": "paid",
                                     "pass_type": vip_pass["category_name"],
                                     "category_name": vip_pass["category_name"], "amount": 900,
                                     "code": "P9", "holder": vip_pass["form"]}]
    shop.shops = {}
    report = diagnose(shop, {"event_lists": {"club_keywords": ["foxes"]}}, [email_hash("Boss@Firm.ch ")])

    contact = report["contacts"][0]
    assert contact["passes_bought"][0]["outcome"].startswith("contact = détenteur, par téléphone")
    assert contact["rows_synced_under_this_email"] == [] and contact["in_vip_list"] is False
    assert report["vip_passes"]["total"] == 1
    assert report["events"][0]["start_read"] == "2026-10-25T17:30:00+01:00"
    text = json.dumps(report, ensure_ascii=False)
    for secret in ("boss@firm.ch", "Kim", "Favre", "Léa", "Roy", "079"):
        assert secret not in text
