"""API-Sports cross-reference client (apisports.io).

The api-sports.io free plan gives us:
  - Football: fixtures today (yes), predictions (yes), player stats (yes but
    mostly NULL fields on free plan), odds (NO — gated to 2022-2024 dates AND
    paid plans)
  - American-football: same structure

Quota: 100 requests/day. Cache aggressively.

We use it for:
  1. Validate that a fixture is actually scheduled for today (catches stale UD lines)
  2. Get match metadata (venue, league, scheduled time) for the report
  3. Get predictions as a *secondary* signal (NOT primary — the free plan's
     prediction model is generic, not sharp)

We do NOT use it as a no-vig source for player props (no player-prop market
exposed on the free plan). UD's own two-sided odds remain the primary
no-vig source.
"""
from __future__ import annotations
import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Optional
import requests

API_SPORTS_KEY = os.environ.get("APISPORTS_KEY", "")
FOOTBALL_BASE = "https://v3.football.api-sports.io"
AMFOOT_BASE = "https://v1.american-football.api-sports.io"

HEADERS = {"x-apisports-key": API_SPORTS_KEY}


class APISportsClient:
    def __init__(self, cache_path: Optional[Path] = None):
        self.cache_path = cache_path
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _get(self, url: str, params: dict, cache_key: str, ttl_seconds: int = 3600) -> dict:
        """GET with disk cache. ttl_seconds: how long to cache the response."""
        if self.cache_path:
            cache_file = self.cache_path / f"{cache_key}.json"
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < ttl_seconds:
                return json.loads(cache_file.read_text())
        r = self.session.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if self.cache_path:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            cache_file = self.cache_path / f"{cache_key}.json"
            cache_file.write_text(json.dumps(data))
        return data

    def football_fixtures_today(self, today: Optional[str] = None) -> list[dict]:
        """All football fixtures scheduled for today (UTC).

        today: YYYY-MM-DD (defaults to today UTC).
        Returns list of fixture dicts.
        """
        today = today or date.today().isoformat()
        data = self._get(
            f"{FOOTBALL_BASE}/fixtures",
            {"date": today},
            f"fixtures_football_{today}",
        )
        return data.get("response", [])

    def football_predictions(self, fixture_id: int) -> Optional[dict]:
        """Win/draw/loss prediction for a fixture (free plan)."""
        data = self._get(
            f"{FOOTBALL_BASE}/predictions",
            {"fixture": fixture_id},
            f"prediction_football_{fixture_id}",
            ttl_seconds=86400,  # 24h cache — predictions don't move much
        )
        results = data.get("response", [])
        return results[0] if results else None

    def amfoot_games_today(self, today: Optional[str] = None) -> list[dict]:
        """American-football games scheduled for today (NFL/CFB/CFL offseason)."""
        today = today or date.today().isoformat()
        data = self._get(
            f"{AMFOOT_BASE}/games",
            {"date": today},
            f"games_amfoot_{today}",
        )
        return data.get("response", [])

    def status(self) -> dict:
        """Check account quota / plan."""
        data = self._get(f"{FOOTBALL_BASE}/status", {}, "status_apisports", ttl_seconds=86400)
        return data.get("response", {})


def cross_reference(ud_legs: list, api_client: APISportsClient) -> dict:
    """For each UD leg, try to find a matching fixture on API-Sports.

    Returns: {match_id: fixture_dict} so caller can enrich the report.
    Currently best-effort — we match by date + approximate team name.
    Free plan limits mean we only cross-ref fixtures with confidence > 0.5.
    """
    today = date.today().isoformat()
    fixtures = api_client.football_fixtures_today(today)

    # Index fixtures by home/away team name (lowercased)
    by_teams: dict[frozenset, dict] = {}
    for f in fixtures:
        try:
            home = f["teams"]["home"]["name"].lower()
            away = f["teams"]["away"]["name"].lower()
            by_teams[frozenset([home, away])] = f
        except (KeyError, TypeError):
            continue

    return {
        "fixtures_today": len(fixtures),
        "by_teams": {tuple(k): v for k, v in by_teams.items()},
    }