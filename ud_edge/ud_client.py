"""Underdog Fantasy API client.

Hits the unauthenticated /beta/v5/over_under_lines endpoint discovered via
the aidanhall21/underdog-fantasy-pickem-scraper repo (verified alive 2026-07-18,
returns 6,718 lines across 17 sports). Parses the nested JSON shape into
typed Leg objects.

Endpoint: https://api.underdogfantasy.com/beta/v5/over_under_lines
Auth:     none required (as of mid-2026)
Returns:  ~14MB JSON with players, appearances, games, solo_games, over_under_lines

Line-value parsing note:
    UD encodes the line value inside `options[].choice_display_name_shorter`,
    NOT in a dedicated `stat_value` field. The threshold is shown as "N+"
    (you need to score ≥ N to win the over) or "N-" (you need to score ≤ N
    to win the under). The actual line is N − 0.5 for "+" and N + 0.5 for
    "−" — because "Higher 2+" means the line is 1.5 (you win by scoring ≥2).
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
from typing import Optional
import requests

from ud_edge.models import Leg, Player, Appearance, Game


UD_LINES_URL = "https://api.underdogfantasy.com/beta/v5/over_under_lines"
UD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}


def _parse_line_value(higher: dict, lower: dict) -> Optional[float]:
    """Extract the prop line from the two option's choice_display_name_shorter.

    Examples:
        higher.shorter="28+" → threshold=28, line = 28 - 0.5 = 27.5
        lower.shorter ="4-"  → threshold=4,  line = 4 + 0.5 = 4.5

    Returns None if neither option has a parseable threshold.
    """
    # Prefer the lower option's "N-" form (most reliable since lower is always shown)
    for opt, sign in [(lower, "-"), (higher, "+")]:
        short = opt.get("choice_display_name_shorter", "")
        m = re.match(r"^(\d+)([+\-])$", short.strip())
        if m:
            n = int(m.group(1))
            return float(n - 0.5) if sign == "+" else float(n + 0.5)

    # Fallback: try parsing the subheader ("Higher 27.5 Points")
    for opt in (higher, lower):
        sub = opt.get("selection_subheader", "")
        m = re.search(r"(\d+\.?\d*)", sub)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


class UDClient:
    def __init__(self, timeout: int = 30, cache_path: Optional[Path] = None):
        self.timeout = timeout
        self.cache_path = cache_path
        self.session = requests.Session()
        self.session.headers.update(UD_HEADERS)

    def fetch(self, force: bool = False) -> dict:
        """Fetch the live UD slate, with optional disk cache."""
        if self.cache_path and self.cache_path.exists() and not force:
            age = time.time() - self.cache_path.stat().st_mtime
            if age < 600:  # 10 min cache
                return json.loads(self.cache_path.read_text())

        r = self.session.get(UD_LINES_URL, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(data))
        return data

    def parse_legs(
        self,
        data: dict,
        sport_filter: Optional[set[str]] = None,
        skip_alternates: bool = True,
    ) -> list[Leg]:
        """Flatten the nested UD response into a list of typed Leg objects."""
        # Index lookup tables
        players_by_id: dict[str, Player] = {
            p["id"]: Player(
                id=p["id"],
                first_name=p.get("first_name", ""),
                last_name=p.get("last_name", ""),
                sport_id=p.get("sport_id", "UNK"),
                team_id=p.get("team_id"),
                position_id=p.get("position_id"),
            )
            for p in data.get("players", [])
        }
        apps_by_id: dict[str, Appearance] = {
            a["id"]: Appearance(
                id=a["id"],
                player_id=a["player_id"],
                match_id=a.get("match_id"),
                match_type=a.get("match_type"),
                team_id=a.get("team_id"),
            )
            for a in data.get("appearances", [])
        }
        games_by_id: dict[int, Game] = {
            g["id"]: Game(
                id=g["id"],
                abbreviated_title=g.get("abbreviated_title", ""),
                full_team_names_title=g.get("full_team_names_title"),
                matchup_text=g.get("matchup_text"),
                scheduled_at=g.get("scheduled_at"),
            )
            for g in data.get("games", [])
        }

        legs: list[Leg] = []
        skipped_alt = 0
        skipped_no_stat = 0
        skipped_filtered = 0
        skipped_no_line = 0

        for line in data.get("over_under_lines", []):
            if skip_alternates and line.get("line_type") == "alternate":
                skipped_alt += 1
                continue

            ou = line.get("over_under") or {}
            stat = ou.get("appearance_stat") or {}
            stat_name = stat.get("stat")
            appearance_id = stat.get("appearance_id")

            if not stat_name or appearance_id is None:
                skipped_no_stat += 1
                continue

            app = apps_by_id.get(appearance_id)
            if not app:
                continue
            player = players_by_id.get(app.player_id)
            if not player:
                continue

            if sport_filter and player.sport_id not in sport_filter:
                skipped_filtered += 1
                continue

            opts = line.get("options", [])
            if len(opts) < 2:
                continue

            higher = next((o for o in opts if o.get("choice") == "higher"), None)
            lower = next((o for o in opts if o.get("choice") == "lower"), None)
            if not higher or not lower:
                continue

            line_value = _parse_line_value(higher, lower)
            if line_value is None:
                skipped_no_line += 1
                continue

            game = games_by_id.get(app.match_id) if app.match_id else None

            legs.append(
                Leg(
                    line_id=line["id"],
                    appearance_id=appearance_id,
                    player_id=player.id,
                    player_name=player.full_name,
                    sport_id=player.sport_id,
                    match_id=app.match_id,
                    match_title=game.abbreviated_title if game else None,
                    scheduled_at=game.scheduled_at if game else None,
                    stat_name=stat_name,
                    line_value=line_value,
                    line_type=line.get("line_type", "balanced"),
                    higher_american=int(higher["american_price"]),
                    higher_decimal=float(higher["decimal_price"]),
                    higher_multiplier=float(higher.get("payout_multiplier", 0.0)),
                    lower_american=int(lower["american_price"]),
                    lower_decimal=float(lower["decimal_price"]),
                    lower_multiplier=float(lower.get("payout_multiplier", 0.0)),
                )
            )

        # Diagnostic print — only when no sport filter applied
        if not sport_filter:
            print(f"[ud_client] parsed {len(legs)} legs "
                  f"(skipped: {skipped_alt} alternate, {skipped_no_stat} no-stat, "
                  f"{skipped_no_line} no-line-value, {skipped_filtered} filtered)")

        return legs