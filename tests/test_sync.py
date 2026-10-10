"""Infomaniak ticketing -> Brevo sync. HTTP fully mocked, no real calls."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from ticketing_sync.infomaniak import (
    InfomaniakTicketingAPIError,
    InfomaniakTicketingService,
    aggregate_buyers,
    normalize_order,
)
from ticketing_sync.sync import TICKETING_ATTRIBUTES, sync_infomaniak_to_brevo


def _response(status=200, json_body=None):
    r = MagicMock()
    r.status_code = status
    r.text = "x" if json_body is not None else ""
    r.json.return_value = json_body
    return r


ORDERS = [
    {"id": 1, "created_at": "2026-09-20T18:00:00", "total": "25.00",
     "customer": {"email": "Fan@Example.ch", "firstname": "Alex", "lastname": "Muller", "newsletter": 1},
     "event": {"name": "Foxes vs Lions"}, "tickets": [{}, {}]},
    {"id": 2, "created_at": "2026-10-04T18:00:00", "amount": 40,
     "email": "fan@example.ch", "event_name": "Foxes vs Fribourg", "ticket_count": 3},
    {"id": 3, "created_at": "2026-10-01", "total": 15,
     "customer": {"email": "other@example.ch"}},
    {"id": 4, "total": 10, "customer": {"email": "not-an-email"}},
]


def test_service_sends_key_header_and_paginates():
    svc = InfomaniakTicketingService("k123", page_size=2)
    pages = [_response(json_body={"data": ORDERS[:2]}), _response(json_body={"data": ORDERS[2:3]})]
    with patch("ticketing_sync.infomaniak.requests.request", side_effect=pages) as req, \
            patch("ticketing_sync.infomaniak.time.sleep"):
        orders = svc.get_orders()
    assert [o["id"] for o in orders] == [1, 2, 3]
    first_call = req.call_args_list[0].kwargs
    assert first_call["headers"]["key"] == "k123"
    assert first_call["url"] == "https://etickets.infomaniak.com/api/shop/orders"
    assert req.call_args_list[1].kwargs["params"]["page"] == 2


def test_service_stops_when_api_ignores_pagination():
    svc = InfomaniakTicketingService("k", page_size=2)
    same = _response(json_body=ORDERS[:2])
    with patch("ticketing_sync.infomaniak.requests.request", return_value=same), \
            patch("ticketing_sync.infomaniak.time.sleep"):
        assert len(svc.get_orders()) == 2


def test_service_maps_auth_error():
    svc = InfomaniakTicketingService("bad")
    with patch("ticketing_sync.infomaniak.requests.request", return_value=_response(401, {})):
        with pytest.raises(InfomaniakTicketingAPIError):
            svc.get_events()
        assert svc.test_connection()["success"] is False


def test_from_config_overrides_paths():
    svc = InfomaniakTicketingService.from_config("k", {"base_url": "https://x.test/api/", "orders_path": "/v2/orders"})
    with patch("ticketing_sync.infomaniak.requests.request", return_value=_response(json_body=[])) as req:
        svc.get_orders()
    assert req.call_args.kwargs["url"] == "https://x.test/api/v2/orders"


def test_normalize_and_aggregate():
    assert normalize_order(ORDERS[3]) is None
    buyers = aggregate_buyers(ORDERS)
    assert set(buyers) == {"fan@example.ch", "other@example.ch"}
    fan = buyers["fan@example.ch"]
    assert fan["orders_count"] == 2
    assert fan["tickets_count"] == 5
    assert fan["total_spent"] == 65.0
    assert fan["first_name"] == "Alex"
    assert fan["last_event"] == "Foxes vs Fribourg"
    assert fan["first_order_date"].startswith("2026-09-20")
    assert fan["optin"] is True
    assert buyers["other@example.ch"]["optin"] is None


def _brevo(existing_attrs=("FIRSTNAME", "LASTNAME"), existing_emails=(), extra_defs=()):
    brevo = MagicMock()
    # existing_emails: iterable of emails, or {email: attributes}.
    index = existing_emails if isinstance(existing_emails, dict) else {e: {} for e in existing_emails}
    brevo.get_contact_index.return_value = index
    brevo.get_contact_attributes.return_value = [{"name": n} for n in existing_attrs] + list(extra_defs)
    brevo.ensure_contact_attributes.return_value = list(TICKETING_ATTRIBUTES)
    brevo.import_contacts.return_value = {"processId": 42}
    return brevo


def test_sync_imports_contacts_into_lists():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS
    brevo = _brevo()
    result = sync_infomaniak_to_brevo(ticketing, brevo, list_ids=[7])

    assert result["contacts_selected"] == 2
    assert result["import_process_ids"] == [42]
    contacts, = brevo.import_contacts.call_args.args
    assert brevo.import_contacts.call_args.kwargs["list_ids"] == [7]
    fan = next(c for c in contacts if c["email"] == "fan@example.ch")
    assert fan["attributes"]["FIRSTNAME"] == "Alex"
    assert fan["attributes"]["TICKETING_TOTAL_SPENT"] == 65.0
    assert fan["attributes"]["TICKETING_LAST_ORDER"] == "2026-10-04"


def test_sync_uses_french_name_attributes():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS[:1]
    brevo = _brevo(existing_attrs=("PRENOM", "NOM"))
    sync_infomaniak_to_brevo(ticketing, brevo)
    contact = brevo.import_contacts.call_args.args[0][0]
    assert contact["attributes"]["PRENOM"] == "Alex"
    assert "FIRSTNAME" not in contact["attributes"]


def test_sync_only_optin_and_dry_run():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS
    brevo = _brevo()
    result = sync_infomaniak_to_brevo(ticketing, brevo, only_optin=True, dry_run=True)
    assert result["contacts_selected"] == 1
    assert result["skipped_no_optin"] == 1
    brevo.import_contacts.assert_not_called()


def test_sync_tags_contacts_without_source_and_keeps_existing_source():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS
    brevo = _brevo(existing_emails={"fan@example.ch": {"SOURCE_ACQUISITION": "Concours", "OPT_IN": "NO"}},
                   extra_defs=[{"name": "OPT_IN", "type": "text"}])
    result = sync_infomaniak_to_brevo(ticketing, brevo)

    assert result["new_contacts"] == 1
    assert result["existing_contacts"] == 1
    assert result["source_filled"] == 1
    contacts = {c["email"]: c for c in brevo.import_contacts.call_args.args[0]}
    assert contacts["other@example.ch"]["attributes"]["SOURCE_ACQUISITION"] == "Billetterie"
    assert contacts["other@example.ch"]["attributes"]["OPT_IN"] == "YES"
    # Existing source kept, and its OPT_IN is not touched.
    assert "SOURCE_ACQUISITION" not in contacts["fan@example.ch"]["attributes"]
    assert "OPT_IN" not in contacts["fan@example.ch"]["attributes"]
    assert brevo.ensure_contact_attributes.call_args.args[0]["SOURCE_ACQUISITION"] == "text"


def test_existing_contact_with_empty_source_is_attributed():
    """Guideline step 6: filter 'SOURCE_ACQUISITION est vide', not only new contacts."""
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS[2:3]
    brevo = _brevo(existing_emails={"other@example.ch": {"SOURCE_ACQUISITION": ""}},
                   extra_defs=[{"name": "OPT_IN", "category": "category",
                                "enumeration": [{"value": 1, "label": "YES"}, {"value": 2, "label": "NO"}]}])
    result = sync_infomaniak_to_brevo(ticketing, brevo)
    attrs = brevo.import_contacts.call_args.args[0][0]["attributes"]
    assert result["new_contacts"] == 0 and result["source_filled"] == 1
    assert attrs["SOURCE_ACQUISITION"] == "Billetterie"
    assert attrs["OPT_IN"] == 1


def test_missing_optin_attribute_is_reported_not_created():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS[2:3]
    brevo = _brevo()
    result = sync_infomaniak_to_brevo(ticketing, brevo)
    assert "OPT_IN" not in brevo.import_contacts.call_args.args[0][0]["attributes"]
    assert "OPT_IN" not in brevo.ensure_contact_attributes.call_args.args[0]
    assert any("OPT_IN" in w for w in result["warnings"])


MATCH_ORDERS = [
    {"id": 1, "created_at": "2026-10-01", "customer": {"email": "fan@example.ch"},
     "event": {"name": "Foxes vs Lions", "date": "2026-10-04T17:30:00"}},
    {"id": 2, "created_at": "2026-10-08", "customer": {"email": "fan@example.ch"},
     "event": {"name": "Foxes vs Fribourg", "date": "2026-10-18T17:30:00"}},
]


def test_dernier_match_is_last_played_match_and_dernier_import_set_for_new():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = MATCH_ORDERS
    brevo = _brevo()
    result = sync_infomaniak_to_brevo(ticketing, brevo, today=date(2026, 10, 10))
    attrs = brevo.import_contacts.call_args.args[0][0]["attributes"]
    # The 18.10 match is not played yet.
    assert attrs["DERNIER_MATCH"] == "2026-10-04"
    assert attrs["DERNIER_IMPORT"] == "2026-10-10"
    assert result["last_import_updated"] == 1
    created = brevo.ensure_contact_attributes.call_args.args[0]
    assert created["DERNIER_MATCH"] == "date" and created["DERNIER_IMPORT"] == "date"


def test_dernier_import_only_changes_when_purchases_or_match_change():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = MATCH_ORDERS
    unchanged = {"fan@example.ch": {"SOURCE_ACQUISITION": "Billetterie", "TICKETING_ORDERS": 2.0,
                                    "DERNIER_MATCH": "2026-10-04", "DERNIER_IMPORT": "2026-10-05"}}
    brevo = _brevo(existing_emails=unchanged)
    result = sync_infomaniak_to_brevo(ticketing, brevo, today=date(2026, 10, 10))
    attrs = brevo.import_contacts.call_args.args[0][0]["attributes"]
    assert "DERNIER_IMPORT" not in attrs
    assert result["last_import_updated"] == 0

    # After the 18.10 match has been played, DERNIER_MATCH moves and DERNIER_IMPORT is stamped.
    brevo = _brevo(existing_emails=unchanged)
    sync_infomaniak_to_brevo(ticketing, brevo, today=date(2026, 10, 19))
    attrs = brevo.import_contacts.call_args.args[0][0]["attributes"]
    assert attrs["DERNIER_MATCH"] == "2026-10-18"
    assert attrs["DERNIER_IMPORT"] == "2026-10-19"


def test_text_date_attributes_use_guideline_format():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = MATCH_ORDERS
    brevo = _brevo(extra_defs=[{"name": "DERNIER_MATCH", "type": "text"},
                               {"name": "DERNIER_IMPORT", "type": "text"}])
    sync_infomaniak_to_brevo(ticketing, brevo, today=date(2026, 10, 19))
    attrs = brevo.import_contacts.call_args.args[0][0]["attributes"]
    assert attrs["DERNIER_MATCH"] == "dimanche, 18 octobre 2026"
    assert attrs["DERNIER_IMPORT"] == "lundi, 19 octobre 2026"


def test_invalid_emails_are_dropped():
    assert normalize_order({"customer": {"email": "foo@bar"}}) is None
    assert normalize_order({"customer": {"email": "a b@x.ch"}}) is None
    assert normalize_order({"customer": {"email": " Fan@X.ch "}})["email"] == "fan@x.ch"


def test_sync_custom_or_disabled_acquisition_attribute():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS[2:3]
    brevo = _brevo()
    sync_infomaniak_to_brevo(ticketing, brevo, acquisition_attribute="ORIGINE", acquisition_value="Ticketing")
    assert brevo.import_contacts.call_args.args[0][0]["attributes"]["ORIGINE"] == "Ticketing"

    brevo = _brevo()
    result = sync_infomaniak_to_brevo(ticketing, brevo, acquisition_attribute="")
    attrs = brevo.import_contacts.call_args.args[0][0]["attributes"]
    assert "SOURCE_ACQUISITION" not in attrs and "" not in attrs
    assert result["acquisition_attribute"] is None


def test_dry_run_with_brevo_reports_new_contacts_without_writing():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS
    brevo = _brevo(existing_emails={"other@example.ch"})
    result = sync_infomaniak_to_brevo(ticketing, brevo, dry_run=True)
    assert result["new_contacts"] == 1
    brevo.import_contacts.assert_not_called()
    brevo.ensure_contact_attributes.assert_not_called()


def test_brevo_get_all_contact_emails_paginates_and_lowercases():
    from ticketing_sync.brevo import BrevoService

    svc = BrevoService("k")
    pages = [
        {"contacts": [{"email": "A@x.ch"}, {"email": "b@x.ch"}]},
        {"contacts": [{"email": "c@x.ch"}]},
    ]
    with patch.object(svc, "_make_request", side_effect=pages) as req, \
            patch("ticketing_sync.brevo.time.sleep"):
        emails = svc.get_all_contact_emails(page_size=2)
    assert emails == {"a@x.ch", "b@x.ch", "c@x.ch"}
    assert req.call_args_list[1].kwargs["params"]["offset"] == 2


ORIGIN_CATEGORY = {
    "name": "SOURCE_ACQUISITION", "category": "category",
    "enumeration": [{"value": 1, "label": "Site web"}, {"value": 4, "label": "billetterie"}],
}


def test_existing_category_attribute_sends_option_value():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS[2:3]
    brevo = _brevo(extra_defs=[ORIGIN_CATEGORY])
    result = sync_infomaniak_to_brevo(ticketing, brevo)
    assert brevo.import_contacts.call_args.args[0][0]["attributes"]["SOURCE_ACQUISITION"] == 4
    assert result["acquisition_value_sent"] == 4
    assert not any("SOURCE_ACQUISITION" in w for w in result["warnings"])


def test_category_without_matching_option_skips_tag_with_warning():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS[2:3]
    category = {**ORIGIN_CATEGORY, "enumeration": [{"value": 1, "label": "Site web"}]}
    brevo = _brevo(extra_defs=[category])
    result = sync_infomaniak_to_brevo(ticketing, brevo)
    assert "SOURCE_ACQUISITION" not in brevo.import_contacts.call_args.args[0][0]["attributes"]
    assert "Site web" in result["warnings"][0]
    assert "SOURCE_ACQUISITION" not in brevo.ensure_contact_attributes.call_args.args[0]


def test_existing_text_attribute_is_reused():
    ticketing = MagicMock()
    ticketing.get_orders.return_value = ORDERS[2:3]
    brevo = _brevo(extra_defs=[{"name": "SOURCE_ACQUISITION", "category": "normal", "type": "text"}])
    result = sync_infomaniak_to_brevo(ticketing, brevo, dry_run=True)
    assert result["acquisition_value_sent"] == "Billetterie"
    assert not any("SOURCE_ACQUISITION" in w for w in result["warnings"])


def test_ensure_contact_attributes_never_recreates_existing():
    from ticketing_sync.brevo import BrevoService

    svc = BrevoService("k")
    with patch.object(svc, "get_contact_attributes",
                      return_value=[{"name": "SOURCE_ACQUISITION", "category": "category"}]), \
            patch.object(svc, "_make_request") as req:
        created = svc.ensure_contact_attributes({"SOURCE_ACQUISITION": "text", "TICKETING_ORDERS": "float"})
    assert created == ["TICKETING_ORDERS"]
    assert req.call_count == 1


def test_normalize_phone_formats():
    from ticketing_sync.infomaniak import normalize_phone

    assert normalize_phone("079 123 45 67") == "+41791234567"
    assert normalize_phone("0041 79 123 45 67") == "+41791234567"
    assert normalize_phone("+33 6 12 34 56 78") == "+33612345678"
    assert normalize_phone("41791234567") == "+41791234567"
    assert normalize_phone("791234567") == "+41791234567"
    assert normalize_phone("123") is None
    assert normalize_phone("") is None


def test_phone_only_orders_merge_into_email_buyer_or_stand_alone():
    orders = [
        {"id": 1, "customer": {"email": "fan@x.ch", "phone": "079 123 45 67"}},
        {"id": 2, "customer": {"phone": "+41 79 123 45 67"}},
        {"id": 3, "customer": {"phone": "078 000 00 00"}},
        {"id": 4, "customer": {}},
    ]
    buyers = aggregate_buyers(orders)
    assert set(buyers) == {"fan@x.ch", "+41780000000"}
    assert buyers["fan@x.ch"]["orders_count"] == 2
    assert buyers["+41780000000"]["email"] is None


def test_sync_sets_sms_and_creates_phone_only_contacts():
    orders = [
        {"id": 1, "customer": {"email": "fan@x.ch", "phone": "079 123 45 67"},
         "event": {"name": "Foxes vs Lions", "date": "2026-10-04"}},
        {"id": 2, "customer": {"phone": "078 000 00 00", "firstname": "Sam"},
         "event": {"name": "Foxes vs Lions", "date": "2026-10-04"}},
        # Phone already held by another Brevo contact: SMS not written.
        {"id": 3, "customer": {"email": "kid@x.ch", "phone": "076 111 11 11"}},
    ]
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    ticketing.get_events.return_value = []
    brevo = _brevo(existing_emails={"parent@x.ch": {"SMS": "41761111111", "SOURCE_ACQUISITION": "Site"}})
    result = sync_infomaniak_to_brevo(ticketing, brevo, list_ids=[7], today=date(2026, 10, 10))

    imported = {c["email"]: c["attributes"] for c in brevo.import_contacts.call_args.args[0]}
    assert imported["fan@x.ch"]["SMS"] == "+41791234567"
    assert "SMS" not in imported["kid@x.ch"]
    assert result["sms_conflicts"] == 1
    attrs = brevo.upsert_contact.call_args.args[0]
    assert attrs["SMS"] == "+41780000000" and attrs["FIRSTNAME"] == "Sam"
    assert attrs["SOURCE_ACQUISITION"] == "Billetterie"
    assert brevo.upsert_contact.call_args.kwargs["list_ids"] == [7]
    assert result["phone_only_buyers"] == 1 and result["phone_only_upserted"] == 1


def test_phone_only_buyer_known_in_brevo_updates_that_contact():
    orders = [{"id": 1, "customer": {"phone": "079 123 45 67"}}]
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    brevo = _brevo(existing_emails={"fan@x.ch": {"SMS": "+41791234567", "SOURCE_ACQUISITION": "Concours"}})
    result = sync_infomaniak_to_brevo(ticketing, brevo)
    assert result["new_contacts"] == 0
    contact = brevo.import_contacts.call_args.args[0][0]
    assert contact["email"] == "fan@x.ch"
    assert "SOURCE_ACQUISITION" not in contact["attributes"]
    brevo.upsert_contact.assert_not_called()


def test_dernier_import_waits_for_the_match_to_be_played():
    orders = [{"id": 1, "created_at": "2026-10-08", "customer": {"email": "new@x.ch"},
               "event": {"name": "Foxes vs Fribourg", "date": "2026-10-18"}}]
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    brevo = _brevo()
    sync_infomaniak_to_brevo(ticketing, brevo, today=date(2026, 10, 10))
    attrs = brevo.import_contacts.call_args.args[0][0]["attributes"]
    # New contact, match not played yet: no DERNIER_MATCH, no DERNIER_IMPORT.
    assert "DERNIER_MATCH" not in attrs and "DERNIER_IMPORT" not in attrs
    assert attrs["SOURCE_ACQUISITION"] == "Billetterie"

    # A new purchase alone (orders count changes) does not stamp DERNIER_IMPORT either.
    brevo = _brevo(existing_emails={"new@x.ch": {"SOURCE_ACQUISITION": "Billetterie", "TICKETING_ORDERS": 0}})
    sync_infomaniak_to_brevo(ticketing, brevo, today=date(2026, 10, 10))
    assert "DERNIER_IMPORT" not in brevo.import_contacts.call_args.args[0][0]["attributes"]


def test_non_match_events_do_not_feed_dernier_match():
    orders = [
        {"id": 1, "customer": {"email": "fan@x.ch"}, "event": {"name": "Foxes vs Lions", "date": "2026-10-04"}},
        {"id": 2, "customer": {"email": "fan@x.ch"}, "event": {"name": "Soirée partenaires", "date": "2026-10-08"}},
    ]
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    brevo = _brevo()
    sync_infomaniak_to_brevo(ticketing, brevo, today=date(2026, 10, 10))
    assert brevo.import_contacts.call_args.args[0][0]["attributes"]["DERNIER_MATCH"] == "2026-10-04"


def test_warns_when_no_match_is_recognised():
    orders = [{"id": 1, "customer": {"email": "fan@x.ch"}, "event": {"name": "Camp d'été", "date": "2026-07-04"}}]
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    result = sync_infomaniak_to_brevo(ticketing, _brevo(), today=date(2026, 10, 10))
    assert any("Aucun match reconnu" in w for w in result["warnings"])


STATUT_DEF = {"name": "TICKET_STATUT", "category": "category",
              "enumeration": [{"value": 1, "label": "NEW"}, {"value": 2, "label": "ONBOARDED"},
                              {"value": 3, "label": "REGULAR"}]}
PLACE_DEF = {"name": "CATEGORIE_PLACE", "category": "category",
             "enumeration": [{"value": 1, "label": "Tribune"}, {"value": 2, "label": "Virage"},
                             {"value": 3, "label": "VIP"}]}
TARIF_DEF = {"name": "CATEGORIE_TARIF", "category": "category",
             "enumeration": [{"value": 1, "label": "Plein"}, {"value": 2, "label": "Réduit"},
                             {"value": 3, "label": "Enfant"}, {"value": 4, "label": "Gratuit"}]}


def _match_order(order_id, email, name, day, tickets):
    return {"id": order_id, "created_at": day, "customer": {"email": email},
            "event": {"name": name, "date": day}, "tickets": tickets}


def test_ticket_status_thresholds():
    from ticketing_sync.sync import ticket_status

    assert [ticket_status(n) for n in (0, 1, 2, 3, 7)] == [None, "NEW", "ONBOARDED", "REGULAR", "REGULAR"]
    assert ticket_status(3, {"REGULAR": 5}) == "ONBOARDED"


def test_sync_writes_ticket_statut_and_categories():
    orders = [
        _match_order(1, "fan@x.ch", "Foxes vs Lions", "2026-10-04",
                     [{"category": "Virage", "tariff": "Adulte"}]),
        _match_order(2, "fan@x.ch", "Foxes vs Fribourg", "2026-10-18",
                     [{"category": {"name": "Tribune"}, "tariff": "Adulte"},
                      {"category": "Tribune", "tariff": "Enfant"},
                      {"category": "Tribune", "tariff": "Enfant"}]),
        # Same match twice and a camp: still 2 distinct matches.
        _match_order(3, "fan@x.ch", "Foxes vs Fribourg", "2026-10-18", [{"category": "Tribune"}]),
        _match_order(4, "fan@x.ch", "Camp d'automne", "2026-10-20", [{"category": "Salle", "tariff": "Camp"}]),
        _match_order(5, "kid@x.ch", "Foxes vs Lions", "2026-10-04", [{"category": "Virage", "tariff": "Gratuit"}]),
    ]
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    brevo = _brevo(extra_defs=[STATUT_DEF, PLACE_DEF, TARIF_DEF])
    result = sync_infomaniak_to_brevo(ticketing, brevo, today=date(2026, 10, 25),
                                      ticket_attributes={"tarif_mapping": {"adulte": "Plein"}})
    contacts = {c["email"]: c["attributes"] for c in brevo.import_contacts.call_args.args[0]}
    fan, kid = contacts["fan@x.ch"], contacts["kid@x.ch"]
    assert fan["TICKET_STATUT"] == 2            # ONBOARDED: 2 distinct matches
    assert fan["CATEGORIE_PLACE"] == 1          # Tribune, latest match
    assert fan["CATEGORIE_TARIF"] == 3          # Enfant, most frequent in the latest match order
    assert kid["TICKET_STATUT"] == 1 and kid["CATEGORIE_PLACE"] == 2 and kid["CATEGORIE_TARIF"] == 4
    assert result["ticket_attributes"] == {"TICKET_STATUT": 2, "CATEGORIE_PLACE": 2, "CATEGORIE_TARIF": 2}
    assert not any("CATEGORIE" in w for w in result["warnings"])


def test_unmapped_category_and_missing_attribute_are_reported():
    orders = [_match_order(1, "fan@x.ch", "Foxes vs Lions", "2026-10-04",
                           [{"category": "Gradin Nord", "tariff": "Adulte"}])]
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    brevo = _brevo(extra_defs=[PLACE_DEF, TARIF_DEF])
    result = sync_infomaniak_to_brevo(ticketing, brevo, today=date(2026, 10, 25))
    attrs = brevo.import_contacts.call_args.args[0][0]["attributes"]
    assert "CATEGORIE_PLACE" not in attrs and "CATEGORIE_TARIF" not in attrs and "TICKET_STATUT" not in attrs
    assert any("Gradin Nord" in w for w in result["warnings"])
    assert any("Adulte" in w for w in result["warnings"])
    assert any("TICKET_STATUT absent" in w for w in result["warnings"])
    assert "TICKET_STATUT" not in brevo.ensure_contact_attributes.call_args.args[0]


def test_ticket_attributes_can_be_disabled():
    orders = [_match_order(1, "fan@x.ch", "Foxes vs Lions", "2026-10-04", [{"category": "Virage"}])]
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    brevo = _brevo(extra_defs=[STATUT_DEF, PLACE_DEF])
    sync_infomaniak_to_brevo(ticketing, brevo, ticket_attributes={"enabled": False})
    attrs = brevo.import_contacts.call_args.args[0][0]["attributes"]
    assert "TICKET_STATUT" not in attrs and "CATEGORIE_PLACE" not in attrs


def test_last_order_amount_and_average_basket():
    orders = [
        {"id": 1, "created_at": "2026-09-20", "total": 30, "customer": {"email": "fan@x.ch"}},
        {"id": 2, "created_at": "2026-10-04", "total": 50, "customer": {"email": "fan@x.ch"}},
        {"id": 3, "created_at": "2026-10-10", "customer": {"email": "fan@x.ch"}},  # no amount
    ]
    fan = aggregate_buyers(orders)["fan@x.ch"]
    assert fan["last_order_amount"] == 50.0
    assert fan["average_basket"] == 40.0
    assert fan["total_spent"] == 80.0

    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    brevo = _brevo()
    sync_infomaniak_to_brevo(ticketing, brevo)
    attrs = brevo.import_contacts.call_args.args[0][0]["attributes"]
    assert attrs["TICKETING_LAST_ORDER_AMOUNT"] == 50.0 and attrs["TICKETING_AVG_BASKET"] == 40.0
    created = brevo.ensure_contact_attributes.call_args.args[0]
    assert created["TICKETING_LAST_ORDER_AMOUNT"] == "float" and created["TICKETING_AVG_BASKET"] == "float"


def test_current_season_ticket_holders_are_regular():
    orders = [
        {"id": 1, "created_at": "2026-08-15", "customer": {"email": "abo@x.ch"},
         "event": {"name": "Abonnement 2026-2027"}, "tickets": [{"category": "Tribune", "tariff": "Plein"}]},
        # Last season's season ticket + one match: only NEW.
        {"id": 2, "created_at": "2025-08-15", "customer": {"email": "old@x.ch"},
         "event": {"name": "Abonnement 2025-2026"}},
        _match_order(3, "old@x.ch", "Foxes vs Lions", "2026-10-04", [{"category": "Virage"}]),
    ]
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    brevo = _brevo(extra_defs=[STATUT_DEF, PLACE_DEF, TARIF_DEF])
    sync_infomaniak_to_brevo(ticketing, brevo, today=date(2026, 10, 25), event_lists={"season": "2026-2027"})
    contacts = {c["email"]: c["attributes"] for c in brevo.import_contacts.call_args.args[0]}
    assert contacts["abo@x.ch"]["TICKET_STATUT"] == 3          # REGULAR
    assert contacts["abo@x.ch"]["CATEGORIE_PLACE"] == 1        # Tribune, from the season ticket
    assert contacts["abo@x.ch"]["CATEGORIE_TARIF"] == 1        # Plein
    assert contacts["old@x.ch"]["TICKET_STATUT"] == 1          # NEW


def test_partner_event_buyers_get_business_engagement_source():
    source_def = {"name": "SOURCE_ACQUISITION", "category": "category",
                  "enumeration": [{"value": 1, "label": "Billetterie"},
                                  {"value": 5, "label": "Business engagement"}]}
    orders = [
        # First purchase is a partner event -> Business Engagement.
        {"id": 1, "created_at": "2026-09-01", "customer": {"email": "boss@x.ch"},
         "event": {"name": "Soirée partenaires", "date": "2026-09-05"}},
        {"id": 2, "created_at": "2026-10-01", "customer": {"email": "boss@x.ch"},
         "event": {"name": "Foxes vs Lions", "date": "2026-10-04"}},
        # First purchase is a match -> Billetterie, even if a partner event follows.
        {"id": 3, "created_at": "2026-09-01", "customer": {"email": "fan@x.ch"},
         "event": {"name": "Foxes vs Lions", "date": "2026-10-04"}},
        {"id": 4, "created_at": "2026-10-01", "customer": {"email": "fan@x.ch"},
         "event": {"name": "Apéro sponsors", "date": "2026-10-08"}},
    ]
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    brevo = _brevo(extra_defs=[source_def, {"name": "OPT_IN", "type": "text"}])
    result = sync_infomaniak_to_brevo(ticketing, brevo)
    contacts = {c["email"]: c["attributes"] for c in brevo.import_contacts.call_args.args[0]}
    assert contacts["boss@x.ch"]["SOURCE_ACQUISITION"] == 5
    assert contacts["fan@x.ch"]["SOURCE_ACQUISITION"] == 1
    assert contacts["boss@x.ch"]["OPT_IN"] == "YES"
    assert result["source_filled_by_value"] == {"Business Engagement": 1, "Billetterie": 1}
    assert not any("SOURCE_ACQUISITION" in w for w in result["warnings"])


def test_partner_source_option_missing_is_reported_and_billetterie_still_written():
    source_def = {"name": "SOURCE_ACQUISITION", "category": "category",
                  "enumeration": [{"value": 1, "label": "Billetterie"}]}
    orders = [
        {"id": 1, "created_at": "2026-09-01", "customer": {"email": "boss@x.ch"},
         "event": {"name": "Soirée partenaires", "date": "2026-09-05"}},
        {"id": 2, "created_at": "2026-09-01", "customer": {"email": "fan@x.ch"},
         "event": {"name": "Foxes vs Lions", "date": "2026-10-04"}},
    ]
    ticketing = MagicMock()
    ticketing.get_orders.return_value = orders
    brevo = _brevo(extra_defs=[source_def])
    result = sync_infomaniak_to_brevo(ticketing, brevo)
    contacts = {c["email"]: c["attributes"] for c in brevo.import_contacts.call_args.args[0]}
    assert "SOURCE_ACQUISITION" not in contacts["boss@x.ch"]
    assert contacts["fan@x.ch"]["SOURCE_ACQUISITION"] == 1
    assert any("Business Engagement" in w for w in result["warnings"])


@pytest.mark.parametrize("mode,header,param", [
    ("key", ("key", "k123"), None),
    ("bearer", ("Authorization", "Bearer k123"), None),
    ("authorization", ("Authorization", "k123"), None),
    ("credential", None, ("credential", "k123")),
    ("credential_header", ("credential", "k123"), None),
])
def test_auth_modes(mode, header, param):
    svc = InfomaniakTicketingService.from_config("k123", {"auth": mode})
    with patch("ticketing_sync.infomaniak.requests.request", return_value=_response(json_body=[])) as req:
        svc.get_events()
    kwargs = req.call_args.kwargs
    if header:
        assert kwargs["headers"][header[0]] == header[1]
    if param:
        assert kwargs["params"][param[0]] == param[1]
    others = {"key", "Authorization", "credential"} - ({header[0]} if header else set())
    assert not others & set(kwargs["headers"])


def test_unknown_auth_mode_is_rejected():
    with pytest.raises(ValueError):
        InfomaniakTicketingService("k", auth="cookie")


def test_probe_auth_reports_each_mode_without_the_key():
    svc = InfomaniakTicketingService("secret-key-123")

    def fake_get(url, headers, params, timeout):
        ok = headers.get("Authorization") == "Bearer secret-key-123"
        body = "[]" if ok else '{"error": "missing credential or Authorization secret-key-123"}'
        response = MagicMock()
        response.status_code, response.text = (200 if ok else 400), body
        return response

    with patch("ticketing_sync.infomaniak.requests.get", side_effect=fake_get):
        results = svc.probe_auth()
    assert [r["auth"] for r in results if r["ok"]] == ["bearer", "key+bearer"]
    assert all("secret-key-123" not in r["response"] for r in results)


def test_combined_auth_mode_sends_both_parts():
    svc = InfomaniakTicketingService.from_config("k123", {"auth": "key+credential_header"})
    with patch("ticketing_sync.infomaniak.requests.request", return_value=_response(json_body=[])) as req:
        svc.get_events()
    headers = req.call_args.kwargs["headers"]
    assert headers["key"] == "k123" and headers["credential"] == "k123"
