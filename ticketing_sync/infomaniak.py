"""Client for the Infomaniak ticketing (eTickets) shop API.

Authentication (https://etickets.infomaniak.com/docs/app/index.html, "Login"):

* ``key`` header: shop API key with sales access (Manager > Shop /
  Availability online > Api access). Enough for public endpoints (events).
* Ticket-office endpoints (orders, tickets, passes) also need either
  ``Authorization``: an API token of the manager account (My profile >
  Developer > API tokens), or ``Credential``: the value returned by
  ``GET /login`` (key + user + application password).

Pagination is ``limit`` / ``offset``. The base URL and endpoint paths are
overridable (``infomaniak`` section of the club config file) and the
response parsing is deliberately tolerant: it accepts either a bare list
or an object wrapping the list under ``data`` / ``orders`` / ``events`` /
``items``, and both nested (``customer: {...}``) and flat buyer fields.
"""
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import requests


class InfomaniakTicketingAPIError(Exception):
    pass


class InfomaniakTicketingRateLimitError(InfomaniakTicketingAPIError):
    pass


DEFAULT_BASE_URL = "https://etickets.infomaniak.com/api/shop"
DEFAULT_EVENTS_PATH = "/events"
DEFAULT_ORDERS_PATH = "/orders"
LANGUAGES = {"fr": "fr_FR", "en": "en_GB", "de": "de_DE"}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_LIST_KEYS = ("data", "orders", "events", "items", "results")


def _extract_list(payload: Any, *extra_keys: str) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in (*extra_keys, *_LIST_KEYS):
            value = payload.get(key)
            if isinstance(value, list):
                return [p for p in value if isinstance(p, dict)]
            if isinstance(value, dict):
                nested = _extract_list(value, *extra_keys)
                if nested:
                    return nested
    return []


def _first(d: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return value
    return None


class InfomaniakTicketingService:
    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 currency: str = "1", language: str = "fr_FR",
                 events_path: Optional[str] = None,
                 orders_path: Optional[str] = None,
                 passes_path: Optional[str] = None,
                 page_size: int = 50,
                 token: Optional[str] = None,
                 credential: Optional[str] = None,
                 token_scheme: str = "Bearer"):
        if not api_key:
            raise ValueError("Infomaniak ticketing API key is required")
        self.api_key = api_key
        self.token = token or None
        self.credential = credential or None
        self.token_scheme = token_scheme
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.events_path = events_path or DEFAULT_EVENTS_PATH
        self.orders_path = orders_path or DEFAULT_ORDERS_PATH
        # Season tickets / passes (holder, birthdate, NPA, phone): /passes,
        # off unless configured.
        self.passes_path = passes_path
        self.page_size = page_size
        self.base_headers = {
            "key": api_key,
            "currency": str(currency),
            "Accept-Language": LANGUAGES.get(language, language),
            "accept": "application/json",
        }
        self.headers = {**self.base_headers, **self._auth_headers(self.token_scheme)}

    @property
    def auth(self) -> str:
        if self.credential:
            return "credential"
        return f"token ({self.token_scheme or 'raw'})" if self.token else "key only"

    def _auth_headers(self, token_scheme: Optional[str]) -> Dict[str, str]:
        if self.credential:
            return {"Credential": self.credential}
        if self.token:
            return {"Authorization": f"{token_scheme} {self.token}" if token_scheme else self.token}
        return {}

    def _mask(self, text: str) -> str:
        for secret in (self.api_key, self.token, self.credential):
            if secret:
                text = text.replace(secret, "***")
        return text

    def probe_auth(self) -> List[Dict[str, Any]]:
        """Read-only check of each available way to authenticate, on the
        events and orders endpoints. Secrets never appear in the result."""
        variants = [("key only", {})]
        if self.token:
            variants += [("key + Authorization: Bearer <token>", {"Authorization": f"Bearer {self.token}"}),
                         ("key + Authorization: <token>", {"Authorization": self.token})]
        if self.credential:
            variants.append(("key + Credential", {"Credential": self.credential}))
        results = []
        for path, params in ((self.events_path, {"limit": 1}), (self.orders_path, {"limit": 1})):
            url = f"{self.base_url}/{path.lstrip('/')}"
            for label, extra in variants:
                try:
                    response = requests.get(url, headers={**self.base_headers, **extra}, params=params, timeout=30)
                    status, body = response.status_code, (response.text or "")
                except requests.exceptions.RequestException as e:
                    status, body = None, str(e)
                results.append({"path": path, "auth": label, "status": status,
                                "ok": status is not None and status < 400,
                                "response": self._mask(body)[:160].replace("\n", " ")})
        return results

    @classmethod
    def from_config(cls, api_key: str, config: Optional[Dict[str, Any]] = None,
                    token: Optional[str] = None, credential: Optional[str] = None) -> "InfomaniakTicketingService":
        config = config or {}
        return cls(
            api_key,
            base_url=config.get("base_url"),
            currency=str(config.get("currency", "1")),
            language=config.get("language", "fr_FR"),
            events_path=config.get("events_path"),
            orders_path=config.get("orders_path"),
            passes_path=config.get("passes_path"),
            token=token,
            credential=credential,
            token_scheme=config.get("token_scheme", "Bearer"),
        )

    def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                      retry_count: int = 0) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            response = requests.request(method=method, url=url, headers=self.headers,
                                        params=params, timeout=30)
        except requests.exceptions.Timeout:
            raise InfomaniakTicketingAPIError("Request timed out")
        except requests.exceptions.ConnectionError:
            raise InfomaniakTicketingAPIError("Connection error - please check your internet connection")
        except requests.exceptions.RequestException as e:
            raise InfomaniakTicketingAPIError(f"Request failed: {e}")

        if response.status_code == 429:
            if retry_count < 3:
                time.sleep((2 ** retry_count) * 2)
                return self._make_request(method, endpoint, params, retry_count + 1)
            raise InfomaniakTicketingRateLimitError("Rate limit exceeded after multiple retries")
        if response.status_code in (401, 403):
            raise InfomaniakTicketingAPIError(
                f"Unauthorized ({response.status_code}) on {endpoint}: {self._mask(response.text or '')[:160]}")
        if response.status_code == 404:
            raise InfomaniakTicketingAPIError(f"Resource not found: {endpoint}")
        if response.status_code >= 400:
            try:
                body = response.json()
                message = body.get("message") or body.get("error") or response.text
            except ValueError:
                message = response.text or "Unknown error"
            raise InfomaniakTicketingAPIError(
                f"Infomaniak API error ({response.status_code}) on {endpoint}: {self._mask(str(message))[:300]}")

        if not response.text:
            return {}
        try:
            return response.json()
        except ValueError:
            raise InfomaniakTicketingAPIError("Infomaniak API returned a non-JSON response")

    def test_connection(self) -> Dict[str, Any]:
        """Events (shop key) then one order (ticket-office access)."""
        try:
            events = self.get_events()
        except InfomaniakTicketingAPIError as e:
            return {"success": False, "auth": self.auth, "error": str(e)}
        try:
            orders = _extract_list(self._make_request("GET", self.orders_path, params={"limit": 1}), "orders")
        except InfomaniakTicketingAPIError as e:
            return {"success": False, "auth": self.auth, "events_count": len(events), "orders_error": str(e)}
        return {"success": True, "auth": self.auth, "events_count": len(events), "orders_readable": True,
                "order_fields": sorted(orders[0]) if orders else []}

    def get_events(self) -> List[Dict[str, Any]]:
        return _extract_list(self._make_request("GET", self.events_path), "events")

    def _iter_pages(self, path: str, list_key: str, params: Optional[Dict[str, Any]] = None,
                    max_items: int = 100000) -> Iterable[Dict[str, Any]]:
        """Yield items page by page.

        Stops on an empty page, on a page shorter than ``page_size``, or
        when a page repeats the previous one (API ignoring pagination).
        """
        offset = 0
        seen = 0
        previous_ids: Optional[tuple] = None
        while seen < max_items:
            query: Dict[str, Any] = {"limit": self.page_size, "offset": offset, **(params or {})}
            items = _extract_list(self._make_request("GET", path, params=query), list_key)
            if not items:
                return
            ids = tuple(_first(o, "id", "order_id", "uuid", "code") for o in items)
            if ids == previous_ids:
                return
            previous_ids = ids
            for item in items:
                yield item
                seen += 1
                if seen >= max_items:
                    return
            if len(items) < self.page_size:
                return
            offset += len(items)
            time.sleep(0.2)

    def iter_orders(self, since: Optional[str] = None,
                    max_orders: int = 100000) -> Iterable[Dict[str, Any]]:
        return self._iter_pages(self.orders_path, "orders", {"begin": since} if since else None, max_orders)

    def get_orders(self, since: Optional[str] = None, max_orders: int = 100000) -> List[Dict[str, Any]]:
        return list(self.iter_orders(since=since, max_orders=max_orders))

    def get_passes(self, max_passes: int = 100000) -> List[Dict[str, Any]]:
        """Season tickets / passes, or [] when ``passes_path`` is not set."""
        if not self.passes_path:
            return []
        return list(self._iter_pages(self.passes_path, "passes", max_items=max_passes))


# Field names tried, in order, for a ticket's seat category and tariff.
_PLACE_KEYS = ("place_category", "seat_category", "category", "zone", "sector", "area", "block")
_TARIFF_KEYS = ("tariff", "tarif", "rate", "price_category", "price_name", "price_type", "ticket_type")
# Line type ("Billet" / "Place abonné") and scan status ("Billet scanné").
_LINE_TYPE_KEYS = ("line_type", "type_de_ligne", "kind", "ticket_kind", "type")
_SCAN_KEYS = ("scanned", "is_scanned", "checked_in", "scan_status", "ticket_status", "status")
_CATALOG_KEYS = ("catalog_price", "catalogue_price", "list_price", "full_price", "prix_catalogue")
_PAID_KEYS = ("paid_amount", "amount", "montant", "price")
_FREE_WORDS = ("free", "gratuit", "offert", "invitation")
_SEASON_TICKET_WORDS = ("abonne", "abonnement", "pass", "season")
_MEMBER_WORDS = ("membre", "member")


def _fold(value: Any) -> str:
    return unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower()


def _has_word(text: str, words: Iterable[str]) -> bool:
    tokens = set(re.split(r"[^a-z0-9]+", _fold(text)))
    return any(w in tokens for w in words)


def line_kind(line_type: Optional[str], tariff: Optional[str]) -> str:
    """``abonne`` (season-ticket seat), ``membre`` (member-card seat) or
    ``billet`` (ticket bought for this event).

    Infomaniak pre-generates abonné and member seats for every match, so
    they only say someone *may* come; a bought ticket says they meant to.
    Member-card seats are typed "Place abonné" too, hence the tariff is
    checked for a member card first.
    """
    if tariff and _has_word(tariff, _MEMBER_WORDS) and _has_word(tariff, ("carte", "card")):
        return "membre"
    for text in (line_type, tariff):
        if text and _has_word(text, _SEASON_TICKET_WORDS):
            return "abonne"
    return "billet"


def scan_state(ticket: Dict[str, Any]) -> Optional[bool]:
    """True = scanned at the entrance, False = not scanned, None = unknown."""
    for key in _SCAN_KEYS:
        if key not in ticket or ticket[key] in (None, ""):
            continue
        value = ticket[key]
        if isinstance(value, bool):
            return value
        tokens = set(re.split(r"[^a-z0-9]+", _fold(value)))
        if any(t.startswith(("scan", "used", "checked", "utilis")) for t in tokens):
            return True
        if tokens & {"valid", "valide", "unused", "pending"}:
            return False
    return None


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _label(value: Any) -> Optional[str]:
    """A category may come as a string or as an object with a name."""
    if isinstance(value, dict):
        value = _first(value, "name", "title", "label")
    if value in (None, ""):
        return None
    return str(value).strip() or None


def normalize_phone(raw: Any, default_country_code: str = "41") -> Optional[str]:
    """Phone number -> international format ``+41791234567`` (Brevo SMS).

    ``079 123 45 67`` and ``0041 79 123 45 67`` both give ``+41791234567``;
    a national number (leading 0) gets ``default_country_code``. Returns
    ``None`` when the result is not 8 to 15 digits long.
    """
    if raw in (None, ""):
        return None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)  # exports store 41791234567 as 41791234567.0
    text = str(raw).strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = default_country_code + digits[1:]
    elif len(digits) == 9:
        digits = default_country_code + digits
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        return None
    return "+" + digits


def normalize_order(order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Flatten an Infomaniak order into the buyer fields we push to Brevo.

    A buyer is identified by email, or by phone when there is no valid
    email (``key`` holds whichever identifies the row). Returns ``None``
    when the order has neither.
    """
    # A pass holder is the fan who comes; the buyer may be a parent or a company.
    customer = _first(order, "holder", "customer", "client", "buyer", "contact") or {}
    if not isinstance(customer, dict):
        customer = {}
    merged = {**order, **customer}

    email = _first(merged, "email", "mail", "customer_email")
    email = email.strip().lower() if isinstance(email, str) and _EMAIL_RE.match(email.strip()) else None
    phone = normalize_phone(_first(merged, "phone", "mobile", "telephone", "phone_number"))
    if not email and not phone:
        return None

    event = _first(order, "event", "show")
    event_name = event_id = event_date = None
    if isinstance(event, dict):
        event_name = _first(event, "name", "title")
        event_id = _first(event, "id", "event_id", "uuid")
        event_date = _first(event, "date", "start", "start_date", "starts_at", "begin")
    elif event is not None:
        event_id = event
    # Passes (season tickets) carry their product name instead of an event.
    event_name = event_name or _first(order, "event_name", "event_title", "pass_type",
                                      "subscription_type", "product_name")
    event_id = event_id if event_id is not None else _first(order, "event_id")
    event_date = event_date or _first(order, "event_date", "event_start")

    amount = _first(order, "total", "amount", "total_amount", "net_amount", "price")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    # "Free" orders and passes paid "Gratuit" can still carry a net amount
    # (the catalogue value): it was not paid, so it counts as 0.
    order_status = _first(order, "status", "order_status", "payment_status")
    payment = _first(order, "payment_method", "payment_mode", "mode_paiement")
    order_is_free = any(_has_word(str(v), _FREE_WORDS) for v in (order_status, payment) if v)
    if amount and order_is_free:
        amount = 0.0

    tickets = _first(order, "tickets", "items")
    places: List[str] = []
    tariffs: List[str] = []
    lines: List[Dict[str, Any]] = []
    for ticket in tickets if isinstance(tickets, list) else [order]:
        if not isinstance(ticket, dict):
            continue
        place = _label(_first(ticket, *_PLACE_KEYS))
        tariff = _label(_first(ticket, *_TARIFF_KEYS))
        if place:
            places.append(place)
        if tariff:
            tariffs.append(tariff)
        line_type = _label(_first(ticket, *_LINE_TYPE_KEYS))
        lines.append({"place": place, "tariff": tariff, "kind": line_kind(line_type, tariff),
                      "scanned": scan_state(ticket),
                      "catalog_price": _number(_first(ticket, *_CATALOG_KEYS)),
                      "paid": 0.0 if order_is_free else _number(_first(ticket, *_PAID_KEYS))})
    ticket_count = _first(order, "ticket_count", "nb_tickets", "quantity")
    if ticket_count is None and isinstance(tickets, list):
        ticket_count = len(tickets)
    try:
        ticket_count = int(ticket_count) if ticket_count is not None else None
    except (TypeError, ValueError):
        ticket_count = None

    optin = _first(merged, "newsletter", "optin", "opt_in", "accept_newsletter", "marketing")

    return {
        "key": email or phone,
        "email": email,
        "first_name": _first(merged, "firstname", "first_name", "prenom"),
        "last_name": _first(merged, "lastname", "last_name", "name", "nom"),
        "phone": phone,
        "zip_code": _first(merged, "zipcode", "zip", "zip_code", "npa", "postal_code"),
        "city": _first(merged, "city", "town", "ville"),
        "order_id": _first(order, "id", "order_id", "uuid"),
        "order_date": _first(order, "created_at", "date", "order_date", "paid_at"),
        "event_name": event_name,
        "event_id": event_id,
        "event_date": event_date,
        "amount": amount,
        "place_categories": places,
        "tariff_categories": tariffs,
        "lines": lines,
        "payment_method": _label(payment),
        "order_number": _label(_first(order, "order_number", "number", "reference", "order_id", "id")),
        "pass_code": _label(_first(order, "pass_code", "code")),
        "birthdate": _first(merged, "birthdate", "birthday", "date_of_birth", "birth_date", "date_naissance"),
        "ticket_count": ticket_count,
        "optin": bool(optin) if optin is not None else None,
    }


def counts_for_event(row: Dict[str, Any], played: bool) -> bool:
    """Does this order show the buyer at (or coming to) the event?

    * a scanned line always counts (the fan was there);
    * pre-generated abonné / member seats count only once scanned;
    * a bought ticket counts unless the event is played and the export
      says it was not scanned (no-show); without scan data it counts.
    Orders without line detail count as before.
    """
    lines = row.get("lines") or []
    if not lines:
        return True
    for line in lines:
        if line["scanned"] is True:
            return True
        if line["kind"] == "billet" and not (played and line["scanned"] is False):
            return True
    return False


def has_season_ticket_line(row: Dict[str, Any]) -> bool:
    return any(line["kind"] == "abonne" for line in row.get("lines") or [])


def was_scanned(row: Dict[str, Any]) -> Optional[bool]:
    """True if a line was scanned, False if scan data exists but none was,
    None without scan data."""
    states = [line["scanned"] for line in row.get("lines") or [] if line["scanned"] is not None]
    if not states:
        return None
    return any(states)


def buyer_keys(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Map each phone to the email it was given with, so phone-only orders
    of a buyer who also ordered with an email are merged into that buyer."""
    phone_to_email: Dict[str, str] = {}
    for row in rows:
        if row["email"] and row["phone"]:
            phone_to_email.setdefault(row["phone"], row["email"])
    return phone_to_email


def row_key(row: Dict[str, Any], phone_to_email: Dict[str, str]) -> str:
    return row["email"] or phone_to_email.get(row["phone"]) or row["phone"]


def _add_line_stats(buyer: Dict[str, Any], row: Dict[str, Any]) -> None:
    """Invitations, offered value and on-site purchases, from bought tickets
    (pre-generated abonné / member seats are not invitations)."""
    for line in row.get("lines") or []:
        if line["kind"] != "billet":
            continue
        if line["tariff"] and _contains_phrase(line["tariff"], ("sur", "place")):
            buyer["onsite_tickets"] += 1
        catalog, paid = line["catalog_price"], line["paid"]
        if not catalog or paid is None:
            continue  # without both prices we cannot tell an invitation
        buyer["free_tickets"] = buyer["free_tickets"] or 0
        buyer["free_value"] = buyer["free_value"] or 0.0
        if paid == 0:
            buyer["free_tickets"] += 1
        if catalog > paid:
            buyer["free_value"] += catalog - paid


def _contains_phrase(text: str, words: Iterable[str]) -> bool:
    tokens = set(re.split(r"[^a-z0-9]+", _fold(text)))
    return all(w in tokens for w in words)


def aggregate_buyers(orders: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Group normalized orders per buyer (email, else phone) with purchase totals."""
    rows = [r for r in (normalize_order(o) for o in orders) if r]
    phone_to_email = buyer_keys(rows)
    buyers: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = row_key(row, phone_to_email)
        buyer = buyers.setdefault(key, {
            "key": key,
            "email": key if "@" in key else None,
            "first_name": None, "last_name": None, "phone": None,
            "zip_code": None, "city": None, "birthdate": None,
            "orders_count": 0, "tickets_count": 0, "total_spent": 0.0,
            "first_order_date": None, "last_order_date": None,
            "last_event": None, "optin": None,
            "priced_orders": 0, "last_order_amount": None, "_last_amount_at": None,
            "free_tickets": None, "free_value": None, "onsite_tickets": 0,
            "payment_method": None, "last_order_number": None, "pass_code": None, "_last_at": "",
        })
        for field in ("first_name", "last_name", "phone", "zip_code", "city", "birthdate"):
            if row[field] and not buyer[field]:
                buyer[field] = row[field]
        buyer["orders_count"] += 1
        _add_line_stats(buyer, row)
        latest_at = str(row["order_date"] or "")
        if latest_at >= buyer["_last_at"]:
            buyer["_last_at"] = latest_at
            buyer["payment_method"] = row["payment_method"] or buyer["payment_method"]
            buyer["last_order_number"] = row["order_number"] or buyer["last_order_number"]
            if row["pass_code"] and row.get("event_name") and _has_word(row["event_name"], _SEASON_TICKET_WORDS):
                buyer["pass_code"] = row["pass_code"]
        buyer["tickets_count"] += row["ticket_count"] or 0
        buyer["total_spent"] += row["amount"] or 0.0
        if row["amount"] is not None:
            buyer["priced_orders"] += 1
            # Latest dated order wins; undated orders only fill a blank.
            amount_at = str(row["order_date"]) if row["order_date"] else ""
            if buyer["_last_amount_at"] is None or amount_at >= buyer["_last_amount_at"]:
                buyer["last_order_amount"] = round(row["amount"], 2)
                buyer["_last_amount_at"] = amount_at
        if row["optin"] is not None:
            buyer["optin"] = bool(buyer["optin"]) or row["optin"]
        date = str(row["order_date"]) if row["order_date"] else None
        if date:
            if not buyer["first_order_date"] or date < buyer["first_order_date"]:
                buyer["first_order_date"] = date
            if not buyer["last_order_date"] or date >= buyer["last_order_date"]:
                buyer["last_order_date"] = date
                buyer["last_event"] = row["event_name"] or buyer["last_event"]
        elif row["event_name"] and not buyer["last_event"]:
            buyer["last_event"] = row["event_name"]
    for buyer in buyers.values():
        buyer["total_spent"] = round(buyer["total_spent"], 2)
        # Average over the orders that carry an amount, so a missing amount
        # does not drag the average down.
        buyer["average_basket"] = (round(buyer["total_spent"] / buyer["priced_orders"], 2)
                                   if buyer["priced_orders"] else None)
        del buyer["_last_amount_at"], buyer["_last_at"]
        if buyer["free_value"] is not None:
            buyer["free_value"] = round(buyer["free_value"], 2)
    return buyers


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
