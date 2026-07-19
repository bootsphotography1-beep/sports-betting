"""Markdown + console output for the daily pick report."""
from __future__ import annotations
from datetime import datetime, timezone
from ud_edge.models import RankedLeg, Leg
from ud_edge.flex_math import UD_PAYOUTS, expected_value, recommend_entry
from ud_edge.matcher import get_player_status


# Per-entry recommendation thresholds: print the cheapest leg's true prob
# so Fin knows the floor of each card.
ENTRY_RECOMMENDATION = {
    0.10: "🟢 STRONG PLAY",
    0.03: "🟡 PLAY",
    0.00: "🟠 SMALL",
}


def _recommend_from_ev(ev: float) -> str:
    for threshold, label in sorted(ENTRY_RECOMMENDATION.items(), reverse=True):
        if ev >= threshold:
            return label
    return "🔴 SKIP"


SPORT_DISPLAY = {
    "NBA": "NBA", "WNBA": "WNBA", "NFL": "NFL", "CFB": "CFB",
    "MLB": "MLB", "NHL": "NHL", "PGA": "PGA", "MMA": "MMA",
    "FIFA": "FIFA", "TENNIS": "Tennis", "RACING": "Racing",
    "ESPORTS": "Esports", "CS": "CS2", "LOL": "LoL", "DOTA": "Dota 2",
    "VAL": "Valorant", "MOTORCYCLE": "Motorcycle", "CFL": "CFL",
    "BASKETBALL": "Basketball",
}


INJURY_FLAG = {
    "ACTIVE": "",
    "DAY_TO_DAY": " ⚠️ DTD",
    "QUESTIONABLE": " ⚠️ Q",
    "PROBABLE": " ℹ️ P",
    "OUT": " ❌ OUT",  # shouldn't appear — filtered out earlier
    "INJURY_RESERVE": " ❌ IR",
    "SUSPENDED": " ❌ SUSP",
    "DOUBTFUL": " ❌ DOUBT",
    "UNKNOWN": "",
}


def _fmt_side(leg: Leg, side: str) -> str:
    arrow = "↑" if side == "higher" else "↓"
    word = "Over" if side == "higher" else "Under"
    return f"{word} {leg.line_value:g} {leg.stat_name.replace('_', ' ')} {arrow}"


def _effective_prob(r: RankedLeg) -> float:
    """Best available same-side hit-rate estimate for EV.

    Prefer sharp-book same-side true prob when present (ground truth);
    otherwise fall back to UD's no-vig favorite-side prob. Never take
    max(UD, sharp) — that cherry-picks the optimistic number.
    """
    if r.sharp_true_prob is not None:
        return r.sharp_true_prob
    return r.picked_true_prob


def build_report(
    ranked: list[RankedLeg],
    entry_type: str = "6-flex",
    top_n: int = 6,
    min_true_prob: float = 0.55,
    fetched_at: datetime | None = None,
    injury_index: dict | None = None,
) -> str:
    """Build a Markdown pick report."""
    fetched_at = fetched_at or datetime.now(timezone.utc)
    entry = UD_PAYOUTS.get(entry_type)
    if entry is None:
        raise ValueError(f"unknown entry type: {entry_type}")

    # Per-leg probability from the top N
    top_legs = ranked[:top_n]
    avg_prob = (
        sum(_effective_prob(r) for r in top_legs) / len(top_legs)
        if top_legs else 0.0
    )
    ev, win_prob, median_payout = expected_value(entry, avg_prob) if top_legs else (0, 0, 0)
    rec = recommend_entry(entry, avg_prob) if top_legs else "skip"

    # Count mispricings detected
    n_mispricings = sum(1 for r in top_legs if r.sharp_true_prob is not None)
    mispricing_summary = ""
    if n_mispricings > 0:
        mispricing_summary = (f" | **{n_mispricings} mispricing{'s' if n_mispricings != 1 else ''} "
                             f"detected** (sharp-book cross-ref)")

    md = []
    md.append(f"# Underdog Edge Bot — {fetched_at.strftime('%Y-%m-%d %H:%M UTC')}")
    md.append("")
    md.append(f"_Entry: **{entry.name}** | Target true-prob: ≥{min_true_prob:.0%} per leg "
              f"| Source: Underdog Fantasy `/beta/v5/over_under_lines`_")
    md.append("")
    md.append(f"**Top {len(top_legs)} legs** "
              f"(avg true prob: **{avg_prob:.2%}**, EV per $1: **{ev:+.4f}**, "
              f"win prob: **{win_prob:.1%}**, median payout: **{median_payout:.1f}x**, "
              f"rec: **{rec}**){mispricing_summary}")
    md.append("")
    md.append("| # | Sport | Player | Match | Pick | UD True | Sharp True | Δ (pp) | Sharp Book |")
    md.append("|---|-------|--------|-------|------|---------|------------|--------|------------|")
    for i, r in enumerate(top_legs, 1):
        leg = r.leg
        sport = SPORT_DISPLAY.get(leg.sport_id, leg.sport_id)
        match = leg.match_title or "—"
        pick = _fmt_side(leg, r.picked_side)

        # Injury flag
        status = get_player_status(leg, injury_index)
        flag = INJURY_FLAG.get(status, "")
        player_display = leg.player_name + flag

        # Sharp-book display
        ud_pct = f"{r.picked_true_prob:.2%}"
        if r.sharp_true_prob is not None:
            sharp_pct = f"**{r.sharp_true_prob:.2%}**"
            delta = r.mispricing_edge_pp or 0
            delta_str = f"**{delta:+.1f}**" if abs(delta) >= 2 else f"{delta:+.1f}"
            sharp_book_str = r.sharp_book or "?"
        else:
            sharp_pct = "—"
            delta_str = "—"
            sharp_book_str = "—"

        md.append(
            f"| {i} | {sport} | {player_display} | {match} | {pick} | "
            f"{ud_pct} | {sharp_pct} | {delta_str} | {sharp_book_str} |"
        )
    md.append("")
    md.append("---")
    md.append("")
    md.append(f"_Math: no-vig calc on UD's own two-sided decimal odds. "
              f"Picks filtered to ≥{min_true_prob:.0%} true prob AND ≥0.5pp edge vs "
              f"{entry.name} break-even ({entry.break_even:.2%})._  ")
    md.append("_Δ (pp) = sharp-book true_prob − UD true_prob. **Bold** when |Δ| ≥ 2pp (mispricing signal)._  ")
    if injury_index is not None:
        total_tracked = sum(len(v) for v in injury_index.values())
        md.append(f"_Injury filter: ESPN feed, {total_tracked} players tracked, "
                  f"OUT/IR/SUSP/DOUBTFUL players excluded. ⚠️ DTD/Q = still listed, "
                  "verify before submitting._  ")
    md.append("_Disclaimer: decision-support tool, not financial advice. "
              "Track every pick; calibrate after 50+ settled legs._")

    return "\n".join(md)


def build_multi_report(
    lineups: list[list[RankedLeg]],
    entry_type: str = "6-flex",
    n_legs: int = 6,
    min_true_prob: float = 0.55,
    fetched_at: datetime | None = None,
    injury_index: dict | None = None,
) -> str:
    """Build a Markdown pick report for N disjoint lineups.

    Renders one section per entry with its own EV / win-prob / median
    summary, followed by per-leg tables. Includes a header summary table
    comparing all entries side-by-side.

    Args:
        lineups: list of N disjoint lineups (each of n_legs RankedLeg)
        entry_type: e.g. "6-flex" (must match UD_PAYOUTS)
        n_legs: legs per entry (default 6 for 6-flex)
        min_true_prob: minimum true prob threshold (for the footer)
        fetched_at: timestamp to print in the title
        injury_index: optional ESPN injury index (for ⚠️ flags)
    """
    fetched_at = fetched_at or datetime.now(timezone.utc)
    entry = UD_PAYOUTS.get(entry_type)
    if entry is None:
        raise ValueError(f"unknown entry type: {entry_type}")

    md = []
    md.append(f"# Underdog Edge Bot — {fetched_at.strftime('%Y-%m-%d %H:%M UTC')}")
    md.append("")
    md.append(f"_Entry: **{entry.name}** | **{len(lineups)} lineups × {n_legs} legs** "
              f"= **{len(lineups) * n_legs} unique legs** | "
              f"Target true-prob: ≥{min_true_prob:.0%} per leg_")
    md.append("")
    md.append("_Source: Underdog Fantasy `/beta/v5/over_under_lines`_")
    md.append("")

    # ── Header summary table: per-entry metrics side-by-side ──
    if len(lineups) > 1:
        md.append("## At-a-glance")
        md.append("")
        md.append("| Entry | Avg True% | EV/$1 | Win% | Med Payout | Rec | Mispricings |")
        md.append("|-------|-----------|-------|------|------------|-----|-------------|")
        for i, lineup in enumerate(lineups, 1):
            avg_prob = sum(_effective_prob(r) for r in lineup) / len(lineup)
            ev, win_prob, med = expected_value(entry, avg_prob)
            rec = _recommend_from_ev(ev)
            n_mis = sum(1 for r in lineup if r.sharp_true_prob is not None)
            mis_str = str(n_mis) if n_mis > 0 else "—"
            md.append(f"| **#{i}** | {avg_prob:.2%} | {ev:+.4f} | {win_prob:.1%} | "
                      f"{med:.1f}x | {rec} | {mis_str} |")
        md.append("")
        md.append("---")
        md.append("")

    # ── Per-entry sections ──
    for i, lineup in enumerate(lineups, 1):
        avg_prob = sum(_effective_prob(r) for r in lineup) / len(lineup)
        ev, win_prob, median_payout = expected_value(entry, avg_prob)
        rec = _recommend_from_ev(ev)

        # Min true prob on this card (the floor — worst leg)
        min_leg_prob = min(_effective_prob(r) for r in lineup)
        n_mis = sum(1 for r in lineup if r.sharp_true_prob is not None)

        md.append(f"## Entry #{i} — {entry.name}")
        md.append("")
        md.append(f"**Avg true prob: {avg_prob:.2%}** (floor: **{min_leg_prob:.2%}**) · "
                  f"EV: **{ev:+.4f}** per $1 · win prob: **{win_prob:.1%}** · "
                  f"median payout: **{median_payout:.1f}x** · "
                  f"**{rec}**"
                  + (f" · **{n_mis} mispricing{'s' if n_mis != 1 else ''}**" if n_mis else ""))
        md.append("")
        md.append("| # | Sport | Player | Match | Pick | UD True | Sharp True | Δ (pp) | Sharp Book |")
        md.append("|---|-------|--------|-------|------|---------|------------|--------|------------|")
        for j, r in enumerate(lineup, 1):
            leg = r.leg
            sport = SPORT_DISPLAY.get(leg.sport_id, leg.sport_id)
            match = leg.match_title or "—"
            pick = _fmt_side(leg, r.picked_side)

            status = get_player_status(leg, injury_index)
            flag = INJURY_FLAG.get(status, "")
            player_display = leg.player_name + flag

            ud_pct = f"{r.picked_true_prob:.2%}"
            if r.sharp_true_prob is not None:
                sharp_pct = f"**{r.sharp_true_prob:.2%}**"
                delta = r.mispricing_edge_pp or 0
                delta_str = f"**{delta:+.1f}**" if abs(delta) >= 2 else f"{delta:+.1f}"
                sharp_book_str = r.sharp_book or "?"
            else:
                sharp_pct = "—"
                delta_str = "—"
                sharp_book_str = "—"

            md.append(
                f"| {j} | {sport} | {player_display} | {match} | {pick} | "
                f"{ud_pct} | {sharp_pct} | {delta_str} | {sharp_book_str} |"
            )
        md.append("")
        md.append("---")
        md.append("")

    # ── Footer ──
    md.append(f"_Math: no-vig calc on UD's own two-sided decimal odds. "
              f"Picks filtered to ≥{min_true_prob:.0%} true prob AND ≥0.5pp edge vs "
              f"{entry.name} break-even ({entry.break_even:.2%})._  ")
    md.append("_Δ (pp) = sharp-book true_prob − UD true_prob. **Bold** when |Δ| ≥ 2pp (mispricing signal)._  ")
    md.append("_Lineups are disjoint (no shared legs between entries). Entry #1 has the highest-edge legs; #4 has the floor._  ")
    if injury_index is not None:
        total_tracked = sum(len(v) for v in injury_index.values())
        md.append(f"_Injury filter: ESPN feed, {total_tracked} players tracked, "
                  "OUT/IR/SUSP/DOUBTFUL players excluded. ⚠️ DTD/Q = still listed, "
                  "verify before submitting._  ")
    md.append("_Disclaimer: decision-support tool, not financial advice. "
              "Track every pick; calibrate after 50+ settled legs._")

    return "\n".join(md)


def print_console_summary(ranked: list[RankedLeg], top_n: int = 6,
                         injury_index: dict | None = None):
    """Print a one-line-per-pick summary to stdout for quick review."""
    if not ranked:
        print("\n[no +EV legs found today — try lowering min_true_prob or expanding sports]")
        return
    print(f"\nTop {min(top_n, len(ranked))} of {len(ranked)} +EV legs:")
    print(f"{'#':>2}  {'SPORT':<10}  {'PLAYER':<28}  {'PICK':<28}  "
          f"{'UD%':>6}  {'SHRP%':>6}  {'Δpp':>6}  {'BOOK':<14}")
    print("-" * 110)
    for i, r in enumerate(ranked[:top_n], 1):
        leg = r.leg
        sport = SPORT_DISPLAY.get(leg.sport_id, leg.sport_id)
        pick = _fmt_side(leg, r.picked_side)
        status = get_player_status(leg, injury_index)
        flag = INJURY_FLAG.get(status, "")
        player = (leg.player_name + flag)[:28]
        ud_pct = f"{r.picked_true_prob*100:>5.1f}"
        if r.sharp_true_prob is not None:
            sharp_pct = f"{r.sharp_true_prob*100:>5.1f}"
            delta = r.mispricing_edge_pp or 0
            delta_str = f"{delta:>+5.1f}"
            book = (r.sharp_book or "?")[:14]
        else:
            sharp_pct = "  —  "
            delta_str = "  —  "
            book = "—"
        print(f"{i:>2}  {sport:<10}  {player:<28}  {pick:<28}  "
              f"{ud_pct}  {sharp_pct}  {delta_str}  {book:<14}")