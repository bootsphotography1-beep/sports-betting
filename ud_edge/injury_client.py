"""ESPN public injury feed client.

ESPN publishes injury data on a public, unauthenticated REST API.
Endpoint pattern: https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/injuries

Coverage (verified 2026-07-18, all returning HTTP 200):
  - basketball/nba     (NBA, 28 teams, 1.5MB)
  - basketball/wnba    (WNBA)
  - football/nfl       (NFL, 32 teams, 9MB, 800 entries)
  - football/college-football (CFB)
  - baseball/mlb       (MLB, 30 teams, 3MB, 281 entries)
  - hockey/nhl         (NHL)
  - soccer/eng.1       (EPL)
  - soccer/usa.1       (MLS)
  - soccer/fifa.world  (FIFA World Cup)

Free, no auth, no quota. Cache aggressively (15-30 min — injury status
changes hour-to-hour near game time).

Usage:
    client = ESPNInjuryClient()
    status = client.get_player_status("Jayson Tatum", sport="NBA")
    # returns: "OUT" | "DAY_TO_DAY" | "PROBABLE" | "QUESTIONABLE" | "ACTIVE"
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
from typing import Optional
import requests


# Map UD sport_id -> ESPN endpoint suffix
SPORT_TO_ESPN = {
    "NBA": ("basketball", "nba"),
    "WNBA": ("basketball", "wnba"),
    "NFL": ("football", "nfl"),
    "CFB": ("football", "college-football"),
    "MLB": ("baseball", "mlb"),
    "NHL": ("hockey", "nhl"),
    "FIFA": ("soccer", "fifa.world"),  # World Cup
}

# UD stat names that should NOT be filtered just because a player is hurt
# (we still flag them, but they're informational)
PLAYING_HURT_OK_STATUSES = {"DAY_TO_DAY", "PROBABLE", "QUESTIONABLE"}

# Status values that mean "will not play" — filter these legs out
WILL_NOT_PLAY_STATUSES = {
    "OUT", "INJURY_RESERVE", "SUSPENDED", "INJURED_RESERVE",
    "DOUBTFUL",  # in NFL, doubtful ≈ 75% chance of not playing
}


def normalize_name(name: str) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace — match across sources."""
    import unicodedata
    # Fold accents: Andrés → Andres (so PropLine/UD/ESPN names align)
    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(
        re.sub(r"[^a-z0-9 ]", "", folded.lower()).split()
    )


def normalize_status(raw_status: str, short_comment: str = "") -> str:
    """Map ESPN's status strings to a normalized enum.

    ESPN uses free-text status like "Day-To-Day", "Out", "Questionable",
    "Probable", "Injury Reserve", plus short comments like "ruled out for...".
    We combine both to make a confident call.
    """
    s = (raw_status or "").lower().strip()
    sc = (short_comment or "").lower().strip()

    # Explicit "ruled out" / "will not play" anywhere in the comment
    if any(p in sc for p in ["ruled out", "won't play", "will not play", "out indefinitely", "season-ending"]):
        return "OUT"

    # Status strings
    if "out" in s and "day" not in s:  # "Out", "Out Indefinitely"
        return "OUT"
    if "injury reserve" in s or s == "ir":
        return "INJURY_RESERVE"
    if "suspended" in s:
        return "SUSPENDED"
    if "doubtful" in s:
        return "DOUBTFUL"
    if "questionable" in s:
        return "QUESTIONABLE"
    if "probable" in s:
        return "PROBABLE"
    if "day-to-day" in s or "day to day" in s:
        return "DAY_TO_DAY"
    if "active" in s or "healthy" in s:
        return "ACTIVE"
    return "UNKNOWN"


class ESPNInjuryClient:
    BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"

    def __init__(self, cache_path: Optional[Path] = None, ttl_seconds: int = 1800):
        self.cache_path = cache_path
        self.ttl_seconds = ttl_seconds
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; ud-edge-bot/0.1)",
            "Accept": "application/json",
        })
        # Cache: {(sport, league): {fetched_at: float, data: dict}}
        self._mem_cache: dict[tuple, dict] = {}

    def _fetch_league(self, sport: str, league: str) -> dict:
        """Fetch injury data for one league, with disk + memory cache."""
        key = (sport, league)
        now = time.time()

        # Memory cache
        if key in self._mem_cache:
            entry = self._mem_cache[key]
            if now - entry["fetched_at"] < self.ttl_seconds:
                return entry["data"]

        # Disk cache
        if self.cache_path:
            cache_file = self.cache_path / f"espn_inj_{sport}_{league}.json"
            if cache_file.exists() and (now - cache_file.stat().st_mtime) < self.ttl_seconds:
                data = json.loads(cache_file.read_text())
                self._mem_cache[key] = {"fetched_at": now, "data": data}
                return data

        url = f"{self.BASE_URL}/{sport}/{league}/injuries"
        r = self.session.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        self._mem_cache[key] = {"fetched_at": now, "data": data}
        if self.cache_path:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            cache_file = self.cache_path / f"espn_inj_{sport}_{league}.json"
            cache_file.write_text(json.dumps(data))
        return data

    def fetch_all_sports(self) -> dict[str, dict]:
        """Fetch injury data for all supported sports.

        Returns: {sport_id: {normalized_player_name: status}} for fast lookup.
        """
        out: dict[str, dict[str, str]] = {}
        for sport_id, (sport, league) in SPORT_TO_ESPN.items():
            try:
                data = self._fetch_league(sport, league)
            except Exception as e:
                # Don't fail the whole run if one league's feed is down
                print(f"[injury] {sport_id} fetch failed: {e}")
                out[sport_id] = {}
                continue

            status_by_name: dict[str, str] = {}
            for team in data.get("injuries", []):
                for inj in team.get("injuries", []):
                    athlete = inj.get("athlete") or {}
                    name = athlete.get("displayName") or ""
                    if not name:
                        continue
                    status = normalize_status(
                        inj.get("status", ""),
                        inj.get("shortComment", ""),
                    )
                    status_by_name[normalize_name(name)] = status

            out[sport_id] = status_by_name

        return out

    def get_player_status(self, player_name: str, sport: str,
                          injury_index: Optional[dict[str, dict[str, str]]] = None) -> str:
        """Look up a player's injury status. Returns normalized enum string."""
        if injury_index is None:
            injury_index = self.fetch_all_sports()
        sport_data = injury_index.get(sport, {})
        return sport_data.get(normalize_name(player_name), "ACTIVE")


if __name__ == "__main__":
    # Quick smoke test
    import sys
    client = ESPNInjuryClient(cache_path=Path("data/injury_cache"))
    idx = client.fetch_all_sports()
    for sport, players in idx.items():
        n_out = sum(1 for s in players.values() if s in WILL_NOT_PLAY_STATUSES)
        n_dtd = sum(1 for s in players.values() if s == "DAY_TO_DAY")
        print(f"  {sport}: {len(players)} players tracked, "
              f"{n_out} OUT, {n_dtd} Day-To-Day")