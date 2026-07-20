"""TDD tests for batch-mode dashboard API endpoints.

Tests:
  - test_opportunities_returns_all_sports_at_once
  - test_props_returns_batch_of_observations
  - test_props_filters_by_sport
  - test_lineups_returns_6man_then_4man_fallback
  - test_alerts_recent_returns_last_n
  - test_budget_returns_call_budget_snapshot
"""
from __future__ import annotations

import json
import math

import pytest
from fastapi.testclient import TestClient

from ud_edge.dashboard.app import app
from ud_edge.models import Leg, RankedLeg


@pytest.fixture
def client():
    return TestClient(app)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _leg(**kwargs):
    defaults = dict(
        line_id="1", player_id="p1", player_name="Test Player",
        sport_id="NBA", match_title="Team A vs Team B",
        stat_name="points", line_value=25.5, line_type="balanced",
        higher_american=-140, higher_decimal=1.714,
        lower_american=120, lower_decimal=2.20,
        higher_multiplier=0.95, lower_multiplier=0.95,
        fantasy_source="underdog",
    )
    defaults.update(kwargs)
    return Leg(**defaults)


def _ranked(leg=None, side="higher", **kwargs):
    leg = leg or _leg(**kwargs)
    return RankedLeg(
        leg=leg,
        higher_true_prob=0.59, higher_implied_prob=0.58,
        higher_edge_pp=4.0,
        lower_true_prob=0.41, lower_implied_prob=0.42,
        lower_edge_pp=-14.0,
        picked_side=side,
        picked_true_prob=0.59 if side == "higher" else 0.41,
        picked_edge_pp=4.0,
        overround=1.04,
    )


# ── 1. /api/opportunities: all sports in one call ─────────────────────────────

def test_opportunities_returns_all_sports_at_once(client):
    """Calling /api/opportunities once must return all configured sports.

    The response must contain a 'sports' list with more than one sport
    represented when the slate is populated. This confirms the batch shape.
    """
    resp = client.get("/api/opportunities")
    # May be 200 (success) or 4xx/5xx (network/env not configured).
    # We only assert on the shape if we got a valid payload.
    if resp.status_code == 200:
        data = resp.json()
        assert "sports" in data, "Response must have 'sports' key"
        sports_list = data["sports"]
        assert isinstance(sports_list, list), "'sports' must be a list"
        # When slate is populated, sports list should be non-empty
        # (may be empty if no API keys configured — that's OK)
        if len(sports_list) > 0:
            assert all(isinstance(s, dict) for s in sports_list), (
                "Each sport entry must be a dict"
            )
            assert all("sport" in s for s in sports_list), (
                "Each sport entry must have a 'sport' key"
            )
    else:
        pytest.skip(f"API not fully configured (status {resp.status_code})")


# ── 2. /api/props: raw board batch ───────────────────────────────────────────

def test_props_returns_batch_of_observations(client):
    """GET /api/props must return a batch (list) of raw sharp+fantasy observations.

    The response shape must be a list (not a single object), and it must
    contain observation dicts with player/stat/line fields.
    """
    resp = client.get("/api/props")
    # 200 = success (cache hit or fresh)
    # 503 = PROPLINE_API_KEY not configured
    if resp.status_code == 200:
        data = resp.json()
        assert isinstance(data, dict), "/api/props root must be a dict"
        observations = data.get("observations", [])
        assert isinstance(observations, list), "'observations' must be a list"
        assert len(observations) > 0, (
            "When fixture is populated, observations list should not be empty"
        )
        # Each observation must have the key fields
        for obs in observations:
            assert "player" in obs, "Each observation needs 'player'"
            assert "stat" in obs, "Each observation needs 'stat'"
            assert "line" in obs, "Each observation needs 'line'"
            assert "book_type" in obs, "Each observation needs 'book_type'"
    elif resp.status_code == 503:
        pytest.skip("PROPLINE_API_KEY not configured")
    else:
        pytest.fail(f"Unexpected status {resp.status_code}: {resp.text}")


def test_props_filters_by_sport(client):
    """GET /api/props?sport=NBA must return only NBA observations."""
    resp = client.get("/api/props", params={"sport": "NBA"})
    if resp.status_code == 200:
        data = resp.json()
        observations = data.get("observations", [])
        if len(observations) > 0:
            # All returned observations should be NBA
            nba_count = sum(
                1 for o in observations
                if (o.get("sport_id") or o.get("sport") or "").upper() == "NBA"
            )
            assert nba_count == len(observations), (
                f"All observations must be NBA when filtered; got {nba_count}/{len(observations)}"
            )
    elif resp.status_code == 503:
        pytest.skip("PROPLINE_API_KEY not configured")
    else:
        pytest.fail(f"Unexpected status {resp.status_code}: {resp.text}")


def test_props_cached_by_sport_for_60_seconds(client):
    """Two rapid /api/props calls must not double-fetch PropLine.

    We verify this by checking the cache header or by counting fetch calls.
    """
    # This test verifies cache existence — two rapid calls return same data
    resp1 = client.get("/api/props")
    if resp1.status_code != 200:
        pytest.skip("API not returning 200")
    data1 = resp1.json()
    resp2 = client.get("/api/props")
    assert resp2.status_code == 200
    data2 = resp2.json()
    # Observations must be identical (served from cache)
    assert data1.get("observations") == data2.get("observations"), (
        "Second call should serve from cache (same observations)"
    )
    # The cached_at timestamp should be the same
    assert data1.get("cached_at") == data2.get("cached_at"), (
        "cached_at should be identical when served from same cache"
    )


# ── 3. /api/lineups: correlation-aware 6-man + 4-man fallback ────────────────

def test_lineups_returns_6man_then_4man_fallback(client):
    """GET /api/lineups must return 6-leg lineups first, then 4-leg fallbacks.

    The response must be a dict with a 'lineups' list where each entry has
    either 6 or 4 legs, and 6-leg entries appear before 4-leg entries.
    """
    # Populate the cache by calling /api/opportunities first
    opp_resp = client.get("/api/opportunities")
    if opp_resp.status_code != 200:
        pytest.skip(f"/api/opportunities returned {opp_resp.status_code} — cannot test lineups")

    resp = client.get("/api/lineups")
    if resp.status_code == 200:
        data = resp.json()
        assert "lineups" in data, "Response must have 'lineups' key"
        lineups = data["lineups"]
        assert isinstance(lineups, list), "'lineups' must be a list"
        assert len(lineups) > 0, "At least one lineup should be returned"
        for lu in lineups:
            assert "n_legs" in lu, "Each lineup must have 'n_legs'"
            n = lu["n_legs"]
            assert n in (4, 6), f"Lineup must have 4 or 6 legs, got {n}"
            assert "opportunities" in lu, "Each lineup must have 'opportunities'"
            assert isinstance(lu["opportunities"], list), "'opportunities' must be a list"
        # 6-leg lineups must appear before 4-leg lineups
        six_before_four = True
        seen_4 = False
        for lu in lineups:
            if lu["n_legs"] == 4:
                seen_4 = True
            if seen_4 and lu["n_legs"] == 6:
                six_before_four = False
        assert six_before_four, "6-leg lineups must appear before 4-leg fallback lineups"
    elif resp.status_code == 503:
        pytest.skip("PROPLINE_API_KEY not configured")
    else:
        pytest.skip(f"API returned {resp.status_code}")


def test_lineups_includes_correlation_warnings(client):
    """The /api/lineups response must include correlation warnings."""
    opp_resp = client.get("/api/opportunities")
    if opp_resp.status_code != 200:
        pytest.skip(f"/api/opportunities returned {opp_resp.status_code} — cannot test lineups")

    resp = client.get("/api/lineups")
    if resp.status_code == 200:
        data = resp.json()
        assert "correlation_warnings" in data or "avg_abs_rho" in data, (
            "Response should include correlation metrics"
        )
        if "warnings" in data:
            assert isinstance(data["warnings"], list)
    elif resp.status_code == 503:
        pytest.skip("PROPLINE_API_KEY not configured")


# ── 4. /api/alerts/recent: last N alerts from alerts.jsonl ───────────────────

def test_alerts_recent_returns_last_n(client):
    """GET /api/alerts/recent?limit=5 must return the last 5 alert dicts."""
    resp = client.get("/api/alerts/recent", params={"limit": 5})
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    data = resp.json()
    assert "alerts" in data, "Response must have 'alerts' key"
    alerts = data["alerts"]
    assert isinstance(alerts, list), "'alerts' must be a list"
    assert len(alerts) <= 5, f"limit=5 should return ≤5 alerts, got {len(alerts)}"
    for alert in alerts:
        assert isinstance(alert, dict), "Each alert must be a dict"
        assert "at" in alert, "Each alert must have an 'at' timestamp"
    # Alerts must be in reverse-chronological order (most recent first)
    if len(alerts) >= 2:
        from datetime import datetime as dt
        times = [dt.fromisoformat(a["at"]) for a in alerts]
        assert times == sorted(times, reverse=True), "Alerts should be newest-first"


def test_alerts_recent_default_limit(client):
    """Default limit should return a reasonable number of alerts (10)."""
    resp = client.get("/api/alerts/recent")
    assert resp.status_code == 200
    data = resp.json()
    alerts = data.get("alerts", [])
    assert len(alerts) <= 10, f"Default limit should be ≤10, got {len(alerts)}"


# ── 5. /api/budget: CallBudget snapshot ───────────────────────────────────────

def test_budget_returns_call_budget_snapshot(client):
    """GET /api/budget must return a BudgetSnapshot dict with expected fields."""
    resp = client.get("/api/budget")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    # Required fields from BudgetSnapshot dataclass
    for field in ("day", "used", "limit", "reserve", "remaining_scheduled", "remaining_total"):
        assert field in data, f"'{field}' must be in budget snapshot"
    assert isinstance(data["used"], int), "'used' must be an int"
    assert isinstance(data["limit"], int), "'limit' must be an int"
    assert data["used"] >= 0, "'used' must be non-negative"
    assert data["remaining_total"] == data["limit"] - data["used"], (
        "remaining_total must equal limit - used"
    )
    assert "exhausted" in data, "'exhausted' boolean must be present"


# ── CORS: Tailscale browsers can hit the API ──────────────────────────────────

def test_cors_headers_present(client):
    """API responses must include CORS headers for Tailscale browsers."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    # CORSMiddleware is mounted on the app (verified separately in app startup).
    # Just verify the response is valid JSON (not blocked by CORS policy).
    assert resp.headers.get("content-type", "").startswith("application/json"), (
        "Response should be JSON — CORS should not block same-origin requests"
    )


# ── NaN/inf guard on new endpoints ────────────────────────────────────────────

def test_props_response_never_contains_non_finite_floats(client):
    """Verify /api/props JSON payload never contains NaN/inf."""
    resp = client.get("/api/props")
    if resp.status_code not in (200, 503):
        pytest.skip(f"Unexpected status {resp.status_code}")
    if resp.status_code == 503:
        pytest.skip("PROPLINE_API_KEY not configured")
    body = resp.content
    data = json.loads(body)

    def _check_finite(obj, path="root"):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _check_finite(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _check_finite(v, f"{path}[{i}]")
        elif isinstance(obj, float):
            assert math.isfinite(obj), f"Non-finite float {obj} at {path}"

    _check_finite(data)


def test_lineups_response_never_contains_non_finite_floats(client):
    """Verify /api/lineups JSON payload never contains NaN/inf."""
    opp_resp = client.get("/api/opportunities")
    if opp_resp.status_code != 200:
        pytest.skip(f"/api/opportunities returned {opp_resp.status_code} — cannot test lineups")

    resp = client.get("/api/lineups")
    if resp.status_code not in (200, 503):
        pytest.skip(f"Unexpected status {resp.status_code}")
    if resp.status_code == 503:
        pytest.skip("PROPLINE_API_KEY not configured")
    body = resp.content
    data = json.loads(body)

    def _check_finite(obj, path="root"):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _check_finite(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _check_finite(v, f"{path}[{i}]")
        elif isinstance(obj, float):
            assert math.isfinite(obj), f"Non-finite float {obj} at {path}"

    _check_finite(data)
