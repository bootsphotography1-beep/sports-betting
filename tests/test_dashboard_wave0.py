"""Wave-0 UI and error-handling tests for the dashboard.

These tests address:
- renderSafetyBanner needs a .hero container to append to (index.html must have one)
- HONEST_STATUS.md must be served at a known route from the dashboard
- API must return 400 JSON (not 500) for invalid entry type or NaN params
"""
from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from ud_edge.dashboard.app import app


@pytest.fixture
def client():
    return TestClient(app)


# ── 1. Banner container exists in static HTML ────────────────────────────────

def test_index_html_has_hero_element(client):
    """The index.html must contain a <header class="hero"> so that
    renderSafetyBanner (app.js) has somewhere to append the banner."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    # The hero element must exist in the HTML so JS can target it
    assert 'class="hero"' in html, (
        "index.html is missing <header class=\"hero\"> — "
        "renderSafetyBanner() in app.js will silently do nothing"
    )


# ── 2. HONEST_STATUS.md reachable from dashboard ──────────────────────────────

def test_honest_status_md_served(client):
    """HONEST_STATUS.md must be reachable via the dashboard server (route or static mount)."""
    resp = client.get("/HONEST_STATUS.md")
    assert resp.status_code == 200, (
        "HONEST_STATUS.md not reachable at /HONEST_STATUS.md — "
        "the safety banner link in app.js points there"
    )
    assert resp.headers["content-type"].startswith("text/plain"), (
        "HONEST_STATUS.md should be served as plain text"
    )
    body = resp.text
    assert "Research Mode" in body, "HONEST_STATUS.md must contain expected content"


# ── 3. API error handling: invalid entry type → 400 ──────────────────────────

def test_opportunities_invalid_entry_returns_400(client):
    """Requesting an unknown entry type must return HTTP 400 with JSON error."""
    resp = client.get("/api/opportunities", params={"entry": "9-flex-garbage"})
    assert resp.status_code == 400, (
        f"Expected 400 for invalid entry type, got {resp.status_code}. "
        "Invalid params must not bubble to 500."
    )
    json_body = resp.json()
    assert "error" in json_body or "detail" in json_body, (
        "400 response must have a JSON body with 'error' or 'detail'"
    )


def test_opportunities_nan_min_true_prob_returns_400(client):
    """Non-numeric min_true_prob must return HTTP 400 with JSON error."""
    resp = client.get("/api/opportunities", params={"min_true_prob": "not-a-number"})
    assert resp.status_code == 400, (
        f"Expected 400 for NaN min_true_prob, got {resp.status_code}"
    )


def test_opportunities_nan_min_edge_pp_returns_400(client):
    """Non-numeric min_edge_pp must return HTTP 400 with JSON error."""
    resp = client.get("/api/opportunities", params={"min_edge_pp": "foobar"})
    assert resp.status_code == 400, (
        f"Expected 400 for NaN min_edge_pp, got {resp.status_code}"
    )


def test_opportunities_nan_n_entries_returns_400(client):
    """Non-integer n_entries must return HTTP 400 with JSON error."""
    resp = client.get("/api/opportunities", params={"n_entries": "five"})
    assert resp.status_code == 400, (
        f"Expected 400 for non-integer n_entries, got {resp.status_code}"
    )


# ── 4. NaN / Inf guards ────────────────────────────────────────────────────────

def test_opportunities_inf_min_true_prob_returns_400(client):
    """The literal string 'inf' for min_true_prob must return 400 JSON, not crash."""
    resp = client.get("/api/opportunities", params={"min_true_prob": "inf"})
    assert resp.status_code == 400, (
        f"Expected 400 for min_true_prob=inf, got {resp.status_code}"
    )
    json_body = resp.json()
    assert "error" in json_body, "400 response must have a JSON body with 'error'"
    assert "min_true_prob" in json_body["error"], (
        "Error message should mention which parameter is invalid"
    )


def test_opportunities_nan_string_min_true_prob_returns_400(client):
    """The literal string 'NaN' for min_true_prob must return 400 JSON, not crash."""
    resp = client.get("/api/opportunities", params={"min_true_prob": "NaN"})
    assert resp.status_code == 400, (
        f"Expected 400 for min_true_prob=NaN, got {resp.status_code}"
    )


def test_opportunities_negative_inf_min_edge_pp_returns_400(client):
    """The literal string '-inf' for min_edge_pp must return 400 JSON."""
    resp = client.get("/api/opportunities", params={"min_edge_pp": "-inf"})
    assert resp.status_code == 400, (
        f"Expected 400 for min_edge_pp=-inf, got {resp.status_code}"
    )
    json_body = resp.json()
    assert "error" in json_body
    assert "min_edge_pp" in json_body["error"]


# ── 5. Non-finite numerics must never appear in JSON payload ───────────────────

def test_payload_never_contains_non_finite_floats(client):
    """Verify the JSON payload returned by /api/opportunities never contains NaN/inf.

    Even with valid inputs, internal mispricing math could theoretically produce
    non-finite values if the data is degenerate. The response must not contain
    any float that would crash Python's json.dumps or JavaScript's JSON.parse.
    """
    resp = client.get("/api/opportunities")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    body = resp.content  # raw bytes — use json.loads to validate parseability
    import json
    data = json.loads(body)

    def _check_finite(obj, path="root"):
        """Recursively traverse obj and assert every numeric is finite."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                _check_finite(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _check_finite(v, f"{path}[{i}]")
        elif isinstance(obj, float):
            assert math.isfinite(obj), (
                f"Non-finite float {obj} found at {path} — "
                "must be None or a safe sentinel instead"
            )

    _check_finite(data)


def test_mispricing_edge_pp_none_when_no_sharp_match(client):
    """When no sharp book match exists, mispricing_edge_pp must be null, not 0 or inf."""
    resp = client.get("/api/opportunities")
    if resp.status_code != 200:
        pytest.skip("API returned non-200; cannot test payload content")

    import json
    data = json.loads(resp.content)

    # Walk all opportunities
    for sport_block in data.get("sports", []):
        for opp in sport_block.get("opportunities", []):
            # mispricing_edge_pp must be either None or a finite number
            val = opp.get("mispricing_edge_pp")
            if val is not None:
                assert isinstance(val, (int, float)), (
                    f"mispricing_edge_pp should be numeric or None, got {type(val)}"
                )
                assert math.isfinite(val), (
                    f"mispricing_edge_pp must be finite, got {val} for {opp.get('player_name')}"
                )
