"""Command line: config loading, secrets, --if-due, no contact data in the report."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ticketing_sync.__main__ import load_club_config, main, sync_kwargs

CLUB = "clubs/foxes.json"


def test_foxes_config_loads_and_maps():
    config = load_club_config(CLUB)
    kwargs = sync_kwargs(config)
    assert kwargs["event_lists"]["club_code"] == "FOXES"
    assert kwargs["acquisition_attribute"] == "SOURCE_ACQUISITION"
    assert kwargs["partner_acquisition_value"] == "Business Engagement"
    assert kwargs["list_ids"] == []


def test_config_without_club_code_is_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"event_lists": {}}))
    with pytest.raises(ValueError):
        load_club_config(str(bad))


def test_missing_keys_fail(monkeypatch, capsys):
    monkeypatch.delenv("INFOMANIAK_API_KEY", raising=False)
    assert main(["--club", CLUB, "--dry-run"]) == 1
    monkeypatch.setenv("INFOMANIAK_API_KEY", "k")
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    with patch("ticketing_sync.__main__.sync_infomaniak_to_brevo") as sync:
        assert main(["--club", CLUB]) == 1                # real sync needs Brevo
        sync.assert_not_called()


def test_report_never_prints_contact_sample(monkeypatch, capsys):
    monkeypatch.setenv("INFOMANIAK_API_KEY", "k")
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    result = {"success": True, "orders_fetched": 3, "sample": [{"email": "fan@x.ch"}]}
    with patch("ticketing_sync.__main__.sync_infomaniak_to_brevo", return_value=result) as sync:
        assert main(["--club", CLUB, "--dry-run"]) == 0
    assert sync.call_args.kwargs["dry_run"] is True
    out = capsys.readouterr().out
    assert "fan@x.ch" not in out and '"orders_fetched": 3' in out


def test_if_due_skips_when_no_match_is_at_h24(monkeypatch, capsys):
    monkeypatch.setenv("INFOMANIAK_API_KEY", "k")
    monkeypatch.setenv("BREVO_API_KEY", "b")
    events = [{"name": "Pully Lausanne Foxes vs Lions de Genève - 10.10.26 - championnat SBL",
               "date": "2026-10-10 19:30:00"}]
    with patch("ticketing_sync.__main__.InfomaniakTicketingService.get_events", return_value=events), \
            patch("ticketing_sync.__main__.sync_infomaniak_to_brevo", return_value={"success": True}) as sync:
        assert main(["--club", CLUB, "--if-due"], now=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)) == 0
        sync.assert_not_called()
        assert main(["--club", CLUB, "--if-due"], now=datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)) == 0
        sync.assert_called_once()
    assert '"mark": "H+24"' in capsys.readouterr().out


def test_test_mode_only_checks_infomaniak(monkeypatch):
    monkeypatch.setenv("INFOMANIAK_API_KEY", "k")
    with patch("ticketing_sync.__main__.InfomaniakTicketingService.test_connection",
               return_value={"success": True, "events_count": 4}), \
            patch("ticketing_sync.__main__.sync_infomaniak_to_brevo") as sync:
        assert main(["--club", CLUB, "--test"]) == 0
        sync.assert_not_called()
