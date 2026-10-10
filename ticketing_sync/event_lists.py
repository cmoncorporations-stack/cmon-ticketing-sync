"""One Brevo list per ticketing event, named after the DMOS convention.

DMOS naming referential (Notion "DMOS — Référentiel de conventions de
nommage", section 3 "Listes et dossiers Brevo"):

* match list: ``CMON-[CLUB]-[OPPOSANT]-[YYYYMMDD]`` (e.g. CMON-FCZ-BSC-20260315),
  club codes are the official abbreviation (3 letters when possible);
* separator ``-``, no accents, no spaces, date always last;
* folders: French name, capitalised, no prefix; season as ``YYYY-YYYY``.

Events are classified before anything is written (``classify_event``),
and each type is filed in its own Brevo folder (``DEFAULT_FOLDERS``,
overridable with ``folders``):

* ``match``: the name opposes the club to an opponent ("Foxes vs Lions")
  -> ``CMON-[CLUB]-[OPPOSANT]-[YYYYMMDD]`` in ``Billetterie YYYY-YYYY``;
  only matches feed DERNIER_MATCH / TICKET_STATUT / CATEGORIE_*;
* ``abonnement``: season tickets -> ``CMON-Abonnes-YYYY-YYYY`` in
  ``Abonnement``;
* ``camp``: camps / stages -> ``CMON-[CLUB]-[Description]-[YYYYMMDD]`` in
  ``Billetterie YYYY-YYYY``;
* ``partenaire``: partner / sponsor events -> same naming, in
  ``Business Engagement``;
* ``membre``: member cards ("Carte membre") -> ``CMON-Membres-YYYY-YYYY`` in
  ``Members & Staff``;
* ``autre``: anything else -> same naming, in ``Billetterie YYYY-YYYY``;
* ``ignorer``: test events, invitations... -> left out of the sync.

``event_types`` (keyword -> type) overrides the automatic detection.
``event_date_overrides`` (keyword -> ``YYYY-MM-DD``) corrects an event date
that is wrong in the ticketing and cannot be fixed there (the time of day
is kept); keywords match whole words of the event name.
Every sync looks folders and lists up by name before creating anything, so
re-running it only creates the lists of new events and keeps adding new
buyers to the existing ones.
"""
import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ticketing_sync.infomaniak import (
    buyer_keys,
    counts_for_event,
    has_season_ticket_line,
    normalize_order,
    row_key,
)

DEFAULT_SEASON_START_MONTH = 7
DEFAULT_FOLDER_TEMPLATE = "Billetterie {season}"
DEFAULT_FOLDERS = {
    "match": DEFAULT_FOLDER_TEMPLATE,
    "camp": DEFAULT_FOLDER_TEMPLATE,
    "autre": DEFAULT_FOLDER_TEMPLATE,
    "abonnement": "Abonnement",
    # VIP season tickets (companies, hospitality) are a B2B relationship.
    "abonnement_vip": "Business Engagement",
    "partenaire": "Business Engagement",
    # Member-card holders ("Carte membre").
    "membre": "Members & Staff",
}
DEFAULT_CLUB_KEYWORDS = ("foxes", "pully")
DEFAULT_ABONNEMENT_KEYWORDS = ("abonnement", "abo", "season ticket", "saisonkarte", "pass")
# Infomaniak placeholders for matches not scheduled yet.
PLACEHOLDER_KEYWORDS = ("a definir", "tbd", "to be defined")
_SEASON_IN_NAME = re.compile(r"(20\d{2})\s*[-/]\s*(20\d{2})")
DEFAULT_PARTENAIRE_KEYWORDS = ("partenaire", "sponsor", "business", "b2b")
DEFAULT_CAMP_KEYWORDS = ("camp", "stage")
DEFAULT_MEMBRE_KEYWORDS = ("carte membre", "member card", "mitgliederkarte")

EVENT_TYPES = ("match", "abonnement", "membre", "camp", "partenaire", "autre", "ignorer")

# "Foxes vs Lions" style separators; checked first because Infomaniak names
# append details with dashes: "Pully Lausanne Foxes vs Fribourg Olympic -
# 09.04.26 - 1/2 Finale - Game 3".
_VERSUS = re.compile(r"\s+(?:vs\.?|v\.?|contre|x)\s+", re.IGNORECASE)
# A dash with a space on at least one side ("A DEFINIR- 03.02.27"), never
# inside a word ("Monthey-Chablais").
_DASHES = re.compile(r"\s+[-–—/]\s*|\s*[-–—/]\s+")
_STOPWORDS = {"de", "du", "des", "la", "le", "les", "d", "l", "et", "the", "of",
              "basket", "basketball", "bbc", "bc", "club", "sa", "fc"}


def ascii_fold(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()


def _words(value: str) -> List[str]:
    return [w for w in re.split(r"[^A-Za-z0-9]+", ascii_fold(value)) if w]


def parse_event_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).date()
    text = str(value).strip()
    for fmt, size in (("%Y-%m-%d", 10), ("%d.%m.%Y", 10), ("%d/%m/%Y", 10), ("%Y%m%d", 8)):
        try:
            return datetime.strptime(text[:size], fmt).date()
        except ValueError:
            continue
    return None


def season_for(day: date, start_month: int = DEFAULT_SEASON_START_MONTH) -> str:
    start = day.year if day.month >= start_month else day.year - 1
    return f"{start}-{start + 1}"


def current_season(start_month: int = DEFAULT_SEASON_START_MONTH) -> str:
    return season_for(datetime.now(timezone.utc).date(), start_month)


def normalize_code(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", ascii_fold(value).upper())


def describe(value: str) -> str:
    """Free-text event name -> ``Soiree-Partenaires`` (no accents/spaces)."""
    return "-".join(w[:1].upper() + w[1:] for w in _words(value))[:60]


def find_opponent(event_name: str, club_keywords: Iterable[str]) -> Optional[str]:
    """Opponent of a match name, or ``None`` when the name is not a match.

    With a "vs" separator, details after a dash are dropped on both sides
    ("Playoffs - Foxes vs Fribourg Olympic - 09.04.26 - Game 3" -> Fribourg
    Olympic). Without one, a single dash is accepted ("Massagno – Foxes").
    """
    versus = [p.strip() for p in _VERSUS.split(event_name) if p.strip()]
    if len(versus) == 2:
        home_part, away_part = versus
        parts = [_DASHES.split(home_part)[-1].strip(), _DASHES.split(away_part)[0].strip()]
    elif len(versus) == 1:
        parts = [p.strip() for p in _DASHES.split(event_name) if p.strip()]
    else:
        return None
    if len(parts) != 2:
        return None
    keywords = [ascii_fold(k).lower() for k in club_keywords]

    def is_club(part: str) -> bool:
        folded = ascii_fold(part).lower()
        return any(k in folded for k in keywords)

    home, away = (is_club(p) for p in parts)
    if home == away:
        return None
    return parts[1] if home else parts[0]


def opponent_code(opponent: str, mapping: Dict[str, str]) -> Tuple[str, bool]:
    """Return ``(code, from_mapping)``.

    The mapping (opponent name -> official code) is matched accent- and
    case-insensitively on whole words, in both directions, so
    "Lions de Genève" covers events titled "Lions" or "Lions de Genève BC".
    Without a match, the first three letters of the first significant
    word are used and the caller reports it so the code can be checked.
    """
    opponent_words = {w.lower() for w in _words(opponent)}
    for name, code in mapping.items():
        key_words = {w.lower() for w in _words(name)}
        if key_words and opponent_words and (key_words <= opponent_words or opponent_words <= key_words):
            return normalize_code(code), True
    words = [w for w in _words(opponent) if w.lower() not in _STOPWORDS] or _words(opponent)
    return normalize_code(words[0][:3]) if words else "XXX", False


def build_list_name(club_code: str, event_name: str, event_day: date,
                    opponent_codes: Dict[str, str], club_keywords: Iterable[str],
                    event_type: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Return ``(list_name, warning)`` for one event.

    Only matches get an opponent code. Other events are described by the
    part of their name before the first dash ("Camp Foxes - 12 au 16
    octobre 2026" -> ``Camp-Foxes``), the date being in the list name.
    """
    stamp = event_day.strftime("%Y%m%d")
    opponent = find_opponent(event_name, club_keywords) if event_type in (None, "match") else None
    if opponent:
        code, mapped = opponent_code(opponent, opponent_codes)
        warning = None if mapped else (
            f"Code adversaire '{code}' déduit automatiquement de '{opponent}' "
            f"(ajouter '{opponent}' dans opponent_codes pour fixer le code officiel)")
        return f"CMON-{club_code}-{code}-{stamp}", warning
    head = _DASHES.split(event_name)[0].strip() if event_type not in (None, "match") else event_name
    return f"CMON-{club_code}-{describe(head) or describe(event_name) or 'Evenement'}-{stamp}", None


def event_catalog(events: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    catalog: Dict[str, Dict[str, Any]] = {}
    for event in events:
        event_id = event.get("id") or event.get("event_id")
        if event_id is not None:
            catalog[str(event_id)] = event
    return catalog


def resolve_event(row: Dict[str, Any], catalog: Dict[str, Dict[str, Any]]) -> Tuple[Optional[str], Optional[date]]:
    """Event name and day of a normalized order, completed from the events endpoint."""
    known = catalog.get(str(row["event_id"])) if row.get("event_id") is not None else None
    name = row.get("event_name") or (known and (known.get("name") or known.get("title"))) or None
    raw_date = row.get("event_date") or (known and (known.get("date") or known.get("start")
                                                    or known.get("start_date")))
    return name, parse_event_date(raw_date)


def _singular(word: str) -> str:
    word = word.lower()
    return word[:-1] if len(word) > 3 and word[-1] in "sx" else word


def _contains_words(name_words: Set[str], keyword: str) -> bool:
    """Every word of ``keyword`` appears in the event name (case, accents
    and plural insensitive: "invitation" matches "Invitations")."""
    words = {_singular(w) for w in _words(keyword)}
    return bool(words) and words <= {_singular(w) for w in name_words}


def classify_event(name: str, config: Dict[str, Any]) -> str:
    """Return one of ``EVENT_TYPES`` for an event name (see module doc)."""
    name_words = {w.lower() for w in _words(name)}
    for keyword, event_type in (config.get("event_types") or {}).items():
        if event_type in EVENT_TYPES and _contains_words(name_words, keyword):
            return event_type
    # Partner packages for a match ("Soirée partenaires Foxes vs Lions") are
    # partner events, hence partners are checked before matches.
    for event_type, defaults in (("membre", DEFAULT_MEMBRE_KEYWORDS),
                                 ("abonnement", DEFAULT_ABONNEMENT_KEYWORDS),
                                 ("partenaire", DEFAULT_PARTENAIRE_KEYWORDS),
                                 ("camp", DEFAULT_CAMP_KEYWORDS)):
        for keyword in config.get(f"{event_type}_keywords") or defaults:
            if _contains_words(name_words, keyword):
                return event_type
    if find_opponent(name, config.get("club_keywords") or DEFAULT_CLUB_KEYWORDS):
        return "match"
    return "autre"


def date_override(name: Optional[str], config: Dict[str, Any]) -> Optional[date]:
    """Corrected day of an event from ``event_date_overrides``, if any."""
    if not name:
        return None
    name_words = {w.lower() for w in _words(name)}
    for keyword, value in (config.get("event_date_overrides") or {}).items():
        if _contains_words(name_words, keyword):
            return parse_event_date(value)
    return None


class EventResolver:
    """Resolve and classify the event of each order (cached per event)."""

    def __init__(self, events: Iterable[Dict[str, Any]], config: Optional[Dict[str, Any]] = None):
        self.catalog = event_catalog(events)
        self.config = config or {}
        self._types: Dict[str, str] = {}

    def __call__(self, row: Dict[str, Any]) -> Tuple[Optional[str], Optional[date], Optional[str]]:
        name, day = resolve_event(row, self.catalog)
        day = date_override(name, self.config) or day
        if not name:
            return None, day, None
        if name not in self._types:
            self._types[name] = classify_event(name, self.config)
        return name, day, self._types[name]

    @property
    def types(self) -> Dict[str, str]:
        return dict(self._types)


def is_vip(label: Optional[str]) -> bool:
    return bool(label) and "vip" in {w.lower() for w in _words(label)}


def season_ticket_labels(row: Dict[str, Any], name: Optional[str], event_type: Optional[str]) -> List[str]:
    """Product names of the season tickets in an order: the pass name for a
    pass / abonnement order, the tariff of its "Place abonné" seats otherwise."""
    if event_type == "abonnement":
        return [name or ""]
    return [line["tariff"] or "" for line in row.get("lines") or [] if line["kind"] == "abonne"]


def is_vip_season_ticket(row: Dict[str, Any], name: Optional[str], event_type: Optional[str]) -> bool:
    return any(is_vip(label) for label in season_ticket_labels(row, name, event_type))


def is_placeholder(name: str) -> bool:
    name_words = {w.lower() for w in _words(name)}
    return any(_contains_words(name_words, k) for k in PLACEHOLDER_KEYWORDS)


def season_ticket_season(name: Optional[str], day: Optional[date], order_date: Any,
                         start_month: int = DEFAULT_SEASON_START_MONTH) -> Optional[str]:
    """Season a season ticket belongs to: event date, else a 'YYYY-YYYY' in
    its name ("Abonnement Adulte I Saison 2025 - 2026"), else the order date."""
    if day:
        return season_for(day, start_month)
    found = _SEASON_IN_NAME.search(name or "")
    if found:
        return f"{found.group(1)}-{found.group(2)}"
    ordered = parse_event_date(order_date)
    return season_for(ordered, start_month) if ordered else None


def plan_event_lists(orders: Iterable[Dict[str, Any]], events: Iterable[Dict[str, Any]],
                     allowed_keys: Set[str], config: Dict[str, Any],
                     today: Optional[date] = None) -> Dict[str, Any]:
    """Group buyer keys (email, else phone) per list for the configured season.

    ``events`` (from the events endpoint) fills name/date for orders that
    only reference an event id.
    """
    start_month = int(config.get("season_start_month") or DEFAULT_SEASON_START_MONTH)
    season = config.get("season") or current_season(start_month)
    club_code = normalize_code(config.get("club_code") or "")
    opponent_codes = config.get("opponent_codes") or {}
    club_keywords = config.get("club_keywords") or DEFAULT_CLUB_KEYWORDS
    folders = dict(DEFAULT_FOLDERS)
    if config.get("lists_folder"):
        folders.update({t: config["lists_folder"] for t in ("match", "camp", "autre")})
    folders.update(config.get("folders") or {})
    folders = {t: f.format(season=season) for t, f in folders.items()}
    season_folder = folders["match"]

    plan: Dict[str, Any] = {
        "season": season,
        "folder": season_folder,
        "folders": folders,
        "lists": {},
        "warnings": [],
        "events_other_season": 0,
        "event_types": {},
    }
    if not club_code:
        plan["warnings"].append("club_code manquant dans la configuration : listes par événement non créées.")
        return plan

    resolve = EventResolver(events, config)
    rows = [r for r in (normalize_order(o) for o in orders) if r]
    phone_to_email = buyer_keys(rows)
    undated: Set[str] = set()
    warned: Set[str] = set()
    today = today or datetime.now(timezone.utc).date()
    placeholders: Set[str] = set()

    def add(list_name: str, name: str, event_type: str, folder: str, list_date: Optional[str], key: str) -> None:
        entry = plan["lists"].setdefault(list_name, {"event": name, "date": list_date, "type": event_type,
                                                     "folder": folder, "keys": set()})
        entry["keys"].add(key)

    abonnes_list = f"CMON-Abonnes-{season}"
    abonnes_vip_list = f"CMON-Abonnes-VIP-{season}"
    membres_list = f"CMON-Membres-{season}"

    def add_season_ticket(row: Dict[str, Any], name: str, event_type: str, key: str) -> None:
        # VIP season tickets get their own list under Business Engagement,
        # and are not repeated in the regular abonnés list.
        if is_vip_season_ticket(row, name, event_type):
            add(abonnes_vip_list, "Abonnements VIP", "abonnement_vip", folders["abonnement_vip"], None, key)
        else:
            add(abonnes_list, "Abonnements", "abonnement", folders["abonnement"], None, key)

    for row in rows:
        key = row_key(row, phone_to_email)
        if key not in allowed_keys:
            continue
        name, event_day, event_type = resolve(row)
        if not name or event_type == "ignorer":
            continue

        if event_type == "membre":
            add(membres_list, "Membres (Carte membre)", "membre", folders["membre"], None, key)
            continue
        if event_type == "abonnement":
            ticket_season = season_ticket_season(name, event_day, row.get("order_date"), start_month)
            if ticket_season not in (None, season):
                plan["events_other_season"] += 1
                continue
            add_season_ticket(row, name, event_type, key)
            continue
        if not event_day:
            undated.add(name)
            continue
        if season_for(event_day, start_month) != season:
            plan["events_other_season"] += 1
            continue
        # Season tickets also show up as "Place abonné" seats on matches, and
        # member cards as "Carte membre" seats (matches and camps).
        if has_season_ticket_line(row):
            add_season_ticket(row, name, event_type, key)
        if any(line["kind"] == "membre" for line in row.get("lines") or []):
            add(membres_list, "Membres (Carte membre)", "membre", folders["membre"], None, key)
        if is_placeholder(name):
            placeholders.add(name)
            continue
        # Match lists hold the fans who came or bought a ticket, not every
        # pre-generated abonné / member seat.
        if not counts_for_event(row, played=event_day <= today):
            continue
        list_name, warning = build_list_name(club_code, name, event_day, opponent_codes, club_keywords,
                                             event_type)
        if warning and list_name not in warned:
            warned.add(list_name)
            plan["warnings"].append(f"{list_name} : {warning}")
        add(list_name, name, event_type, folders.get(event_type, season_folder), event_day.isoformat(), key)

    for name in sorted(placeholders):
        plan["warnings"].append(f"Événement '{name}' encore « A DEFINIR » : liste créée quand il sera programmé.")
    plan["event_types"] = resolve.types
    for name in sorted(undated):
        plan["warnings"].append(f"Événement '{name}' sans date : pas de liste (la date est obligatoire dans le nom).")
    return plan


def _norm_name(value: Any) -> str:
    return " ".join(w.lower() for w in _words(str(value or "")))


def find_folder(folders: Iterable[Dict[str, Any]], folder_name: str) -> Optional[Dict[str, Any]]:
    """The club's folder for ``folder_name``: same name ignoring case,
    accents and punctuation, else the only folder whose name contains it
    ("01 - Business Engagement (B2B)" for "Business Engagement")."""
    folders = list(folders)
    target = _norm_name(folder_name)
    for folder in folders:
        if folder.get("name") == folder_name:
            return folder
    same = [f for f in folders if _norm_name(f.get("name")) == target]
    if same:
        return same[0]
    containing = [f for f in folders if target and f" {target} " in f" {_norm_name(f.get('name'))} "]
    return containing[0] if len(containing) == 1 else None


def ensure_event_lists(brevo, folder_name: str, list_names: Iterable[str],
                       create: bool = True) -> Dict[str, Any]:
    """Find or create the folder and one list per name.

    Lists are looked up by name in the whole account, so a list moved to
    another folder by hand is reused, never duplicated. The folder is only
    needed (and created when missing) for lists that do not exist yet.
    Returns ``{"folder_id", "list_ids": {name: id|None}, "created_folder",
    "created_lists"}``. With ``create=False`` (dry run) nothing is
    written and missing lists map to ``None``.
    """
    names = sorted(set(list_names))
    result: Dict[str, Any] = {"folder_id": None, "list_ids": {n: None for n in names},
                              "created_folder": False, "created_lists": []}
    folder = find_folder(brevo.get_all_folders(), folder_name)
    existing: Dict[str, Any] = {}
    for lst in list(brevo.get_every_list() or []) + (list(brevo.get_folder_lists(folder["id"]) or [])
                                                    if folder else []):
        existing.setdefault(_norm_name(lst.get("name")), lst.get("id"))
    for name in names:
        result["list_ids"][name] = existing.get(_norm_name(name))
    if folder is not None:
        result["folder_id"] = folder["id"]
    missing = [n for n in names if result["list_ids"][n] is None]
    if not missing or not create:
        return result
    if folder is None:
        folder = brevo.create_folder(folder_name)
        result["created_folder"] = True
        result["folder_id"] = folder["id"]
    for name in missing:
        result["list_ids"][name] = brevo.create_list(name, folder_id=folder["id"])["id"]
        result["created_lists"].append(name)
    return result
