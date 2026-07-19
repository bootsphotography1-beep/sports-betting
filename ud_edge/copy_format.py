"""Platform-specific copy/paste formatters for fantasy apps.

Builds plain-text lines you can paste into PrizePicks, Sleeper, Underdog,
or a generic notepad. Each formatter returns human-readable text shaped
for that app's search/entry UX (player + side + line + stat).
"""
from __future__ import annotations
from typing import Iterable, Optional

from ud_edge.models import RankedLeg


# Display labels for stat types across platforms
_STAT_LABELS = {
    "points": "Points",
    "rebounds": "Rebounds",
    "assists": "Assists",
    "threes": "3-Pointers Made",
    "steals": "Steals",
    "blocks": "Blocks",
    "turnovers": "Turnovers",
    "fantasy_points": "Fantasy Points",
    "pts_rebs_asts": "Pts+Rebs+Asts",
    "double_double": "Double Double",
    "triple_double": "Triple Double",
    "hits": "Hits",
    "runs": "Runs",
    "rbis": "RBIs",
    "home_runs": "Home Runs",
    "stolen_bases": "Stolen Bases",
    "walks": "Walks",
    "total_bases": "Total Bases",
    "hits_runs_rbis": "Hits+Runs+RBIs",
    "strikeouts": "Strikeouts",
    "pass_yds": "Pass Yards",
    "pass_tds": "Pass TDs",
    "rush_yds": "Rush Yards",
    "rush_tds": "Rush TDs",
    "rec_yds": "Receiving Yards",
    "rec_tds": "Receiving TDs",
    "receptions": "Receptions",
    "passing_yds": "Pass Yards",
    "rushing_yds": "Rush Yards",
    "receiving_yds": "Receiving Yards",
    "goals": "Goals",
    "shots_on_goal": "Shots on Goal",
    "saves": "Saves",
}


def _stat_label(stat_name: str) -> str:
    if stat_name in _STAT_LABELS:
        return _STAT_LABELS[stat_name]
    return stat_name.replace("_", " ").title()


def _side_word(side: str, platform: str = "generic") -> str:
    side = (side or "").lower()
    if platform == "prizepicks":
        return "More" if side == "higher" else "Less"
    if platform == "sleeper":
        return "Over" if side == "higher" else "Under"
    if platform == "underdog":
        return "Higher" if side == "higher" else "Lower"
    return "Over" if side == "higher" else "Under"


def format_one_line(r: RankedLeg, platform: str = "generic") -> str:
    """Format a single ranked leg for a fantasy platform."""
    leg = r.leg
    side = _side_word(r.picked_side, platform)
    stat = _stat_label(leg.stat_name)
    line = f"{leg.line_value:g}"
    sport = leg.sport_id or "UNK"
    match = leg.match_title or ""

    if platform == "prizepicks":
        # PrizePicks search: "LeBron James More 27.5 Points"
        return f"{leg.player_name} · {side} {line} {stat}"
    if platform == "sleeper":
        return f"{leg.player_name} {side} {line} {stat}"
    if platform == "underdog":
        return f"{leg.player_name} · {side} {line} {stat}"
    # Generic clipboard block with sport context
    match_bit = f" ({match})" if match else ""
    return f"[{sport}] {leg.player_name}{match_bit} · {side} {line} {stat}"


def format_block(
    ranked: Iterable[RankedLeg],
    platform: str = "generic",
    *,
    include_header: bool = True,
    sport: Optional[str] = None,
) -> str:
    """Format many legs as a pasteable block for one platform."""
    legs = list(ranked)
    if sport:
        legs = [r for r in legs if (r.leg.sport_id or "").upper() == sport.upper()]

    lines: list[str] = []
    if include_header:
        label = {
            "prizepicks": "PrizePicks",
            "sleeper": "Sleeper",
            "underdog": "Underdog",
        }.get(platform, "Picks")
        sport_bit = f" — {sport}" if sport else ""
        lines.append(f"{label} lines{sport_bit}")
        lines.append("-" * 40)

    for i, r in enumerate(legs, 1):
        lines.append(f"{i}. {format_one_line(r, platform)}")

    if not legs:
        lines.append("(no picks)")
    return "\n".join(lines)


def format_entry_card(
    lineup: list[RankedLeg],
    platform: str = "underdog",
    entry_label: str = "Entry",
) -> str:
    """Format a full entry/lineup as a pasteable card."""
    lines = [f"{entry_label} ({len(lineup)} legs) — {platform.title()}", "-" * 40]
    for i, r in enumerate(lineup, 1):
        lines.append(f"{i}. {format_one_line(r, platform)}")
    return "\n".join(lines)


def opportunities_to_dict(r: RankedLeg) -> dict:
    """Serialize a RankedLeg for the dashboard JSON API."""
    leg = r.leg
    return {
        "player_name": leg.player_name,
        "sport_id": leg.sport_id or "UNK",
        "match_title": leg.match_title,
        "scheduled_at": leg.scheduled_at,
        "stat_name": leg.stat_name,
        "stat_label": _stat_label(leg.stat_name),
        "line_value": leg.line_value,
        "picked_side": r.picked_side,
        "side_label": _side_word(r.picked_side, "generic"),
        "side_prizepicks": _side_word(r.picked_side, "prizepicks"),
        "side_sleeper": _side_word(r.picked_side, "sleeper"),
        "side_underdog": _side_word(r.picked_side, "underdog"),
        "ud_true_prob": round(r.picked_true_prob, 4),
        "ud_edge_pp": round(r.picked_edge_pp, 2),
        "sharp_true_prob": round(r.sharp_true_prob, 4) if r.sharp_true_prob is not None else None,
        "sharp_book": r.sharp_book,
        "mispricing_edge_pp": (
            round(r.mispricing_edge_pp, 2) if r.mispricing_edge_pp is not None else None
        ),
        "is_mispriced": bool(
            r.mispricing_edge_pp is not None and r.mispricing_edge_pp >= 2.0
        ),
        "copy": {
            "prizepicks": format_one_line(r, "prizepicks"),
            "sleeper": format_one_line(r, "sleeper"),
            "underdog": format_one_line(r, "underdog"),
            "generic": format_one_line(r, "generic"),
        },
    }
