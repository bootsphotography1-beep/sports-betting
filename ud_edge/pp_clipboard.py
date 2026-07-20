"""PrizePicks clipboard-to-CSV adapter for the second-source snapshot pipeline.

This module provides:
- parse_prizepicks_csv(): parse a CSV board into observation dicts using the
  canonical column vocabulary (player_name, league, stat_type, line,
  higher_decimal, lower_decimal, event_title, scheduled_at).
- read_clipboard(): read the Windows clipboard (for --ingest-prizepicks-clipboard).

The clipboard parser is copied verbatim from the sibling
prizepicks-edge-bot/scripts/clipboard_to_csv.py for portability.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

__all__ = ["parse_prizepicks_csv", "read_clipboard"]


# ─── CSV parser ───────────────────────────────────────────────────────────────

def parse_prizepicks_csv(
    csv_path: str | Path,
    source_name: str = "prizepicks",
    strict: bool = False,
) -> list[dict] | tuple[list[dict], dict]:
    """Parse a PrizePicks-board CSV into observation dicts.

    Canonical column vocabulary (in order):
        player_name, league, stat_type, line,
        higher_decimal, lower_decimal, event_title, scheduled_at

    Extra columns are ignored.  Rows missing player_name, stat_type, or line
    are skipped and counted in the diagnostics dict.

    When strict=False (default): returns (observations, diagnostics_dict) tuple.
    diagnostics_dict has keys: parsed, skipped_invalid, skipped_missing_critical.

    When strict=True: returns just the observations list (legacy behavior).
    """
    path = Path(csv_path)
    if not path.exists():
        return ([], {"parsed": 0, "skipped_invalid": 0, "skipped_missing_critical": 0}) if not strict else []

    observations = []
    parsed = skipped_invalid = skipped_missing_critical = 0

    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                player_name = row.get("player_name", "").strip()
                stat_type = row.get("stat_type", "").strip()
                line_raw = row.get("line", "").strip()

                # Track missing critical fields
                if not player_name or not stat_type or not line_raw:
                    skipped_missing_critical += 1
                    continue

                # Parse line value
                line_match = re.search(r"(\d+(?:\.\d+)?)", line_raw)
                if not line_match:
                    skipped_invalid += 1
                    continue
                try:
                    line_value = float(line_match.group(1))
                except ValueError:
                    skipped_invalid += 1
                    continue

                # Parse optional decimal odds
                higher_raw = row.get("higher_decimal", "").strip()
                lower_raw = row.get("lower_decimal", "").strip()
                try:
                    higher_decimal = float(higher_raw) if higher_raw else 0.0
                    lower_decimal = float(lower_raw) if lower_raw else 0.0
                except ValueError:
                    higher_decimal = 0.0
                    lower_decimal = 0.0

                # Normalize stat name to the internal vocabulary
                stat_name = _normalize_stat(stat_type)

                # League → sport_id
                league = row.get("league", "").strip().upper()
                sport_id = _league_to_sport(league)

                # Event title
                event_title = row.get("event_title", "").strip()

                # Scheduled time
                scheduled_at = row.get("scheduled_at", "").strip()

                observations.append({
                    "player_name": player_name,
                    "sport_id": sport_id,
                    "stat_name": stat_name,
                    "line_value": line_value,
                    "match_title": event_title,
                    "scheduled_at": scheduled_at,
                    "higher_decimal": higher_decimal,
                    "lower_decimal": lower_decimal,
                    "source_line_id": "",
                    # Per-row source from CSV (if present), else the default source_name
                    "source": row.get("source", "").strip() or source_name,
                })
                parsed += 1
    except Exception:
        pass

    diagnostics = {
        "parsed": parsed,
        "skipped_invalid": skipped_invalid,
        "skipped_missing_critical": skipped_missing_critical,
    }

    if strict:
        return observations
    return observations, diagnostics


# ─── Internal helpers ────────────────────────────────────────────────────────

_STAT_SYNONYMS = {
    "pts": "points",
    "pt": "points",
    "reb": "rebounds",
    "rebs": "rebounds",
    "ast": "assists",
    "a": "assists",
    "to": "turnovers",
    "tos": "turnovers",
    "stl": "steals",
    "blk": "blocks",
    "3pt": "threes",
    "3pts": "threes",
    "three": "threes",
    "hr": "home runs",
    "runs": "runs",
    "rbi": "runs_batted_in",
    "hits": "hits",
    "h": "hits",
    "ks": "strikeouts",
    "so": "strikeouts",
    "walks": "walks",
    "bb": "walks",
    # PrizePicks-specific display names
    "points": "points",
    "rebounds": "rebounds",
    "assists": "assists",
    "threes": "threes",
    "steals": "steals",
    "blocks": "blocks",
    "turnovers": "turnovers",
    "home runs": "home runs",
    "runs batted in": "runs_batted_in",
    "strikeouts": "strikeouts",
    "shots": "shots",
    "goals": "goals",
    "saves": "saves",
}


def _normalize_stat(stat: str) -> str:
    """Collapse common stat aliases to a canonical form."""
    s = stat.strip().lower()
    return _STAT_SYNONYMS.get(s, s)


def _league_to_sport(league: str) -> str:
    """Map a league code to a sport_id."""
    mapping = {
        "NBA": "NBA",
        "WNBA": "WNBA",
        "NFL": "NFL",
        "NCAAF": "NCAAF",
        "CFB": "NCAAF",
        "MLB": "MLB",
        "NHL": "NHL",
        "NCAAB": "NCAAB",
        "PGA": "PGA",
        "MMA": "MMA",
        "SOC": "SOCCER",
        "SOCCER": "SOCCER",
    }
    return mapping.get(league.upper(), league.upper())


# ─── Clipboard reader ─────────────────────────────────────────────────────────

def read_clipboard() -> str:
    """Read text from the Windows clipboard via tkinter. Returns '' on failure."""
    try:
        import tkinter as tk
    except ImportError:
        return ""
    try:
        root = tk.Tk()
        root.withdraw()
        root.update()
        plain = root.clipboard_get()
    except Exception:
        return ""
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return plain
