"""Per-event Brevo lists (DMOS naming). HTTP fully mocked."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from ticketing_sync.event_lists import (
    build_list_name,
    ensure_event_lists,
    find_opponent,
    plan_event_lists,
    season_for,
)
from ticketing_sync.sync import sync_infomaniak_to_brevo

CONFIG = {"club_code": "FOXES", "season": "2026-2027",
          "opponent_codes": {"Lions de Genève": "LIONS", "Fribourg Olympic": "FOB"}}

ORDERS = [
    {"id": 1, "customer": {"email": "a@x.ch"},
     "event": {"id": 10, "name": "Pully Lausanne Foxes vs Lions de Genève", "date": "2026-10-18T17:30:00"}},
    {"id": 2, "customer": {"email": "b@x.ch"},
     "event": {"id": 10, "name": "Pully Lausanne Foxes vs Lions de Genève", "date": "2026-10-18T17:30:00"}},
    # Only an event id: name/date come from the events endpoint.
    {"id": 3, "customer": {"email": "a@x.ch"}, "event_id": 11},
    # Previous season: no list in the 2026-2027 folder.
    {"id": 4, "customer": {"email": "c@x.ch"},
     "event": {"id": 5, "name": "Foxes - Fribourg Olympic", "date": "2026-03-01"}},
    # Not a match.
    {"id": 5, "customer": {"email": "c@x.ch"},
     "event": {"id": 12, "name": "Soirée Partenaires", "date": "2026-11-05"}},
]
EVENTS = [{"id": 11, "name": "Massagno – Pully Lausanne Foxes", "date": "2026-11-01"}]


def test_season_boundaries():
    assert season_for(date(2026, 10, 18)) == "2026-2027"
    assert season_for(date(2027, 4, 1)) == "2026-2027"
    assert season_for(date(2026, 6, 30)) == "2025-2026"


def test_list_names_follow_dmos_convention():
    name, warning = build_list_name("FOXES", "Pully Lausanne Foxes vs Lions de Genève", date(2026, 10, 18),
                                    {"lions de geneve": "LIONS"}, ("foxes",))
    assert (name, warning) == ("CMON-FOXES-LIONS-20261018", None)

    name, warning = build_list_name("FOXES", "Massagno – Pully Foxes", date(2026, 11, 1), {}, ("foxes",))
    assert name == "CMON-FOXES-MAS-20261101"
    assert "MAS" in warning

    name, _ = build_list_name("FOXES", "Soirée Partenaires", date(2026, 11, 5), {}, ("foxes",))
    assert name == "CMON-FOXES-Soiree-Partenaires-20261105"


def test_find_opponent_needs_exactly_one_club_side():
    assert find_opponent("Foxes vs Union Neuchâtel", ("foxes",)) == "Union Neuchâtel"
    assert find_opponent("Lions vs Fribourg", ("foxes",)) is None
    assert find_opponent("Portes ouvertes", ("foxes",)) is None


def test_plan_groups_emails_per_event_of_the_season():
    plan = plan_event_lists(ORDERS, EVENTS, {"a@x.ch", "b@x.ch", "c@x.ch"}, CONFIG)
    assert plan["folder"] == "Billetterie 2026-2027"
    assert plan["events_other_season"] == 1
    assert plan["lists"]["CMON-FOXES-LIONS-20261018"]["keys"] == {"a@x.ch", "b@x.ch"}
    assert plan["lists"]["CMON-FOXES-MAS-20261101"]["keys"] == {"a@x.ch"}
    soiree = plan["lists"]["CMON-FOXES-Soiree-Partenaires-20261105"]
    assert soiree["keys"] == {"c@x.ch"} and soiree["type"] == "partenaire"
    assert soiree["folder"] == "Business Engagement"
    assert plan["event_types"]["Soirée Partenaires"] == "partenaire"
    assert plan["event_types"]["Massagno – Pully Lausanne Foxes"] == "match"


def test_plan_respects_allowed_emails_and_requires_club_code():
    plan = plan_event_lists(ORDERS, EVENTS, {"b@x.ch"}, CONFIG)
    assert list(plan["lists"]) == ["CMON-FOXES-LIONS-20261018"]

    plan = plan_event_lists(ORDERS, EVENTS, {"a@x.ch"}, {"season": "2026-2027"})
    assert plan["lists"] == {}
    assert "club_code" in plan["warnings"][0]


def test_plan_skips_undated_events_with_warning():
    orders = [{"id": 1, "customer": {"email": "a@x.ch"}, "event": {"id": 9, "name": "Foxes vs Monthey"}}]
    plan = plan_event_lists(orders, [], {"a@x.ch"}, CONFIG)
    assert plan["lists"] == {}
    assert "sans date" in plan["warnings"][0]


def _brevo_with_folder(existing_lists=()):
    brevo = MagicMock()
    brevo.get_all_folders.return_value = [{"id": 3, "name": "Billetterie 2026-2027"}]
    brevo.get_folder_lists.return_value = [{"id": 70, "name": n} for n in existing_lists]
    brevo.create_list.side_effect = lambda name, folder_id: {"id": 100 + len(brevo.create_list.mock_calls)}
    return brevo


def test_ensure_event_lists_is_idempotent():
    brevo = _brevo_with_folder(existing_lists=["CMON-FOXES-LIONS-20261018"])
    state = ensure_event_lists(brevo, "Billetterie 2026-2027",
                               ["CMON-FOXES-LIONS-20261018", "CMON-FOXES-MAS-20261101"])
    assert state["list_ids"]["CMON-FOXES-LIONS-20261018"] == 70
    assert state["created_lists"] == ["CMON-FOXES-MAS-20261101"]
    brevo.create_list.assert_called_once_with("CMON-FOXES-MAS-20261101", folder_id=3)
    brevo.create_folder.assert_not_called()


def test_ensure_event_lists_creates_folder_and_dry_run_writes_nothing():
    brevo = MagicMock()
    brevo.get_all_folders.return_value = []
    state = ensure_event_lists(brevo, "Billetterie 2026-2027", ["CMON-FOXES-LIONS-20261018"], create=False)
    assert state["list_ids"] == {"CMON-FOXES-LIONS-20261018": None}
    brevo.create_folder.assert_not_called()

    brevo.create_folder.return_value = {"id": 8}
    brevo.create_list.return_value = {"id": 81}
    state = ensure_event_lists(brevo, "Billetterie 2026-2027", ["CMON-FOXES-LIONS-20261018"])
    assert state["created_folder"] and state["list_ids"]["CMON-FOXES-LIONS-20261018"] == 81
    brevo.get_folder_lists.assert_not_called()


def test_sync_feeds_each_event_list():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS
    ticketing.get_events.return_value = EVENTS
    brevo = _brevo_with_folder(existing_lists=["CMON-FOXES-LIONS-20261018"])
    brevo.get_contact_index.return_value = {}
    brevo.get_contact_attributes.return_value = []
    brevo.ensure_contact_attributes.return_value = []
    brevo.import_contacts.return_value = {"processId": 1}

    result = sync_infomaniak_to_brevo(ticketing, brevo, event_lists=CONFIG)

    lists = result["event_lists"]["lists"]
    assert lists["CMON-FOXES-LIONS-20261018"] == {"event": "Pully Lausanne Foxes vs Lions de Genève",
                                                  "date": "2026-10-18", "type": "match",
                                                  "folder": "Billetterie 2026-2027", "contacts": 2,
                                                  "list_id": 70, "created": False}
    assert lists["CMON-FOXES-MAS-20261101"]["created"] is True
    calls = {tuple(c.kwargs["list_ids"] or []): sorted(x["email"] for x in c.args[0])
             for c in brevo.import_contacts.call_args_list}
    assert calls[(70,)] == ["a@x.ch", "b@x.ch"]
    # Per-event payloads carry the same attributes (incl. acquisition) as the global import.
    ldg_payload = next(c.args[0] for c in brevo.import_contacts.call_args_list if c.kwargs["list_ids"] == [70])
    assert ldg_payload[0]["attributes"]["SOURCE_ACQUISITION"] == "Billetterie"


def test_sync_without_event_lists_config_creates_no_list():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS
    brevo = _brevo_with_folder()
    brevo.get_contact_index.return_value = {}
    brevo.get_contact_attributes.return_value = []
    brevo.import_contacts.return_value = {"processId": 1}
    result = sync_infomaniak_to_brevo(ticketing, brevo)
    assert result["event_lists"] is None
    brevo.create_list.assert_not_called()
    brevo.get_all_folders.assert_not_called()


def test_opponent_mapping_matches_short_and_long_names():
    from ticketing_sync.event_lists import opponent_code

    mapping = CONFIG["opponent_codes"]
    assert opponent_code("Lions", mapping) == ("LIONS", True)
    assert opponent_code("Lions de Genève", mapping) == ("LIONS", True)
    assert opponent_code("Fribourg", mapping) == ("FOB", True)
    assert opponent_code("Fribourg Olympic Basket", mapping) == ("FOB", True)
    assert opponent_code("Union Neuchâtel", mapping) == ("UNI", False)


def test_classify_event_types_and_overrides():
    from ticketing_sync.event_lists import classify_event

    assert classify_event("Pully Lausanne Foxes vs Lions de Genève", {}) == "match"
    assert classify_event("Abonnement saison 2026-2027", {}) == "abonnement"
    assert classify_event("Camp de basket d'automne", {}) == "camp"
    assert classify_event("Stage de Pâques U12", {}) == "camp"
    assert classify_event("Soirée sponsors Foxes vs Lions", {}) == "partenaire"
    assert classify_event("Portes ouvertes", {}) == "autre"
    config = {"event_types": {"invitation": "ignorer", "match amical": "match"}}
    assert classify_event("Invitations partenaires - Foxes vs Lions", config) == "ignorer"
    assert classify_event("Match amical", config) == "match"


def test_season_tickets_go_to_abonnement_folder():
    orders = [
        {"id": 1, "customer": {"email": "a@x.ch"}, "event": {"id": 1, "name": "Abonnement 2026-2027"}},
        {"id": 2, "customer": {"email": "b@x.ch"},
         "event": {"id": 2, "name": "Abo Saison", "date": "2026-09-01"}},
        {"id": 3, "customer": {"email": "c@x.ch"},
         "event": {"id": 3, "name": "Invitations Foxes vs Lions", "date": "2026-10-18"}},
    ]
    config = {**CONFIG, "event_types": {"invitations": "ignorer"}}
    plan = plan_event_lists(orders, [], {"a@x.ch", "b@x.ch", "c@x.ch"}, config)
    assert list(plan["lists"]) == ["CMON-Abonnes-2026-2027"]
    entry = plan["lists"]["CMON-Abonnes-2026-2027"]
    assert entry["folder"] == "Abonnement" and entry["keys"] == {"a@x.ch", "b@x.ch"}
    assert plan["warnings"] == []


def test_sync_creates_lists_in_each_folder_and_skips_ignored_orders():
    orders = [
        {"id": 1, "customer": {"email": "a@x.ch"}, "event": {"id": 1, "name": "Abonnement 2026-2027"}},
        {"id": 2, "customer": {"email": "b@x.ch"},
         "event": {"id": 2, "name": "Foxes vs Lions", "date": "2026-10-18"}},
        {"id": 3, "customer": {"email": "test@x.ch"},
         "event": {"id": 3, "name": "TEST billetterie", "date": "2026-10-18"}},
    ]
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    ticketing.get_events.return_value = []
    brevo = MagicMock()
    brevo.get_all_folders.return_value = [{"id": 3, "name": "Billetterie 2026-2027"},
                                          {"id": 9, "name": "Abonnement"}]
    brevo.get_folder_lists.return_value = []
    brevo.create_list.side_effect = lambda name, folder_id: {"id": folder_id * 100}
    brevo.get_contact_index.return_value = {}
    brevo.get_contact_attributes.return_value = []
    brevo.import_contacts.return_value = {"processId": 1}

    config = {**CONFIG, "event_types": {"test": "ignorer"}}
    result = sync_infomaniak_to_brevo(ticketing, brevo, event_lists=config)

    assert result["orders_ignored"] == 1
    assert result["contacts_selected"] == 2
    brevo.create_list.assert_any_call("CMON-Abonnes-2026-2027", folder_id=9)
    brevo.create_list.assert_any_call("CMON-FOXES-LIONS-20261018", folder_id=3)
    imported = {c["email"] for call in brevo.import_contacts.call_args_list for c in call.args[0]}
    assert "test@x.ch" not in imported


def test_folders_per_event_type():
    orders = [
        {"id": 1, "customer": {"email": "m@x.ch"}, "event": {"name": "Foxes vs Lions", "date": "2026-10-18"}},
        {"id": 2, "customer": {"email": "c@x.ch"}, "event": {"name": "Camp d'automne", "date": "2026-10-20"}},
        {"id": 3, "customer": {"email": "p@x.ch"}, "event": {"name": "Apéro partenaires", "date": "2026-11-05"}},
        {"id": 4, "customer": {"email": "a@x.ch"}, "event": {"name": "Abonnement 2026-2027"}},
        {"id": 5, "customer": {"email": "o@x.ch"}, "event": {"name": "Portes ouvertes", "date": "2026-09-01"}},
    ]
    plan = plan_event_lists(orders, [], {"m@x.ch", "c@x.ch", "p@x.ch", "a@x.ch", "o@x.ch"}, CONFIG)
    folders = {name: (e["type"], e["folder"]) for name, e in plan["lists"].items()}
    assert folders == {
        "CMON-FOXES-LIONS-20261018": ("match", "Billetterie 2026-2027"),
        "CMON-FOXES-Camp-D-Automne-20261020": ("camp", "Billetterie 2026-2027"),
        "CMON-FOXES-Apero-Partenaires-20261105": ("partenaire", "Business Engagement"),
        "CMON-Abonnes-2026-2027": ("abonnement", "Abonnement"),
        "CMON-FOXES-Portes-Ouvertes-20260901": ("autre", "Billetterie 2026-2027"),
    }

    custom = plan_event_lists(orders, [], {"a@x.ch"}, {**CONFIG, "folders": {"abonnement": "Abonnement {season}"}})
    assert custom["lists"]["CMON-Abonnes-2026-2027"]["folder"] == "Abonnement 2026-2027"


def test_real_infomaniak_match_names():
    name = "Pully Lausanne Foxes vs Fribourg Olympic - 09.04.26 - 1/2 Finale - Game 3"
    assert find_opponent(name, ("foxes",)) == "Fribourg Olympic"
    assert find_opponent("Playoffs - Pully Lausanne Foxes vs Lions de Genève - Game 1", ("foxes",)) == "Lions de Genève"
    list_name, warning = build_list_name("FOXES", name, date(2026, 5, 9), CONFIG["opponent_codes"], ("foxes",))
    assert (list_name, warning) == ("CMON-FOXES-FOB-20260509", None)


def test_camp_list_uses_its_description_not_an_opponent():
    name, warning = build_list_name("FOXES", "Camp Foxes - 12 au 16 octobre 2026", date(2026, 10, 12),
                                    {}, ("foxes", "pully"), "camp")
    assert (name, warning) == ("CMON-FOXES-Camp-Foxes-20261012", None)


def test_member_card_is_classified_membre():
    from ticketing_sync.event_lists import classify_event
    assert classify_event("Carte membre", {}) == "membre"
    assert classify_event("Abonnement saison régulière - Adulte I 2026 - 2027", {}) == "abonnement"


def test_list_moved_to_another_folder_is_reused_not_duplicated():
    brevo = MagicMock()
    brevo.get_all_folders.return_value = [{"id": 5, "name": "B2B"}]
    brevo.get_every_list.return_value = [{"id": 229, "name": "CMON-Abonnes-VIP-2026-2027"}]
    state = ensure_event_lists(brevo, "Business Engagement", ["CMON-Abonnes-VIP-2026-2027"])
    assert state["list_ids"] == {"CMON-Abonnes-VIP-2026-2027": 229}
    brevo.create_folder.assert_not_called()
    brevo.create_list.assert_not_called()


@pytest.mark.parametrize("existing", ["business engagement", "Business  Engagement ", "01 - Business Engagement (B2B)"])
def test_folder_found_despite_case_spacing_or_prefix(existing):
    brevo = MagicMock()
    brevo.get_all_folders.return_value = [{"id": 9, "name": existing}, {"id": 1, "name": "Billetterie 2026-2027"}]
    brevo.get_every_list.return_value = []
    brevo.get_folder_lists.return_value = []
    brevo.create_list.return_value = {"id": 300}
    state = ensure_event_lists(brevo, "Business Engagement", ["CMON-FOXES-Partenaires-20261105"])
    assert state["folder_id"] == 9 and not state["created_folder"]
    brevo.create_folder.assert_not_called()
    brevo.create_list.assert_called_once_with("CMON-FOXES-Partenaires-20261105", folder_id=9)
