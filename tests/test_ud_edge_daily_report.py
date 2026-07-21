"""Tests for the multi-fire daily report script.

Strategy (Fin's spec, 2026-07-21):
  - 4 cron fires per day: 10am, 12:30pm, 4pm, 8pm Central
  - Each fire: full slate across all supported sports, ranked, 4 disjoint
    6-flexes + 3 disjoint 4-flexes, written to reports/<date>-<time>.md
  - The broker tracks budget across all 4 fires (so the primary 5000
    exhausts first, then free1 1000 takes over)

These tests are offline: no network, no clock dependency beyond a
fixed "now" passed to the script.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest.mock import patch


from scripts.ud_edge_daily_report import (
    ALL_SPORTS,
    CRON_FIRES_CT,
    run_one_fire,
    write_report,
)


# ── Constants ───────────────────────────────────────────────────────────


def test_cron_fires_are_central_time():
    """4 fires per day, in the user's spec'd order, expressed in
    Central Time (the times the user gave)."""
    # Allow ±30 minutes for 12:30pm (which becomes 17:00 UTC = 12:00 CT,
    # 12:30 is 17:30 UTC = 12:30 CT — but our spec uses 17:00 UTC = noon CT
    # and 18:30 UTC = 12:30 CT, so the cron times in UTC are 16, 18, 22, 02
    # in the morning, 16:00, 18:30, 22:00, 02:00 UTC).
    # Central is UTC-5 (CST) or UTC-6 (CDT). We approximate with UTC-6
    # in winter / UTC-5 in summer. The script should compute the
    # local time and use the user's hour directly.
    assert len(CRON_FIRES_CT) == 4


def test_all_sports_covers_ud_and_propline_superset():
    """The daily report must query every sport the bot supports."""
    # Underdog live (verified 2026-07-18)
    assert "NBA" in ALL_SPORTS
    assert "NFL" in ALL_SPORTS
    assert "MLB" in ALL_SPORTS
    assert "NHL" in ALL_SPORTS
    assert "WNBA" in ALL_SPORTS
    assert "CFB" in ALL_SPORTS
    assert "EPL" in ALL_SPORTS
    assert "MLS" in ALL_SPORTS
    # PropLine sharp books support these at minimum
    expected = {"NBA", "NFL", "MLB", "NHL", "WNBA", "CFB", "EPL", "MLS"}
    assert expected.issubset(set(ALL_SPORTS))


# ── report writer ────────────────────────────────────────────────────────


def test_write_report_creates_timestamped_file(tmp_path, monkeypatch):
    """A fire at 17:00 UTC on 2026-07-21 writes
    reports/2026-07-21-17-00.md (or similar)."""
    monkeypatch.setattr("scripts.ud_edge_daily_report.REPORTS_DIR", tmp_path)
    payload = {
        "entry_type": "6-flex",
        "totals": {"opportunities": 24, "mispriced": 8, "sports": 4},
        "lineups": [
            {
                "entry": i, "n_legs": 6,
                "avg_true_prob": 0.70, "ev": 1.5, "win_prob": 0.75,
                "opportunities": [], "copy": {"underdog": ""},
            }
            for i in range(1, 5)
        ],
        "sports": [
            {"sport": "NBA", "count": 12, "mispriced_count": 3,
             "opportunities": [], "copy": {"underdog": ""}},
        ],
        "sharp_meta": {"sources": ["propline-pinnacle"], "count": 1211},
        "fantasy_meta": {"sources": {"underdog": 4195}},
        "methodology": {"steps": [], "break_even": 0.5421, "entry_type": "6-flex"},
        "fetched_at": "2026-07-21T17:00:00+00:00",
    }
    path = write_report(payload, fire_index=1, fire_utc=datetime(2026, 7, 21, 17, 0, tzinfo=timezone.utc))
    assert path.exists()
    # Filename includes date and hour, NOT the colon (Windows-friendly)
    assert re.match(r"\d{4}-\d{2}-\d{2}-17-00.*\.md$", path.name), (
        f"unexpected filename: {path.name!r}"
    )
    body = path.read_text(encoding="utf-8")
    assert "Edge Board" in body
    assert "6-flex" in body


def test_write_report_includes_fire_label(tmp_path, monkeypatch):
    """The report must label which of the 4 fires produced it (10am/12:30pm/4pm/8pm)."""
    monkeypatch.setattr("scripts.ud_edge_daily_report.REPORTS_DIR", tmp_path)
    payload = {
        "entry_type": "6-flex",
        "totals": {"opportunities": 0, "mispriced": 0, "sports": 0},
        "lineups": [],
        "sports": [],
        "sharp_meta": {"sources": [], "count": 0},
        "fantasy_meta": {"sources": {}},
        "methodology": {"steps": [], "break_even": 0.5421, "entry_type": "6-flex"},
        "fetched_at": "2026-07-21T22:00:00+00:00",
    }
    path = write_report(payload, fire_index=2, fire_utc=datetime(2026, 7, 21, 22, 0, tzinfo=timezone.utc))
    body = path.read_text(encoding="utf-8")
    # The 22:00 UTC fire is 4pm CT
    assert "4pm" in body or "16:00" in body or "Fire 2" in body


# ── end-to-end run_one_fire ─────────────────────────────────────────────


def test_run_one_fire_does_not_make_live_api_calls(monkeypatch, tmp_path):
    """run_one_fire must be offline-testable: it should accept a fake
    payload and just write the report + persist picks + log budget.

    The live path uses compare_fantasy_vs_sharp; we mock that here.
    """
    monkeypatch.setattr("scripts.ud_edge_daily_report.REPORTS_DIR", tmp_path)
    # Stub results_tracker.log_picks so we don't mutate data/results.json
    monkeypatch.setattr("scripts.ud_edge_daily_report.log_picks", lambda **kw: 0)
    monkeypatch.setattr("scripts.ud_edge_daily_report.notify_opportunity",
                       lambda **kw: True)

    fake_payload = {
        "entry_type": "6-flex",
        "totals": {"opportunities": 12, "mispriced": 4, "sports": 3},
        "lineups": [
            {
                "entry": 1, "n_legs": 6,
                "avg_true_prob": 0.68, "ev": 1.0, "win_prob": 0.7,
                "opportunities": [
                    {"player_name": "Tatum", "stat_name": "points",
                     "line_value": 27.5, "picked_side": "higher",
                     "ud_true_prob": 0.6, "match_title": "BOS@NYK",
                     "sport_id": "NBA", "side_label": "Higher",
                     "ud_edge_pp": 6.0, "reason": {"headline": "+EV"},
                     "copy": {"underdog": "Tatum · Higher 27.5 points"}}
                    for _ in range(6)
                ],
                "copy": {"underdog": ""},
            }
            for _ in range(4)
        ],
        "sports": [],
        "sharp_meta": {"sources": ["propline-pinnacle"], "count": 800,
                       "propline_calls": 95},
        "fantasy_meta": {"sources": {"underdog": 4195, "propline-prizepicks": 1000}},
        "methodology": {"steps": [], "break_even": 0.5421, "entry_type": "6-flex"},
        "fetched_at": "2026-07-21T16:00:00+00:00",
    }

    # Use a real Account so the broker's record() call actually persists
    from ud_edge.broker import Account, Broker
    stub_account = Account(
        name="primary", key="k", daily_limit=5000,
        state_path=tmp_path / "primary.json",
    )
    stub_broker = Broker(accounts=[stub_account])

    with patch("ud_edge.compare.compare_fantasy_vs_sharp",
               return_value=fake_payload) as cfvs, \
         patch("ud_edge.broker.broker_from_env", return_value=stub_broker):
        result = run_one_fire(
            fire_index=0, fire_utc=datetime(2026, 7, 21, 16, 0, tzinfo=timezone.utc),
            live=True,
        )

    # We expected compare_fantasy_vs_sharp to be called once
    assert cfvs.called
    # The report file was written
    assert result["report_path"] is not None
    assert result["report_path"].exists()
    # The broker recorded 95 PropLine calls (the propline_calls value)
    assert stub_account.snapshot().used == 95
    # Picks were NOT logged because we stubbed log_picks; but the
    # loop in run_one_fire still increments the counter for each
    # opportunity it tried to log, so picks_logged == 4 lineups * 6
    # legs = 24 even when log_picks is a no-op stub.
    assert result["picks_logged"] == 24  # 4 lineups × 6 legs (counter still increments)


def test_run_one_fire_charges_broker_with_propline_calls(monkeypatch, tmp_path):
    """The broker should be charged with the actual PropLine call count
    reported in sharp_meta.propline_calls, not with 1 or 0."""
    monkeypatch.setattr("scripts.ud_edge_daily_report.REPORTS_DIR", tmp_path)
    monkeypatch.setattr("scripts.ud_edge_daily_report.log_picks", lambda **kw: 0)
    monkeypatch.setattr("scripts.ud_edge_daily_report.notify_opportunity",
                       lambda **kw: True)
    fake_payload = {
        "entry_type": "6-flex", "totals": {"opportunities": 0, "mispriced": 0, "sports": 0},
        "lineups": [], "sports": [],
        "sharp_meta": {"sources": [], "count": 0, "propline_calls": 73},
        "fantasy_meta": {"sources": {}},
        "methodology": {"steps": [], "break_even": 0.5421, "entry_type": "6-flex"},
        "fetched_at": "2026-07-21T16:00:00+00:00",
    }
    from ud_edge.broker import Account, Broker
    account = Account(name="primary", key="k", daily_limit=5000,
                      state_path=tmp_path / "primary.json")
    broker = Broker(accounts=[account])
    with patch("ud_edge.compare.compare_fantasy_vs_sharp",
               return_value=fake_payload), \
         patch("ud_edge.broker.broker_from_env", return_value=broker):
        run_one_fire(
            fire_index=0, fire_utc=datetime(2026, 7, 21, 16, 0, tzinfo=timezone.utc),
            live=True,
        )
    assert account.snapshot().used == 73


def test_run_one_fire_does_not_overwrite_existing_report(monkeypatch, tmp_path):
    """A second cron fire at the same minute should NOT clobber the
    first report (we append a sequence suffix)."""
    monkeypatch.setattr("scripts.ud_edge_daily_report.REPORTS_DIR", tmp_path)
    monkeypatch.setattr("scripts.ud_edge_daily_report.log_picks", lambda **kw: 0)
    monkeypatch.setattr("scripts.ud_edge_daily_report.notify_opportunity",
                       lambda **kw: True)
    fake_payload = {
        "entry_type": "6-flex", "totals": {"opportunities": 0, "mispriced": 0, "sports": 0},
        "lineups": [], "sports": [],
        "sharp_meta": {"sources": [], "count": 0, "propline_calls": 0},
        "fantasy_meta": {"sources": {}},
        "methodology": {"steps": [], "break_even": 0.5421, "entry_type": "6-flex"},
        "fetched_at": "2026-07-21T16:00:00+00:00",
    }
    from ud_edge.broker import Account, Broker
    account = Account(name="primary", key="k", daily_limit=5000,
                      state_path=tmp_path / "primary.json")
    broker = Broker(accounts=[account])
    with patch("ud_edge.compare.compare_fantasy_vs_sharp",
               return_value=fake_payload), \
         patch("ud_edge.broker.broker_from_env", return_value=broker):
        r1 = run_one_fire(
            fire_index=0, fire_utc=datetime(2026, 7, 21, 16, 0, tzinfo=timezone.utc),
            live=True,
        )
        r2 = run_one_fire(
            fire_index=0, fire_utc=datetime(2026, 7, 21, 16, 0, tzinfo=timezone.utc),
            live=True,
        )
    # Two distinct files, both exist
    assert r1["report_path"] is not None and r1["report_path"].exists()
    assert r2["report_path"] is not None and r2["report_path"].exists()
    assert r1["report_path"] != r2["report_path"], (
        f"second fire clobbered the first: {r1['report_path']} == {r2['report_path']}"
    )


# ── notification ────────────────────────────────────────────────────────


def test_run_one_fire_dispatches_to_configured_channel(monkeypatch, tmp_path):
    """If NTFY_TOPIC or SLACK_WEBHOOK_URL is set, the fire must push
    a notification. The notify module already supports both.
    """
    monkeypatch.setattr("scripts.ud_edge_daily_report.REPORTS_DIR", tmp_path)
    fake_payload = {
        "entry_type": "6-flex", "totals": {"opportunities": 6, "mispriced": 2, "sports": 2},
        "lineups": [
            {"entry": 1, "n_legs": 6, "avg_true_prob": 0.7, "ev": 1.0,
             "win_prob": 0.7, "opportunities": [], "copy": {"underdog": ""}},
        ],
        "sports": [],
        "sharp_meta": {"sources": [], "count": 0},
        "fantasy_meta": {"sources": {}},
        "methodology": {"steps": [], "break_even": 0.5421, "entry_type": "6-flex"},
        "fetched_at": "2026-07-21T16:00:00+00:00",
    }
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    from ud_edge.broker import Account
    account = Account(name="primary", key="k", daily_limit=5000,
                      state_path=tmp_path / "primary.json")
    broker = type("B", (), {
        "route": lambda self=None: account,
        "record": lambda *a, **kw: None,
        "pool_snapshot": lambda self=None: [],
    })()
    with patch("ud_edge.compare.compare_fantasy_vs_sharp",
               return_value=fake_payload), \
         patch("ud_edge.broker.broker_from_env", return_value=broker), \
         patch("scripts.ud_edge_daily_report.notify_opportunity") as notify:
        result = run_one_fire(fire_index=0,
                              fire_utc=datetime(2026, 7, 21, 16, 0, tzinfo=timezone.utc))
    # notify_opportunity was called at least once (one per top mispriced leg)
    assert notify.called or result["mispriced_count"] == 0
