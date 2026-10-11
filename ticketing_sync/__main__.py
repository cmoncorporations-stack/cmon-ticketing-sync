"""Run the ticketing -> Brevo sync for one club.

    python -m ticketing_sync --club clubs/foxes.json --test      # Infomaniak access OK?
    python -m ticketing_sync --club clubs/foxes.json --probe     # which auth header works?
    python -m ticketing_sync --club clubs/foxes.json --schema    # API field names (no values)
    python -m ticketing_sync --club clubs/foxes.json --dry-run   # read only
    python -m ticketing_sync --club clubs/foxes.json             # real sync
    python -m ticketing_sync --club clubs/foxes.json --if-due    # only at H-24 / H+24 of a match

API keys come from the environment, never from the config file:
``INFOMANIAK_API_KEY`` (shop key), ``INFOMANIAK_TOKEN`` (manager API token,
or ``INFOMANIAK_CREDENTIAL`` from the /login method) and ``BREVO_API_KEY``
(optional for ``--dry-run``: without it Brevo is not read either).

A club with several Infomaniak shops (e.g. fan and VIP ticketing) lists the
extra ones in ``infomaniak.extra_shops`` (``["VIP"]``); each one's key comes
from ``INFOMANIAK_API_KEY_<SHOP>`` and its token from ``INFOMANIAK_TOKEN_<SHOP>``
(default: the main token). All shops are synced together.

Exit codes: 0 success (or nothing due), 1 configuration / API error.
The printed report never contains contact data (emails, phones).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ticketing_sync.brevo import BrevoAPIError, BrevoService
from ticketing_sync.infomaniak import (
    InfomaniakTicketingAPIError,
    InfomaniakTicketingService,
    MultiShopTicketing,
)
from ticketing_sync.schedule import due_runs
from ticketing_sync.sync import (
    DEFAULT_ACQUISITION_ATTRIBUTE,
    DEFAULT_ACQUISITION_VALUE,
    DEFAULT_OPTIN_ATTRIBUTE,
    DEFAULT_OPTIN_VALUE,
    DEFAULT_PARTNER_ACQUISITION_VALUE,
    sync_infomaniak_to_brevo,
)

logger = logging.getLogger("ticketing_sync")

# Result keys that can hold contact data; never printed.
_PRIVATE_KEYS = ("sample",)


def load_club_config(path: str) -> Dict[str, Any]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not config.get("event_lists", {}).get("club_code"):
        raise ValueError(f"{path}: 'event_lists.club_code' is required")
    return config


def sync_kwargs(config: Dict[str, Any]) -> Dict[str, Any]:
    """Map the club config file onto ``sync_infomaniak_to_brevo`` arguments."""
    brevo = config.get("brevo") or {}
    return {
        "list_ids": [int(i) for i in brevo.get("list_ids") or []],
        "only_optin": bool(brevo.get("only_optin", False)),
        "acquisition_attribute": brevo.get("acquisition_attribute", DEFAULT_ACQUISITION_ATTRIBUTE),
        "acquisition_value": brevo.get("acquisition_value", DEFAULT_ACQUISITION_VALUE),
        "partner_acquisition_value": brevo.get("partner_acquisition_value", DEFAULT_PARTNER_ACQUISITION_VALUE),
        "optin_attribute": brevo.get("optin_attribute", DEFAULT_OPTIN_ATTRIBUTE),
        "optin_value": brevo.get("optin_value", DEFAULT_OPTIN_VALUE),
        "event_lists": config.get("event_lists"),
        "ticket_attributes": config.get("ticket_attributes"),
    }


def build_ticketing(config: Dict[str, Any]):
    """Infomaniak client of the club: one shop, or every shop of
    ``infomaniak.extra_shops`` merged. Raises ``ValueError`` on a missing key."""
    infomaniak = config.get("infomaniak") or {}
    token = os.environ.get("INFOMANIAK_TOKEN")
    credential = os.environ.get("INFOMANIAK_CREDENTIAL")
    main_key = os.environ.get("INFOMANIAK_API_KEY")
    if not main_key:
        raise ValueError("INFOMANIAK_API_KEY manquant")
    main_shop = InfomaniakTicketingService.from_config(main_key, infomaniak, token=token, credential=credential)
    extra = [str(s).upper() for s in infomaniak.get("extra_shops") or []]
    if not extra:
        return main_shop
    shops = {"PRINCIPALE": main_shop}
    for name in extra:
        key = os.environ.get(f"INFOMANIAK_API_KEY_{name}")
        if not key:
            raise ValueError(f"INFOMANIAK_API_KEY_{name} manquant (boutique {name})")
        shops[name] = InfomaniakTicketingService.from_config(
            key, infomaniak, token=os.environ.get(f"INFOMANIAK_TOKEN_{name}") or token, credential=credential)
    return MultiShopTicketing(shops)


def public_report(result: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in result.items() if k not in _PRIVATE_KEYS}


def main(argv: Optional[List[str]] = None, now: Optional[datetime] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ticketing_sync", description=__doc__.splitlines()[0])
    parser.add_argument("--club", required=True, help="Club config file, e.g. clubs/foxes.json")
    parser.add_argument("--dry-run", action="store_true", help="Read Infomaniak (and Brevo), write nothing")
    parser.add_argument("--test", action="store_true", help="Only test the Infomaniak connection")
    parser.add_argument("--schema", action="store_true",
                        help="Print the field names and types returned by the API (no values)")
    parser.add_argument("--probe", action="store_true",
                        help="Try every way of sending the Infomaniak key (read only) and report which one works")
    parser.add_argument("--if-due", action="store_true",
                        help="Sync only if a match is at H-24 or H+24 (see schedule in the config)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        config = load_club_config(args.club)
    except (OSError, ValueError) as e:
        print(f"Configuration invalide : {e}", file=sys.stderr)
        return 1
    club = config.get("club") or args.club

    try:
        ticketing = build_ticketing(config)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    if args.schema:
        print(json.dumps({"club": club, **ticketing.schema_samples()}, indent=1, ensure_ascii=False))
        return 0

    if args.probe:
        results = ticketing.probe_auth()
        print(json.dumps({"club": club, "base_url": ticketing.base_url, "configured_auth": ticketing.auth,
                          "results": results}, indent=2, ensure_ascii=False))
        return 0 if any(r["ok"] for r in results) else 1

    if args.test:
        result = ticketing.test_connection()
        print(json.dumps({"club": club, **result}, indent=2, ensure_ascii=False))
        return 0 if result["success"] else 1

    if args.if_due:
        try:
            events = ticketing.get_events()
        except InfomaniakTicketingAPIError as e:
            print(f"Infomaniak : {e}", file=sys.stderr)
            return 1
        schedule = {**(config.get("schedule") or {}), "event_lists": config.get("event_lists")}
        due = due_runs(events, schedule, now)
        if not due:
            print(json.dumps({"club": club, "due": [], "message": "Aucun match à H-24 / H+24"},
                             ensure_ascii=False))
            return 0
        print(json.dumps({"club": club, "due": due}, indent=2, ensure_ascii=False))

    brevo_key = os.environ.get("BREVO_API_KEY")
    if not brevo_key and not args.dry_run:
        print("BREVO_API_KEY manquant", file=sys.stderr)
        return 1
    brevo = BrevoService(brevo_key) if brevo_key else None

    try:
        result = sync_infomaniak_to_brevo(ticketing, brevo, dry_run=args.dry_run, **sync_kwargs(config))
    except (InfomaniakTicketingAPIError, BrevoAPIError) as e:
        print(f"Synchronisation interrompue : {e}", file=sys.stderr)
        return 1

    if isinstance(ticketing, MultiShopTicketing):
        result = {**result, "shops": list(ticketing.shops), "duplicates_skipped": ticketing.duplicates_skipped}
    print(json.dumps({"club": club, "ran_at": datetime.now(timezone.utc).isoformat(), **public_report(result)},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
