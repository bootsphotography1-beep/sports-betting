"""PropLine API client — primary sharp + fantasy prop feed.

PropLine is the-odds-api compatible and, crucially for Edge Board, returns
sportsbook props (Pinnacle/DK/FD/…) and DFS boards (PrizePicks/Underdog/Sleeper)
in the same payload.

Auth: set PROPLINE_API_KEY (query `apiKey=` or header `X-API-Key`).
Docs: https://api.prop-line.com/docs
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Optional

import requests

from ud_edge.sharp_books_client import canonicalize_stat, _to_decimal


# Avoid circular import issues by defining market map locally
PROPLINE_MARKET_TO_STAT: dict[str, str] = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
    "player_threes": "threes",
    "player_blocks": "blocks",
    "player_steals": "steals",
    "player_turnovers": "turnovers",
    "player_points_rebounds_assists": "pts_rebs_asts",
    "player_double_double": "double_double",
    "player_triple_double": "triple_double",
    "batter_hits": "hits",
    "batter_home_runs": "home_runs",
    "batter_total_bases": "total_bases",
    "batter_rbis": "rbis",
    "batter_runs": "runs",
    "batter_stolen_bases": "stolen_bases",
    "batter_walks": "walks",
    "pitcher_strikeouts": "strikeouts",
    "player_pass_yds": "pass_yds",
    "player_pass_tds": "pass_tds",
    "player_rush_yds": "rush_yds",
    "player_rush_tds": "rush_tds",
    "player_reception_yds": "rec_yds",
    "player_receptions": "receptions",
    "player_goals": "goals",
    "player_shots_on_goal": "shots_on_goal",
    "goalie_saves": "saves",
}

SPORT_KEYS = {
    "NBA": "basketball_nba",
    "NFL": "football_nfl",
    "MLB": "baseball_mlb",
    "NHL": "hockey_nhl",
    "WNBA": "basketball_wnba",
    "CFB": "football_ncaaf",
    "MLS": "soccer_mls",
    "EPL": "soccer_epl",
    "MMA": "mma_ufc",
    "UFC": "mma_ufc",
}

# Prefer sharpest available book first for the sharp index
SHARP_BOOK_PRIORITY = (
    "pinnacle",
    "draftkings",
    "fanduel",
    "betmgm",
    "betrivers",
    "bovada",
    "unibet",
    "williamhill_us",
)

FANTASY_BOOKS = ("prizepicks", "underdog", "sleeper")


class PropLineClient:
    """Thin REST client for api.prop-line.com (Odds-API-compatible JSON)."""

    BASE = "https://api.prop-line.com/v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_path: Optional[Path] = None,
        ttl_seconds: int = 90,
    ):
        key = api_key or os.environ.get("PROPLINE_API_KEY", "")
        if not key:
            raise ValueError("PROPLINE_API_KEY required")
        self.api_key = key
        self.cache_path = cache_path
        self.ttl_seconds = ttl_seconds
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": self.api_key})
        # Audit P1 #4: count real HTTP calls so the poller can record the
        # right number against its daily budget. Cached _get() calls do NOT
        # increment (we already paid for them earlier in the same window).
        self.calls_made: int = 0

    def _cache_file(self, cache_key: str) -> Optional[Path]:
        if not self.cache_path or not cache_key:
            return None
        return self.cache_path / f"propline_{cache_key}.json"

    def _read_cache_file(self, cache_file: Path) -> object:
        return json.loads(cache_file.read_text(encoding="utf-8"))

    def _get(self, path: str, params: Optional[dict] = None, cache_key: str = "") -> object:
        """GET with TTL cache + stale-on-failure fallback.

        Audit P1 #8: previously we only served cache when fresh (< ttl_seconds).
        On HTTP 429/5xx after TTL expiry the on-disk files were ignored, so a
        rate-limit day produced sharp_meta.count=0 even with a full sharp_cache/.
        Now: prefer fresh cache → try HTTP → on failure return stale cache if
        present (and log), else re-raise.
        """
        params = dict(params or {})
        params["apiKey"] = self.api_key
        cache_file = self._cache_file(cache_key)

        if cache_file is not None and cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < self.ttl_seconds:
                # Fresh cache hit — do NOT count as a billable call.
                return self._read_cache_file(cache_file)

        url = f"{self.BASE}{path}"
        try:
            r = self.session.get(url, params=params, timeout=25)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            # Audit P1 #8: serve stale cache rather than empty indexes.
            if cache_file is not None and cache_file.exists():
                age_m = (time.time() - cache_file.stat().st_mtime) / 60.0
                print(
                    f"[propline] HTTP failed ({e}); serving stale cache "
                    f"{cache_file.name} (age {age_m:.1f}m)"
                )
                return self._read_cache_file(cache_file)
            raise

        # Real HTTP call — increment the counter.
        self.calls_made += 1
        if cache_file is not None:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data), encoding="utf-8")
        return data

    def list_sports(self) -> list[dict]:
        data = self._get("/sports", cache_key="sports")
        return data if isinstance(data, list) else []

    def list_events(self, sport_key: str) -> list[dict]:
        data = self._get(f"/sports/{sport_key}/events", cache_key=f"events_{sport_key}")
        return data if isinstance(data, list) else []

    def event_odds(
        self,
        sport_key: str,
        event_id: str | int,
        markets: list[str],
        bookmakers: Optional[list[str]] = None,
    ) -> dict:
        params: dict = {"markets": ",".join(markets)}
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        data = self._get(
            f"/sports/{sport_key}/events/{event_id}/odds",
            params=params,
            cache_key=f"odds_{sport_key}_{event_id}_{params['markets'][:40]}",
        )
        return data if isinstance(data, dict) else {}

    def fetch_sport_props(
        self,
        sport_id: str,
        *,
        markets: Optional[list[str]] = None,
        bookmakers: Optional[list[str]] = None,
        max_events: int = 12,
    ) -> list[dict]:
        """Fetch player props for a UD-style sport id (NBA/MLB/…).

        Returns flat prop dicts:
          {player, stat, line, over_decimal, under_decimal, bookmaker, sport_id,
           source_book_type: 'sharp'|'fantasy', event, commence}
        """
        sport_key = SPORT_KEYS.get(sport_id)
        if not sport_key:
            return []
        markets = markets or _default_markets_for(sport_id)
        if not markets:
            return []

        try:
            events = self.list_events(sport_key)
        except Exception as e:
            print(f"[propline] events failed for {sport_id}: {e}")
            return []

        props: list[dict] = []
        for event in events[:max_events]:
            event_id = event.get("id")
            if event_id is None:
                continue
            try:
                detail = self.event_odds(sport_key, event_id, markets, bookmakers=bookmakers)
            except Exception as e:
                print(f"[propline] odds failed for {sport_id}/{event_id}: {e}")
                continue
            props.extend(parse_event_odds(detail, sport_id))
        return props


def _default_markets_for(sport_id: str) -> list[str]:
    if sport_id in ("NBA", "WNBA"):
        return [
            "player_points", "player_rebounds", "player_assists", "player_threes",
            "player_points_rebounds_assists", "player_steals", "player_blocks",
        ]
    if sport_id == "MLB":
        return [
            "pitcher_strikeouts", "batter_hits", "batter_home_runs",
            "batter_total_bases", "batter_rbis", "batter_runs", "batter_stolen_bases",
        ]
    if sport_id == "NHL":
        return ["player_goals", "player_shots_on_goal", "goalie_saves", "player_assists"]
    if sport_id in ("NFL", "CFB"):
        return [
            "player_pass_yds", "player_pass_tds", "player_rush_yds",
            "player_rush_tds", "player_reception_yds", "player_receptions",
        ]
    return ["player_points"]


def parse_event_odds(detail: dict, sport_id: str) -> list[dict]:
    """Parse a PropLine / Odds-API style event odds payload into flat props."""
    if not isinstance(detail, dict):
        return []
    out: list[dict] = []
    bookmakers = detail.get("bookmakers") or []

    def book_rank(b: dict) -> int:
        key = (b.get("key") or "").lower()
        if key in SHARP_BOOK_PRIORITY:
            return SHARP_BOOK_PRIORITY.index(key)
        if key in FANTASY_BOOKS:
            return 50 + FANTASY_BOOKS.index(key)
        return 99

    bookmakers_sorted = sorted(bookmakers, key=book_rank)
    # Separate sharp vs fantasy: keep best sharp per (player,stat,line);
    # keep each fantasy book independently.
    sharp_seen: set[str] = set()

    for book in bookmakers_sorted:
        book_key = (book.get("key") or book.get("title") or "propline").lower()
        is_fantasy = book_key in FANTASY_BOOKS
        book_type = "fantasy" if is_fantasy else "sharp"

        for market in book.get("markets") or []:
            mkey = market.get("key", "")
            # Skip goblin/demon DFS alternate markets unless standard
            # (PrizePicks tags dfs_odds_type on outcomes)
            stat = PROPLINE_MARKET_TO_STAT.get(mkey) or canonicalize_stat(mkey)
            if not stat or mkey in ("h2h", "spreads", "totals"):
                continue

            by_player: dict[tuple, dict] = {}
            for outcome in market.get("outcomes") or []:
                dfs_type = (outcome.get("dfs_odds_type") or "standard").lower()
                if is_fantasy and dfs_type not in ("standard", ""):
                    continue  # skip goblin/demon for comparison
                # Underdog boosts: skip non-standard multipliers when present
                mult = outcome.get("payout_multiplier")
                if is_fantasy and book_key == "underdog" and mult not in (None, 1, 1.0):
                    continue

                side_name = (outcome.get("name") or "").strip().lower()
                player = (outcome.get("description") or "").strip()
                point = outcome.get("point")
                price = outcome.get("price")
                if not player or point is None or price is None:
                    continue
                if side_name not in ("over", "under"):
                    continue

                pk = (player.lower(), float(point), stat, book_key if is_fantasy else "sharp")
                slot = by_player.setdefault(
                    pk,
                    {
                        "player": player,
                        "line": float(point),
                        "stat": stat,
                        "bookmaker": book_key,
                        "book_type": book_type,
                    },
                )
                dec = _american_or_decimal_to_decimal(price)
                if dec is None:
                    continue
                slot[f"{side_name}_decimal"] = dec
                # Also keep American for diagnostics
                slot[f"{side_name}_american"] = int(price) if isinstance(price, (int, float)) else price

            for pk, slot in by_player.items():
                if "over_decimal" not in slot or "under_decimal" not in slot:
                    continue
                dedupe = f"{pk[0]}|{pk[2]}|{pk[1]}|{slot['bookmaker']}"
                if not is_fantasy:
                    # One sharp book per player/stat/line (priority order)
                    sharp_key = f"{pk[0]}|{pk[2]}|{pk[1]}"
                    if sharp_key in sharp_seen:
                        continue
                    sharp_seen.add(sharp_key)
                else:
                    if dedupe in sharp_seen:
                        continue
                    sharp_seen.add(dedupe)

                out.append({
                    "player": slot["player"],
                    "stat": slot["stat"],
                    "line": slot["line"],
                    "over_decimal": slot["over_decimal"],
                    "under_decimal": slot["under_decimal"],
                    "bookmaker": slot["bookmaker"],
                    "sport_id": sport_id,
                    "book_type": slot["book_type"],
                    "event": f"{detail.get('away_team', '')}@{detail.get('home_team', '')}",
                    "commence": detail.get("commence_time"),
                    "source": f"propline-{slot['bookmaker']}",
                })
    return out


def _american_or_decimal_to_decimal(price) -> Optional[float]:
    """PropLine prices are American ints; Odds API may send decimal."""
    return _to_decimal(price)


# Reverse map: PropLine sport_key → UD sport id (NBA/MLB/…)
_SPORT_KEY_TO_ID: dict[str, str] = {v: k for k, v in SPORT_KEYS.items()}


def _sport_id_from_detail(detail: dict, fallback: str = "UNK") -> str:
    """Infer UD sport id from an odds payload or filename fragment."""
    key = (detail.get("sport_key") or "").strip().lower()
    if key in _SPORT_KEY_TO_ID:
        return _SPORT_KEY_TO_ID[key]
    return fallback


def _props_to_indexes(
    props: list[dict],
    *,
    sharp_index: dict[str, dict],
    fantasy_props: list[dict],
) -> None:
    """Merge flat prop dicts into sharp_index + fantasy_props (mutates both)."""
    from ud_edge.sharp_books_client import sharp_lookup_key

    for p in props:
        if not p.get("over_decimal") or not p.get("under_decimal"):
            continue
        if p.get("book_type") == "fantasy":
            fantasy_props.append(p)
            continue
        player = (p.get("player") or "").strip()
        if not player:
            continue
        # Key shape matches legacy build_propline_indexes (player|stat, no event)
        # so find_sharp_match fuzzy path keeps working unchanged.
        key = sharp_lookup_key(player, p["stat"])
        # Prefer higher-priority sharp books (already ordered in parse)
        if key in sharp_index:
            continue
        sharp_index[key] = {
            "over_decimal": p["over_decimal"],
            "under_decimal": p["under_decimal"],
            "bookmaker": p["bookmaker"],
            "line_value": p["line"],
            "player_name": player,
            "stat_name": canonicalize_stat(p["stat"]),
            "sport_id": p.get("sport_id") or "UNK",
            "source": p.get("source") or f"propline-{p.get('bookmaker', 'sharp')}",
            "event_title": p.get("event"),
            "captured_at": p.get("commence"),
        }


def load_cached_indexes(
    cache_path: Optional[Path] = None,
    sports: Optional[list[str]] = None,
) -> tuple[dict[str, dict], list[dict], dict]:
    """Rebuild sharp + fantasy indexes from on-disk PropLine cache files.

    Audit P1 #8: used when the live API is unavailable (429 / outage) or when
    no PROPLINE_API_KEY is set but `data/sharp_cache/propline_odds_*.json`
    already exists from an earlier successful pull.

    Does not require an API key. Ignores TTL — any readable odds file is used.
    """
    meta: dict = {
        "count_sharp": 0,
        "count_fantasy": 0,
        "sources": [],
        "errors": [],
        "propline_calls": 0,
        "from_cache": True,
    }
    if cache_path is None or not Path(cache_path).exists():
        meta["errors"].append("cache_path missing or empty")
        return {}, [], meta

    cache_path = Path(cache_path)
    sport_filter = set(sports) if sports else None
    sharp_index: dict[str, dict] = {}
    fantasy_props: list[dict] = []
    n_files = 0

    for path in sorted(cache_path.glob("propline_odds_*.json")):
        try:
            detail = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            meta["errors"].append(f"{path.name}: {e}")
            continue
        if not isinstance(detail, dict):
            continue
        # Filename: propline_odds_{sport_key}_{event_id}_{markets…}.json
        # sport_key itself has underscores; prefer payload sport_key.
        sport_id = _sport_id_from_detail(detail)
        if sport_id == "UNK":
            # Best-effort parse from filename prefix after 'propline_odds_'
            rest = path.name[len("propline_odds_") :]
            for key, sid in _SPORT_KEY_TO_ID.items():
                if rest.startswith(key + "_"):
                    sport_id = sid
                    break
        if sport_filter is not None and sport_id not in sport_filter:
            continue
        props = parse_event_odds(detail, sport_id)
        _props_to_indexes(props, sharp_index=sharp_index, fantasy_props=fantasy_props)
        n_files += 1

    meta["count_sharp"] = len(sharp_index)
    meta["count_fantasy"] = len(fantasy_props)
    meta["sources"] = sorted({
        *(v.get("source", "propline-cache") for v in sharp_index.values()),
        *(p.get("source", "propline-cache") for p in fantasy_props),
    })
    meta["cache_files_loaded"] = n_files
    if n_files == 0:
        meta["errors"].append("no propline_odds_*.json files loaded")
    return sharp_index, fantasy_props, meta


def build_propline_indexes(
    api_key: Optional[str] = None,
    sports: Optional[list[str]] = None,
    cache_path: Optional[Path] = None,
) -> tuple[dict[str, dict], list[dict], dict]:
    """Pull PropLine and split into sharp index + fantasy prop observations.

    Returns:
      sharp_index: {norm_player|stat: sharp odds dict}
      fantasy_props: list of fantasy-book prop dicts (prizepicks/underdog/sleeper)
      meta: counts / sources / errors

    Audit P1 #8: when the live pull yields an empty sharp index (rate-limit /
    outage) but cache_path has odds files, fall back to load_cached_indexes.
    """
    meta: dict = {"count_sharp": 0, "count_fantasy": 0, "sources": [], "errors": []}
    sports = sports or ["NBA", "NFL", "MLB", "NHL", "WNBA", "CFB"]
    sharp_index: dict[str, dict] = {}
    fantasy_props: list[dict] = []

    try:
        client = PropLineClient(api_key=api_key, cache_path=cache_path)
    except Exception as e:
        meta["errors"].append(str(e))
        # No client (missing key) — still try disk cache.
        if cache_path:
            return load_cached_indexes(cache_path=cache_path, sports=sports)
        return {}, [], meta

    # Audit P1 #4: snapshot the counter so we can report how many real HTTP
    # calls this cycle made. The poller uses this to advance its budget by
    # the actual amount instead of a fixed 1.
    start_calls = client.calls_made

    books = list(SHARP_BOOK_PRIORITY[:5]) + list(FANTASY_BOOKS)
    for sport in sports:
        try:
            props = client.fetch_sport_props(sport, bookmakers=books)
        except Exception as e:
            meta["errors"].append(f"{sport}: {e}")
            continue
        _props_to_indexes(props, sharp_index=sharp_index, fantasy_props=fantasy_props)

    meta["count_sharp"] = len(sharp_index)
    meta["count_fantasy"] = len(fantasy_props)
    meta["sources"] = sorted({
        *(v.get("source", "propline") for v in sharp_index.values()),
        *(p.get("source", "propline") for p in fantasy_props),
    })
    # How many billable HTTP calls happened during this build. The poller
    # reads this and calls budget.record(meta["propline_calls"]) accordingly.
    meta["propline_calls"] = client.calls_made - start_calls

    # Audit P1 #8: empty live result + cache on disk → load stale indexes.
    if not sharp_index and cache_path:
        cached_sharp, cached_fantasy, cached_meta = load_cached_indexes(
            cache_path=cache_path, sports=sports
        )
        if cached_sharp or cached_fantasy:
            meta["errors"].append(
                "live PropLine returned no sharp props; served sharp_cache fallback"
            )
            meta["from_cache"] = True
            meta["cache_files_loaded"] = cached_meta.get("cache_files_loaded", 0)
            meta["errors"].extend(cached_meta.get("errors") or [])
            return cached_sharp, cached_fantasy, meta

    return sharp_index, fantasy_props, meta


def fantasy_props_to_legs(fantasy_props: list[dict]):
    """Convert PropLine fantasy props into Leg objects for ranking.

    Skips malformed rows (missing player / line) so alerts never fire as
    player='Unknown' line=0.0 from parser gaps.
    """
    from ud_edge.models import Leg

    legs = []
    for i, p in enumerate(fantasy_props):
        player = (p.get("player") or "").strip()
        if not player:
            continue
        if p.get("line") is None:
            continue
        try:
            line_value = float(p["line"])
        except (TypeError, ValueError):
            continue
        higher = float(p.get("over_decimal") or 0)
        lower = float(p.get("under_decimal") or 0)
        if higher <= 1.0:
            higher = 2.0  # PrizePicks synthetic even money ≈ +100
        if lower <= 1.0:
            lower = 2.0
        source = (p.get("bookmaker") or "prizepicks").lower()
        legs.append(
            Leg(
                line_id=f"propline-{source}-{i}",
                player_id=f"propline-{source}-p{i}",
                player_name=player,
                sport_id=(p.get("sport_id") or "UNK").upper(),
                match_title=p.get("event"),
                scheduled_at=p.get("commence"),
                stat_name=canonicalize_stat(p.get("stat") or "points"),
                line_value=line_value,
                line_type="balanced",
                higher_american=-110,
                higher_decimal=higher,
                higher_multiplier=0.9,
                lower_american=-110,
                lower_decimal=lower,
                lower_multiplier=0.9,
                fantasy_source=source,
            )
        )
    return legs
