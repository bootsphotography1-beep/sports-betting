"""PropLine (prop-line.com) multi-book player-props client.

PropLine exposes a the-odds-api-compatible surface with DFS pick'em books
and prediction exchanges in the same response. This is the planned primary
cross-book feed once ``PROPLINE_API_KEY`` is set.

Books we care about for ud-edge-bot
───────────────────────────────────
| key          | Role in this bot                                              |
|--------------|---------------------------------------------------------------|
| underdog     | Primary board (also fetched directly today via UD client)     |
| prizepicks   | Second-source *lines* for stale detection (synthetic +100/100)|
| sleeper      | Second-source DFS lines (when present in PropLine feed)       |
| dabble       | Second-source DFS lines (when present in PropLine feed)       |
| pinnacle     | Sharp ground-truth for same-side no-vig mispricing            |
| draftkings   | Retail sharp-ish cross-ref                                    |
| fanduel      | Retail sharp-ish cross-ref                                    |
| kalshi       | Exchange / event contracts (mostly game lines, not props)     |
| polymarket   | Prediction markets (mostly game lines / totals / spreads)     |

Important pricing caveats (from PropLine docs)
──────────────────────────────────────────────
* PrizePicks / Sleeper / Dabble often quote synthetic even-money (+100/+100).
  Those prices are **not** usable for no-vig true-prob — use them for
  **line comparison / stale detection** only.
* Underdog on PropLine carries real two-way American prices + optional
  ``payout_multiplier`` on boosts/discounts — filter non-null multipliers
  when comparing to sportsbook consensus.
* PropLine also grades settled props (``/results``) — candidate for
  replacing manual ``--settle`` once wired.

Auth: ``PROPLINE_API_KEY`` env var (query ``apiKey=`` or ``X-API-Key`` header).
Docs: https://prop-line.com/docs  ·  Base: https://api.prop-line.com/v1
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable, Optional

import requests

from ud_edge.injury_client import normalize_name as _normalize_name


BASE_URL = "https://api.prop-line.com/v1"

# Bookmaker keys as returned by PropLine
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

# Books whose American prices are trustworthy for no-vig / mispricing
SHARP_PROB_BOOKS = frozenset({
    BOOK_PINNACLE,
    BOOK_DRAFTKINGS,
    BOOK_FANDUEL,
    BOOK_BETMGM,
    BOOK_UNDERDOG,  # real two-way prices (skip boosted multipliers)
})

# DFS pick'em books — line values only (synthetic even-money pricing)
DFS_LINE_ONLY_BOOKS = frozenset({
    BOOK_PRIZEPICKS,
    BOOK_SLEEPER,
    BOOK_DABBLE,
})

# Exchange / prediction markets — useful later for game-line fair value;
# generally not player-prop no-vig sources today.
EXCHANGE_BOOKS = frozenset({
    BOOK_KALSHI,
    BOOK_POLYMARKET,
})

# UD sport_id → PropLine sport key
SPORT_MAP = {
    "NBA": "basketball_nba",
    "WNBA": "basketball_wnba",
    "NFL": "americanfootball_nfl",
    "CFB": "americanfootball_ncaaf",
    "MLB": "baseball_mlb",
    "NHL": "icehockey_nhl",
    "MMA": "mma_mixed_martial_arts",
    "UFC": "mma_mixed_martial_arts",
}

# PropLine market key → UD-ish stat_name (extend as we validate live payloads)
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
}


def _american_to_decimal(american: int | float) -> float:
    """American odds → decimal payout odds."""
    a = int(american)
    if a == 0:
        raise ValueError("american odds cannot be 0")
    if a < 0:
        return 1.0 + 100.0 / abs(a)
    return 1.0 + a / 100.0


class PropLineClient:
    """Thin HTTP client for api.prop-line.com.

    Inactive until ``PROPLINE_API_KEY`` (or constructor ``api_key``) is set.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_path: Optional[Path] = None,
        ttl_seconds: int = 120,
        timeout: int = 25,
    ):
        self.api_key = (api_key or os.environ.get("PROPLINE_API_KEY") or "").strip()
        if not self.api_key:
            raise ValueError(
                "PROPLINE_API_KEY required — set env or pass api_key= once you have a key"
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

    def _get(self, path: str, params: Optional[dict] = None, cache_key: Optional[str] = None) -> dict | list:
        params = dict(params or {})
        # Prefer header auth; also pass query for drop-in the-odds-api compatibility
        params.setdefault("apiKey", self.api_key)

        if cache_key and self.cache_path:
            cache_file = self.cache_path / f"propline_{cache_key}.json"
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < self.ttl_seconds:
                return json.loads(cache_file.read_text())

        url = f"{BASE_URL}{path}"
        r = self.session.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()

        if cache_key and self.cache_path:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            cache_file = self.cache_path / f"propline_{cache_key}.json"
            cache_file.write_text(json.dumps(data))
        return data

    def list_sports(self) -> list[dict]:
        data = self._get("/sports", cache_key="sports")
        return data if isinstance(data, list) else data.get("sports", [])

    def list_events(self, sport_key: str) -> list[dict]:
        data = self._get(f"/sports/{sport_key}/events", cache_key=f"events_{sport_key}")
        return data if isinstance(data, list) else data.get("events", [])

    def fetch_event_odds(
        self,
        sport_key: str,
        event_id: str,
        markets: Optional[str] = None,
        bookmakers: Optional[str] = None,
    ) -> dict:
        """Per-event odds including player props.

        ``markets`` / ``bookmakers`` are comma-separated PropLine keys.
        """
        params: dict = {}
        if markets:
            params["markets"] = markets
        if bookmakers:
            params["bookmakers"] = bookmakers
        return self._get(
            f"/sports/{sport_key}/events/{event_id}/odds",
            params=params,
            cache_key=f"odds_{sport_key}_{event_id}_{markets or 'all'}_{bookmakers or 'all'}",
        )


def parse_prop_outcomes_to_index_rows(
    event_payload: dict,
    *,
    allow_books: Optional[Iterable[str]] = None,
    for_true_prob: bool = True,
) -> list[dict]:
    """Flatten a PropLine event odds payload into sharp-index-friendly rows.

    Each row: player, stat, line, over_decimal, under_decimal, bookmaker, source.

    When ``for_true_prob=True`` (default):
      - only ``SHARP_PROB_BOOKS``
      - skip PrizePicks/Sleeper/Dabble synthetic pricing
      - skip Underdog outcomes with a non-null payout_multiplier (boosts/discounts)

    When ``for_true_prob=False``:
      - include DFS_LINE_ONLY_BOOKS for line/stale workflows (decimals may be 2.0/2.0)
    """
    allow = set(allow_books) if allow_books is not None else None
    rows: list[dict] = []

    for book in event_payload.get("bookmakers") or []:
        book_key = (book.get("key") or "").lower()
        if allow is not None and book_key not in allow:
            continue
        if for_true_prob and book_key not in SHARP_PROB_BOOKS:
            continue
        if not for_true_prob and book_key not in DFS_LINE_ONLY_BOOKS | SHARP_PROB_BOOKS:
            continue

        for market in book.get("markets") or []:
            market_key = market.get("key") or ""
            stat = MARKET_TO_STAT.get(market_key, market_key)
            # Group Over/Under by (player, point)
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
                except (TypeError, ValueError, KeyError):
                    continue

                # Skip DFS boost/discount specials on Underdog
                if book_key == BOOK_UNDERDOG and outcome.get("payout_multiplier") not in (None, "", 1, 1.0):
                    continue
                # PrizePicks: prefer standard market line when flavor is present
                dfs_type = outcome.get("dfs_odds_type")
                if book_key == BOOK_PRIZEPICKS and dfs_type not in (None, "", "standard"):
                    continue

                side = "over" if name in {"over", "higher"} else "under"
                try:
                    dec = _american_to_decimal(price)
                except ValueError:
                    continue

                key = (_normalize_name(player), line_val)
                slot = by_player_line.setdefault(key, {
                    "player": player,
                    "stat": stat,
                    "line": line_val,
                    "bookmaker": book.get("title") or book_key,
                    "book_key": book_key,
                })
                slot[f"{side}_decimal"] = dec
                slot[f"{side}_american"] = price

            for slot in by_player_line.values():
                if "over_decimal" not in slot or "under_decimal" not in slot:
                    continue
                # Guard: DFS synthetic even-money is useless for true-prob index
                if for_true_prob and book_key in DFS_LINE_ONLY_BOOKS:
                    continue
                if for_true_prob:
                    # Reject exact 2.0/2.0 (even money both sides) as non-informative
                    if abs(slot["over_decimal"] - 2.0) < 1e-9 and abs(slot["under_decimal"] - 2.0) < 1e-9:
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
                })
    return rows


def fetch_sharp_props(
    client: PropLineClient,
    sport_id: str,
    *,
    bookmakers: Optional[str] = None,
    max_events: int = 12,
) -> list[dict]:
    """Fetch PropLine player-prop rows for one UD sport_id (e.g. ``NBA``).

    Uses Pinnacle/DK/FD/Underdog by default for true-prob indexing.
    """
    sport_key = SPORT_MAP.get(sport_id)
    if not sport_key:
        return []
    books = bookmakers or ",".join(sorted(SHARP_PROB_BOOKS))
    events = client.list_events(sport_key)
    rows: list[dict] = []
    for event in events[:max_events]:
        eid = str(event.get("id") or "")
        if not eid:
            continue
        try:
            payload = client.fetch_event_odds(
                sport_key,
                eid,
                bookmakers=books,
            )
        except Exception as e:
            print(f"[propline] event {eid} odds failed: {e}")
            continue
        rows.extend(parse_prop_outcomes_to_index_rows(payload, for_true_prob=True))
    return rows


def propline_configured() -> bool:
    """True when a PropLine API key is present in the environment."""
    return bool(os.environ.get("PROPLINE_API_KEY", "").strip())
