"""Infomaniak ticketing -> Brevo contact sync.

Pulls every order from the Infomaniak ticketing shop, aggregates them per
buyer email and upserts the buyers into Brevo (optionally into one or more
lists) with purchase attributes, so newsletters can segment on ticketing
behaviour (e.g. "bought a ticket this season but no season pass").

Totals are recomputed from the full order history on every run, which is
why the sync never runs incrementally: a partial window would overwrite
TICKETING_TOTAL_SPENT with a partial sum.

Follows the manual-import guideline "Source d'acquisition - Billetterie"
(Notion, Products Library > Enrichissement quantitatif de la base CRM):

* contacts whose ``SOURCE_ACQUISITION`` is empty (new contacts, or existing
  ones never attributed) get ``SOURCE_ACQUISITION = Billetterie`` and
  ``OPT_IN = YES``; a contact that already has a source keeps it and its
  OPT_IN is left untouched;
* ``DERNIER_MATCH`` = date of the last match the buyer bought a ticket for
  that has already been played (a manual export is made after the match);
* ``DERNIER_IMPORT`` = sync date, written only when the contact is new or
  its purchases / DERNIER_MATCH changed, so post-purchase automations keyed
  on it fire once per new purchase instead of on every run.

With ``event_lists`` configured, buyers are also added to one list per
event of the season (see ``ticketing_sync/event_lists.py`` for the
DMOS naming), filed in the season folder and topped up on every run.
"""
import logging
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from ticketing_sync.brevo import BrevoAPIError, is_invalid_phone_error
from ticketing_sync.event_lists import (
    DEFAULT_SEASON_START_MONTH,
    EventResolver,
    _contains_words,
    _words,
    current_season,
    ensure_event_lists,
    is_vip_season_ticket,
    parse_event_date,
    plan_event_lists,
    season_for,
    season_ticket_season,
)
from ticketing_sync.infomaniak import (
    InfomaniakTicketingAPIError,
    aggregate_buyers,
    buyer_keys,
    counts_for_event,
    normalize_order,
    normalize_phone,
    row_key,
    utc_now_iso,
    was_scanned,
)

logger = logging.getLogger(__name__)

# Custom Brevo attributes created on first sync (name -> Brevo type).
TICKETING_ATTRIBUTES = {
    "TICKETING_SOURCE": "text",
    "TICKETING_ORDERS": "float",
    "TICKETING_TICKETS": "float",
    "TICKETING_TOTAL_SPENT": "float",
    "TICKETING_FIRST_ORDER": "date",
    "TICKETING_LAST_ORDER": "date",
    "TICKETING_LAST_EVENT": "text",
    "TICKETING_LAST_ORDER_AMOUNT": "float",
    "TICKETING_AVG_BASKET": "float",
    "TICKETING_MATCHES": "float",
    "TICKETING_MATCHES_ATTENDED": "float",
    "TICKETING_FREE_TICKETS": "float",
    "TICKETING_FREE_VALUE": "float",
    "TICKETING_ONSITE_TICKETS": "float",
    "TICKETING_PAYMENT_METHOD": "text",
    "TICKETING_LAST_ORDER_ID": "text",
    "TICKETING_PASS_CODE": "text",
    "TICKETING_SEASON_TICKET": "text",
}

IMPORT_BATCH_SIZE = 1000

DEFAULT_ACQUISITION_ATTRIBUTE = "SOURCE_ACQUISITION"
DEFAULT_ACQUISITION_VALUE = "Billetterie"
DEFAULT_PARTNER_ACQUISITION_VALUE = "Business Engagement"
DEFAULT_OPTIN_ATTRIBUTE = "OPT_IN"
DEFAULT_OPTIN_VALUE = "YES"
LAST_IMPORT_ATTRIBUTE = "DERNIER_IMPORT"
LAST_MATCH_ATTRIBUTE = "DERNIER_MATCH"
SMS_ATTRIBUTE = "SMS"
# DMOS referential "Billetterie" attributes (category type in Brevo).
TICKET_STATUT_ATTRIBUTE = "TICKET_STATUT"
PLACE_ATTRIBUTE = "CATEGORIE_PLACE"
TARIFF_ATTRIBUTE = "CATEGORIE_TARIF"
DEFAULT_STATUT_THRESHOLDS = {"ONBOARDED": 2, "REGULAR": 3}
# Referential identity attributes, filled only when empty in Brevo.
ZIP_ATTRIBUTE = "NPA"
BIRTHDAY_ATTRIBUTE = "BIRTHDAY"

_FR_DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_FR_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
              "septembre", "octobre", "novembre", "décembre")


def _brevo_date(value: Optional[str]) -> Optional[str]:
    # Brevo date attributes only accept YYYY-MM-DD.
    if not value or len(value) < 10:
        return None
    candidate = value[:10]
    return candidate if candidate[4] == "-" and candidate[7] == "-" else None


def _name_attributes(existing: set) -> Dict[str, str]:
    # French-language Brevo accounts ship PRENOM/NOM instead of FIRSTNAME/LASTNAME.
    first = "PRENOM" if "PRENOM" in existing and "FIRSTNAME" not in existing else "FIRSTNAME"
    last = "NOM" if "NOM" in existing and "LASTNAME" not in existing else "LASTNAME"
    return {"first": first, "last": last}


def _normalize_label(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return text.strip().lower()


def french_long_date(day: date) -> str:
    """Guideline format for text date attributes: ``lundi, 7 mars 2025``."""
    return f"{_FR_DAYS[day.weekday()]}, {day.day} {_FR_MONTHS[day.month - 1]} {day.year}"


def _find_definition(attribute_defs: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    return next((a for a in attribute_defs if a.get("name") == name), None)


def resolve_attribute_value(attribute_defs: List[Dict[str, Any]], name: str,
                            value: str) -> Tuple[Optional[Any], Optional[str]]:
    """Translate a label to what Brevo expects for attribute ``name``.

    Returns ``(value_to_send, warning)``; ``value_to_send`` is ``None``
    when the attribute must not be written. A missing attribute is sent
    as-is (callers create it as text when they want it created). For a
    *category* attribute Brevo stores the option's numeric ``value``, not
    its label: the label is matched case- and accent-insensitively
    ("billetterie" == "Billetterie"). Booleans accept YES/OUI/TRUE.
    """
    definition = _find_definition(attribute_defs, name)
    if definition is None:
        return value, None

    enumeration = definition.get("enumeration") or []
    is_category = (definition.get("category") == "category"
                   or definition.get("type") == "category" or bool(enumeration))
    if is_category:
        wanted = _normalize_label(value)
        for option in enumeration:
            if _normalize_label(option.get("label", "")) == wanted:
                return option.get("value"), None
        labels = ", ".join(str(o.get("label")) for o in enumeration) or "aucune"
        return None, (f"Brevo category attribute {name} has no option '{value}' "
                      f"(options: {labels}); {name} was not written. "
                      f"Add the option in Brevo or configure an existing label.")

    attr_type = definition.get("type")
    if attr_type in (None, "text"):
        return value, None
    if attr_type == "boolean":
        return _normalize_label(value) in ("yes", "oui", "true", "1"), None
    return None, (f"Brevo attribute {name} is of type '{attr_type}', not text, boolean or category; "
                  f"{name} was not written.")


# Backwards-compatible name used by earlier callers/tests.
resolve_acquisition_value = resolve_attribute_value


def date_attribute_writer(attribute_defs: List[Dict[str, Any]], name: str):
    """Return ``day -> value`` for a date attribute stored as date or text."""
    definition = _find_definition(attribute_defs, name)
    if definition is not None and definition.get("type") == "text":
        return french_long_date
    return lambda day: day.isoformat()


def _same_value(existing: Any, new: Any) -> bool:
    if existing in (None, ""):
        return new in (None, "")
    if isinstance(new, (int, float)):
        try:
            return float(existing) == float(new)
        except (TypeError, ValueError):
            return False
    return str(existing)[:len(str(new))] == str(new)


def last_played_matches(rows: List[Dict[str, Any]], resolve: EventResolver,
                        phone_to_email: Dict[str, str], today: date) -> Dict[str, date]:
    """Per buyer key, the date of the latest *match* already played that the
    buyer attended (scanned) or bought a ticket for (see ``counts_for_event``).

    Season tickets, camps and other events never count, nor do
    pre-generated abonné / member seats that were not scanned.
    """
    last: Dict[str, date] = {}
    for row in rows:
        _, day, event_type = resolve(row)
        if event_type != "match" or not day or day > today or not counts_for_event(row, played=True):
            continue
        key = row_key(row, phone_to_email)
        if key not in last or day > last[key]:
            last[key] = day
    return last


def ticket_status(match_count: int, thresholds: Optional[Dict[str, int]] = None) -> Optional[str]:
    """NEW / ONBOARDED / REGULAR from the number of distinct matches bought."""
    limits = {**DEFAULT_STATUT_THRESHOLDS, **(thresholds or {})}
    if match_count >= limits["REGULAR"]:
        return "REGULAR"
    if match_count >= limits["ONBOARDED"]:
        return "ONBOARDED"
    return "NEW" if match_count >= 1 else None


# Kept under its former name for readability of the profile code.
_order_season = season_ticket_season


def _most_common(values: List[Optional[str]]) -> Optional[str]:
    values = [v for v in values if v]
    return Counter(values).most_common(1)[0][0] if values else None


def ticket_profiles(rows: List[Dict[str, Any]], resolve: EventResolver, phone_to_email: Dict[str, str],
                    season: str, start_month: int = DEFAULT_SEASON_START_MONTH,
                    today: Optional[date] = None) -> Dict[str, Dict[str, Any]]:
    """Per buyer key, what TICKET_STATUT / CATEGORIE_* / attendance are computed from.

    * ``matches``: distinct matches attended or bought (``counts_for_event``);
    * ``attended``: distinct played matches with a scanned ticket;
    * ``season_ticket``: holds a season ticket for ``season`` (-> REGULAR),
      seen either as a pass / abonnement order or as "Place abonné" seats
      on this season's matches;
    * ``place`` / ``tariff``: from the latest bought match ticket (most
      frequent value within that order, e.g. 2 x Plein + 1 x Enfant ->
      Plein), else from the season ticket.
    Camps, partner and other events are ignored.
    """
    today = today or datetime.now(timezone.utc).date()
    profiles: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        name, day, event_type = resolve(row)
        if event_type not in ("match", "abonnement"):
            continue
        lines = row.get("lines") or []
        if event_type == "abonnement":
            if _order_season(name, day, row.get("order_date"), start_month) != season:
                continue
            abo_lines = lines
        else:
            abo_lines = [line for line in lines if line["kind"] == "abonne"]
            if abo_lines and not (day and season_for(day, start_month) == season):
                abo_lines = []
        profile = profiles.setdefault(row_key(row, phone_to_email), {
            "matches": set(), "attended": set(), "season_ticket": False,
            "place": None, "tariff": None, "place_at": None, "tariff_at": None,
            "abo_place": None, "abo_tariff": None, "scan_known": False,
        })
        if abo_lines or event_type == "abonnement":
            profile["season_ticket"] = True
            profile["season_ticket_name"] = (name if event_type == "abonnement"
                                             else _most_common([line["tariff"] for line in abo_lines])
                                             ) or profile.get("season_ticket_name")
            profile["abo_place"] = _most_common([line["place"] for line in abo_lines]) or profile["abo_place"]
            # On an abonnement order the "tariff" is the product name
            # ("Abonnement saison régulière - Adulte"), mapped like a tariff.
            profile["abo_tariff"] = (_most_common([line["tariff"] for line in abo_lines])
                                     or (name if event_type == "abonnement" else None) or profile["abo_tariff"])
        if event_type == "abonnement":
            continue

        played = bool(day and day <= today)
        if was_scanned(row) is not None:
            profile["scan_known"] = True
        if not counts_for_event(row, played):
            continue
        profile["matches"].add((name, day))
        if played and was_scanned(row):
            profile["attended"].add((name, day))
        bought = [line for line in lines if line["kind"] == "billet"] if lines else None
        places = [line["place"] for line in bought] if bought is not None else row.get("place_categories")
        tariffs = [line["tariff"] for line in bought] if bought is not None else row.get("tariff_categories")
        when = (day or date.min, str(row.get("order_date") or ""))
        for field, values in (("place", places), ("tariff", tariffs)):
            value = _most_common(values or [])
            if value and (profile[f"{field}_at"] is None or when >= profile[f"{field}_at"]):
                profile[field] = value
                profile[f"{field}_at"] = when
    for profile in profiles.values():
        profile["place"] = profile["place"] or profile["abo_place"]
        profile["tariff"] = profile["tariff"] or profile["abo_tariff"]
    return profiles


def first_order_types(rows: List[Dict[str, Any]], resolve: EventResolver,
                      phone_to_email: Dict[str, str]) -> Dict[str, Optional[str]]:
    """Per buyer key, the acquisition channel of the first purchase (earliest
    order date; undated orders only count when no dated order exists).

    A VIP season ticket is a B2B relationship, filed under Business
    Engagement like partner events, so it counts as ``partenaire``.
    """
    first: Dict[str, Tuple[str, Optional[str]]] = {}
    for row in rows:
        when = str(row.get("order_date") or "\uffff")
        key = row_key(row, phone_to_email)
        if key not in first or when < first[key][0]:
            name, _, event_type = resolve(row)
            if is_vip_season_ticket(row, name, event_type):
                event_type = "partenaire"
            first[key] = (when, event_type)
    return {key: event_type for key, (_, event_type) in first.items()}


def profile_status(profile: Dict[str, Any], thresholds: Optional[Dict[str, int]] = None) -> Optional[str]:
    """Season ticket holders are REGULAR; others by number of matches."""
    if profile["season_ticket"]:
        return "REGULAR"
    return ticket_status(len(profile["matches"]), thresholds)


class CategoryWriter:
    """Map a ticketing label to the value Brevo expects for one attribute.

    ``mapping`` translates Infomaniak labels to Brevo labels ("Adulte" ->
    "Plein"): an exact label first (case- and accent-insensitive), then the
    first key whose words are all in the label, so a few keywords cover
    the dozens of Infomaniak tariffs ("etudiant" -> "Réduit"). The attribute is
    never created (it is a category in the DMOS referential): when it is
    missing, or a label has no Brevo option, nothing is written and a
    single warning is raised.
    """

    def __init__(self, attribute_defs: List[Dict[str, Any]], name: str,
                 mapping: Optional[Dict[str, str]] = None):
        self.attribute_defs = attribute_defs
        self.name = name
        self.rules = list((mapping or {}).items())
        self.mapping = {_normalize_label(k): v for k, v in self.rules}
        self.available = _find_definition(attribute_defs, name) is not None
        self._cache: Dict[str, Any] = {}
        self.unmatched: Set[str] = set()

    def __call__(self, label: Optional[str]) -> Optional[Any]:
        if not label or not self.available:
            return None
        if label not in self._cache:
            target = self.mapping.get(_normalize_label(label)) or self._rule(label) or label
            value, warning = resolve_attribute_value(self.attribute_defs, self.name, target)
            if warning:
                self.unmatched.add(label)
            self._cache[label] = value
        return self._cache[label]

    def _rule(self, label: str) -> Optional[str]:
        """First mapping key whose words all appear in the label ("adulte"
        matches "Bundle ticket adulte + boisson"), in mapping order."""
        label_words = {w.lower() for w in _words(label)}
        for key, target in self.rules:
            if _contains_words(label_words, key):
                return target
        return None

    def warnings(self, used: bool) -> List[str]:
        if used and not self.available:
            return [f"Attribut {self.name} absent de Brevo : non renseigné."]
        if self.unmatched:
            labels = ", ".join(sorted(self.unmatched))
            return [f"{self.name} : valeur(s) billetterie sans option Brevo correspondante ({labels}). "
                    f"Ajouter l'option dans Brevo ou une correspondance dans ticket_attributes."]
        return []


def build_brevo_contact(buyer: Dict[str, Any], name_attrs: Dict[str, str]) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {
        "TICKETING_SOURCE": "Infomaniak",
        "TICKETING_ORDERS": buyer["orders_count"],
        "TICKETING_TICKETS": buyer["tickets_count"],
        "TICKETING_TOTAL_SPENT": buyer["total_spent"],
    }
    if buyer.get("first_name"):
        attributes[name_attrs["first"]] = buyer["first_name"]
    if buyer.get("last_name"):
        attributes[name_attrs["last"]] = buyer["last_name"]
    first_order = _brevo_date(buyer.get("first_order_date"))
    if first_order:
        attributes["TICKETING_FIRST_ORDER"] = first_order
    last_order = _brevo_date(buyer.get("last_order_date"))
    if last_order:
        attributes["TICKETING_LAST_ORDER"] = last_order
    if buyer.get("last_event"):
        attributes["TICKETING_LAST_EVENT"] = str(buyer["last_event"])[:255]
    if buyer.get("last_order_amount") is not None:
        attributes["TICKETING_LAST_ORDER_AMOUNT"] = buyer["last_order_amount"]
    if buyer.get("average_basket") is not None:
        attributes["TICKETING_AVG_BASKET"] = buyer["average_basket"]
    if buyer.get("free_tickets") is not None:
        attributes["TICKETING_FREE_TICKETS"] = buyer["free_tickets"]
        attributes["TICKETING_FREE_VALUE"] = buyer["free_value"]
    attributes["TICKETING_ONSITE_TICKETS"] = buyer.get("onsite_tickets", 0)
    for attribute, field in (("TICKETING_PAYMENT_METHOD", "payment_method"),
                             ("TICKETING_LAST_ORDER_ID", "last_order_number"),
                             ("TICKETING_PASS_CODE", "pass_code")):
        if buyer.get(field):
            attributes[attribute] = str(buyer[field])[:255]
    contact: Dict[str, Any] = {"attributes": attributes}
    if buyer.get("email"):
        contact["email"] = buyer["email"]
    return contact


def normalize_zip(value: Any) -> Optional[str]:
    """Swiss NPA from 1006, "1006" or "1004 Lausanne" (4 digits)."""
    if value in (None, ""):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    found = re.search(r"\b(\d{4,5})\b", str(value))
    return found.group(1) if found else None


def _phone_index(existing: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """``{+41...: email}`` for the SMS numbers already stored in Brevo."""
    index: Dict[str, str] = {}
    for email, attrs in existing.items():
        phone = normalize_phone(attrs.get(SMS_ATTRIBUTE))
        if phone:
            index.setdefault(phone, email)
    return index


def sync_infomaniak_to_brevo(ticketing, brevo, list_ids: Optional[List[int]] = None,
                             only_optin: bool = False, dry_run: bool = False,
                             max_orders: int = 100000,
                             acquisition_attribute: Optional[str] = DEFAULT_ACQUISITION_ATTRIBUTE,
                             acquisition_value: str = DEFAULT_ACQUISITION_VALUE,
                             event_lists: Optional[Dict[str, Any]] = None,
                             optin_attribute: Optional[str] = DEFAULT_OPTIN_ATTRIBUTE,
                             optin_value: str = DEFAULT_OPTIN_VALUE,
                             today: Optional[date] = None,
                             ticket_attributes: Optional[Dict[str, Any]] = None,
                             partner_acquisition_value: Optional[str] = DEFAULT_PARTNER_ACQUISITION_VALUE,
                             ) -> Dict[str, Any]:
    """Run one full sync.

    ``ticketing`` is an ``InfomaniakTicketingService``, ``brevo`` a
    ``BrevoService``. With ``only_optin`` buyers whose orders carry no
    newsletter consent are skipped. ``dry_run`` fetches and aggregates but
    writes nothing to Brevo; when ``brevo`` is given it still reads the
    existing contacts to report what would change.
    ``acquisition_attribute`` set to a falsy value disables the tagging
    (and therefore OPT_IN); ``optin_attribute`` falsy disables OPT_IN only.
    ``event_lists`` is the per-event list config (``club_code``,
    ``opponent_codes``, ``season``, ``event_types``...); ``None`` disables
    per-event lists (events are still classified with the defaults).
    ``ticket_attributes`` configures TICKET_STATUT / CATEGORIE_PLACE /
    CATEGORIE_TARIF (``statut_thresholds``, ``place_mapping``,
    ``tarif_mapping``; ``{"enabled": false}`` turns them off).
    ``partner_acquisition_value`` is the source written for contacts whose
    first purchase is a partner event (falsy: use ``acquisition_value``).
    """
    today = today or datetime.now(timezone.utc).date()
    orders = ticketing.get_orders(max_orders=max_orders)

    warnings: List[str] = []
    # Passes are season tickets: their holder, product name, net amount and
    # payment mode go through the same pipeline as orders (they classify as
    # "abonnement" from their product name).
    passes_path = getattr(ticketing, "passes_path", None)
    if isinstance(passes_path, str) and passes_path:
        try:
            orders = orders + ticketing.get_passes()
        except InfomaniakTicketingAPIError as e:
            warnings.append(f"Abonnements (passes) indisponibles ({e}) : seules les commandes sont synchronisées.")
    events: List[Dict[str, Any]] = []
    if orders:
        try:
            events = ticketing.get_events()
        except InfomaniakTicketingAPIError as e:
            warnings.append(f"Liste des événements indisponible ({e}) : noms/dates lus dans les commandes.")
    resolve = EventResolver(events, event_lists or {})

    # Events classified "ignorer" (tests, invitations...) are left out entirely.
    kept_orders, ignored_orders = [], 0
    for order in orders:
        row = normalize_order(order)
        if row and resolve(row)[2] == "ignorer":
            ignored_orders += 1
            continue
        kept_orders.append(order)
    rows = [r for r in (normalize_order(o) for o in kept_orders) if r]
    phone_to_email = buyer_keys(rows)
    buyers = aggregate_buyers(kept_orders)

    skipped_no_optin = 0
    selected = []
    for buyer in buyers.values():
        if only_optin and buyer.get("optin") is not True:
            skipped_no_optin += 1
            continue
        selected.append(buyer)

    event_types = resolve.types
    if event_types and "match" not in event_types.values():
        warnings.append("Aucun match reconnu parmi les événements : DERNIER_MATCH ne sera pas renseigné "
                        "(vérifier club_keywords ou event_types).")

    result: Dict[str, Any] = {
        "success": True,
        "dry_run": dry_run,
        "orders_fetched": len(orders),
        "orders_ignored": ignored_orders,
        "event_types": event_types,
        "unique_buyers": len(buyers),
        "phone_only_buyers": sum(1 for b in buyers.values() if not b["email"]),
        "contacts_selected": len(selected),
        "skipped_no_optin": skipped_no_optin,
        "new_contacts": None,
        "existing_contacts": None,
        "source_filled": None,
        "last_import_updated": None,
        "sms_conflicts": None,
        "acquisition_attribute": acquisition_attribute or None,
        "acquisition_value": acquisition_value if acquisition_attribute else None,
        "warnings": warnings,
        "list_ids": list_ids or [],
        "attributes_created": [],
        "import_process_ids": [],
        "event_lists": None,
        "synced_at": utc_now_iso(),
    }

    last_match = last_played_matches(rows, resolve, phone_to_email, today)
    ticket_config = ticket_attributes or {}
    start_month = int((event_lists or {}).get("season_start_month") or DEFAULT_SEASON_START_MONTH)
    season = (event_lists or {}).get("season") or current_season(start_month)
    profiles = (ticket_profiles(rows, resolve, phone_to_email, season, start_month, today)
                if ticket_config.get("enabled", True) else {})

    plan = None
    if event_lists is not None and selected:
        plan = plan_event_lists(kept_orders, events, {b["key"] for b in selected}, event_lists, today)
        warnings.extend(plan["warnings"])
        result["event_lists"] = {
            "season": plan["season"],
            "folder": plan["folder"],
            "events_other_season": plan["events_other_season"],
            "lists": {name: {"event": e["event"], "date": e["date"], "type": e["type"], "folder": e["folder"],
                             "contacts": len(e["keys"]), "list_id": None, "created": False}
                      for name, e in sorted(plan["lists"].items())},
        }

    if not selected or brevo is None:
        if dry_run:
            result["sample"] = [build_brevo_contact(b, _name_attributes(set())) for b in selected[:5]]
        return result

    # Read before writing: which buyers are not in Brevo yet, and what the
    # existing ones already hold (source, last match, SMS). Phone-only
    # buyers are matched on the SMS attribute.
    existing = brevo.get_contact_index()
    phones_in_brevo = _phone_index(existing)

    def brevo_email(buyer: Dict[str, Any]) -> Optional[str]:
        if buyer["email"]:
            return buyer["email"] if buyer["email"] in existing else None
        return phones_in_brevo.get(buyer["phone"])

    matched = {b["key"]: brevo_email(b) for b in selected}
    current_attrs = {key: existing.get(email) if email else None for key, email in matched.items()}
    new_keys = {key for key, attrs in current_attrs.items() if attrs is None}
    result["new_contacts"] = len(new_keys)
    result["existing_contacts"] = len(selected) - len(new_keys)

    attribute_defs = brevo.get_contact_attributes()
    name_attrs = _name_attributes({a.get("name") for a in attribute_defs})

    # Guideline step 6: SOURCE_ACQUISITION + OPT_IN = YES on every contact
    # whose SOURCE_ACQUISITION is empty. The source follows the buyer's first
    # purchase: a partner event -> Business Engagement, anything else ->
    # Billetterie.
    source_empty = set()
    if acquisition_attribute:
        source_empty = {key for key, attrs in current_attrs.items()
                        if (attrs or {}).get(acquisition_attribute) in (None, "")}
    first_types = first_order_types(rows, resolve, phone_to_email)

    def source_label(key: str) -> str:
        if partner_acquisition_value and first_types.get(key) == "partenaire":
            return partner_acquisition_value
        return acquisition_value

    sources_sent: Dict[str, Any] = {}
    for label in sorted({source_label(k) for k in source_empty}):
        sources_sent[label], warning = resolve_attribute_value(attribute_defs, acquisition_attribute, label)
        if warning:
            warnings.append(warning)
    acquisition_for = {k: sources_sent[source_label(k)] for k in source_empty
                       if sources_sent.get(source_label(k)) is not None}
    optin_to_send = None
    if acquisition_for and optin_attribute:
        if _find_definition(attribute_defs, optin_attribute) is None:
            warnings.append(f"Attribut {optin_attribute} absent de Brevo : OPT_IN non renseigné.")
        else:
            optin_to_send, warning = resolve_attribute_value(attribute_defs, optin_attribute, optin_value)
            if warning:
                warnings.append(warning)
    for warning in warnings:
        logger.warning(warning)
    result["acquisition_value_sent"] = sources_sent.get(acquisition_value)
    result["acquisition_values_sent"] = sources_sent
    result["optin_value_sent"] = optin_to_send
    result["source_filled"] = len(acquisition_for)
    result["source_filled_by_value"] = dict(Counter(source_label(k) for k in acquisition_for))

    # DERNIER_MATCH / DERNIER_IMPORT (guideline step 3) and SMS.
    write_match = date_attribute_writer(attribute_defs, LAST_MATCH_ATTRIBUTE)
    write_import = date_attribute_writer(attribute_defs, LAST_IMPORT_ATTRIBUTE)
    has_zip = _find_definition(attribute_defs, ZIP_ATTRIBUTE) is not None
    has_birthday = _find_definition(attribute_defs, BIRTHDAY_ATTRIBUTE) is not None
    write_birthday = date_attribute_writer(attribute_defs, BIRTHDAY_ATTRIBUTE)
    statut_thresholds = ticket_config.get("statut_thresholds")
    write_statut = CategoryWriter(attribute_defs, TICKET_STATUT_ATTRIBUTE)
    write_place = CategoryWriter(attribute_defs, PLACE_ATTRIBUTE, ticket_config.get("place_mapping"))
    write_tariff = CategoryWriter(attribute_defs, TARIFF_ATTRIBUTE, ticket_config.get("tarif_mapping"))
    extra: Dict[str, Dict[str, Any]] = {}
    sms_assigned: Dict[str, str] = {}
    sms_conflicts = 0
    for buyer in selected:
        key = buyer["key"]
        current = current_attrs[key] or {}
        attrs: Dict[str, Any] = {}
        match_day = last_match.get(key)
        if match_day:
            attrs[LAST_MATCH_ATTRIBUTE] = write_match(match_day)
            # Stamped only once a new match has been played, as a manual
            # import is made after the match: a purchase alone does not.
            if not _same_value(current.get(LAST_MATCH_ATTRIBUTE), attrs[LAST_MATCH_ATTRIBUTE]):
                attrs[LAST_IMPORT_ATTRIBUTE] = write_import(today)
        zip_code = normalize_zip(buyer.get("zip_code"))
        if zip_code and has_zip and current.get(ZIP_ATTRIBUTE) in (None, ""):
            attrs[ZIP_ATTRIBUTE] = zip_code
        birthday = parse_event_date(buyer.get("birthdate"))
        if birthday and has_birthday and current.get(BIRTHDAY_ATTRIBUTE) in (None, ""):
            attrs[BIRTHDAY_ATTRIBUTE] = write_birthday(birthday)
        phone = buyer.get("phone")
        if phone and not normalize_phone(current.get(SMS_ATTRIBUTE)):
            # Brevo refuses an SMS number already held by another contact.
            owner = phones_in_brevo.get(phone) or sms_assigned.get(phone)
            if owner in (None, matched[key] or key):
                attrs[SMS_ATTRIBUTE] = phone
                sms_assigned[phone] = matched[key] or key
            else:
                sms_conflicts += 1
        profile = profiles.get(key)
        if profile:
            attrs["TICKETING_MATCHES"] = len(profile["matches"])
            if profile.get("season_ticket_name"):
                attrs["TICKETING_SEASON_TICKET"] = str(profile["season_ticket_name"])[:255]
            if profile["scan_known"]:
                attrs["TICKETING_MATCHES_ATTENDED"] = len(profile["attended"])
            for attribute, value in ((TICKET_STATUT_ATTRIBUTE, write_statut(
                                         profile_status(profile, statut_thresholds))),
                                     (PLACE_ATTRIBUTE, write_place(profile["place"])),
                                     (TARIFF_ATTRIBUTE, write_tariff(profile["tariff"]))):
                if value is not None:
                    attrs[attribute] = value
        if key in acquisition_for:
            attrs[acquisition_attribute] = acquisition_for[key]
            if optin_to_send is not None:
                attrs[optin_attribute] = optin_to_send
        extra[key] = attrs
    result["last_import_updated"] = sum(1 for a in extra.values() if LAST_IMPORT_ATTRIBUTE in a)
    if profiles:
        warnings.extend(write_statut.warnings(True))
        warnings.extend(write_place.warnings(any(p["place"] for p in profiles.values())))
        warnings.extend(write_tariff.warnings(any(p["tariff"] for p in profiles.values())))
    result["ticket_attributes"] = {
        attribute: sum(1 for a in extra.values() if attribute in a)
        for attribute in (TICKET_STATUT_ATTRIBUTE, PLACE_ATTRIBUTE, TARIFF_ATTRIBUTE)
    }
    result["sms_conflicts"] = sms_conflicts
    if sms_conflicts:
        warnings.append(f"{sms_conflicts} numéro(s) déjà associé(s) à un autre contact Brevo : SMS non renseigné.")

    if plan and plan["lists"]:
        summary = result["event_lists"]
        summary["folders"] = {}
        for folder in sorted({e["folder"] for e in plan["lists"].values()}):
            names = [n for n, e in plan["lists"].items() if e["folder"] == folder]
            state = ensure_event_lists(brevo, folder, names, create=not dry_run)
            summary["folders"][folder] = {"id": state["folder_id"], "created": state["created_folder"]}
            for name, list_id in state["list_ids"].items():
                summary["lists"][name]["list_id"] = list_id
                summary["lists"][name]["created"] = name in state["created_lists"]
        missing = [f for f, v in summary["folders"].items() if v["id"] is None]
        if missing and brevo is not None:
            # Folder names only (no contact data), to fix the "folders" setting.
            summary["brevo_folders"] = {str(f.get("id")): f.get("name") for f in brevo.get_all_folders()}
            warnings.append(f"Dossier(s) Brevo introuvable(s) : {', '.join(missing)}"
                            + (" (créé(s) à la synchro)" if dry_run else "")
                            + ". Voir brevo_folders et régler 'folders' dans la config si le dossier existe sous un autre nom.")

    contacts: Dict[str, Dict[str, Any]] = {}
    for buyer in selected:
        contact = build_brevo_contact(buyer, name_attrs)
        contact["attributes"].update(extra[buyer["key"]])
        if not buyer["email"] and matched[buyer["key"]]:
            # Phone-only buyer already in Brevo under an email: update that contact.
            contact["email"] = matched[buyer["key"]]
        contacts[buyer["key"]] = contact

    if dry_run:
        result["sample"] = [contacts[b["key"]] for b in selected[:5]]
        return result

    # No-op for attributes the club's Brevo already has.
    wanted_attrs = {**TICKETING_ATTRIBUTES, LAST_MATCH_ATTRIBUTE: "date", LAST_IMPORT_ATTRIBUTE: "date"}
    if acquisition_for:
        wanted_attrs[acquisition_attribute] = "text"
    result["attributes_created"] = brevo.ensure_contact_attributes(wanted_attrs)

    event_list_ids: Dict[str, List[int]] = {}
    if plan:
        for name, entry in plan["lists"].items():
            for key in entry["keys"]:
                event_list_ids.setdefault(key, []).append(result["event_lists"]["lists"][name]["list_id"])

    # Contacts with an email: bulk import. Contacts known only by phone are
    # created one by one (POST /contacts accepts SMS as sole identifier),
    # directly into the global and event lists.
    with_email = [c for c in contacts.values() if c.get("email")]
    invalid_phones = 0
    for start in range(0, len(with_email), IMPORT_BATCH_SIZE):
        batch = with_email[start:start + IMPORT_BATCH_SIZE]
        response = brevo.import_contacts(batch, list_ids=list_ids)
        result["import_process_ids"].append(response.get("processId"))
        invalid_phones += response.get("invalid_phones") or 0
        logger.info("Brevo import batch %s: %s contacts", response.get("processId"), len(batch))
    phone_only_created = 0
    for key, contact in contacts.items():
        if contact.get("email"):
            continue
        if SMS_ATTRIBUTE not in contact["attributes"]:
            warnings.append("Un acheteur sans email, dont le numéro est déjà pris par un autre contact : ignoré.")
            continue
        try:
            brevo.upsert_contact(contact["attributes"],
                                 list_ids=sorted(set((list_ids or []) + event_list_ids.get(key, []))))
        except BrevoAPIError as e:
            # A number Brevo rejects identifies nobody: skip this buyer, keep syncing.
            if not is_invalid_phone_error(e):
                raise
            invalid_phones += 1
            continue
        phone_only_created += 1
    result["phone_only_upserted"] = phone_only_created
    if invalid_phones:
        warnings.append(f"{invalid_phones} numéro(s) refusé(s) par Brevo : SMS non renseigné "
                        "(acheteurs sans email ignorés).")
    result["invalid_phones"] = invalid_phones

    # One import per event list: the import creates or updates the contact
    # and adds it to the list in one step, so it does not depend on the
    # (asynchronous) global import having finished. Payloads are identical
    # to the global one, so the order Brevo processes them in is irrelevant.
    if plan:
        for name, entry in sorted(plan["lists"].items()):
            list_id = result["event_lists"]["lists"][name]["list_id"]
            members = [contacts[k] for k in sorted(entry["keys"]) if contacts[k].get("email")]
            for start in range(0, len(members), IMPORT_BATCH_SIZE):
                response = brevo.import_contacts(members[start:start + IMPORT_BATCH_SIZE], list_ids=[list_id])
                result["import_process_ids"].append(response.get("processId"))

    return result
