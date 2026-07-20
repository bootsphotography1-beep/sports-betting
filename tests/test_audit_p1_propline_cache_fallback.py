"""Audit P1 #8: PropLine sharp_cache must be loadable when the live API fails.

Repro from the 2026-07-20 revised audit:
- On disk: propline_odds_*.json files from an earlier successful pull.
- Live API: 429 on every sport.
- Pre-fix: _get() only served cache when fresh (<90s TTL); on 429 after
  TTL expiry the files were ignored → sharp_meta.count=0, mispriced_only=0.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ud_edge.propline_client import (
    PropLineClient,
    load_cached_indexes,
    build_propline_indexes,
    fantasy_props_to_legs,
    parse_event_odds,
)
from tests.test_propline import SAMPLE_EVENT


def _write_odds_cache(cache_dir: Path, sport_key: str = "basketball_nba", event_id: str = "evt1") -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Match the filename shape PropLineClient.event_odds writes.
    markets = "player_points"
    path = cache_dir / f"propline_odds_{sport_key}_{event_id}_{markets}.json"
    detail = dict(SAMPLE_EVENT)
    detail["sport_key"] = sport_key
    path.write_text(json.dumps(detail), encoding="utf-8")
    return path


# ── _get stale-on-failure ────────────────────────────────────────────────────


def test_get_falls_back_to_stale_cache_on_http_429(tmp_path: Path):
    """After TTL expiry, a 429 must still return the on-disk cache body."""
    cache_dir = tmp_path / "sharp_cache"
    cache_file = _write_odds_cache(cache_dir)
    # Age the file past the default 90s TTL.
    old = time.time() - 600
    import os
    os.utime(cache_file, (old, old))

    client = PropLineClient(api_key="test-key", cache_path=cache_dir, ttl_seconds=90)

    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("429 Too Many Requests")

    with patch.object(client.session, "get", return_value=mock_resp) as mock_get:
        data = client._get(
            "/sports/basketball_nba/events/evt1/odds",
            params={"markets": "player_points"},
            cache_key="odds_basketball_nba_evt1_player_points",
        )

    assert isinstance(data, dict)
    assert data.get("id") == "evt1"
    assert mock_get.called
    # Stale fallback is not a billable call.
    assert client.calls_made == 0


def test_get_raises_when_http_fails_and_no_cache(tmp_path: Path):
    """With no cache file, HTTP failure must still raise (no silent empty)."""
    cache_dir = tmp_path / "sharp_cache"
    cache_dir.mkdir()
    client = PropLineClient(api_key="test-key", cache_path=cache_dir, ttl_seconds=90)

    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("429 Too Many Requests")

    with patch.object(client.session, "get", return_value=mock_resp):
        with pytest.raises(Exception, match="429"):
            client._get("/sports", cache_key="sports")


# ── load_cached_indexes ──────────────────────────────────────────────────────


def test_load_cached_indexes_rebuilds_sharp_and_fantasy(tmp_path: Path):
    cache_dir = tmp_path / "sharp_cache"
    _write_odds_cache(cache_dir)

    sharp, fantasy, meta = load_cached_indexes(cache_path=cache_dir, sports=["NBA"])

    assert meta.get("from_cache") is True
    assert meta.get("cache_files_loaded") == 1
    assert meta["count_sharp"] >= 1
    assert meta["count_fantasy"] >= 1
    # Pinnacle preferred over DK for the same player/stat/line
    assert any(v.get("bookmaker") == "pinnacle" for v in sharp.values())
    assert any(p.get("bookmaker") == "prizepicks" for p in fantasy)


def test_load_cached_indexes_no_key_required(tmp_path: Path):
    """Disk fallback must work with no PROPLINE_API_KEY / no client."""
    cache_dir = tmp_path / "sharp_cache"
    _write_odds_cache(cache_dir)
    sharp, fantasy, meta = load_cached_indexes(cache_path=cache_dir)
    assert sharp or fantasy
    assert meta["propline_calls"] == 0


def test_build_propline_indexes_falls_back_when_live_empty(tmp_path: Path, monkeypatch):
    """When fetch_sport_props returns [] for every sport, serve cache."""
    cache_dir = tmp_path / "sharp_cache"
    _write_odds_cache(cache_dir)

    def _empty(self, sport_id, **kw):
        return []

    monkeypatch.setattr(PropLineClient, "fetch_sport_props", _empty)

    sharp, fantasy, meta = build_propline_indexes(
        api_key="test-key",
        sports=["NBA"],
        cache_path=cache_dir,
    )
    assert sharp, "expected sharp index from cache fallback"
    assert meta.get("from_cache") is True
    assert any("sharp_cache fallback" in e for e in meta.get("errors", []))


# ── fantasy_props_to_legs skips Unknown / missing line ───────────────────────


def test_fantasy_props_to_legs_skips_unknown_player():
    legs = fantasy_props_to_legs([
        {"player": "", "line": 27.5, "over_decimal": 1.9, "under_decimal": 1.9,
         "bookmaker": "prizepicks", "stat": "points", "sport_id": "NBA"},
        {"player": "Jayson Tatum", "line": None, "over_decimal": 1.9, "under_decimal": 1.9,
         "bookmaker": "prizepicks", "stat": "points", "sport_id": "NBA"},
        {"player": "Jayson Tatum", "line": 27.5, "over_decimal": 1.9, "under_decimal": 1.9,
         "bookmaker": "prizepicks", "stat": "points", "sport_id": "NBA"},
    ])
    assert len(legs) == 1
    assert legs[0].player_name == "Jayson Tatum"
    assert legs[0].line_value == 27.5


# ── poller alert guard ───────────────────────────────────────────────────────


def test_alert_mispriced_skips_unknown_and_zero_line(tmp_path: Path, monkeypatch):
    from ud_edge.models import Leg, RankedLeg
    from ud_edge import poller
    from datetime import datetime, timezone

    def _bad(player, line):
        leg = Leg(
            line_id="x", player_id="p", player_name=player, sport_id="NBA",
            stat_name="points", line_value=line, line_type="balanced",
            higher_american=-110, higher_decimal=1.91, higher_multiplier=0.9,
            lower_american=-110, lower_decimal=1.91, lower_multiplier=0.9,
            fantasy_source="underdog",
        )
        return RankedLeg(
            leg=leg,
            higher_true_prob=0.55, higher_implied_prob=0.52, higher_edge_pp=1.0,
            lower_true_prob=0.45, lower_implied_prob=0.48, lower_edge_pp=-5.0,
            picked_side="higher", picked_true_prob=0.55, picked_edge_pp=1.0,
            overround=1.05,
            sharp_true_prob=0.60, sharp_book="pinnacle",
            mispricing_edge_pp=5.0,
        )

    called = {"n": 0}

    def fake_notify(**kwargs):
        called["n"] += 1
        return True

    monkeypatch.setattr(poller, "notify_opportunity", fake_notify)
    monkeypatch.setattr(poller, "should_alert", lambda *a, **k: True)

    now = datetime.now(timezone.utc)
    sent = poller._alert_mispriced(
        [_bad("Unknown", 0.0), _bad("", 27.5), _bad("Good Player", 27.5)],
        now,
        min_alert_pp=1.5,
    )
    assert sent == 1
    assert called["n"] == 1
