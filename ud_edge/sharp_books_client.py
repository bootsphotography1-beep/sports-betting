"""Sharp-book cross-reference client.

Goal: detect mispricings between fantasy platforms (Underdog / PrizePicks /
Sleeper) and sharp/reputable sportsbooks. If a fantasy app offers a softer
price on the same player+stat+line than DraftKings / FanDuel / BetMGM /
Pinnacle (manual), that is a +EV signal.

Source strategies (priority: later overrides earlier):
  1. MANUAL CSV  — data/sharp_lines.csv
  2. The Odds API — set ODDS_API_KEY (player props on supported sports)
  3. SportsGameOdds — set SPORTSGAMEODDS_KEY

CSV format:
  player_name,stat_name,line_value,over_decimal,under_decimal,bookmaker
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


# Canonical fantasy/UD stat names → common sportsbook market aliases
STAT_ALIASES: dict[str, set[str]] = {
    "points": {"points", "pts", "player_points", "point"},
    "rebounds": {"rebounds", "reb", "rebs", "player_rebounds"},
    "assists": {"assists", "ast", "asts", "player_assists"},
    "threes": {"threes", "3pm", "three_pointers", "threes_made", "made_threes",
               "player_threes", "three_point_made"},
    "steals": {"steals", "stl", "player_steals"},
    "blocks": {"blocks", "blk", "player_blocks"},
    "turnovers": {"turnovers", "to", "player_turnovers"},
    "fantasy_points": {"fantasy_points", "fantasy", "fp"},
    "pts_rebs_asts": {"pts_rebs_asts", "pra", "points_rebounds_assists"},
    "hits": {"hits", "player_hits", "batter_hits"},
    "runs": {"runs", "player_runs", "batter_runs"},
    "rbis": {"rbis", "rbi", "runs_batted_in", "batter_rbis"},
    "home_runs": {"home_runs", "hr", "homeruns", "batter_home_runs"},
    "stolen_bases": {"stolen_bases", "sb", "steals"},
    "strikeouts": {"strikeouts", "k", "ks", "pitcher_strikeouts", "so"},
    "total_bases": {"total_bases", "tb", "batter_total_bases"},
    "hits_runs_rbis": {"hits_runs_rbis", "hrr", "hits_+_runs_+_rbis"},
    "pass_yds": {"pass_yds", "passing_yards", "pass_yards", "passing_yds"},
    "pass_tds": {"pass_tds", "passing_tds", "pass_touchdowns"},
    "rush_yds": {"rush_yds", "rushing_yards", "rush_yards", "rushing_yds"},
    "rush_tds": {"rush_tds", "rushing_tds", "rush_touchdowns"},
    "rec_yds": {"rec_yds", "receiving_yards", "rec_yards", "receiving_yds"},
    "rec_tds": {"rec_tds", "receiving_tds", "rec_touchdowns"},
    "receptions": {"receptions", "recs", "player_receptions"},
    "goals": {"goals", "player_goals"},
    "shots_on_goal": {"shots_on_goal", "sog", "shots"},
    "saves": {"saves", "goalie_saves"},
}


def canonicalize_stat(stat: str) -> str:
    """Map a free-form sportsbook/fantasy stat string to a UD-ish canonical name."""
    if not stat:
        return ""
    raw = stat.strip().lower()
    # Normalize separators
    key = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    for canon, aliases in STAT_ALIASES.items():
        if key in aliases or raw in aliases:
            return canon
    return key


def sharp_lookup_key(player_name: str, stat_name: str) -> str:
    return f"{_normalize_name(player_name)}|{canonicalize_stat(stat_name)}"


# ── Manual CSV loader ──────────────────────────────────────────────────────
class ManualSharpBookClient:
    """Reads sharp-book lines from a CSV the user maintains.

    CSV columns: player_name,stat_name,line_value,over_decimal,under_decimal,bookmaker
    Header row required.
    """
    def __init__(self, csv_path: Path):
        self.csv_path = csv_path

    def load(self) -> dict[str, dict]:
        """Returns: {f"{normalize(player)}|{stat}": {...}}"""
        if not self.csv_path.exists():
            return {}
        out: dict[str, dict] = {}
        with open(self.csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row.get("player_name") or not row.get("stat_name"):
                    continue
                try:
                    key = sharp_lookup_key(row["player_name"], row["stat_name"])
                    out[key] = {
                        "over_decimal": float(row["over_decimal"]),
                        "under_decimal": float(row["under_decimal"]),
                        "bookmaker": row.get("bookmaker", "unknown"),
                        "line_value": float(row["line_value"]),
                        "player_name": row["player_name"].strip(),
                        "stat_name": canonicalize_stat(row["stat_name"]),
                    }
                except (ValueError, KeyError, TypeError):
                    continue
        return out


# ── The Odds API (player props) ────────────────────────────────────────────
class OddsApiClient:
    """the-odds-api.com player-prop adapter.

    Free tier: limited credits/month. Set ODDS_API_KEY to enable.
    Docs: https://the-odds-api.com/liveapi/guides/v4/#player-props

    Preferred books (in order): draftkings, fanduel, betmgm, williamhill_us,
    bovada. First book that prices both sides wins for that market.
    """
    BASE = "https://api.the-odds-api.com/v4"

    SPORT_KEYS = {
        "NBA": "basketball_nba",
        "NFL": "americanfootball_nfl",
        "MLB": "baseball_mlb",
        "NHL": "icehockey_nhl",
        "WNBA": "basketball_wnba",
        "CFB": "americanfootball_ncaaf",
        "MLS": "soccer_usa_mls",
        "EPL": "soccer_epl",
    }

    # Odds API market keys we care about → canonical stat
    MARKET_TO_STAT = {
        "player_points": "points",
        "player_rebounds": "rebounds",
        "player_assists": "assists",
        "player_threes": "threes",
        "player_blocks": "blocks",
        "player_steals": "steals",
        "player_turnovers": "turnovers",
        "player_points_rebounds_assists": "pts_rebs_asts",
        "batter_hits": "hits",
        "batter_home_runs": "home_runs",
        "batter_total_bases": "total_bases",
        "batter_rbis": "rbis",
        "batter_runs_scored": "runs",
        "batter_stolen_bases": "stolen_bases",
        "pitcher_strikeouts": "strikeouts",
        "player_pass_yds": "pass_yds",
        "player_pass_tds": "pass_tds",
        "player_rush_yds": "rush_yds",
        "player_rush_tds": "rush_tds",
        "player_reception_yds": "rec_yds",
        "player_receptions": "receptions",
        "player_goals": "goals",
        "player_shots_on_goal": "shots_on_goal",
    }

    PREFERRED_BOOKS = (
        "draftkings", "fanduel", "betmgm", "williamhill_us",
        "bovada", "betrivers", "pointsbetus",
    )

    def __init__(self, api_key: str, cache_path: Optional[Path] = None,
                 ttl_seconds: int = 600):
        if not api_key:
            raise ValueError("Odds API api_key required")
        self.api_key = api_key
        self.cache_path = cache_path
        self.ttl_seconds = ttl_seconds
        self.session = requests.Session()

    def _get(self, url: str, params: dict, cache_key: str):
        if self.cache_path:
            cache_file = self.cache_path / f"oddsapi_{cache_key}.json"
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < self.ttl_seconds:
                return json.loads(cache_file.read_text())
        params = dict(params)
        params["apiKey"] = self.api_key
        r = self.session.get(url, params=params, timeout=25)
        r.raise_for_status()
        data = r.json()
        if self.cache_path:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            cache_file = self.cache_path / f"oddsapi_{cache_key}.json"
            cache_file.write_text(json.dumps(data))
        return data

    def fetch_player_props(self, sport_id: str) -> list[dict]:
        sport_key = self.SPORT_KEYS.get(sport_id)
        if not sport_key:
            return []
        # First get events, then pull props per event (Odds API pattern)
        try:
            events = self._get(
                f"{self.BASE}/sports/{sport_key}/events",
                {},
                f"events_{sport_key}",
            )
        except Exception as e:
            print(f"[odds_api] events fetch failed for {sport_id}: {e}")
            return []

        if not isinstance(events, list):
            return []

        markets = ",".join(self.MARKET_TO_STAT.keys())
        all_props: list[dict] = []
        # Cap events to protect free-tier credits
        for event in events[:12]:
            event_id = event.get("id")
            if not event_id:
                continue
            try:
                detail = self._get(
                    f"{self.BASE}/sports/{sport_key}/events/{event_id}/odds",
                    {
                        "regions": "us",
                        "markets": markets,
                        "oddsFormat": "decimal",
                    },
                    f"props_{sport_key}_{event_id}",
                )
            except Exception as e:
                print(f"[odds_api] props fetch failed for {event_id}: {e}")
                continue
            all_props.extend(self._parse_event_odds(detail, sport_id))
        return all_props

    def _parse_event_odds(self, detail: dict, sport_id: str) -> list[dict]:
        if not isinstance(detail, dict):
            return []
        out: list[dict] = []
        bookmakers = detail.get("bookmakers") or []
        # Prefer sharper/more liquid US books
        bookmakers_sorted = sorted(
            bookmakers,
            key=lambda b: (
                self.PREFERRED_BOOKS.index(b.get("key"))
                if b.get("key") in self.PREFERRED_BOOKS else 99
            ),
        )
        # Track which (player,stat,line) we've already taken from a preferred book
        seen: set[str] = set()
        for book in bookmakers_sorted:
            book_key = book.get("key") or book.get("title") or "oddsapi"
            for market in book.get("markets") or []:
                mkey = market.get("key", "")
                stat = self.MARKET_TO_STAT.get(mkey) or canonicalize_stat(mkey)
                # Group outcomes by player+point into over/under pairs
                by_player: dict[tuple, dict] = {}
                for outcome in market.get("outcomes") or []:
                    name = (outcome.get("description") or outcome.get("name") or "").strip()
                    # Skip "Over"/"Under" as player names when description is separate
                    side_name = (outcome.get("name") or "").strip().lower()
                    point = outcome.get("point")
                    price = outcome.get("price")
                    if not name or point is None or price is None:
                        continue
                    # When description is the player and name is Over/Under
                    player = name
                    if side_name in ("over", "under") and outcome.get("description"):
                        player = outcome["description"].strip()
                        side = side_name
                    elif side_name in ("over", "under"):
                        # name is Over/Under without description — unusable
                        continue
                    else:
                        # Some schemas put player in name and Over/Under elsewhere
                        side = "over" if "over" in side_name else (
                            "under" if "under" in side_name else None
                        )
                        if side is None:
                            continue
                    pk = (player.lower(), float(point), stat)
                    slot = by_player.setdefault(pk, {"player": player, "line": float(point),
                                                     "stat": stat})
                    slot[f"{side}_decimal"] = _to_decimal(price)

                for pk, slot in by_player.items():
                    if "over_decimal" not in slot or "under_decimal" not in slot:
                        continue
                    dedupe = f"{pk[0]}|{pk[2]}|{pk[1]}"
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)
                    out.append({
                        "player": slot["player"],
                        "stat": slot["stat"],
                        "line": slot["line"],
                        "over_decimal": slot["over_decimal"],
                        "under_decimal": slot["under_decimal"],
                        "bookmaker": book_key,
                        "sport_id": sport_id,
                        "event": detail.get("home_team", "") + "@" + detail.get("away_team", ""),
                    })
        return out


# ── SportsGameOdds (free tier) ─────────────────────────────────────────────
class SportsGameOddsClient:
    """SportsGameOdds.com Amateur (free) tier.

    Free tier limits (verified 2026-07-18):
      - 2,500 objects / month (~80/day)
      - 10 requests / minute
      - Player props included; Pinnacle NOT in free tier
    """
    BASE = "https://api.sportsgameodds.com/v2"

    LEAGUE_MAP = {
        "NBA": "NBA",
        "NFL": "NFL",
        "MLB": "MLB",
        "NHL": "NHL",
        "WNBA": "WNBA",
        "CFB": "CFB",
        "EPL": "EPL",
        "MLS": "MLS",
        "UCL": "UCL",
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
        params = dict(params)
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
        """Fetch upcoming events for a sport, including player-prop odds.

        Parses both legacy `{odds: {book: {lines: ...}}}` shapes and newer
        nested market lists when present.
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
            all_props.extend(self._parse_event(event, sport_id))
        return all_props

    def _parse_event(self, event: dict, sport_id: str) -> list[dict]:
        props: list[dict] = []
        odds = event.get("odds", {}) or {}

        # Shape A: odds keyed by bookmaker → lines → market
        if isinstance(odds, dict):
            for book_key, book_data in odds.items():
                if not isinstance(book_data, dict):
                    continue
                lines = book_data.get("lines", book_data.get("markets", {}))
                if isinstance(lines, dict):
                    for market_id, market_data in lines.items():
                        parsed = self._parse_market(market_id, market_data, book_key, event, sport_id)
                        if parsed:
                            props.append(parsed)
                elif isinstance(lines, list):
                    for market_data in lines:
                        mid = (market_data or {}).get("id") or (market_data or {}).get("marketID") or ""
                        parsed = self._parse_market(mid, market_data, book_key, event, sport_id)
                        if parsed:
                            props.append(parsed)

        # Shape B: flat list of odd objects on the event
        flat = event.get("playerProps") or event.get("props") or []
        if isinstance(flat, list):
            for item in flat:
                if not isinstance(item, dict):
                    continue
                player = item.get("playerName") or item.get("player") or item.get("name")
                stat = canonicalize_stat(item.get("stat") or item.get("market") or "")
                line = item.get("line") or item.get("point") or item.get("total")
                over = item.get("over_decimal") or item.get("over")
                under = item.get("under_decimal") or item.get("under")
                if player and stat and line is not None and over and under:
                    od, ud = _to_decimal(over), _to_decimal(under)
                    if od and ud:
                        props.append({
                            "event_id": event.get("eventID") or event.get("id"),
                            "player": player,
                            "stat": stat,
                            "line": float(line),
                            "over_decimal": od,
                            "under_decimal": ud,
                            "bookmaker": item.get("bookmaker") or item.get("book") or "sgo",
                            "sport_id": sport_id,
                            "commence": event.get("commence") or event.get("startTime"),
                        })
        return props

    def _parse_market(self, market_id, market_data, book_key, event, sport_id) -> Optional[dict]:
        if not isinstance(market_data, dict):
            return None
        blob = str(market_data) + str(market_id)
        if "playerName" not in blob and "player" not in str(market_id).lower():
            # Still allow if explicit player field exists
            if not market_data.get("playerName") and not market_data.get("player"):
                return None
        player = market_data.get("playerName") or market_data.get("player") or "?"
        stat = canonicalize_stat(market_data.get("stat") or str(market_id))
        line = market_data.get("line") or market_data.get("point") or market_data.get("total")
        odds_data = market_data.get("odds", {}) or {}
        over_odds = odds_data.get("over") or market_data.get("over")
        under_odds = odds_data.get("under") or market_data.get("under")
        if line is None or over_odds is None or under_odds is None:
            return None
        od, ud = _to_decimal(over_odds), _to_decimal(under_odds)
        if not od or not ud:
            return None
        return {
            "event_id": event.get("eventID") or event.get("id"),
            "player": player,
            "stat": stat,
            "line": float(line),
            "over_decimal": od,
            "under_decimal": ud,
            "bookmaker": book_key,
            "sport_id": sport_id,
            "commence": event.get("commence") or event.get("startTime"),
        }


def _to_decimal(odds) -> Optional[float]:
    """Convert decimal or American odds string/number to decimal."""
    if odds is None:
        return None
    if isinstance(odds, dict):
        # Nested {price: ...} / {decimal: ...} / {american: ...}
        for k in ("decimal", "price", "odds", "american"):
            if k in odds:
                return _to_decimal(odds[k])
        return None
    if isinstance(odds, (int, float)):
        # American odds are typically ≤ -100 or ≥ +100; decimal are > 1.0
        val = float(odds)
        if val <= -100 or val >= 100:
            # American numeric
            if val > 0:
                return 1 + val / 100
            return 1 + 100 / abs(val)
        if val > 1.0:
            return val
        return None
    s = str(odds).strip()
    if not s:
        return None
    try:
        if s.startswith("+"):
            am = int(s[1:])
            return 1 + am / 100
        if s.startswith("-") and s[1:].isdigit():
            am = int(s[1:])
            return 1 + 100 / am
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


# ── Unified index builder ──────────────────────────────────────────────────
def build_sharp_index(manual_csv: Optional[Path] = None,
                      sgo_key: Optional[str] = None,
                      sgo_sports: Optional[list[str]] = None,
                      odds_api_key: Optional[str] = None,
                      odds_api_sports: Optional[list[str]] = None,
                      cache_path: Optional[Path] = None) -> dict[str, dict]:
    """Build a sharp-book lookup index.

    Returns: {f"{normalize(player)}|{stat}": {over_decimal, under_decimal,
              bookmaker, line_value, source, ...}}

    Priority (later wins): manual CSV < Odds API < SportsGameOdds
    """
    index: dict[str, dict] = {}

    # 1. Manual CSV
    if manual_csv is not None and manual_csv.exists():
        manual = ManualSharpBookClient(manual_csv).load()
        for k, v in manual.items():
            v["source"] = "manual-csv"
            index[k] = v

    # 2. The Odds API
    odds_key = odds_api_key or os.environ.get("ODDS_API_KEY", "")
    if odds_key and (odds_api_sports or sgo_sports):
        sports = odds_api_sports or sgo_sports or []
        try:
            client = OddsApiClient(odds_key, cache_path=cache_path)
            for sport in sports:
                props = client.fetch_player_props(sport)
                for p in props:
                    if not p.get("over_decimal") or not p.get("under_decimal"):
                        continue
                    key = sharp_lookup_key(p["player"], p["stat"])
                    index[key] = {
                        "over_decimal": p["over_decimal"],
                        "under_decimal": p["under_decimal"],
                        "bookmaker": p.get("bookmaker", "oddsapi"),
                        "line_value": p.get("line"),
                        "player_name": p["player"],
                        "stat_name": canonicalize_stat(p["stat"]),
                        "sport_id": p.get("sport_id") or sport,
                        "source": f"oddsapi-{sport}",
                    }
            print(f"[sharp_books] Odds API indexed props for {sports}")
        except Exception as e:
            print(f"[sharp_books] Odds API fetch failed: {e}")

    # 3. SportsGameOdds
    if sgo_key and sgo_sports:
        try:
            sgo = SportsGameOddsClient(sgo_key, cache_path=cache_path)
            for sport in sgo_sports:
                props = sgo.fetch_player_props(sport)
                for p in props:
                    if not p.get("over_decimal") or not p.get("under_decimal"):
                        continue
                    key = sharp_lookup_key(p["player"], p["stat"])
                    index[key] = {
                        "over_decimal": p["over_decimal"],
                        "under_decimal": p["under_decimal"],
                        "bookmaker": p.get("bookmaker", "sgo"),
                        "line_value": p.get("line"),
                        "player_name": p["player"],
                        "stat_name": canonicalize_stat(p["stat"]),
                        "sport_id": p.get("sport_id") or sport,
                        "source": f"sgo-{sport}",
                    }
        except Exception as e:
            print(f"[sharp_books] SGO fetch failed: {e}")

    return index


def find_sharp_match(
    sharp_index: dict[str, dict],
    player_name: str,
    stat_name: str,
    line_value: float,
    line_tolerance: float = 0.5,
) -> Optional[dict]:
    """Look up a sharp line for a fantasy prop, with line tolerance."""
    if not sharp_index:
        return None
    key = sharp_lookup_key(player_name, stat_name)
    sharp = sharp_index.get(key)
    if sharp is not None:
        lv = sharp.get("line_value")
        if lv is None or abs(float(lv) - line_value) <= line_tolerance:
            return sharp
        # Same player+stat but line too far — still useful as reference
        if abs(float(lv) - line_value) <= line_tolerance * 2:
            return sharp

    # Fuzzy: same normalized player, canonical stat, nearby line
    canon = canonicalize_stat(stat_name)
    norm_player = _normalize_name(player_name)
    for k, v in sharp_index.items():
        parts = k.split("|", 1)
        if len(parts) != 2:
            continue
        if parts[0] != norm_player:
            continue
        if canonicalize_stat(parts[1]) != canon:
            continue
        lv = v.get("line_value")
        if lv is not None and abs(float(lv) - line_value) <= line_tolerance:
            return v
    return None
