"""Pure-function no-vig math.

Takes two-sided decimal odds from a single book (here: Underdog itself),
strips the bookmaker's overround, and returns the implied "true" probability
of each side. The favorite side (higher true probability after vig removal)
is the +EV side per Derek @BTS methodology.

Common bug to avoid: confusing -100/odds (favorite-payout math) with
100/(|odds|+100) (implied probability). The latter is correct.
"""
from __future__ import annotations


def american_to_implied(american: int) -> float:
    """Convert American odds -> raw implied probability (includes vig).

    Favorite (negative): abs(odds) / (abs(odds) + 100)
      -110 → 110/210 ≈ 0.52381
    Underdog (positive): 100 / (odds + 100)
      +110 → 100/210 ≈ 0.47619
    """
    if american == 0:
        raise ValueError("american odds cannot be 0")
    if american < 0:
        return abs(american) / (abs(american) + 100.0)
    return 100.0 / (american + 100.0)


def decimal_to_implied(decimal_odds: float) -> float:
    """Convert decimal odds -> raw implied probability (includes vig).

    decimal_price=1.74 -> implied = 1/1.74 = 0.5747
    decimal_price=2.10 -> implied = 1/2.10 = 0.4762
    """
    if decimal_odds <= 1.0:
        raise ValueError(f"decimal odds must be > 1.0, got {decimal_odds}")
    return 1.0 / decimal_odds


def no_vig(over_decimal: float, under_decimal: float) -> tuple[float, float, float]:
    """Strip the vig from two-sided decimal odds.

    Returns: (true_over_prob, true_under_prob, overround)
        overround > 1.0 means book is taking a cut (typical)
        overround = 1.0 means no vig
        overround < 1.0 means soft book, one side is over-priced (still valid)

    Example:
        over_decimal=1.74, under_decimal=2.10
        implied_over  = 1/1.74 = 0.5747
        implied_under = 1/2.10 = 0.4762
        overround = 0.5747 + 0.4762 = 1.0509  (5.09% vig)
        true_over  = 0.5747 / 1.0509 = 0.5469 (54.69%)
        true_under = 0.4762 / 1.0509 = 0.4531 (45.31%)
        Favorite side = "higher" (over) at 54.69%
    """
    if over_decimal <= 1.0 or under_decimal <= 1.0:
        raise ValueError(
            f"decimal odds must be > 1.0 (got over={over_decimal}, under={under_decimal})"
        )

    implied_over = decimal_to_implied(over_decimal)
    implied_under = decimal_to_implied(under_decimal)
    overround = implied_over + implied_under
    if overround <= 0:
        raise ValueError(f"overround must be > 0, got {overround}")

    true_over = implied_over / overround
    true_under = implied_under / overround
    return true_over, true_under, overround


def edge_pp(true_prob: float, break_even: float) -> float:
    """Edge in percentage points = true_prob - break_even (per-leg)."""
    return (true_prob - break_even) * 100.0


def pick_side(true_over: float, true_under: float, min_true_prob: float) -> tuple[str, float]:
    """Pick the favorite (higher true prob) side if it clears the threshold.

    Returns: (side, true_prob). side is "higher" or "lower".
    Raises ValueError if no side clears the threshold.
    """
    if true_over >= true_under:
        side, prob = "higher", true_over
    else:
        side, prob = "lower", true_under

    if prob < min_true_prob:
        raise ValueError(
            f"favorite side {side} at {prob:.4f} below threshold {min_true_prob}"
        )
    return side, prob