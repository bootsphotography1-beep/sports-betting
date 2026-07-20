"""Underdog Fantasy entry-type payout tables + per-leg break-even.

Source: Underdog Fantasy official payout tables (2026-07 verified).
All multipliers are PAYOUT multipliers — they multiply the ENTRY STAKE,
not the per-leg contribution. E.g. 6/6 = 25x means a $5 entry returns
$125 on a perfect card.

Wave 1: Break-even is now computed NUMERICALLY from the payout table via
bisection (not hardcoded). EV is computed by enumerating all 2^N outcomes
weighted by per-leg probability (heterogeneous exact EV).
"""
from __future__ import annotations
import math
from ud_edge.models import FlexEntryType


# ─────────────────────────────────────────────────────────────────────────────
# Break-even numerical solver (bisection)
# ─────────────────────────────────────────────────────────────────────────────

def _ev_func(entry: FlexEntryType, p: float) -> float:
    """Compute E[payouts] - 1 at per-leg hit rate p.

    Excludes the 0-hit tier (k=0 is not in payouts dict for flex entries).
    """
    n = entry.n_legs
    ev = 0.0
    for k, mult in entry.payouts.items():
        if k == 0:
            continue  # exclude 0-hit tier
        binom = math.comb(n, k)
        ev += mult * binom * (p ** k) * ((1 - p) ** (n - k))
    return ev - 1.0  # break-even when this == 0


def break_even_numerical(
    entry: FlexEntryType,
    tol: float = 1e-10,
    max_iter: int = 200,
    return_iterations: bool = False,
) -> float | tuple[float, int]:
    """Solve for per-leg break-even probability via bisection.

    Finds p in (0, 1) such that sum_k payouts[k] * C(n,k) * p^k * (1-p)^(n-k) = 1.

    The 0-hit tier (k=0) is EXCLUDED from the equation, matching the actual
    payout structure where no payout is returned for 0 hits.

    Args:
        entry: FlexEntryType with n_legs and payouts dict.
        tol: Convergence tolerance on |f(p)|.
        max_iter: Maximum bisection iterations (hard cap at 200).
        return_iterations: If True, returns (break_even, iterations).

    Returns:
        The break-even per-leg probability, or (break_even, iterations) if
        return_iterations=True.

    Raises:
        ValueError: If no solution found in (0,1) or solver diverges.
    """
    a, b = 0.0, 1.0
    fa = _ev_func(entry, a)  # = -1 (always, since k>=1 terms are 0 at p=0)
    fb = _ev_func(entry, b)  # = max(payouts) - 1 > 0 for any real payout

    # Sanity checks
    if fa > 0:
        raise ValueError(f"EV function positive at p=0: {fa} — check payouts dict")
    if fb < 0:
        raise ValueError(f"EV function negative at p=1: {fb} — check payouts dict")

    for iteration in range(1, max_iter + 1):
        p = (a + b) / 2.0
        fp = _ev_func(entry, p)

        if abs(fp) < tol or (b - a) / 2.0 < tol:
            if return_iterations:
                return p, iteration
            return p

        if fp * fa < 0:
            b, fb = p, fp
        else:
            a, fa = p, fp

    # Did not converge
    p_final = (a + b) / 2.0
    if return_iterations:
        return p_final, max_iter
    return p_final


# ─────────────────────────────────────────────────────────────────────────────
# Compute and store exact numerical break-evens
# ─────────────────────────────────────────────────────────────────────────────

def _compute_break_even(entry: FlexEntryType) -> float:
    """Helper: compute numerical break-even for one entry type."""
    return break_even_numerical(entry)


# ─────────────────────────────────────────────────────────────────────────────
# Underdog Fantasy payout tables (verified July 2026)
# Break-even values are COMPUTED, not hardcoded (Wave 1 fix)
# ─────────────────────────────────────────────────────────────────────────────

_6FLEX = FlexEntryType(
    name="6-flex",
    n_legs=6,
    payouts={6: 25.0, 5: 2.0, 4: 0.4},
    break_even=_compute_break_even(FlexEntryType(
        name="_temp", n_legs=6, payouts={6: 25.0, 5: 2.0, 4: 0.4}, break_even=0.0
    )),
)

UD_PAYOUTS: dict[str, FlexEntryType] = {
    # ── Power plays (all legs must hit) ──
    "2-man-power": FlexEntryType(
        name="2-man-power", n_legs=2,
        payouts={2: 3.0},
        break_even=_compute_break_even(FlexEntryType(
            name="_temp", n_legs=2, payouts={2: 3.0}, break_even=0.0
        )),
    ),
    "3-man-power": FlexEntryType(
        name="3-man-power", n_legs=3,
        payouts={3: 6.0},
        break_even=_compute_break_even(FlexEntryType(
            name="_temp", n_legs=3, payouts={3: 6.0}, break_even=0.0
        )),
    ),
    "4-man-power": FlexEntryType(
        name="4-man-power", n_legs=4,
        payouts={4: 10.0},
        break_even=_compute_break_even(FlexEntryType(
            name="_temp", n_legs=4, payouts={4: 10.0}, break_even=0.0
        )),
    ),
    # ── Flex plays (tiered payouts) ──
    "3-flex": FlexEntryType(
        name="3-flex", n_legs=3,
        payouts={3: 6.0, 2: 1.0},
        break_even=_compute_break_even(FlexEntryType(
            name="_temp", n_legs=3, payouts={3: 6.0, 2: 1.0}, break_even=0.0
        )),
    ),
    "4-flex": FlexEntryType(
        name="4-flex", n_legs=4,
        payouts={4: 6.0, 3: 1.5},
        break_even=_compute_break_even(FlexEntryType(
            name="_temp", n_legs=4, payouts={4: 6.0, 3: 1.5}, break_even=0.0
        )),
    ),
    "5-flex": FlexEntryType(
        name="5-flex", n_legs=5,
        payouts={5: 10.0, 4: 4.0, 3: 2.0},
        break_even=_compute_break_even(FlexEntryType(
            name="_temp", n_legs=5, payouts={5: 10.0, 4: 4.0, 3: 2.0}, break_even=0.0
        )),
    ),
    "6-flex": _6FLEX,
}


# ─────────────────────────────────────────────────────────────────────────────
# Standard expected_value (uniform per-leg probability, backward compatible)
# ─────────────────────────────────────────────────────────────────────────────

def expected_value(entry: FlexEntryType, per_leg_prob: float) -> tuple[float, float, float]:
    """Compute EV per $1 staked + probability of returning *anything* + median payout.

    Assumes ALL legs have the same per_leg_prob (uniform probability).
    For heterogeneous per-leg probabilities use expected_value_per_card().

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
    payouts_at_hits = []

    for k in range(0, n + 1):
        binom = math.comb(n, k)
        prob_k = binom * (p ** k) * ((1 - p) ** (n - k))
        mult = entry.payouts.get(k, 0.0)
        ev += prob_k * mult
        if mult > 0:
            win_prob += prob_k
            payouts_at_hits.append((k, mult, prob_k))

    ev -= 1.0

    # Median payout: find hit-count k where cumulative prob crosses 0.5
    median_payout = 0.0
    if payouts_at_hits:
        payouts_at_hits.sort(key=lambda x: -x[2])
        cum = 0.0
        for k, mult, prob_k in sorted(payouts_at_hits, key=lambda x: -x[1]):
            cum += prob_k
            if cum >= 0.5:
                median_payout = mult
                break

    return ev, win_prob, median_payout


# ─────────────────────────────────────────────────────────────────────────────
# Heterogeneous per-leg EV (exact — enumerates all 2^N outcomes)
# ─────────────────────────────────────────────────────────────────────────────

def expected_value_per_card(
    entry: FlexEntryType,
    leg_probs: list[float],
) -> tuple[float, float, float]:
    """Exact EV per $1 staked using per-leg probabilities (heterogeneous).

    Enumerates all 2^N outcome patterns, each weighted by its exact joint
    probability under the provided per-leg hit probabilities.

    Args:
        entry: FlexEntryType with n_legs and payouts dict.
        leg_probs: List of per-leg hit probabilities, one per leg.
                   Must have exactly entry.n_legs elements.

    Returns: (ev_per_dollar, win_prob, median_payout)
        ev_per_dollar: Expected net EV per $1 staked.
        win_prob: Probability of returning any payout (> 0 multiplier).
        median_payout: Payout at the median outcome (50th percentile).

    Raises:
        ValueError: If len(leg_probs) != entry.n_legs or values out of (0,1).
    """
    n = entry.n_legs
    if len(leg_probs) != n:
        raise ValueError(
            f"len(leg_probs)={len(leg_probs)} must equal n_legs={n}"
        )
    for p in leg_probs:
        if not 0 <= p <= 1:
            raise ValueError(f"leg_probs values must be in [0,1], got {p}")

    # Enumerate all 2^n outcome patterns
    # bit i of outcome mask = 1 means leg i HIT
    total_ev = 0.0
    total_win_prob = 0.0

    # For median: collect (payout, cumulative_prob) sorted by payout desc
    # We compute cumulative prob by enumerating all outcomes in payout order
    outcome_payouts: list[tuple[float, float]] = []

    for mask in range(1 << n):
        # Joint probability of this exact outcome pattern
        prob = 1.0
        n_hits = 0
        for i in range(n):
            p_hit = leg_probs[i]
            if (mask >> i) & 1:
                prob *= p_hit
                n_hits += 1
            else:
                prob *= (1 - p_hit)

        payout = entry.payouts.get(n_hits, 0.0)
        total_ev += prob * payout
        if payout > 0:
            total_win_prob += prob
            outcome_payouts.append((payout, prob))

    net_ev = total_ev - 1.0

    # Median payout: sort by payout descending, accumulate until >= 50%
    outcome_payouts.sort(key=lambda x: -x[0])
    cum = 0.0
    median_payout = 0.0
    for payout, prob in outcome_payouts:
        cum += prob
        if cum >= 0.5:
            median_payout = payout
            break

    return net_ev, total_win_prob, median_payout


# ─────────────────────────────────────────────────────────────────────────────
# best_per_leg_prob (existing — kept for backward compatibility)
# ─────────────────────────────────────────────────────────────────────────────

def best_per_leg_prob(multipliers: dict[int, float], n_legs: int) -> float:
    """Solve for p (per-leg hit rate) where EV(p) == stake, for power plays."""
    if not multipliers:
        raise ValueError("multipliers dict empty")
    max_k = max(multipliers.keys())
    if max_k != n_legs:
        raise ValueError("only power plays have a closed-form per-leg break-even")
    mult = multipliers[n_legs]
    return mult ** (-1.0 / n_legs)


# ─────────────────────────────────────────────────────────────────────────────
# recommend_entry (backward compatible — accepts float for single-leg p)
# ─────────────────────────────────────────────────────────────────────────────

def recommend_entry(entry: FlexEntryType, per_leg_prob: float | list[float]) -> str:
    """Heuristic play/skip tag based on EV vs break-even margin.

    Accepts either:
    - A single float per_leg_prob (uniform across all legs, backward compat)
    - A list of per-leg probabilities (heterogeneous, exact)

    For exact EV with heterogeneous legs, pass a list.
    """
    if entry.name not in UD_PAYOUTS:
        return "unknown"

    if isinstance(per_leg_prob, list):
        # Heterogeneous exact EV
        ev, _, _ = expected_value_per_card(entry, per_leg_prob)
    else:
        # Uniform (backward compatible)
        ev, _, _ = expected_value(entry, per_leg_prob)

    if ev > 0.10:
        return "play-strong"
    if ev > 0.03:
        return "play"
    if ev > 0:
        return "small"
    return "skip"
