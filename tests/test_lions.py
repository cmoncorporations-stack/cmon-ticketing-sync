"""Lions de Genève: club config, and the fan + VIP shops synced together.
Event names follow the Infomaniak naming seen at the Foxes; check them
against the first Lions dry run. HTTP fully mocked."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from ticketing_sync.__main__ import build_ticketing, load_club_config, main, sync_kwargs
from ticketing_sync.event_lists import build_list_name, classify_event
from ticketing_sync.infomaniak import InfomaniakTicketingService, MultiShopTicketing

CLUB = "clubs/lions.json"

# Real names from the Lions shops (dry run of 11.10.2026).
LIONS_EVENT_NAMES = {
    "Lions de Genève vs. Fribourg Olympic | SBL 2026-27": ("match", "FOB"),
    "Lions de Genève vs. Pully Lausanne Foxes | SBL 2026-27": ("match", "FOXES"),
    "Lions de Genève vs. GC Zürich | SBL 2026-27": ("match", "GCZ"),
    "Lions de Genève vs. Union Neuchâtel | SBL 2026-27": ("match", "UNI"),
    "Lions de Genève vs. Spinelli Massagno | SBL 2026-27": ("match", "MAS"),
    "Lions de Genève vs. Lugano Tigers | SBL 2026-27": ("match", "LUG"),
    "Lions de Genève vs. BBC Nyon | SBL 2026-27": ("match", "NYO"),
    "Lions de Genève vs. SL Benfica | FIBA Europe Cup": ("match", "SLB"),
    "Lions de Genève vs. Yukatel Denizli Basket | FIBA Europe Cup": ("match", "DEN"),
    "Lions de Genève vs. Bakken Bears | FIBA Europe Cup": ("match", "BAK"),
    "Lions de Genève vs. BC Prievidza | FIBA Europe Cup": ("match", "PRI"),
    "Lions de Genève vs. Landau Lions | FIBA Europe Cup": ("match", "LAN"),
    "VIP | GC Zürich  vs.  Lions de Genève | SBL 2026-27": ("match", "GCZ"),
    "VIP | A++ Massagno vs. Lions de Genève | SBL 2026-27": ("match", "MAS"),
    "VIP | Lugano Tigers Lions vs. Lions de Genève | SBL 2026-27": ("match", "LUG"),
    "Abonnement Adulte | Saison 2026 - 2027": ("abonnement", None),
    "Pass Europe 5 matchs | Adulte | Centrales": ("abonnement", None),
    "Abonnement VIP – Domaine du Paradis": ("abonnement", None),
}


def test_lions_config_classifies_events_and_codes_opponents():
    config = load_club_config(CLUB)
    event_config = sync_kwargs(config)["event_lists"]
    for name, (expected_type, expected_code) in LIONS_EVENT_NAMES.items():
        assert classify_event(name, event_config) == expected_type, name
        if expected_code:
            list_name, warning = build_list_name("LIONS", name, date(2026, 10, 18), event_config["opponent_codes"],
                                                 event_config["club_keywords"], "match")
            assert warning is None, name
            assert list_name == f"CMON-LIONS-{expected_code}-20261018", name


def test_lions_matches_need_club_keywords():
    assert classify_event("Lions de Genève vs Fribourg Olympic", {}) == "autre"
    # "lions" alone would also match opponents named Lions (Landau Lions).
    assert classify_event("Lions de Genève vs. Landau Lions | FIBA Europe Cup",
                          {"club_keywords": ["lions"]}) == "autre"


def _shop(orders, events=(), passes=()):
    shop = MagicMock(spec=InfomaniakTicketingService)
    shop.passes_path = "/passes"
    shop.base_url = "https://etickets.infomaniak.com/api/shop"
    shop.get_orders.return_value = list(orders)
    shop.get_events.return_value = list(events)
    shop.get_passes.return_value = list(passes)
    return shop


def _order(order_id, event_id, amount):
    return {"order_id": order_id, "event_id": event_id, "status": "paid", "amount": amount,
            "customer": {"email": "fan@example.ch"}}


def test_multi_shop_merges_and_counts_shared_orders_once():
    fan = _shop([_order(1, 10, 30.0), _order(2, 11, 20.0)], events=[{"id": 10}, {"id": 11}],
                passes=[{"code": "P1", "order_id": 5}])
    vip = _shop([_order(2, 11, 20.0), _order(900, 10, 250.0)], events=[{"id": 10}],
                passes=[{"code": "P1", "order_id": 5}, {"code": "V1", "order_id": 901}])
    multi = MultiShopTicketing({"PRINCIPALE": fan, "VIP": vip})
    assert [o["order_id"] for o in multi.get_orders()] == [1, 2, 900]
    assert len(multi.get_events()) == 2
    assert [p["code"] for p in multi.get_passes()] == ["P1", "V1"]
    assert multi.duplicates_skipped == 3
    assert multi.passes_path == "/passes"


def test_multi_shop_connection_fails_if_one_shop_fails():
    fan, vip = _shop([]), _shop([])
    fan.test_connection.return_value = {"success": True, "events_count": 4}
    vip.test_connection.return_value = {"success": False, "error": "401"}
    result = MultiShopTicketing({"PRINCIPALE": fan, "VIP": vip}).test_connection()
    assert result["success"] is False
    assert result["shops"]["PRINCIPALE"]["events_count"] == 4


def test_build_ticketing_reads_the_vip_shop(monkeypatch):
    monkeypatch.setenv("INFOMANIAK_API_KEY", "fan-key")
    monkeypatch.setenv("INFOMANIAK_TOKEN", "token")
    monkeypatch.setenv("INFOMANIAK_API_KEY_VIP", "vip-key")
    monkeypatch.delenv("INFOMANIAK_TOKEN_VIP", raising=False)
    ticketing = build_ticketing(load_club_config(CLUB))
    assert isinstance(ticketing, MultiShopTicketing)
    assert ticketing.shops["VIP"].api_key == "vip-key"
    assert ticketing.shops["VIP"].token == "token"           # main token by default


def test_missing_vip_key_fails(monkeypatch, capsys):
    monkeypatch.setenv("INFOMANIAK_API_KEY", "fan-key")
    monkeypatch.delenv("INFOMANIAK_API_KEY_VIP", raising=False)
    with pytest.raises(ValueError):
        build_ticketing(load_club_config(CLUB))
    assert main(["--club", CLUB, "--dry-run"]) == 1
    assert "INFOMANIAK_API_KEY_VIP" in capsys.readouterr().err


def test_single_shop_clubs_are_unchanged(monkeypatch):
    monkeypatch.setenv("INFOMANIAK_API_KEY", "k")
    monkeypatch.setenv("INFOMANIAK_API_KEY_VIP", "ignored")
    assert isinstance(build_ticketing(load_club_config("clubs/foxes.json")), InfomaniakTicketingService)


def test_dry_run_report_lists_shops(monkeypatch, capsys):
    monkeypatch.setenv("INFOMANIAK_API_KEY", "fan-key")
    monkeypatch.setenv("INFOMANIAK_API_KEY_VIP", "vip-key")
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    with patch("ticketing_sync.__main__.sync_infomaniak_to_brevo", return_value={"success": True}):
        assert main(["--club", CLUB, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert '"PRINCIPALE"' in out and '"VIP"' in out and "vip-key" not in out


# Zones and tariffs returned by the Lions shops (schema run of 11.10.2026).
LIONS_TARIFFS = {
    "Plein tarif": "Plein", "Adulte": "Plein", "PASS EUROPE | ADULTE | LATERALES": "Plein",
    "Abonnement Adulte | Saison 26 - 27": "Plein", "Adultes (18+)": "Plein",
    "Enfant (-16 ans)": "Enfant", "Tarif enfant (10 ans et moins)": "Enfant",
    "Tarif Ados (11 - 17 ans)": "Enfant", "Enfants (11-17 ans)": "Enfant",
    "Tarif AVS/AI": "Réduit", "Etudiant": "Réduit", "AVS / AI / CHOMAGE": "Réduit",
    "EUROPE | AVS/AI/CHÔM | LATÉRALES": "Réduit",
    "Abonnement Sponsor VIP": "VIP", "Abonnement Sponsors - Saison 26 - 27": "VIP",
}
LIONS_ZONES = {
    "BUSINESS SEAT": "Business Seats", "BUSINESS SEATS": "Business Seats", "LATÉRALES": "Latérales",
    "CENTRALES": "Centrales", "VIRAGES": "Virages", "Zone 1": "Zone 1",
    "Espace Paradis": "Espace Paradis", "Courtside Family": "Courtside Family",
    "Lounge Florimont": "Lounge Florimont",
}


@pytest.mark.parametrize("mapping_key,labels", [("tarif_mapping", LIONS_TARIFFS), ("place_mapping", LIONS_ZONES)])
def test_every_lions_tariff_and_zone_has_a_brevo_option(mapping_key, labels):
    from ticketing_sync.sync import CategoryWriter

    writer = CategoryWriter([], "X", load_club_config(CLUB)["ticket_attributes"][mapping_key])
    for label, option in labels.items():
        assert writer._rule(label) == option, label


def test_lions_lists_go_to_existing_brevo_folders():
    from ticketing_sync.event_lists import find_folder

    brevo_folders = [{"id": 195, "name": "Cmon Sports | Billetterie 26-27"}, {"id": 192, "name": "Abonnement 26-27"},
                     {"id": 136, "name": "Billetterie 25-26"}, {"id": 122, "name": "Abonnement 25-26"},
                     {"id": 60, "name": "Members & Staff"}, {"id": 56, "name": "Business engagement"}]
    folders = load_club_config(CLUB)["event_lists"]["folders"]
    assert find_folder(brevo_folders, folders["match"])["id"] == 195
    assert find_folder(brevo_folders, folders["abonnement"])["id"] == 192
    assert find_folder(brevo_folders, "Business Engagement")["id"] == 56
    assert find_folder(brevo_folders, "Members & Staff")["id"] == 60
