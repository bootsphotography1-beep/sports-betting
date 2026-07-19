"""Underdog Fantasy entry-type payout tables + per-leg break-even.

Source: Underdog Fantasy official payout tables (2026-07 verified).
All multipliers are PAYOUT multipliers — they multiply the ENTRY STAKE,
not the per-leg contribution. E.g. 6/6 = 25x means a $5 entry returns
$125 on a perfect card.
"""
from __future__ import annotations
import math
from typing import Optional
from ud_edge.models import FlexEntryType


def _solve_break_even(payouts: dict[int, float], n_legs: int) -> float:
    """Binary-search the per-leg hit rate where EV == 0 for the payout table."""
    lo, hi = 0.01, 0.99
    for _ in range(80):
        mid = (lo + hi) / 2.0
        ev = -1.0
        for k in range(0, n_legs + 1):
            binom = math.comb(n_legs, k)
            ev += binom * (mid ** k) * ((1.0 - mid) ** (n_legs - k)) * payouts.get(k, 0.0)
        if ev < 0:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2.0, 4)


# Underdog Fantasy payout tables (verified July 2026).
# break_even values are the mathematically exact per-leg hit rates where EV=0
# under an i.i.d. binomial model (not padded "safety" thresholds).
UD_PAYOUTS: dict[str, FlexEntryType] = {
    # ── Power plays (all legs must hit) ──
    "2-man-power": FlexEntryType(
        name="2-man-power", n_legs=2,
        payouts={2: 3.0},
        break_even=_solve_break_even({2: 3.0}, 2),  # ≈ 0.5774
    ),
    "3-man-power": FlexEntryType(
        name="3-man-power", n_legs=3,
        payouts={3: 6.0},
        break_even=_solve_break_even({3: 6.0}, 3),  # ≈ 0.5503
    ),
    "4-man-power": FlexEntryType(
        name="4-man-power", n_legs=4,
        payouts={4: 10.0},
        break_even=_solve_break_even({4: 10.0}, 4),  # ≈ 0.5623
    ),

    # ── Flex plays (tiered payouts) ──
    "3-flex": FlexEntryType(
        name="3-flex", n_legs=3,
        payouts={3: 6.0, 2: 1.0},
        break_even=_solve_break_even({3: 6.0, 2: 1.0}, 3),  # ≈ 0.4753
    ),
    "4-flex": FlexEntryType(
        name="4-flex", n_legs=4,
        payouts={4: 6.0, 3: 1.5},
        break_even=_solve_break_even({4: 6.0, 3: 1.5}, 4),  # ≈ 0.5503
    ),
    "5-flex": FlexEntryType(
        name="5-flex", n_legs=5,
        payouts={5: 10.0, 4: 4.0, 3: 2.0},
        break_even=_solve_break_even({5: 10.0, 4: 4.0, 3: 2.0}, 5),  # ≈ 0.4216
    ),
    "6-flex": FlexEntryType(
        name="6-flex", n_legs=6,
        payouts={6: 25.0, 5: 2.0, 4: 0.4},
        break_even=_solve_break_even({6: 25.0, 5: 2.0, 4: 0.4}, 6),  # ≈ 0.5421
    ),
}


def best_per_leg_prob(multipliers: dict[int, float], n_legs: int) -> float:
    """Solve for p (per-leg hit rate) where EV(p) == stake, for power plays."""
    # E[net] = 0 means: sum_k C(n,k) p^k (1-p)^(n-k) * mult_k == 1.0
    # Power play: only k=n counts, so p^n * mult == 1.0 → p = (1/mult)^(1/n)
    if not multipliers:
        raise ValueError("multipliers dict empty")
    max_k = max(multipliers.keys())
    if max_k != n_legs:
        # Flex play — return None or approximate; use weighted-break-even instead
        raise ValueError("only power plays have a closed-form per-leg break-even")
    mult = multipliers[n_legs]
    return mult ** (-1.0 / n_legs)


def expected_value(entry: FlexEntryType, per_leg_prob: float) -> tuple[float, float, float]:
    """Compute EV per $1 staked + probability of returning *anything* + median payout.

    EV = sum_k C(n,k) p^k (1-p)^(n-k) * mult_k - 1

    Returns: (ev_per_dollar, win_prob, median_payout)
        win_prob = sum over k where mult_k > 0 of P(k hits)
        median_payout = payout at the median hit count given the distribution
    """
    n = entry.n_legs
    p = per_leg_prob
    if not 0 < p < 1:
        raise ValueError(f"per_leg_prob must be in (0,1), got {p}")

    ev = 0.0
    win_prob = 0.0
    median_payout = 0.0
    cum = 0.0
    median_found = False

    for k in range(0, n + 1):
        # Binomial coefficient
        binom = math.comb(n, k)
        prob_k = binom * (p ** k) * ((1 - p) ** (n - k))
        mult = entry.payouts.get(k, 0.0)
        ev += prob_k * mult
        if mult > 0:
            win_prob += prob_k
        # Median payout over the full outcome distribution (including 0x losses)
        if not median_found:
            cum += prob_k
            if cum >= 0.5:
                median_payout = mult
                median_found = True

    ev -= 1.0  # subtract stake to get net EV

    return ev, win_prob, median_payout


def recommend_entry(entry: FlexEntryType, per_leg_prob: float) -> str:
    """Heuristic play/skip tag based on EV vs break-even margin."""
    if entry.name not in UD_PAYOUTS:
        return "unknown"
    ev, _, _ = expected_value(entry, per_leg_prob)
    margin = per_leg_prob - entry.break_even
    if ev > 0.10:
        return "play-strong"
    if ev > 0.03:
        return "play"
    if ev > 0:
        return "small"
    return "skip"