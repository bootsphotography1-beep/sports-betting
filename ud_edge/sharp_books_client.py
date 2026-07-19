"""Sharp-book cross-reference client.

Goal: detect mispricings between Underdog Fantasy and sharp/reputable
sportsbooks. If UD is offering a worse line on the same player+stat+total
than Pinnacle/DK/FanDuel, that's a +EV signal — we should pick the better
side on UD regardless of how UD prices it, because the sharp book is our
ground truth.

Source strategies (priority order for build_sharp_index):
  1. MANUAL: CSV at data/sharp_lines.csv
  2. Owned scrapers (DraftKings + FanDuel public JSON) — no API key
  3. SportsGameOdds free tier — set SPORTSGAMEODDS_KEY
  4. PropLine free tier (optional overlay) — set PROPLINE_API_KEY
     Live Pinnacle / DK / FD / BetMGM + PrizePicks / Sleeper props.
     1,000 req/day free; one bulk /odds call per sport.

Owned scrapers are the default path so the pipeline works without
third-party odds keys. PropLine, when present, overrides matching keys
(better two-sided + Pinnacle coverage).

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
    """Convert decimal or American odds string/number to decimal.

    PropLine returns American integers (e.g. -165, 125, 100). Decimal
    prices are typically in (1.0, ~50]. Treat |odds| >= 100 as American.
    """
    if odds is None:
        return None
    if isinstance(odds, (int, float)):
        if odds <= -100 or odds >= 100:
            am = int(odds)
            if am < 0:
                return 1.0 + 100.0 / abs(am)
            return 1.0 + am / 100.0
        if odds > 1.0:
            return float(odds)  # already decimal
        return None
    s = str(odds).strip()
    if not s:
        return None
    try:
        if s.startswith("+"):
            am = int(s[1:])
            return 1.0 + am / 100.0
        if s.startswith("-"):
            am = int(s[1:])
            return 1.0 + 100.0 / am
        val = float(s)
        if val <= -100 or val >= 100:
            return _to_decimal(val)
        if val > 1.0:
            return val
        return None
    except (ValueError, ZeroDivisionError):
        return None


# ── PropLine (free tier: 1,000 req/day) ─────────────────────────────────────
class PropLineClient:
    """PropLine player-props API — DK/FD/Pinnacle + PrizePicks/Underdog/Sleeper.

    Free tier (verified 2026-07-19):
      - 1,000 requests / day, no credit card
      - Books include pinnacle, draftkings, fanduel, betmgm, prizepicks,
        underdog, sleeper
      - Player props via /v1/sports/{sport}/odds?markets=...

    Auth: ?apiKey=... or X-API-Key header.
    Docs: https://prop-line.com/docs
    """
    BASE = "https://api.prop-line.com/v1"

    # UD sport_id -> PropLine sport key
    SPORT_MAP = {
        "MLB": "baseball_mlb",
        "NBA": "basketball_nba",
        "NFL": "football_nfl",
        "NHL": "hockey_nhl",
        "WNBA": "basketball_wnba",
        "CFB": "football_ncaaf",
        "TENNIS": "tennis",
        "FIFA": "soccer_fifa_world_cup",
    }

    # PropLine market key -> UD appearance_stat.stat name
    MARKET_TO_UD_STAT = {
        "batter_hits": "hits",
        "batter_total_bases": "total_bases",
        "batter_home_runs": "home_runs",
        "batter_rbis": "rbis",
        "batter_runs_scored": "runs",
        "batter_stolen_bases": "stolen_bases",
        "batter_walks": "walks",
        "batter_strikeouts": "batter_strikeouts",
        "pitcher_strikeouts": "strikeouts",
        "pitcher_hits_allowed": "hits_allowed",
        "pitcher_walks": "walks_allowed",
        "pitcher_earned_runs": "earned_runs",
        "player_points": "points",
        "player_rebounds": "rebounds",
        "player_assists": "assists",
        "player_threes": "threes",
        "player_steals": "steals",
        "player_blocks": "blocks",
        "player_turnovers": "turnovers",
        "player_points_rebounds_assists": "pts_rebs_asts",
        "player_shots_on_goal": "shots_on_goal",
        "player_goals": "goals",
        "player_saves": "saves",
        "player_power_play_points": "power_play_points",
        "player_pass_yds": "pass_yds",
        "player_pass_tds": "pass_tds",
        "player_rush_yds": "rush_yds",
        "player_rush_tds": "rush_tds",
        "player_reception_yds": "rec_yds",
        "player_receptions": "receptions",
        "player_anytime_td": "rec_tds",
    }

    # Prefer sharp / two-sided books for the UD pick pipeline index.
    BOOK_PRIORITY = (
        "pinnacle",
        "draftkings",
        "fanduel",
        "betmgm",
        "betrivers",
        "bovada",
        "sleeper",
        # underdog omitted — live UD API is the primary board for --once
    )

    SHARP_BOOKS = (
        "pinnacle",
        "draftkings",
        "fanduel",
        "betmgm",
        "betrivers",
        "bovada",
    )
    DFS_BOOKS = (
        "underdog",
        "sleeper",
        "prizepicks",
        "dabble",
    )

    DEFAULT_MARKETS_BY_SPORT = {
        "baseball_mlb": (
            "batter_hits,batter_total_bases,batter_home_runs,batter_rbis,"
            "batter_runs_scored,batter_stolen_bases,batter_walks,"
            "pitcher_strikeouts,pitcher_hits_allowed,pitcher_earned_runs"
        ),
        "basketball_nba": (
            "player_points,player_rebounds,player_assists,player_threes,"
            "player_steals,player_blocks,player_points_rebounds_assists"
        ),
        "basketball_wnba": (
            "player_points,player_rebounds,player_assists,player_threes,"
            "player_points_rebounds_assists"
        ),
        "football_nfl": (
            "player_pass_yds,player_pass_tds,player_rush_yds,player_rush_tds,"
            "player_reception_yds,player_receptions"
        ),
        "hockey_nhl": (
            "player_points,player_assists,player_goals,player_shots_on_goal,"
            "player_saves"
        ),
        "tennis": "player_aces,player_double_faults,player_games_won,player_break_points_won",
    }

    def __init__(self, api_key: str, cache_path: Optional[Path] = None,
                 ttl_seconds: int = 600):
        if not api_key:
            raise ValueError("PropLine api_key required")
        self.api_key = api_key
        self.cache_path = cache_path
        self.ttl_seconds = ttl_seconds
        self.session = requests.Session()

    def _get(self, path: str, params: dict, cache_key: str) -> object:
        if self.cache_path:
            cache_file = self.cache_path / f"propline_{cache_key}.json"
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < self.ttl_seconds:
                return json.loads(cache_file.read_text())
        params = dict(params)
        params["apiKey"] = self.api_key
        r = self.session.get(f"{self.BASE}{path}", params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        if self.cache_path:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            cache_file = self.cache_path / f"propline_{cache_key}.json"
            cache_file.write_text(json.dumps(data))
        return data

    def fetch_props_by_books(
        self,
        sport_id: str,
        bookmakers: list[str],
        *,
        collapse: bool = True,
    ) -> list[dict]:
        """Fetch two-sided player props for specific books.

        When collapse=True (default), keep the highest-priority book per
        player|stat|line. When False, return every book separately (needed
        for DFS-vs-sharp misprice scanning).
        """
        sport_key = self.SPORT_MAP.get(sport_id)
        if not sport_key:
            return []
        markets = self.DEFAULT_MARKETS_BY_SPORT.get(sport_key)
        if not markets:
            return []
        if not bookmakers:
            return []

        # Cache key must distinguish sharp vs DFS batches
        tag = "_".join(bookmakers[:4])
        data = self._get(
            f"/sports/{sport_key}/odds",
            {
                "markets": markets,
                "bookmakers": ",".join(bookmakers),
            },
            cache_key=f"odds_{sport_key}_{tag}",
        )
        if not isinstance(data, list):
            return []

        book_rank = {b: i for i, b in enumerate(self.BOOK_PRIORITY + self.DFS_BOOKS)}
        # Any unknown book still accepted when explicitly requested
        for i, b in enumerate(bookmakers):
            book_rank.setdefault(b, 100 + i)

        best: dict[str, tuple[int, dict]] = {}
        all_props: list[dict] = []

        for event in data:
            for book in event.get("bookmakers") or []:
                book_key = (book.get("key") or "").lower()
                if book_key not in book_rank and book_key not in bookmakers:
                    continue
                pairs: dict[tuple, dict] = {}
                for market in book.get("markets") or []:
                    mkey = market.get("key") or ""
                    ud_stat = self.MARKET_TO_UD_STAT.get(mkey)
                    if not ud_stat:
                        continue
                    for outcome in market.get("outcomes") or []:
                        dfs_type = outcome.get("dfs_odds_type")
                        if dfs_type and dfs_type != "standard":
                            continue
                        side = (outcome.get("name") or "").strip()
                        if side not in ("Over", "Under"):
                            continue
                        raw_player = (outcome.get("description") or "").strip()
                        # "Anthony Volpe (NYY)" → "Anthony Volpe"
                        player = re.sub(r"\s*\([^)]*\)\s*$", "", raw_player).strip()
                        point = outcome.get("point")
                        if not player or point is None:
                            continue
                        try:
                            line_val = float(point)
                        except (TypeError, ValueError):
                            continue
                        dec = _to_decimal(outcome.get("price"))
                        if dec is None:
                            continue
                        pair_key = (mkey, player, line_val)
                        bucket = pairs.setdefault(pair_key, {"stat": ud_stat})
                        if side == "Over":
                            bucket["over_decimal"] = dec
                        else:
                            bucket["under_decimal"] = dec
                        bucket["player"] = player
                        bucket["line"] = line_val

                for bucket in pairs.values():
                    if "over_decimal" not in bucket or "under_decimal" not in bucket:
                        continue
                    # Keep PrizePicks ±100 rows when collapse=False (line shopping);
                    # drop them when collapsing into a sharp index.
                    if collapse and (
                        abs(bucket["over_decimal"] - 2.0) < 1e-9
                        and abs(bucket["under_decimal"] - 2.0) < 1e-9
                    ):
                        continue
                    prop = {
                        "player": bucket["player"],
                        "stat": bucket["stat"],
                        "line": bucket["line"],
                        "over_decimal": bucket["over_decimal"],
                        "under_decimal": bucket["under_decimal"],
                        "bookmaker": book_key,
                        "sport": sport_id,
                    }
                    if not collapse:
                        all_props.append(prop)
                        continue
                    idx_key = (
                        f"{_normalize_name(prop['player'])}|{prop['stat']}|"
                        f"{prop['line']}"
                    )
                    rank = book_rank.get(book_key, 99)
                    prev = best.get(idx_key)
                    if prev is None or rank < prev[0]:
                        best[idx_key] = (rank, prop)

        if collapse:
            return [v for _, v in best.values()]
        return all_props

    def fetch_player_props(
        self,
        sport_id: str,
        bookmakers: Optional[list[str]] = None,
    ) -> list[dict]:
        """Fetch two-sided player props for a UD sport_id (collapsed to best book)."""
        books = bookmakers or list(self.BOOK_PRIORITY)
        return self.fetch_props_by_books(sport_id, books, collapse=True)


_SCRAPER_BOOK_PRIORITY = ["draftkings", "fanduel", "bovada"]


def _index_prop_rows(
    index: dict[str, dict],
    props: list[dict],
    *,
    source_default: str,
    book_priority: Optional[list[str]] = None,
) -> int:
    """Merge prop rows into index. Returns count of rows considered."""
    priority = book_priority or PropLineClient.BOOK_PRIORITY
    n = 0
    for p in props:
        if p.get("over_decimal") is None or p.get("under_decimal") is None:
            continue
        base_key = f"{_normalize_name(p['player'])}|{p['stat']}"
        line_key = f"{base_key}|{p['line']:g}"
        entry = {
            "over_decimal": p["over_decimal"],
            "under_decimal": p["under_decimal"],
            "bookmaker": p["bookmaker"],
            "line_value": p["line"],
            "source": p.get("source") or source_default,
        }
        index[line_key] = entry
        prev = index.get(base_key)
        new_rank = (
            priority.index(p["bookmaker"]) if p["bookmaker"] in priority else 99
        )
        old_rank = (
            priority.index(prev["bookmaker"])
            if prev and prev.get("bookmaker") in priority
            else 99
        )
        if prev is None or new_rank < old_rank:
            index[base_key] = entry
        n += 1
    return n


# ── Unified index builder ──────────────────────────────────────────────────
def build_sharp_index(manual_csv: Optional[Path] = None,
                      sgo_key: Optional[str] = None,
                      sgo_sports: Optional[list[str]] = None,
                      cache_path: Optional[Path] = None,
                      propline_key: Optional[str] = None,
                      propline_sports: Optional[list[str]] = None,
                      use_scrapers: bool = True,
                      scraper_sports: Optional[list[str]] = None) -> dict[str, dict]:
    """Build a sharp-book lookup index.

    Returns: {f"{normalize(player)}|{stat}": {over_decimal, under_decimal,
              bookmaker, line_value, source}}

    Priority (later sources override earlier):
      1. Manual CSV
      2. Owned scrapers (DK + FanDuel public endpoints)
      3. SportsGameOdds
      4. PropLine (optional overlay — live Pinnacle/DK/FD + DFS books)
    """
    index: dict[str, dict] = {}

    # 1. Manual CSV
    if manual_csv is not None and manual_csv.exists():
        manual = ManualSharpBookClient(manual_csv).load()
        for k, v in manual.items():
            v["source"] = "manual-csv"
            index[k] = v

    # 2. Owned scrapers (no third-party API key)
    if use_scrapers:
        try:
            from ud_edge.book_scrapers import fetch_owned_sharp_props
            sports = scraper_sports or ["MLB"]
            props = fetch_owned_sharp_props(sports=sports, cache_path=cache_path)
            n = _index_prop_rows(
                index, props,
                source_default="scraper",
                book_priority=_SCRAPER_BOOK_PRIORITY,
            )
            print(f"[sharp_books] owned scrapers indexed {n} props across {sports}")
        except Exception as e:
            print(f"[sharp_books] owned scrapers failed: {e}")

    # 3. SportsGameOdds
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

    # 4. PropLine (optional overlay — preferred when key present)
    if propline_key and propline_sports:
        try:
            pl = PropLineClient(propline_key, cache_path=cache_path)
            n_props = 0
            for sport in propline_sports:
                props = pl.fetch_player_props(sport)
                n_props += _index_prop_rows(
                    index, props, source_default=f"propline-{sport}"
                )
            print(f"[sharp_books] PropLine indexed {n_props} props across {propline_sports}")
        except Exception as e:
            print(f"[sharp_books] PropLine fetch failed: {e}")

    return index