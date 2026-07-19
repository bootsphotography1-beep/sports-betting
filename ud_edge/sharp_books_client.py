"""Sharp-book cross-reference client.

Goal: detect mispricings between Underdog Fantasy and sharp/reputable
sportsbooks. When a sharp line matches a UD leg, we compare the sharp book's
true probability for the SAME side UD already picked. If sharp assigns a
higher same-side probability, that is a +EV mispricing signal. If sharp
favors the opposite side, the leg is demoted/filtered — we do not flip sides
solely from sharp data.

Two source strategies:
  1. AUTO:   SportsGameOdds API (free tier: 2,500 objects/month, 9 books
             including Pinnacle/DK/FanDuel; verified 2026-07-18).
             Set SPORTSGAMEODDS_KEY env var to enable.
  2. MANUAL: CSV file at data/sharp_lines.csv. Format:
       player_name,stat_name,line_value,over_decimal,under_decimal,bookmaker
     You copy today's sharp lines from your sportsbook of choice and paste
     into this file. ~5 min/day for ~10-30 lines.

The matcher's rank_legs() accepts an optional `sharp_book_index` argument
(built from this client) and uses the sharper price when available.
"""
from __future__ import annotations
import csv
import json
import os
import re
import time
from pathlib import Path
from typing import Optional
import requests

from ud_edge.injury_client import normalize_name as _normalize_name


# ── Manual CSV loader ──────────────────────────────────────────────────────
class ManualSharpBookClient:
    """Reads sharp-book lines from a CSV the user maintains.

    CSV columns: player_name,stat_name,line_value,over_decimal,under_decimal,bookmaker
    Header row required.

    Example rows (NBA, Pinnacle):
        LeBron James,points,27.5,1.91,1.91,Pinnacle
        Jayson Tatum,rebounds,8.5,1.85,1.95,DraftKings
    """
    def __init__(self, csv_path: Path):
        self.csv_path = csv_path

    def load(self) -> dict[str, dict]:
        """Returns: {f"{normalize(player)}|{stat}": {over_decimal, under_decimal, bookmaker, line_value}}"""
        if not self.csv_path.exists():
            return {}
        out: dict[str, dict] = {}
        with open(self.csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = f"{_normalize_name(row['player_name'])}|{row['stat_name']}"
                out[key] = {
                    "over_decimal": float(row["over_decimal"]),
                    "under_decimal": float(row["under_decimal"]),
                    "bookmaker": row.get("bookmaker", "unknown"),
                    "line_value": float(row["line_value"]),
                }
        return out


# ── SportsGameOdds (free tier) ─────────────────────────────────────────────
class SportsGameOddsClient:
    """SportsGameOdds.com Amateur (free) tier.

    Free tier limits (verified 2026-07-18):
      - 2,500 objects / month (~80/day)
      - 10 requests / minute
      - 10-min update frequency
      - 8 leagues, 9 bookmakers (FanDuel, DraftKings, BetMGM, Caesars, ESPN BET,
        Bovada, Unibet, PointsBet, William Hill — Pinnacle NOT in free tier)
      - Player props INCLUDED ✅

    Endpoints (v2):
      GET /v2/events?leagueID=NFL&apiKey=...
      GET /v2/events/{eventID}?apiKey=...

    Each event response includes bookOdds with player props.
    """
    BASE = "https://api.sportsgameodds.com/v2"

    # Map our sport_id -> SportsGameOdds leagueID
    LEAGUE_MAP = {
        "NBA": "NBA",
        "NFL": "NFL",
        "MLB": "MLB",
        "NHL": "NHL",
        "WNBA": "WNBA",
        "CFB": "CFB",
        "EPL": "EPL",
        "MLS": "MLS",
        "UCL": "UCL",  # Champions League
    }

    def __init__(self, api_key: str, cache_path: Optional[Path] = None,
                 ttl_seconds: int = 600):
        if not api_key:
            raise ValueError("SportsGameOdds api_key required")
        self.api_key = api_key
        self.cache_path = cache_path
        self.ttl_seconds = ttl_seconds
        self.session = requests.Session()

    def _get(self, url: str, params: dict, cache_key: str) -> dict:
        if self.cache_path:
            cache_file = self.cache_path / f"sgo_{cache_key}.json"
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < self.ttl_seconds:
                return json.loads(cache_file.read_text())
        params["apiKey"] = self.api_key
        r = self.session.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if self.cache_path:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            cache_file = self.cache_path / f"sgo_{cache_key}.json"
            cache_file.write_text(json.dumps(data))
        return data

    def fetch_player_props(self, sport_id: str, look_ahead_days: int = 2) -> list[dict]:
        """Fetch all upcoming events for a sport, including player-prop odds.

        Returns flat list of: {player, stat, line, over_decimal, under_decimal, bookmaker}
        """
        league_id = self.LEAGUE_MAP.get(sport_id)
        if not league_id:
            return []
        data = self._get(
            f"{self.BASE}/events",
            {"leagueID": league_id, "oddsAvailable": "true", "limit": 50},
            f"events_{league_id}",
        )
        events = data.get("data", [])
        all_props: list[dict] = []
        for event in events:
            odds = event.get("odds", {}) or {}
            for book_key, book_data in odds.items():
                if not isinstance(book_data, dict):
                    continue
                # bookData may have lines or markets depending on schema
                lines = book_data.get("lines", {})
                if not isinstance(lines, dict):
                    continue
                for market_id, market_data in lines.items():
                    if not isinstance(market_data, dict):
                        continue
                    # Player props have a playerName somewhere
                    if "playerName" not in str(market_data) and "player" not in market_id.lower():
                        continue
                    odds_data = market_data.get("odds", {}) or {}
                    if "over" in odds_data and "under" in odds_data:
                        # Decimal odds preferred; fall back to American
                        over_odds = odds_data["over"]
                        under_odds = odds_data["under"]
                        all_props.append({
                            "event_id": event.get("eventID"),
                            "player": market_data.get("playerName") or market_data.get("player", "?"),
                            "stat": market_data.get("stat", market_id),
                            "line": market_data.get("line"),
                            "over_decimal": _to_decimal(over_odds),
                            "under_decimal": _to_decimal(under_odds),
                            "bookmaker": book_key,
                            "commence": event.get("commence"),
                        })
        return all_props


def _to_decimal(odds) -> Optional[float]:
    """Convert decimal or American odds string/number to decimal."""
    if odds is None:
        return None
    if isinstance(odds, (int, float)):
        # Likely already decimal
        if odds > 1.0:
            return float(odds)
        return None
    s = str(odds).strip()
    if not s:
        return None
    try:
        if s.startswith("+"):
            am = int(s[1:])
            return 1 + am / 100
        if s.startswith("-"):
            am = int(s[1:])
            return 1 + 100 / am
        # Try decimal directly
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


# ── Unified index builder ──────────────────────────────────────────────────
def build_sharp_index(manual_csv: Optional[Path] = None,
                      sgo_key: Optional[str] = None,
                      sgo_sports: Optional[list[str]] = None,
                      propline_key: Optional[str] = None,
                      propline_sports: Optional[list[str]] = None,
                      cache_path: Optional[Path] = None) -> dict[str, dict]:
    """Build a sharp-book lookup index.

    Returns: {f"{normalize(player)}|{stat}": {over_decimal, under_decimal,
              bookmaker, line_value, source}}

    Priority (later sources override earlier):
      1. Manual CSV
      2. SportsGameOdds (if key)
      3. PropLine (if key) — preferred: includes Pinnacle + Underdog two-way
         + DFS books for line workflows (PrizePicks/Sleeper/Dabble excluded
         from true-prob rows; see ``ud_edge.propline_client``)
    """
    index: dict[str, dict] = {}

    # 1. Manual CSV
    if manual_csv is not None and manual_csv.exists():
        manual = ManualSharpBookClient(manual_csv).load()
        for k, v in manual.items():
            v["source"] = "manual-csv"
            index[k] = v

    # 2. SportsGameOdds
    if sgo_key and sgo_sports:
        try:
            sgo = SportsGameOddsClient(sgo_key, cache_path=cache_path)
            for sport in sgo_sports:
                props = sgo.fetch_player_props(sport)
                for p in props:
                    if p.get("over_decimal") is None or p.get("under_decimal") is None:
                        continue
                    key = f"{_normalize_name(p['player'])}|{p['stat']}"
                    index[key] = {
                        "over_decimal": p["over_decimal"],
                        "under_decimal": p["under_decimal"],
                        "bookmaker": p["bookmaker"],
                        "line_value": p["line"],
                        "source": f"sgo-{sport}",
                    }
        except Exception as e:
            print(f"[sharp_books] SGO fetch failed: {e}")

    # 3. PropLine (prop-line.com) — activate when PROPLINE_API_KEY is provided
    if propline_key and propline_sports:
        try:
            from ud_edge.propline_client import (
                PropLineClient, fetch_sharp_props, BOOK_PRIORITY,
            )
            pl = PropLineClient(
                api_key=propline_key,
                cache_path=(cache_path / "propline") if cache_path else None,
            )
            for sport in propline_sports:
                for p in fetch_sharp_props(pl, sport):
                    key = f"{_normalize_name(p['player'])}|{p['stat']}"
                    new_pri = BOOK_PRIORITY.get(p.get("book_key", ""), 0)
                    old = index.get(key)
                    if old is not None:
                        old_src = (old.get("source") or "")
                        old_book = old_src.replace("propline-", "") if old_src.startswith("propline-") else ""
                        old_pri = BOOK_PRIORITY.get(old_book, 0)
                        if new_pri < old_pri:
                            continue
                    index[key] = {
                        "over_decimal": p["over_decimal"],
                        "under_decimal": p["under_decimal"],
                        "bookmaker": p["bookmaker"],
                        "line_value": p["line"],
                        "source": p.get("source", "propline"),
                    }
        except Exception as e:
            print(f"[sharp_books] PropLine fetch failed: {e}")

    return index