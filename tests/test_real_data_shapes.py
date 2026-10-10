"""Sync behaviour on data shaped like the Foxes Infomaniak exports
(Billets / Abonnements / Camps sheets): line types, scan status, member and
season-ticket seats, free orders, passes, placeholders. HTTP fully mocked."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from ticketing_sync.event_lists import find_opponent, is_placeholder
from ticketing_sync.infomaniak import (
    InfomaniakTicketingService,
    counts_for_event,
    line_kind,
    normalize_order,
    normalize_phone,
)
from ticketing_sync.sync import sync_infomaniak_to_brevo

TODAY = date(2026, 10, 12)
CONFIG = {"club_code": "FOXES", "season": "2026-2027",
          "opponent_codes": {"Lions de Genève": "LIONS", "Fribourg Olympic": "FOB"}}
LIONS = {"name": "Pully Lausanne Foxes vs Lions de Genève - 10.10.26 - championnat SBL",
         "date": "2026-10-10 19:30:00"}
FRIBOURG = {"name": "Pully Lausanne Foxes vs Fribourg Olympic - 06.12.26 - championnat SBL",
            "date": "2026-12-06 17:30:00"}


def _line(kind, tariff, zone="Places assises standards", status="Billet valide"):
    return {"line_type": kind, "tariff": tariff, "zone": zone, "ticket_status": status}


def _order(oid, email, event, lines, total=0, status="Paid", created="2026-10-01 12:00:00"):
    return {"id": oid, "created_at": created, "status": status, "total": total,
            "customer": {"email": email}, "event": event, "tickets": lines}


def _brevo(defs=()):
    brevo = MagicMock()
    brevo.get_contact_index.return_value = {}
    brevo.get_contact_attributes.return_value = list(defs)
    brevo.import_contacts.return_value = {"processId": 1}
    brevo.get_all_folders.return_value = []
    brevo.create_folder.side_effect = lambda name: {"id": len(name)}
    brevo.create_list.side_effect = lambda name, folder_id: {"id": hash(name) % 1000}
    return brevo


def _contacts(brevo):
    return {c["email"]: c["attributes"] for c in brevo.import_contacts.call_args_list[0].args[0]}


def test_line_kinds_from_export_values():
    assert line_kind("Billet", "Adulte") == "billet"
    assert line_kind("Billet", "Parents de membres") == "billet"
    assert line_kind("Place abonné", "Carte membre") == "membre"
    assert line_kind("Place abonné", "Carte membre - Pully Lausanne Foxes") == "membre"
    assert line_kind("Place abonné", "(EARLY BIRD) Abonnement saison régulière - Adulte I 2026 - 2027") == "abonne"
    assert line_kind(None, "Pass ENBL I 2026 - 2027") == "abonne"


def test_scan_status_and_free_amounts():
    row = normalize_order(_order(1, "a@x.ch", LIONS, [_line("Billet", "Adulte", status="Billet scanné")],
                                 total=20, status="Free"))
    assert row["lines"][0]["scanned"] is True
    assert row["amount"] == 0.0
    row = normalize_order(_order(2, "a@x.ch", LIONS, [_line("Billet", "Adulte")], total=20))
    assert row["lines"][0]["scanned"] is False and row["amount"] == 20.0


def test_attendance_rules():
    def row(lines):
        return normalize_order(_order(1, "a@x.ch", LIONS, lines))

    member = row([_line("Place abonné", "Carte membre")])
    assert not counts_for_event(member, played=True)
    assert not counts_for_event(member, played=False)
    assert counts_for_event(row([_line("Place abonné", "Carte membre", status="Billet scanné")]), played=True)
    bought = row([_line("Billet", "Adulte")])
    assert counts_for_event(bought, played=False)          # ticket for an upcoming match
    assert not counts_for_event(bought, played=True)       # played, never scanned: no-show
    unknown = normalize_order({"id": 9, "customer": {"email": "a@x.ch"}, "event": LIONS,
                               "tickets": [{"tariff": "Adulte"}]})
    assert counts_for_event(unknown, played=True)          # no scan data: purchase counts


def test_real_names_placeholders_and_opponent_variants():
    assert find_opponent("Pully Lausanne Foxes vs A DEFINIR- 03.02.27 - ENBL", ("foxes",)) == "A DEFINIR"
    assert is_placeholder("Pully Lausanne Foxes vs Lugano Tigers - A DEFINIR - championnat SBL")
    assert find_opponent("Pully Lausanne Foxes vs BBC Monthey-Chablais - 03.10.26 - championnat SBL",
                         ("foxes",)) == "BBC Monthey-Chablais"
    assert find_opponent("1/4 de finale GAME 1 - Déplacement à Neuchâtel en car", ("foxes",)) is None


def test_sync_on_export_shaped_orders():
    orders = [
        # Paying fan, scanned at the Lions game.
        _order(1, "fan@x.ch", LIONS, [_line("Billet", "Étudiant / AVS / AI", status="Billet scanné")], total=15),
        # Member: seats generated for every match, never scanned -> nothing match-related.
        _order(2, "member@x.ch", LIONS, [_line("Place abonné", "Carte membre", zone="Places assises")]),
        _order(3, "member@x.ch", FRIBOURG, [_line("Place abonné", "Carte membre", zone="Places assises")]),
        # Season-ticket holder seen through "Place abonné" seats -> REGULAR.
        _order(4, "abo@x.ch", FRIBOURG, [_line("Place abonné", "Abonnement saison régulière - Adulte I 2026 - 2027",
                                              zone="Places assises")]),
        # Not scheduled yet: no list.
        _order(5, "fan@x.ch", {"name": "Pully Lausanne Foxes vs Lugano Tigers - A DEFINIR - championnat SBL",
                               "date": "2026-11-01 19:00:00"}, [_line("Billet", "Adulte")], total=20),
    ]
    statut = {"name": "TICKET_STATUT", "category": "category",
              "enumeration": [{"value": 1, "label": "NEW"}, {"value": 3, "label": "REGULAR"}]}
    tarif = {"name": "CATEGORIE_TARIF", "category": "category",
             "enumeration": [{"value": 1, "label": "Plein"}, {"value": 2, "label": "Réduit"}]}
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    ticketing.get_events.return_value = []
    ticketing.passes_path = None
    brevo = _brevo([statut, tarif])
    result = sync_infomaniak_to_brevo(
        ticketing, brevo, event_lists=CONFIG, today=TODAY,
        ticket_attributes={"tarif_mapping": {"etudiant": "Réduit", "avs": "Réduit", "adulte": "Plein"}})
    contacts = _contacts(brevo)

    fan, member, abo = contacts["fan@x.ch"], contacts["member@x.ch"], contacts["abo@x.ch"]
    assert fan["DERNIER_MATCH"] == "2026-10-10"
    assert fan["TICKETING_MATCHES_ATTENDED"] == 1 and fan["TICKETING_MATCHES"] == 2
    assert fan["CATEGORIE_TARIF"] == 1   # latest bought ticket: Lugano (upcoming), Adulte -> Plein
    assert "DERNIER_MATCH" not in member and "TICKET_STATUT" not in member
    assert member["TICKETING_MATCHES_ATTENDED"] == 0
    assert abo["TICKET_STATUT"] == 3 and abo["CATEGORIE_TARIF"] == 1

    lists = result["event_lists"]["lists"]
    assert lists["CMON-FOXES-LIONS-20261010"]["contacts"] == 1          # only the scanned fan
    assert "CMON-FOXES-FOB-20261206" not in lists                       # only generated seats so far
    assert lists["CMON-Abonnes-2026-2027"]["folder"] == "Abonnement"
    assert lists["CMON-Abonnes-2026-2027"]["contacts"] == 1
    assert not any(name.startswith("CMON-FOXES-LUG") for name in lists)
    assert any("A DEFINIR" in w for w in result["warnings"])


def test_passes_endpoint_brings_holder_details():
    ticketing = InfomaniakTicketingService("k", passes_path="/passes")
    ticketing.get_orders = MagicMock(return_value=[])
    ticketing.get_events = MagicMock(return_value=[])
    ticketing.get_passes = MagicMock(return_value=[
        {"code": "VKW49K", "pass_type": "(EARLY BIRD) Abonnement saison régulière - Adulte I 2026 - 2027",
         "net_amount": 175, "payment_method": "Twint", "created_at": "2026-06-30 08:15:44",
         "holder": {"email": "holder@x.ch", "firstname": "Alex", "lastname": "Muller",
                    "birthdate": "1990-04-02 00:00:00", "zipcode": "1004 Lausanne", "phone": 41791234567.0},
         "buyer": {"email": "parent@x.ch"}},
        {"code": "AB12CD", "pass_type": "Abonnement VIP Expérience Njørden - Individuel I 2026 - 2027",
         "net_amount": 1500, "payment_method": "Gratuit", "created_at": "2026-07-01 10:00:00",
         "holder": {"email": "vip@x.ch"}},
    ])
    brevo = _brevo([{"name": "NPA", "type": "text"}, {"name": "BIRTHDAY", "type": "date"},
                    {"name": "TICKET_STATUT", "category": "category",
                     "enumeration": [{"value": 3, "label": "REGULAR"}]}])
    result = sync_infomaniak_to_brevo(ticketing, brevo, event_lists=CONFIG, today=TODAY)
    contacts = _contacts(brevo)

    holder = contacts["holder@x.ch"]
    assert "parent@x.ch" not in contacts                     # the holder is the fan, not the buyer
    assert holder["NPA"] == "1004" and holder["BIRTHDAY"] == "1990-04-02"
    assert holder["SMS"] == "+41791234567"
    assert holder["TICKET_STATUT"] == 3
    assert holder["TICKETING_TOTAL_SPENT"] == 175.0
    assert contacts["vip@x.ch"]["TICKETING_TOTAL_SPENT"] == 0.0   # paid "Gratuit"
    lists = result["event_lists"]["lists"]
    assert lists["CMON-Abonnes-2026-2027"]["contacts"] == 1
    assert lists["CMON-Abonnes-VIP-2026-2027"]["contacts"] == 1
    assert lists["CMON-Abonnes-VIP-2026-2027"]["folder"] == "Business Engagement"
    assert holder["TICKETING_SEASON_TICKET"].startswith("(EARLY BIRD) Abonnement saison")
    assert holder["TICKETING_PASS_CODE"] == "VKW49K"
    assert holder["TICKETING_PAYMENT_METHOD"] == "Twint"


def test_float_phone_from_export():
    assert normalize_phone(41791234567.0) == "+41791234567"


def test_vip_season_tickets_members_and_analysis_attributes():
    vip_seat = "Abonnement VIP Entreprise - TEAM FOXES I Saison 2026 - 2027"
    orders = [
        _order(1, "boss@x.ch", LIONS, [_line("Place abonné", vip_seat, zone="Zone VIP")],
               created="2026-08-01 10:00:00"),
        _order(2, "member@x.ch", LIONS, [_line("Place abonné", "Carte membre", zone="Places assises")]),
        _order(3, "member@x.ch", {"name": "Camp Foxes - 12 au 16 octobre 2026", "date": "2026-10-12 09:00:00"},
               [_line("Place abonné", "Carte membre - Pully Lausanne Foxes", zone=None)], total=200),
        _order(4, "fan@x.ch", LIONS,
               [{**_line("Billet", "VIP - billet offert", zone="Zone VIP", status="Billet scanné"),
                 "catalog_price": 50, "amount": 0},
                {**_line("Billet", "Adulte - sur place", status="Billet scanné"), "catalog_price": 20, "amount": 20}],
               total=20),
        {"id": 5, "created_at": "2026-09-01", "status": "Paid", "total": 0, "payment_method": "Twint",
         "order_number": "70510331", "customer": {"email": "partner@x.ch"},
         "event": {"name": "FOXES NIGHT 2026", "date": "2026-02-26 18:30:00"},
         "tickets": [{"tariff": "Table de 10 personnes", "zone": "FOXES NIGHT"}]},
    ]
    source = {"name": "SOURCE_ACQUISITION", "category": "category",
              "enumeration": [{"value": 1, "label": "Billetterie"}, {"value": 5, "label": "Business Engagement"}]}
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    ticketing.get_events.return_value = []
    ticketing.passes_path = None
    brevo = _brevo([source])
    config = {**CONFIG, "event_types": {"foxes night": "partenaire"}}
    result = sync_infomaniak_to_brevo(ticketing, brevo, event_lists=config, today=TODAY)
    contacts = _contacts(brevo)
    lists = result["event_lists"]["lists"]

    assert lists["CMON-Abonnes-VIP-2026-2027"]["folder"] == "Business Engagement"
    assert "CMON-Abonnes-2026-2027" not in lists                 # VIP seats are not repeated there
    assert lists["CMON-Membres-2026-2027"]["folder"] == "Members & Staff"
    assert lists["CMON-Membres-2026-2027"]["contacts"] == 1
    assert contacts["boss@x.ch"]["SOURCE_ACQUISITION"] == 5      # VIP season ticket -> Business Engagement
    assert contacts["member@x.ch"]["SOURCE_ACQUISITION"] == 1
    fan = contacts["fan@x.ch"]
    assert fan["TICKETING_FREE_TICKETS"] == 1 and fan["TICKETING_FREE_VALUE"] == 50.0
    assert fan["TICKETING_ONSITE_TICKETS"] == 1
    partner = contacts["partner@x.ch"]
    assert partner["TICKETING_PAYMENT_METHOD"] == "Twint" and partner["TICKETING_LAST_ORDER_ID"] == "70510331"
    assert partner["SOURCE_ACQUISITION"] == 5                    # Foxes Night = partner event
    assert result["event_types"]["FOXES NIGHT 2026"] == "partenaire"


REAL_EVENT_NAMES = {
    # 2025-2026 and 2026-2027 names from the Foxes exports -> expected type / opponent code.
    "Pully Lausanne Foxes vs Lions de Genève - 10.10.26 - championnat SBL": ("match", "LIONS"),
    "Pully Lausanne Foxes vs Fribourg olympic - 12.05.26 - 1/2 Finale - Game 4": ("match", "FOB"),
    "Pully Lausanne Foxes vs GC Zürich - 26.09.26 - championnat SBL": ("match", "GCZ"),
    "Pully Lausanne Foxes vs BBC Monthey-Chablais - 03.10.26 - championnat SBL": ("match", "MON"),
    "Pully Lausanne Foxes vs BBC Monthey - 06.12.25 - championnat SBL": ("match", "MON"),
    "Pully Lausanne Foxes vs BBC Nyon - 25.10.26 - championnat SBL": ("match", "NYO"),
    "Pully Lausanne Foxes vs Starwings Basket - 16.01.26 - championnat SBL": ("match", "SWB"),
    "Pully Lausanne Foxes vs Starwings Bâle - 21.02.26 - championnat SBL": ("match", "SWB"),
    "Pully Lausanne Foxes vs Union Neuchâtel Basket - A DEFINIR - championnat SBL": ("match", "UNI"),
    "Pully Lausanne Foxes vs Spinelli Massagno - 01.04.26 - championnat SBL": ("match", "MAS"),
    "Pully Lausanne Foxes vs Lugano Tigers - 04.11.25 - championnat SBL": ("match", "LUG"),
    "Pully Lausanne Foxes vs Mulhouse Mustangs - 01.09.26 - PRÉ-SAISON": ("match", "MUL"),
    "Pully Lausanne Foxes vs Hefte Helfen Bulls - 23.09.26 - ENBL": ("match", "HHB"),
    "Pully Lausanne Foxes vs Syntainics MBC - 21.10.26 - ENBL": ("match", "MBC"),
    "Pully Lausanne Foxes vs Bristol Flyers - 26.01.27 - ENBL": ("match", "BRI"),
    "1/4 de finale GAME 1 - Déplacement à Neuchâtel en car": ("autre", None),
    "FONDUE d'après-match - 06.12.2025 (Match contre Monthey)": ("autre", None),
    "Camp Foxes - 12 au 16 octobre 2026": ("camp", None),
    "FOXES NIGHT 2026": ("partenaire", None),
    "Les matchs arrivent bientôt !": ("ignorer", None),
}


def test_proposed_foxes_config_on_real_event_names():
    import json
    from pathlib import Path

    from ticketing_sync.event_lists import classify_event, opponent_code

    config = json.loads((Path(__file__).parents[1] / "clubs" / "foxes.json").read_text(encoding="utf-8"))
    event_config = config["event_lists"]
    for name, (expected_type, expected_code) in REAL_EVENT_NAMES.items():
        assert classify_event(name, event_config) == expected_type, name
        if expected_code:
            code, mapped = opponent_code(find_opponent(name, ("foxes", "pully")), event_config["opponent_codes"])
            assert (code, mapped) == (expected_code, True), name


def test_real_tariffs_map_to_brevo_options():
    from ticketing_sync.sync import CategoryWriter
    options = ["Plein", "Réduit", "Enfant", "Famille", "Membre", "VIP", "Gratuit"]
    attribute_defs = [{"name": "CATEGORIE_TARIF", "category": "normal", "type": "category",
                       "enumeration": [{"value": i + 1, "label": o} for i, o in enumerate(options)]}]
    config = json.loads(Path("clubs/foxes.json").read_text(encoding="utf-8"))
    write = CategoryWriter(attribute_defs, "CATEGORIE_TARIF", config["ticket_attributes"]["tarif_mapping"])
    expected = {
        "Adulte": "Plein", "Parents membres": "Membre", "Parents membres - sur place": "Membre",
        "Enfant (moins de 12 ans)": "Enfant", "Étudiant / AVS / AI": "Réduit", "Jeune (12-15 ans)": "Enfant",
        "VIP": "VIP", "Famille": "Famille", "Famille - sur place": "Famille", "Networking Event": "VIP",
        "VIP - billet offert": "Gratuit", "Prix non-membre": "Plein", "Prix membre": "Membre",
        "Billet offert standard": "Gratuit",
        "Abonnement saison régulière - Adulte I 2026 - 2027": "Plein",
    }
    for label, option in expected.items():
        assert write(label) == options.index(option) + 1, label
    assert not write.unmatched


def test_starwings_date_is_corrected():
    from datetime import datetime, timezone

    from ticketing_sync.event_lists import EventResolver
    from ticketing_sync.schedule import due_runs
    config = json.loads(Path("clubs/foxes.json").read_text(encoding="utf-8"))
    name = "Pully Lausanne Foxes vs Starwings Basket - 16.01.26 - championnat SBL"
    resolve = EventResolver([], config["event_lists"])
    assert resolve({"event_name": name, "event_date": "2026-01-16 19:30:00"})[1].isoformat() == "2027-01-16"
    events = [{"name": name, "start": "2026-01-16 19:30:00"}]
    schedule = {**config["schedule"], "event_lists": config["event_lists"]}
    assert due_runs(events, schedule, datetime(2027, 1, 15, 18, 40, tzinfo=timezone.utc)) == [
        {"event": name, "start": "2027-01-16T19:30:00+01:00", "mark": "H-24"}]
