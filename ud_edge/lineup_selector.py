"""Correlation-aware lineup builder: tries 6-flex first, falls back to 4-flex.

The problem: naive chunk-building can put fighting-correlation legs together
(e.g. QB pass yards + WR receptions on the same script, or pitcher K OVER
vs opposing batter contact OVER). When fighting_pairs > threshold OR avg_abs_rho
> 0.45, we drop the worst-fighting leg and retry up to 3 times.
If no clean 6-flex is found in 3 attempts, we fall back to 4-flex.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ud_edge.correlation import analyze_slip, SlipCorrelationReport
from ud_edge.matcher import build_lineups, RankedLeg


# Correlation thresholds
DEFAULT_FIGHTING_THRESHOLD = 2   # max acceptable fighting pairs before fallback
DEFAULT_AVG_RHO_THRESHOLD = 0.45  # max avg |rho| before fallback
MAX_RETRIES = 3                   # attempts to find a clean 6-flex before fallback


@dataclass
class LineupSelectorResult:
    """Result of select_lineups_for_card."""
    lineups: list[list[RankedLeg]]
    dropped_legs: list[tuple[RankedLeg, str]]  # (leg, reason)


def select_lineups_for_card(
    ranked: list[RankedLeg],
    *,
    prefer_6man: bool = True,
    max_entries: int = 4,
    correlation_threshold: Optional[int] = None,
    avg_rho_threshold: float = DEFAULT_AVG_RHO_THRESHOLD,
) -> LineupSelectorResult:
    """Build correlation-aware lineups with automatic 6-man → 4-man fallback.

    Algorithm
    ─────────
    1. Try to build `max_entries` 6-flex lineups using `build_lineups`.
    2. For each candidate lineup, call `analyze_slip` from correlation.py.
    3. If fighting_pairs > threshold OR avg_abs_rho > avg_rho_threshold:
         - Record which leg has the highest |rho| in a fighting pair
         - Drop that leg from the candidate lineup
         - Retry (up to MAX_RETRIES)
    4. If a lineup cannot be cleaned in MAX_RETRIES attempts, mark it as
       "unfixable 6-flex" and try 4-flex instead.
    5. Return up to `max_entries` disjoint lineups with the most legs cleaned.

    Args:
        ranked: pre-sorted list of RankedLeg (best edge first), from rank_legs().
        prefer_6man: if True (default), start with 6-flex and fall back to 4-flex.
            If False, skip 6-flex entirely and build only 4-flex lineups.
        max_entries: maximum number of lineups to return (default 4).
        correlation_threshold: max fighting pairs before leg-drop. Default 2.
        avg_rho_threshold: max avg |rho| before leg-drop. Default 0.45.

    Returns:
        LineupSelectorResult with .lineups (list of list[RankedLeg]) and
        .dropped_legs (list of (RankedLeg, reason) for tracking).
    """
    fighting_thresh = (
        correlation_threshold
        if correlation_threshold is not None
        else DEFAULT_FIGHTING_THRESHOLD
    )
    dropped_legs: list[tuple[RankedLeg, str]] = []

    # ── 6-flex attempt ────────────────────────────────────────────────────
    six_lineups_candidates: list[list[RankedLeg]] = []
    six_failures: list[tuple[list[RankedLeg], str]] = []

    if prefer_6man:
        raw_six = build_lineups(ranked, n_entries=max_entries, n_legs=6)
        for lineup in raw_six:
            cleaned, dropped, retry_msg = _clean_lineup(
                lineup,
                fighting_threshold=fighting_thresh,
                avg_rho_threshold=avg_rho_threshold,
            )
            dropped_legs.extend(dropped)
            if cleaned is not None:
                six_lineups_candidates.append(cleaned)
            else:
                six_failures.append((lineup, retry_msg))

    # ── Fallback to 4-flex ─────────────────────────────────────────────────
    four_lineups: list[list[RankedLeg]] = []
    # Pull fresh legs from ranked pool that aren't already used in 6-flex candidates
    used_leg_ids = _leg_ids_in(six_lineups_candidates)

    # Build 4-flex from remaining ranked pool
    remaining = [r for r in ranked if _leg_id(r) not in used_leg_ids]
    raw_four = build_lineups(remaining, n_entries=max_entries, n_legs=4)
    for lineup in raw_four:
        cleaned, dropped, _ = _clean_lineup(
            lineup,
            fighting_threshold=fighting_thresh,
            avg_rho_threshold=avg_rho_threshold,
        )
        dropped_legs.extend(dropped)
        if cleaned is not None:
            four_lineups.append(cleaned)

    # ── Merge results ──────────────────────────────────────────────────────
    # Prefer 6-flex candidates; pad to max_entries with 4-flex if needed
    result_lineups: list[list[RankedLeg]] = []
    for lu in six_lineups_candidates:
        if len(result_lineups) < max_entries:
            result_lineups.append(lu)

    for lu in four_lineups:
        if len(result_lineups) < max_entries:
            result_lineups.append(lu)

    return LineupSelectorResult(lineups=result_lineups, dropped_legs=dropped_legs)


def _clean_lineup(
    lineup: list[RankedLeg],
    *,
    fighting_threshold: int,
    avg_rho_threshold: float,
) -> tuple[Optional[list[RankedLeg]], list[tuple[RankedLeg, str]], str]:
    """Attempt to clean a single lineup by dropping fighting legs.

    Returns (cleaned_lineup or None, list of dropped legs with reasons, status message).
    """
    for attempt in range(MAX_RETRIES):
        report: SlipCorrelationReport = analyze_slip(lineup)

        fighting = report.fighting_pairs
        avg_abs = report.avg_abs_rho

        if fighting <= fighting_threshold and avg_abs <= avg_rho_threshold:
            return lineup, [], "clean"

        # Find the leg with the highest |rho| in a fighting pair to drop
        worst_leg_idx, worst_rho = _worst_fighting_leg(lineup, report)
        if worst_leg_idx is None:
            # Can't identify a specific leg to drop — give up
            return None, [], f"fighting={fighting}, avg_rho={avg_abs:.2f} — cannot identify drop target"

        dropped = lineup[worst_leg_idx]
        _reason = (
            f"dropped attempt {attempt+1}/{MAX_RETRIES}: "
            f"fighting={fighting} (limit={fighting_threshold}), "
            f"avg_rho={avg_abs:.2f} (limit={avg_rho_threshold}), "
            f"worst |rho|={abs(worst_rho):.2f} ({dropped.leg.player_name} {dropped.leg.stat_name})"
        )
        lineup = lineup[:worst_leg_idx] + lineup[worst_leg_idx + 1:]

    return None, [], f"exceeded {MAX_RETRIES} retries — fighting={fighting}, avg_rho={avg_abs:.2f}"


def _worst_fighting_leg(
    lineup: list[RankedLeg],
    report: SlipCorrelationReport,
) -> tuple[Optional[int], float]:
    """Return (leg_index, rho) of the leg most involved in fighting pairs.

    Scored by summing |rho| across all fighting pairs each leg participates in.
    Returns (None, 0.0) if no fighting pairs found.
    """
    if not report.pairs:
        return None, 0.0

    leg_scores: dict[int, float] = {i: 0.0 for i in range(len(lineup))}

    for pair in report.pairs:
        if pair.direction != "negative":
            continue
        # Both legs in a fighting pair contribute their |rho|
        leg_scores[pair.i] = leg_scores.get(pair.i, 0.0) + abs(pair.rho)
        leg_scores[pair.j] = leg_scores.get(pair.j, 0.0) + abs(pair.rho)

    if not leg_scores:
        return None, 0.0

    # Return the LEG index with the highest accumulated |rho| in fighting pairs
    worst_leg_idx = max(leg_scores, key=lambda i: leg_scores[i])
    if leg_scores[worst_leg_idx] == 0.0:
        return None, 0.0

    # Find the rho of the single worst fighting pair (for return value)
    worst_pair_idx = max(
        (p for p in range(len(report.pairs)) if report.pairs[p].direction == "negative"),
        key=lambda p: abs(report.pairs[p].rho),
        default=None,
    )
    worst_rho = report.pairs[worst_pair_idx].rho if worst_pair_idx is not None else 0.0

    return worst_leg_idx, worst_rho


def _leg_ids_in(lineups: list[list[RankedLeg]]) -> set[str]:
    return {_leg_id(r) for lu in lineups for r in lu}


def _leg_id(r: RankedLeg) -> str:
    leg = r.leg
    return (
        f"{leg.player_id}|{leg.stat_name}|{leg.line_value}|"
        f"{r.picked_side}|{leg.match_id or leg.match_title or ''}"
    )
