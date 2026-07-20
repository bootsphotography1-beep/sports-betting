"""TDD tests for ud_edge.poller — budget interval logic and poll loop."""
from __future__ import annotations

import os
from unittest.mock import patch


from ud_edge.budget import compute_poll_interval_seconds


# ── compute_poll_interval_seconds tests ──────────────────────────────────────

def test_compute_poll_interval_45s_when_tip_within_90min():
    # Tip 60 min away → urgency band [-20, 90] → 45s interval.
    # Use very large remaining_scheduled so budget_floor << 45s (no floor interference).
    interval = compute_poll_interval_seconds(
        nearest_tip_minutes=60.0,
        remaining_scheduled=5000,
        seconds_left_in_utc_day=8 * 3600,
    )
    assert interval == 45.0


def test_compute_poll_interval_3min_when_tip_90_to_240min():
    # Tip 120 min away → band (90, 240] → 180s (3 min)
    interval = compute_poll_interval_seconds(
        nearest_tip_minutes=120.0,
        remaining_scheduled=500,
        seconds_left_in_utc_day=8 * 3600,
    )
    assert interval == 180.0


def test_compute_poll_interval_10min_when_tip_240_to_720min():
    # Tip 480 min away → band (240, 720] → 600s (10 min)
    interval = compute_poll_interval_seconds(
        nearest_tip_minutes=480.0,
        remaining_scheduled=500,
        seconds_left_in_utc_day=8 * 3600,
    )
    assert interval == 600.0


def test_compute_poll_interval_15min_when_tip_beyond_720min():
    # Tip 800 min away → beyond 720 → 900s (15 min) urgency band
    interval = compute_poll_interval_seconds(
        nearest_tip_minutes=800.0,
        remaining_scheduled=500,
        seconds_left_in_utc_day=8 * 3600,
    )
    assert interval == 900.0


def test_budget_floor_lifts_interval_when_low_on_calls():
    # Only 100 calls left but 5 hours remain
    # budget_floor = 18000 / 100 = 180s > 45s urgency → floor wins
    seconds_left = 5 * 3600  # 5 hours
    interval = compute_poll_interval_seconds(
        nearest_tip_minutes=60.0,  # would be 45s urgency
        remaining_scheduled=100,
        seconds_left_in_utc_day=seconds_left,
    )
    # 18000 / 100 = 180s; max(45, 180) = 180
    assert interval == 180.0


def test_budget_zero_remaining_scheduled_returns_max_interval():
    """When remaining_scheduled=0, budget_floor is capped and max_interval is returned."""
    # The function uses max(remaining_scheduled, 1) internally to prevent ZeroDivisionError.
    # With 0 remaining, budget_floor = seconds_left / 1, which could still be large,
    # but the result is clamped to max_interval=1200s when urgency is quiet.
    interval = compute_poll_interval_seconds(
        nearest_tip_minutes=60.0,  # would be 45s urgency
        remaining_scheduled=0,
        seconds_left_in_utc_day=8 * 3600,
    )
    # With 0 remaining, budget_floor = 28800 / 1 = 28800, clamped to max_interval 1200
    assert interval == 1200.0


# ── run_poll_loop exit-code tests ─────────────────────────────────────────────

def test_run_poll_loop_returns_1_when_propline_not_configured(tmp_path):
    """When PROPLINE_API_KEY is absent from env, run_poll_loop exits with 1."""
    with patch.dict(os.environ, {}, clear=True):
        from ud_edge.poller import run_poll_loop

        result = run_poll_loop(
            daily_limit=5000,
            min_mispricing_pp=1.5,
            cache_path=tmp_path / "data",
            once=True,
        )
    assert result == 1


def test_run_poll_loop_once_returns_0_with_propline_configured(tmp_path, monkeypatch):
    """When PROPLINE_API_KEY is set and once=True, one cycle runs and returns 0."""
    monkeypatch.setenv("PROPLINE_API_KEY", "dummy-key-for-test")
    # Mock compare_fantasy_vs_sharp to return a minimal valid result
    mock_result = {
        "flat": [],
        "sports": [],
        "lineups": [],
        "fantasy_meta": {"sources": {}},
        "sharp_meta": {"sources": []},
        "totals": {"opportunities": 0},
    }
    with patch("ud_edge.poller.compare_fantasy_vs_sharp", return_value=mock_result):
        from ud_edge.poller import run_poll_loop

        result = run_poll_loop(
            daily_limit=5000,
            min_mispricing_pp=1.5,
            cache_path=tmp_path / "data",
            once=True,
        )
    assert result == 0
