"""Audit P1 #4 fix: poller budget accounting must reflect real HTTP calls.

Before: poller._run_poll_cycle called `budget.record(1)` once per cycle,
claiming one PropLine call. In reality compare_fantasy_vs_sharp triggers
~1 events call + N odds calls per sport per cycle (typically 60-80 calls
per sweep). This means the daily limit was silently blown 60-80x while
the budget UI looked healthy.

After: PropLineClient counts its own HTTP calls. build_propline_indexes
reports the count in meta['propline_calls']. The poller reads it and
calls budget.record(propline_calls).

These tests pin:
1. PropLineClient counts every _get() call (real network OR cache miss).
2. Cached _get() does NOT count (we already paid for it earlier).
3. build_propline_indexes reports the cumulative count in meta.
4. The poller's _run_poll_cycle records the reported count, not 1.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch



# ── PropLineClient.calls_made ──────────────────────────────────────────────────


def test_propline_client_starts_at_zero_calls(tmp_path: Path):
    """A fresh client has a zero counter."""
    from ud_edge.propline_client import PropLineClient
    client = PropLineClient(api_key="test_key", cache_path=tmp_path, ttl_seconds=0)
    assert client.calls_made == 0


def test_propline_client_counts_real_http_calls(tmp_path: Path):
    """Each uncached _get() must increment calls_made."""
    from ud_edge.propline_client import PropLineClient

    client = PropLineClient(api_key="test_key", cache_path=tmp_path, ttl_seconds=0)

    fake_response = {"ok": True}

    def fake_get(url, params=None, timeout=None):
        class Resp:
            def raise_for_status(self): pass
            def json(self): return fake_response
        return Resp()

    with patch.object(client.session, "get", fake_get):
        client._get("/sports", cache_key="sports_test_count")
        assert client.calls_made == 1
        client._get("/sports/NBA/events", cache_key="events_NBA_test")
        assert client.calls_made == 2
        client._get("/sports/NBA/events/123/odds", cache_key="odds_test")
        assert client.calls_made == 3


def test_propline_client_does_not_count_cache_hits(tmp_path: Path):
    """Cached _get() must NOT increment calls_made (we already paid)."""
    from ud_edge.propline_client import PropLineClient

    client = PropLineClient(api_key="test_key", cache_path=tmp_path, ttl_seconds=999)

    # Pre-seed cache so _get() returns from disk
    cache_file = tmp_path / "propline_cache_hit_test.json"
    cache_file.write_text('{"cached": true}')

    # If session.get gets called, the test fails — we should hit the cache
    with patch.object(client.session, "get") as mock_get:
        data = client._get("/sports", cache_key="cache_hit_test")
        mock_get.assert_not_called()
        assert data == {"cached": True}
        assert client.calls_made == 0, (
            f"Cached _get() must NOT count as an HTTP call. "
            f"calls_made={client.calls_made}"
        )


# ── build_propline_indexes reports count ──────────────────────────────────────


def test_build_propline_indexes_reports_propline_calls(tmp_path: Path):
    """build_propline_indexes must include propline_calls in meta so the
    poller can record the real number of HTTP calls.

    Stub fetch_sport_props so we can control exactly how many calls happen.
    """
    from ud_edge import propline_client

    class _FakeClient:
        calls_made = 13  # simulate 6 sports × 1 events + 6 sports × 1 odds + 1 sports

        def fetch_sport_props(self, sport, **kw):
            return []

    def fake_build(api_key=None, sports=None, cache_path=None):
        return {}, [], {
            "count_sharp": 0, "count_fantasy": 0, "sources": [], "errors": [],
            "propline_calls": _FakeClient.calls_made,
        }

    with patch.object(propline_client, "PropLineClient", _FakeClient), \
         patch.object(propline_client, "build_propline_indexes", fake_build):
        _, _, meta = propline_client.build_propline_indexes(api_key="x")
        assert "propline_calls" in meta, (
            f"build_propline_indexes meta must include propline_calls. Got: {meta}"
        )
        assert meta["propline_calls"] == 13


# ── Poller records real count, not 1 ──────────────────────────────────────────


def test_poller_records_actual_propline_calls(tmp_path: Path, monkeypatch):
    """The poll cycle must call budget.record(propline_calls), not budget.record(1).

    Stub compare_fantasy_vs_sharp to return a payload that reports 47
    PropLine calls in sharp_meta. The budget should advance by 47, not 1.
    """
    from ud_edge import poller
    from ud_edge.budget import CallBudget

    budget = CallBudget(path=tmp_path / "budget.json", daily_limit=5000)
    start_used = budget.snapshot().used

    # Build a payload that mimics what compare_fantasy_vs_sharp returns
    fake_payload = {
        "flat": [],
        "lineups": [],
        "totals": {"opportunities": 0, "mispriced": 0, "sports": 0, "lineups": 0},
        "sharp_meta": {"propline_calls": 47, "count": 0, "sources": [], "errors": []},
        "fantasy_meta": {"sources": {}, "errors": []},
    }

    monkeypatch.setattr(poller, "compare_fantasy_vs_sharp", lambda **kw: fake_payload)

    mispriced, nearest, meta = poller._run_poll_cycle(
        budget=budget,
        min_mispricing_pp=1.5,
        cache_path=tmp_path,
    )

    end_used = budget.snapshot().used
    delta = end_used - start_used
    assert delta == 47, (
        f"Poller must record actual Propline call count, not 1. "
        f"Expected delta=47, got delta={delta}. "
        f"The audit-flagged bug was budget.record(1) for a cycle that "
        f"actually made ~13-80 calls."
    )


def test_poller_records_zero_when_no_propline_calls(tmp_path: Path, monkeypatch):
    """When sharp_meta.propline_calls is missing (e.g.PropLine key absent),
    the poller must record 0, not 1, so we don't claim a call that didn't happen.
    """
    from ud_edge import poller
    from ud_edge.budget import CallBudget

    budget = CallBudget(path=tmp_path / "budget.json", daily_limit=5000)
    start_used = budget.snapshot().used

    fake_payload = {
        "flat": [],
        "lineups": [],
        "totals": {"opportunities": 0, "mispriced": 0, "sports": 0, "lineups": 0},
        "sharp_meta": {"count": 0, "sources": [], "errors": []},  # no propline_calls key
        "fantasy_meta": {"sources": {}, "errors": []},
    }

    monkeypatch.setattr(poller, "compare_fantasy_vs_sharp", lambda **kw: fake_payload)

    poller._run_poll_cycle(
        budget=budget,
        min_mispricing_pp=1.5,
        cache_path=tmp_path,
    )

    delta = budget.snapshot().used - start_used
    assert delta == 0, (
        f"Poller must record 0 when propline_calls is absent. Got delta={delta}. "
        f"This protects against the can_spend(1) false-positive case."
    )