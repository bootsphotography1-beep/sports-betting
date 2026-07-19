"""PropLine (prop-line.com) multi-book player-props client.

Activated by ``PROPLINE_API_KEY``. Uses the bulk ``/v1/sports/{sport}/odds``
endpoint (the-odds-api compatible) so one call returns Underdog, PrizePicks,
Sleeper, Dabble, Pinnacle, DK/FD, Kalshi, Polymarket, etc.

Books
─────
| key          | Role                                                              |
|--------------|-------------------------------------------------------------------|
| pinnacle     | Preferred sharp ground-truth for same-side no-vig                 |
| draftkings   | Retail sharp-ish                                                  |
| fanduel      | Retail sharp-ish                                                  |
| sleeper      | Real two-way DFS prices (usable for no-vig when not even-money)   |
| dabble       | Real two-way DFS prices (usable for no-vig when not even-money)   |
| underdog     | Real two-way American + payout_multiplier                         |
| prizepicks   | Line-only (synthetic +100/+100) — stale/line compare, not no-vig  |
| kalshi       | Exchange / event contracts (mostly game lines)                    |
| polymarket   | Prediction markets (mostly game lines)                            |

Docs: https://prop-line.com/docs  ·  Base: https://api.prop-line.com/v1
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Iterable, Optional

import requests

from ud_edge.injury_client import normalize_name as _normalize_name


BASE_URL = "https://api.prop-line.com/v1"

BOOK_UNDERDOG = "underdog"
BOOK_PRIZEPICKS = "prizepicks"
BOOK_SLEEPER = "sleeper"
BOOK_DABBLE = "dabble"
BOOK_PINNACLE = "pinnacle"
BOOK_DRAFTKINGS = "draftkings"
BOOK_FANDUEL = "fanduel"
BOOK_BETMGM = "betmgm"
BOOK_KALSHI = "kalshi"
BOOK_POLYMARKET = "polymarket"

# Higher wins when multiple books map to the same player|stat
BOOK_PRIORITY = {
    BOOK_PINNACLE: 100,
    BOOK_DRAFTKINGS: 80,
    BOOK_FANDUEL: 75,
    BOOK_BETMGM: 70,
    BOOK_SLEEPER: 55,
    BOOK_DABBLE: 50,
    BOOK_UNDERDOG: 40,
    BOOK_PRIZEPICKS: 10,
}

# Usable for same-side true-prob when both sides have non-even-money prices
SHARP_PROB_BOOKS = frozenset({
    BOOK_PINNACLE,
    BOOK_DRAFTKINGS,
    BOOK_FANDUEL,
    BOOK_BETMGM,
    BOOK_SLEEPER,
    BOOK_DABBLE,
    BOOK_UNDERDOG,
})

# Synthetic even-money DFS — line compare / stale only
DFS_LINE_ONLY_BOOKS = frozenset({BOOK_PRIZEPICKS})

EXCHANGE_BOOKS = frozenset({BOOK_KALSHI, BOOK_POLYMARKET})

# UD sport_id → PropLine sport key (verified live 2026-07-19)
SPORT_MAP = {
    "NBA": "basketball_nba",
    "WNBA": "basketball_wnba",
    "NFL": "football_nfl",
    "CFB": "football_ncaaf",
    "MLB": "baseball_mlb",
    "NHL": "hockey_nhl",
    "MMA": "mma_ufc",
    "UFC": "mma_ufc",
    "PGA": "golf",
    "TENNIS": "tennis",
}

# Default prop markets requested per PropLine sport key
DEFAULT_MARKETS = {
    "baseball_mlb": (
        "pitcher_strikeouts,batter_hits,batter_home_runs,batter_total_bases,"
        "batter_rbis,batter_runs_scored,batter_stolen_bases,batter_walks,"
        "pitcher_hits_allowed,pitcher_walks,pitcher_earned_runs"
    ),
    "basketball_nba": (
        "player_points,player_rebounds,player_assists,player_threes,"
        "player_blocks,player_steals,player_turnovers,player_points_rebounds_assists"
    ),
    "basketball_wnba": (
        "player_points,player_rebounds,player_assists,player_threes,"
        "player_points_rebounds_assists"
    ),
    "football_nfl": (
        "player_pass_yds,player_pass_tds,player_rush_yds,player_rush_tds,"
        "player_receptions,player_reception_yds,player_reception_tds"
    ),
    "football_ncaaf": (
        "player_pass_yds,player_pass_tds,player_rush_yds,player_reception_yds"
    ),
    "hockey_nhl": (
        "player_points,player_goals,player_assists,player_shots_on_goal,player_goalie_saves"
    ),
    "mma_ufc": "fighter_significant_strikes,fighter_takedowns",
}

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
    "batter_rbis": "rbis",
    "batter_runs_scored": "runs",
    "batter_total_bases": "total_bases",
    "batter_stolen_bases": "stolen_bases",
    "batter_walks": "walks",
    "pitcher_strikeouts": "strikeouts",
    "pitcher_hits_allowed": "hits_allowed",
    "pitcher_walks": "walks_allowed",
    "pitcher_earned_runs": "earned_runs",
    "player_shots_on_goal": "shots_on_goal",
    "player_goals": "goals",
    "player_goalie_saves": "saves",
    "player_pass_yds": "pass_yds",
    "player_pass_tds": "pass_tds",
    "player_rush_yds": "rush_yds",
    "player_rush_tds": "rush_tds",
    "player_receptions": "receptions",
    "player_reception_yds": "rec_yds",
    "player_reception_tds": "rec_tds",
    "fighter_significant_strikes": "significant_strikes",
    "fighter_takedowns": "takedowns",
}

# Reverse: PropLine sport key → UD sport_id
SPORT_KEY_TO_UD = {v: k for k, v in SPORT_MAP.items()}


def _american_to_decimal(american: int | float) -> float:
    a = int(american)
    if a == 0:
        raise ValueError("american odds cannot be 0")
    if a < 0:
        return 1.0 + 100.0 / abs(a)
    return 1.0 + a / 100.0


def _is_extreme_ud_multiplier(mult) -> bool:
    """True for clear boost/discount specials; normal UD multipliers are ~0.75–1.15."""
    if mult in (None, "", 1, 1.0):
        return False
    try:
        m = float(mult)
    except (TypeError, ValueError):
        return False
    return m >= 1.35 or m <= 0.65


class PropLineClient:
    """HTTP client for api.prop-line.com."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_path: Optional[Path] = None,
        ttl_seconds: int = 120,
        timeout: int = 45,
    ):
        if api_key is None:
            self.api_key = (os.environ.get("PROPLINE_API_KEY") or "").strip()
        else:
            self.api_key = api_key.strip()
        if not self.api_key:
            raise ValueError(
                "PROPLINE_API_KEY required — set env or pass api_key="
            )
        self.cache_path = cache_path
        self.ttl_seconds = ttl_seconds
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "ud-edge-bot/0.1 (+propline)",
        })

    def _cache_file(self, cache_key: str) -> Path:
        """Short, filesystem-safe cache path (markets/bookmakers lists are long)."""
        digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:16]
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in cache_key)[:48]
        return self.cache_path / f"propline_{safe}_{digest}.json"

    def _get(self, path: str, params: Optional[dict] = None, cache_key: Optional[str] = None):
        params = dict(params or {})
        params.setdefault("apiKey", self.api_key)

        if cache_key and self.cache_path:
            cache_file = self._cache_file(cache_key)
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < self.ttl_seconds:
                return json.loads(cache_file.read_text())

        r = self.session.get(f"{BASE_URL}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()

        if cache_key and self.cache_path:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            self._cache_file(cache_key).write_text(json.dumps(data))
        return data

    def list_sports(self) -> list[dict]:
        data = self._get("/sports", cache_key="sports")
        return data if isinstance(data, list) else []

    def list_events(self, sport_key: str) -> list[dict]:
        data = self._get(f"/sports/{sport_key}/events", cache_key=f"events_{sport_key}")
        return data if isinstance(data, list) else []

    def fetch_bulk_odds(
        self,
        sport_key: str,
        markets: Optional[str] = None,
        bookmakers: Optional[str] = None,
    ) -> list[dict]:
        """Bulk odds for a sport — preferred path for player props."""
        params: dict = {}
        params["markets"] = markets or DEFAULT_MARKETS.get(sport_key, "h2h")
        if bookmakers:
            params["bookmakers"] = bookmakers
        data = self._get(
            f"/sports/{sport_key}/odds",
            params=params,
            cache_key=f"bulk_{sport_key}_{params['markets']}_{bookmakers or 'all'}",
        )
        return data if isinstance(data, list) else []

    def fetch_event_odds(
        self,
        sport_key: str,
        event_id: str,
        markets: Optional[str] = None,
        bookmakers: Optional[str] = None,
    ) -> dict:
        params: dict = {}
        if markets:
            params["markets"] = markets
        if bookmakers:
            params["bookmakers"] = bookmakers
        data = self._get(
            f"/sports/{sport_key}/events/{event_id}/odds",
            params=params,
            cache_key=f"odds_{sport_key}_{event_id}_{markets or 'all'}_{bookmakers or 'all'}",
        )
        return data if isinstance(data, dict) else {}


def parse_prop_outcomes_to_index_rows(
    event_payload: dict,
    *,
    allow_books: Optional[Iterable[str]] = None,
    for_true_prob: bool = True,
    sport_id: str = "",
) -> list[dict]:
    """Flatten a PropLine event (or bulk-odds event) into index/snapshot rows."""
    allow = set(allow_books) if allow_books is not None else None
    rows: list[dict] = []
    away = event_payload.get("away_team") or ""
    home = event_payload.get("home_team") or ""
    match_title = f"{away}@{home}".strip("@") if (away or home) else ""
    scheduled = event_payload.get("commence_time") or ""
    sport_key = event_payload.get("sport_key") or ""
    ud_sport = sport_id or SPORT_KEY_TO_UD.get(sport_key, sport_key)

    for book in event_payload.get("bookmakers") or []:
        book_key = (book.get("key") or "").lower()
        if allow is not None and book_key not in allow:
            continue
        if for_true_prob and book_key not in SHARP_PROB_BOOKS:
            continue
        if not for_true_prob and book_key not in (DFS_LINE_ONLY_BOOKS | SHARP_PROB_BOOKS):
            continue

        for market in book.get("markets") or []:
            market_key = market.get("key") or ""
            # Skip pure game lines when we asked for props
            if market_key in {"h2h", "spreads", "totals"}:
                continue
            stat = MARKET_TO_STAT.get(market_key, market_key)
            by_player_line: dict[tuple[str, float], dict] = {}

            for outcome in market.get("outcomes") or []:
                name = (outcome.get("name") or "").strip().lower()
                player = (outcome.get("description") or "").strip()
                if not player or name not in {"over", "under", "higher", "lower"}:
                    continue
                point = outcome.get("point")
                if point is None:
                    continue
                try:
                    line_val = float(point)
                    price = int(outcome["price"])
                    dec = _american_to_decimal(price)
                except (TypeError, ValueError, KeyError):
                    continue

                if book_key == BOOK_UNDERDOG and _is_extreme_ud_multiplier(
                    outcome.get("payout_multiplier")
                ):
                    continue
                dfs_type = outcome.get("dfs_odds_type")
                if book_key == BOOK_PRIZEPICKS and dfs_type not in (None, "", "standard"):
                    continue

                side = "over" if name in {"over", "higher"} else "under"
                key = (_normalize_name(player), line_val)
                slot = by_player_line.setdefault(key, {
                    "player": player,
                    "stat": stat,
                    "line": line_val,
                    "bookmaker": book.get("title") or book_key,
                    "book_key": book_key,
                    "match_title": match_title,
                    "scheduled_at": scheduled,
                    "sport_id": ud_sport,
                })
                slot[f"{side}_decimal"] = dec
                slot[f"{side}_american"] = price

            for slot in by_player_line.values():
                if "over_decimal" not in slot or "under_decimal" not in slot:
                    continue
                if for_true_prob:
                    if book_key in DFS_LINE_ONLY_BOOKS:
                        continue
                    # Reject synthetic even-money both sides
                    if (
                        abs(slot["over_decimal"] - 2.0) < 1e-9
                        and abs(slot["under_decimal"] - 2.0) < 1e-9
                    ):
                        continue
                rows.append({
                    "player": slot["player"],
                    "stat": slot["stat"],
                    "line": slot["line"],
                    "over_decimal": slot["over_decimal"],
                    "under_decimal": slot["under_decimal"],
                    "bookmaker": slot["bookmaker"],
                    "book_key": slot["book_key"],
                    "source": f"propline-{slot['book_key']}",
                    "match_title": slot["match_title"],
                    "scheduled_at": slot["scheduled_at"],
                    "sport_id": slot["sport_id"],
                })
    return rows


def fetch_sharp_props(
    client: PropLineClient,
    sport_id: str,
    *,
    bookmakers: Optional[str] = None,
    max_events: int = 40,
) -> list[dict]:
    """Fetch true-prob-usable prop rows for one UD sport_id via bulk /odds."""
    sport_key = SPORT_MAP.get(sport_id)
    if not sport_key:
        return []
    books = bookmakers or ",".join(sorted(SHARP_PROB_BOOKS))
    events = client.fetch_bulk_odds(
        sport_key,
        markets=DEFAULT_MARKETS.get(sport_key),
        bookmakers=books,
    )
    rows: list[dict] = []
    for event in events[:max_events]:
        rows.extend(
            parse_prop_outcomes_to_index_rows(
                event, for_true_prob=True, sport_id=sport_id
            )
        )
    # Prefer higher-priority books first so callers that last-write-wins keep Pinnacle
    rows.sort(key=lambda r: BOOK_PRIORITY.get(r.get("book_key", ""), 0))
    return rows


def fetch_line_observations(
    client: PropLineClient,
    sport_id: str,
    *,
    books: Optional[Iterable[str]] = None,
    max_events: int = 40,
) -> list[dict]:
    """Fetch observation dicts for snapshot/stale ingestion (includes PrizePicks)."""
    sport_key = SPORT_MAP.get(sport_id)
    if not sport_key:
        return []
    want = list(books) if books is not None else sorted(
        DFS_LINE_ONLY_BOOKS | {BOOK_SLEEPER, BOOK_DABBLE, BOOK_UNDERDOG, BOOK_PINNACLE}
    )
    events = client.fetch_bulk_odds(
        sport_key,
        markets=DEFAULT_MARKETS.get(sport_key),
        bookmakers=",".join(want),
    )
    obs: list[dict] = []
    for event in events[:max_events]:
        for row in parse_prop_outcomes_to_index_rows(
            event, for_true_prob=False, allow_books=want, sport_id=sport_id
        ):
            obs.append({
                "player_name": row["player"],
                "sport_id": row.get("sport_id") or sport_id,
                "stat_name": row["stat"],
                "line_value": row["line"],
                "match_title": row.get("match_title") or "",
                "scheduled_at": row.get("scheduled_at") or "",
                "higher_decimal": row["over_decimal"],
                "lower_decimal": row["under_decimal"],
                "source_line_id": (
                    f"{row['book_key']}|{row['player']}|{row['stat']}|{row['line']}"
                ),
                "book_key": row["book_key"],
                "source": row["book_key"],  # snapshot source label
            })
    return obs


def propline_configured() -> bool:
    return bool(os.environ.get("PROPLINE_API_KEY", "").strip())
