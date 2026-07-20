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


def explain_pick(r: RankedLeg, break_even: float = 0.524) -> dict:
    """Build plain-English reasoning for why this pick is on the board.

    Returns a structured explanation the dashboard can render:
      headline, summary, bullets, math
    """
    leg = r.leg
    side = _side_word(r.picked_side, "generic")
    other_side = "Under" if side == "Over" else "Over"
    stat = _stat_label(leg.stat_name)
    line = f"{leg.line_value:g}"
    ud_pct = r.picked_true_prob * 100
    edge = r.picked_edge_pp
    overround_pct = (r.overround - 1.0) * 100

    bullets: list[str] = []
    math_bits: list[str] = []

    # Core no-vig story
    bullets.append(
        f"Fantasy two-way price implies {ud_pct:.1f}% true chance on "
        f"{side} {line} {stat} after stripping {overround_pct:.1f}% book vig."
    )
    math_bits.append(
        f"no-vig: Higher {r.higher_true_prob*100:.1f}% / Lower {r.lower_true_prob*100:.1f}% "
        f"(overround {r.overround:.3f})"
    )

    # Why this side
    fav = "Higher" if r.higher_true_prob >= r.lower_true_prob else "Lower"
    if (r.picked_side == "higher" and fav == "Higher") or (
        r.picked_side == "lower" and fav == "Lower"
    ):
        bullets.append(
            f"Picked {side} because it is the favorite side after vig removal "
            f"({ud_pct:.1f}% vs {other_side} "
            f"{(r.lower_true_prob if r.picked_side == 'higher' else r.higher_true_prob)*100:.1f}%)."
        )
    else:
        bullets.append(
            f"Picked {side} because the sharp book disagreed with fantasy's favorite "
            f"and flipped the side toward the sharper price."
        )

    # Edge vs break-even
    if edge >= 0:
        bullets.append(
            f"Edge vs entry break-even ({break_even*100:.1f}%): "
            f"+{edge:.1f} percentage points — clears the play threshold."
        )
    else:
        bullets.append(
            f"Edge vs entry break-even ({break_even*100:.1f}%): "
            f"{edge:.1f}pp (kept because sharp mispricing still supports it)."
        )
    math_bits.append(f"edge = true_prob − break_even → {ud_pct/100:.4f} − {break_even:.4f} = {edge/100:.4f} ({edge:+.1f}pp)")

    # Sharp cross-ref
    if r.sharp_true_prob is not None and r.mispricing_edge_pp is not None:
        sharp_pct = r.sharp_true_prob * 100
        delta = r.mispricing_edge_pp
        book = r.sharp_book or "sharp book"
        if delta >= 2.0:
            bullets.append(
                f"Mispricing vs {book}: sharp same-side true prob is {sharp_pct:.1f}% "
                f"({delta:+.1f}pp above fantasy). Fantasy is soft on this side — "
                f"that is the main reason this pick ranks high."
            )
            headline = f"Soft fantasy line vs {book} (+{delta:.1f}pp)"
        elif delta <= -2.0:
            bullets.append(
                f"Caution vs {book}: sharp same-side true prob is only {sharp_pct:.1f}% "
                f"({delta:+.1f}pp vs fantasy). Fantasy looks richer than the sharp book."
            )
            headline = f"Fantasy richer than {book} ({delta:.1f}pp)"
        else:
            bullets.append(
                f"Sharp check ({book}): {sharp_pct:.1f}% on the same side "
                f"({delta:+.1f}pp vs fantasy) — books roughly agree."
            )
            headline = f"Fantasy + sharp agree on {side}"
        math_bits.append(
            f"mispricing = sharp_same_side − fantasy_same_side → "
            f"{sharp_pct/100:.4f} − {ud_pct/100:.4f} = {delta/100:.4f} ({delta:+.1f}pp)"
        )
    else:
        bullets.append(
            "No matching sharp-book line found for this player/stat/line — "
            "ranked from fantasy no-vig pricing alone."
        )
        headline = f"No-vig edge on {side} {line} {stat}"

    # Odds snapshot
    math_bits.append(
        f"fantasy decimals: Higher {leg.higher_decimal:.3f} / Lower {leg.lower_decimal:.3f}"
    )

    summary = (
        f"{leg.player_name}: {side} {line} {stat} at {ud_pct:.1f}% true "
        f"({edge:+.1f}pp edge)"
    )
    if r.mispricing_edge_pp is not None and r.mispricing_edge_pp >= 2.0:
        summary += (
            f"; sharp ({r.sharp_book or 'book'}) says "
            f"{(r.sharp_true_prob or 0)*100:.1f}% (+{r.mispricing_edge_pp:.1f}pp soft)."
        )

    return {
        "headline": headline,
        "summary": summary,
        "bullets": bullets,
        "math": math_bits,
        "why_shown": (
            "Shown because it cleared min true-prob and min edge filters, "
            "and ranked among the strongest available edges"
            + (
                " with a sharp-vs-fantasy mispricing boost"
                if r.mispricing_edge_pp is not None and r.mispricing_edge_pp >= 2.0
                else ""
            )
            + "."
        ),
    }


def opportunities_to_dict(r: RankedLeg, break_even: float = 0.524) -> dict:
    """Serialize a RankedLeg for the dashboard JSON API."""
    leg = r.leg
    reason = explain_pick(r, break_even=break_even)

    # Build copy dict only for platforms where the leg was actually observed
    fantasy_source = leg.fantasy_source or ""
    available_platforms: list[str] = []
    copy: dict[str, str] = {}

    if fantasy_source:
        available_platforms = [fantasy_source]
        # Only generate copy text for the observed platform
        copy[fantasy_source] = format_one_line(r, fantasy_source)
        # Always include generic as a fallback
        copy["generic"] = format_one_line(r, "generic")
    else:
        # Legacy leg with no known source: include generic only
        copy["generic"] = format_one_line(r, "generic")

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
        "higher_true_prob": round(r.higher_true_prob, 4),
        "lower_true_prob": round(r.lower_true_prob, 4),
        "overround": round(r.overround, 4),
        "sharp_true_prob": round(r.sharp_true_prob, 4) if r.sharp_true_prob is not None else None,
        "sharp_book": r.sharp_book,
        "mispricing_edge_pp": (
            round(r.mispricing_edge_pp, 2) if r.mispricing_edge_pp is not None else None
        ),
        "is_mispriced": bool(
            r.mispricing_edge_pp is not None and r.mispricing_edge_pp >= 2.0
        ),
        "fantasy_source": fantasy_source,
        "available_copy_platforms": available_platforms,
        "reason": reason,
        "copy": copy,
    }
