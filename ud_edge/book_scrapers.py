"""First-party sportsbook scrapers (no third-party odds API keys).

Owns the pipeline by hitting the same public JSON endpoints the books'
web apps use. Verified 2026-07-19 from this environment:

  ✅ Underdog Fantasy — already in ud_client.py
  ✅ DraftKings Sportsbook — sportsbook-nash OData markets (Chrome TLS)
  ✅ FanDuel Sportsbook — sbapi.nj event-page JSON
  ✅ Bovada — coupon JSON (game lines; limited player props)
  ❌ PrizePicks — DataDome 403 (not bypassed)
  ❌ Pinnacle guest API — 403 / geo-blocked from this host

DraftKings batter props are mostly *milestones* (1+, 2+, …) rather than
two-sided O/U. We map "N+" → Over (N − 0.5) and synthesize a complementary
Under so the existing no-vig matcher can compare same-side probs. The yes
price's implied probability is treated as the probability estimate
(overround forced to 1.0).
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Optional

import requests

from ud_edge.injury_client import normalize_name as _normalize_name
from ud_edge.sharp_books_client import _to_decimal


def _american_to_decimal(american: int | float | str) -> Optional[float]:
    return _to_decimal(american)


def _synthetic_under_decimal(over_decimal: float) -> float:
    """Complement Under so overround == 1.0 and true_over == implied(over)."""
    implied = 1.0 / over_decimal
    implied = min(max(implied, 0.01), 0.99)
    return 1.0 / (1.0 - implied)


def milestone_label_to_line(label: str) -> Optional[float]:
    """Map DK/FD milestone label 'N+' → Over line (N − 0.5)."""
    m = re.match(r"^(\d+)\+$", (label or "").strip().lower())
    if not m:
        return None
    return int(m.group(1)) - 0.5


# ── DraftKings ──────────────────────────────────────────────────────────────
class DraftKingsScraper:
    """DraftKings sportsbook player-prop scraper via sportsbook-nash OData."""

    SITE = "US-OH-SB"  # geo-detected site name from this host; override if needed
    LEAGUE_IDS = {
        "MLB": "84240",
        "WNBA": "94682",
        "NBA": "42648",
        "NFL": "88808",
    }
    # MLB batter/pitcher milestone + O/U subcategories (from DK page JSON)
    MLB_SUBCATEGORIES = {
        "17320": ("hits", "Hits Milestones"),
        "17321": ("total_bases", "Total Bases Milestones"),
        "17319": ("home_runs", "Home Runs Milestones"),
        "17322": ("rbis", "RBIs Milestones"),
        "17323": ("strikeouts", "Strikeouts Thrown Milestones"),
        "17843": ("hits_runs_rbis", "Hits + Runs + RBIs Milestones"),
        "17413": ("outs", "Outs O/U"),  # true two-sided
    }

    def __init__(self, cache_path: Optional[Path] = None, ttl_seconds: int = 600):
        self.cache_path = cache_path
        self.ttl_seconds = ttl_seconds
        try:
            from curl_cffi import requests as crequests
            self._session = crequests.Session(impersonate="chrome120")
            self._engine = "curl_cffi"
        except ImportError:
            self._session = requests.Session()
            self._engine = "requests"
        self._session.headers.update({
            "Accept": "application/json",
            "Referer": "https://sportsbook.draftkings.com/",
            "Origin": "https://sportsbook.draftkings.com",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        })

    def _get(self, url: str, cache_key: str) -> dict:
        if self.cache_path:
            cache_file = self.cache_path / f"dk_{cache_key}.json"
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < self.ttl_seconds:
                return json.loads(cache_file.read_text())
        r = self._session.get(url, timeout=40)
        r.raise_for_status()
        data = r.json()
        if self.cache_path:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            (self.cache_path / f"dk_{cache_key}.json").write_text(json.dumps(data))
        return data

    def _markets_url(self, league_id: str, subcat: str) -> str:
        path = (
            f"https://sportsbook-nash.draftkings.com/sites/{self.SITE}"
            f"/api/sportscontent/controldata/league/leagueSubcategory/v1/markets"
        )
        params = {
            "eventsQuery": (
                f"$filter=leagueId eq '{league_id}' AND "
                f"clientMetadata/Subcategories/any(s: s/Id eq '{subcat}') AND "
                f"status eq 'NotStarted'"
            ),
            "marketsQuery": (
                f"$filter=clientMetadata/subCategoryId eq '{subcat}' AND "
                f"tags/all(t: t ne 'SportcastBetBuilder')"
            ),
            "include": "Events",
            "entity": "markets",
            "isBatchable": "false",
        }
        return path + "?" + urllib.parse.urlencode(params)

    def fetch_mlb_props(self) -> list[dict]:
        """Return props: {player, stat, line, over_decimal, under_decimal, bookmaker}."""
        league = self.LEAGUE_IDS["MLB"]
        out: list[dict] = []
        for subcat, (stat, _label) in self.MLB_SUBCATEGORIES.items():
            try:
                data = self._get(self._markets_url(league, subcat), f"mlb_{subcat}")
            except Exception as e:
                print(f"[dk] subcategory {subcat} failed: {e}")
                continue
            markets = {m["id"]: m for m in data.get("markets") or []}
            by_market: dict[str, list] = defaultdict(list)
            for sel in data.get("selections") or []:
                by_market[sel.get("marketId")].append(sel)

            for mid, sels in by_market.items():
                market = markets.get(mid) or {}
                player = (market.get("name") or "").strip()
                # "James Wood Home Runs" → player before trailing stat words
                player = re.sub(
                    r"\s+(Home Runs|Hits|Total Bases|RBIs|Strikeouts Thrown|"
                    r"Hits \+ Runs \+ RBIs|Outs)\s*$",
                    "",
                    player,
                    flags=re.I,
                ).strip()
                if not player:
                    continue

                labels = {((s.get("label") or "").strip().lower()): s for s in sels}

                # True two-sided O/U
                if "over" in labels and "under" in labels:
                    over_dec = _american_to_decimal(
                        (labels["over"].get("displayOdds") or {}).get("american")
                        or (labels["over"].get("displayOdds") or {}).get("decimal")
                    )
                    under_dec = _american_to_decimal(
                        (labels["under"].get("displayOdds") or {}).get("american")
                        or (labels["under"].get("displayOdds") or {}).get("decimal")
                    )
                    pts = labels["over"].get("points")
                    if over_dec and under_dec and pts is not None:
                        out.append({
                            "player": player,
                            "stat": stat,
                            "line": float(pts),
                            "over_decimal": over_dec,
                            "under_decimal": under_dec,
                            "bookmaker": "draftkings",
                            "source": "scraper-dk",
                        })
                    continue

                # Milestone "N+" → Over (N-0.5)
                for lab, sel in labels.items():
                    line = milestone_label_to_line(lab)
                    if line is None:
                        continue
                    am = (sel.get("displayOdds") or {}).get("american")
                    over_dec = _american_to_decimal(am)
                    if not over_dec:
                        continue
                    out.append({
                        "player": player,
                        "stat": stat,
                        "line": float(line),
                        "over_decimal": over_dec,
                        "under_decimal": _synthetic_under_decimal(over_dec),
                        "bookmaker": "draftkings",
                        "source": "scraper-dk-milestone",
                    })
        return out


# ── FanDuel ─────────────────────────────────────────────────────────────────
class FanDuelScraper:
    """FanDuel sportsbook scraper via sbapi.nj public JSON."""

    AK = "FhMFpcPWXMeyZxOx"  # public web app key embedded in FD frontend
    BASE = "https://sbapi.nj.sportsbook.fanduel.com/api"

    # Map FD market types → (ud_stat, line_for_milestone)
    MILESTONE_MAP = {
        "PLAYER_TO_RECORD_2+_HITS": ("hits", 1.5),
        "PLAYER_TO_RECORD_3+_HITS": ("hits", 2.5),
        "TO_RECORD_2+_TOTAL_BASES": ("total_bases", 1.5),
        "TO_RECORD_3+_TOTAL_BASES": ("total_bases", 2.5),
        "TO_RECORD_AN_RBI": ("rbis", 0.5),
        "TO_RECORD_2+_RBIS": ("rbis", 1.5),
        "TO_HIT_A_HOME_RUN": ("home_runs", 0.5),
        "PLAYER_TO_RECORD_A_HIT": ("hits", 0.5),
    }

    def __init__(self, cache_path: Optional[Path] = None, ttl_seconds: int = 600):
        self.cache_path = cache_path
        self.ttl_seconds = ttl_seconds
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Referer": "https://sportsbook.fanduel.com/",
        })

    def _get(self, path: str, params: dict, cache_key: str) -> dict:
        if self.cache_path:
            cache_file = self.cache_path / f"fd_{cache_key}.json"
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < self.ttl_seconds:
                return json.loads(cache_file.read_text())
        params = {**params, "_ak": self.AK}
        r = self.session.get(f"{self.BASE}{path}", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if self.cache_path:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            (self.cache_path / f"fd_{cache_key}.json").write_text(json.dumps(data))
        return data

    def fetch_mlb_props(self, max_events: int = 12) -> list[dict]:
        """Pull milestone-style MLB props from upcoming event pages."""
        page = self._get(
            "/content-managed-page",
            {"page": "SPORT", "eventTypeId": "7511", "timezone": "America/New_York"},
            "mlb_sport_page",
        )
        events = (page.get("attachments") or {}).get("events") or {}
        game_ids = []
        for eid, ev in events.items():
            name = ev.get("name") or ""
            if " @ " in name:
                game_ids.append(str(ev.get("eventId") or eid))
        game_ids = game_ids[:max_events]

        out: list[dict] = []
        for eid in game_ids:
            try:
                data = self._get(
                    "/event-page",
                    {"eventId": eid, "tab": "popular"},
                    f"mlb_event_{eid}",
                )
            except Exception as e:
                print(f"[fd] event {eid} failed: {e}")
                continue
            markets = (data.get("attachments") or {}).get("markets") or {}
            for market in markets.values():
                mtype = market.get("marketType") or ""
                mapping = self.MILESTONE_MAP.get(mtype)
                if not mapping:
                    continue
                stat, line = mapping
                for runner in market.get("runners") or []:
                    player = (runner.get("runnerName") or "").strip()
                    if not player:
                        continue
                    odds = runner.get("winRunnerOdds") or {}
                    american = (odds.get("americanDisplayOdds") or {}).get("americanOddsInt")
                    if american is None:
                        american = (odds.get("americanDisplayOdds") or {}).get("americanOdds")
                    over_dec = _american_to_decimal(american)
                    if not over_dec:
                        continue
                    out.append({
                        "player": player,
                        "stat": stat,
                        "line": float(line),
                        "over_decimal": over_dec,
                        "under_decimal": _synthetic_under_decimal(over_dec),
                        "bookmaker": "fanduel",
                        "source": "scraper-fd-milestone",
                    })
        return out


# ── Bovada (game lines — limited prop coverage) ─────────────────────────────
class BovadaScraper:
    """Bovada coupon API — game lines only from this host (no player props)."""

    BASE = "https://www.bovada.lv/services/sports/event/coupon/events/A/description"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })

    def fetch_mlb_game_lines(self) -> list[dict]:
        """Return moneyline/total style rows (not player props). Empty for matcher."""
        # Intentionally returns [] for player-prop index — kept for future expansion.
        r = self.session.get(
            f"{self.BASE}/baseball/mlb",
            params={"marketFilterId": "def", "preMatchOnly": "true", "lang": "en"},
            timeout=30,
        )
        r.raise_for_status()
        return []  # no player props in coupon feed


def fetch_owned_sharp_props(
    sports: Optional[list[str]] = None,
    cache_path: Optional[Path] = None,
) -> list[dict]:
    """Fetch player props from owned scrapers (DK + FanDuel).

    Returns flat list compatible with build_sharp_index PropLine entries.
    """
    sports = sports or ["MLB"]
    props: list[dict] = []

    if "MLB" in sports:
        dk = DraftKingsScraper(cache_path=cache_path)
        try:
            dk_props = dk.fetch_mlb_props()
            print(f"[scraper] DraftKings MLB props: {len(dk_props)} ({dk._engine})")
            props.extend(dk_props)
        except Exception as e:
            print(f"[scraper] DraftKings failed: {e}")

        fd = FanDuelScraper(cache_path=cache_path)
        try:
            fd_props = fd.fetch_mlb_props()
            print(f"[scraper] FanDuel MLB props: {len(fd_props)}")
            props.extend(fd_props)
        except Exception as e:
            print(f"[scraper] FanDuel failed: {e}")

    return props
