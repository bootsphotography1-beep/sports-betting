"""Audit P0 fix: deliver.py must use a single probability contract.

Before: deliver.py used `max(picked_prob, sharp_prob)` for the per-lineup
average, which overstates edge whenever sharp is slightly bearish-but-in-band
(within ±2pp of fantasy). The audit called this out as the single biggest
reason board EV overstates real edge.

After: deliver.py must use `effective_true_prob(picked, sharp)` which returns
sharp when sharp is present (sharp-authoritative), and falls back to picked
when sharp is absent. This matches the contract already used by
correlation.py.

These tests pin the contract. They will fail under the old `max()` code,
pass under the new `effective_true_prob` code.
"""
from __future__ import annotations


from ud_edge.deliver import build_multi_report
from ud_edge.matcher import effective_true_prob
from ud_edge.models import Leg, RankedLeg


def _make_ranked(
    *,
    picked_prob: float,
    sharp_prob: float | None,
    mispricing_pp: float | None = None,
    sharp_book: str | None = None,
    player: str = "Test Player",
    stat: str = "points",
    line: float = 20.5,
    sport: str = "NBA",
) -> RankedLeg:
    """Construct a RankedLeg with both fantasy (picked) and optional sharp probs."""
    higher_prob = 1.0 - picked_prob
    lower_prob = picked_prob
    leg = Leg(
        player_id=f"p_{player.replace(' ', '_').lower()}",
        player_name=player,
        sport_id=sport,
        stat_name=stat,
        line_value=line,
        line_id=f"l_{player.replace(' ', '_').lower()}_{stat}",
        line_type="balanced",
        higher_american=-110,
        higher_decimal=1.91,
        higher_multiplier=0.95,
        lower_american=-110,
        lower_decimal=1.91,
        lower_multiplier=0.95,
        fantasy_source="underdog",
    )
    return RankedLeg(
        leg=leg,
        higher_true_prob=higher_prob,
        higher_implied_prob=higher_prob + 0.02,
        higher_edge_pp=(higher_prob - 0.5421) * 100,
        lower_true_prob=lower_prob,
        lower_implied_prob=lower_prob + 0.02,
        lower_edge_pp=(lower_prob - 0.5421) * 100,
        picked_side="lower",
        picked_true_prob=picked_prob,
        picked_edge_pp=(picked_prob - 0.5421) * 100,
        overround=1.05,
        sharp_true_prob=sharp_prob,
        sharp_book=sharp_book,
        mispricing_edge_pp=mispricing_pp,
    )


def _build_report(lineup: list[RankedLeg]) -> str:
    return build_multi_report(
        lineups=[lineup],
        entry_type="6-flex",
        min_true_prob=0.5,
    )


# ── effective_true_prob contract ──────────────────────────────────────────────


def test_effective_true_prob_returns_sharp_when_present():
    """Sharp is authoritative when present — sharp wins, even if fantasy was higher."""
    # Fantasy 58%, sharp 56.5% — sharp inside ±2pp band, so quarantine passed.
    # Old `max()` would return 0.58; new contract must return 0.565.
    assert effective_true_prob(0.58, 0.565) == 0.565


def test_effective_true_prob_returns_sharp_when_sharper():
    """Sharp is bullish — sharp wins (same as max(), but verified by contract)."""
    assert effective_true_prob(0.58, 0.61) == 0.61


def test_effective_true_prob_falls_back_to_picked_when_no_sharp():
    """No sharp data → fantasy only. max() and effective_true_prob agree here."""
    assert effective_true_prob(0.58, None) == 0.58


# ── build_multi_report must use the contract (not max()) ──────────────────────


def test_build_report_uses_sharp_not_max_when_sharp_slightly_bearish():
    """A 6-leg lineup where fantasy = 0.58, sharp = 0.565 (sharp inside ±2pp).

    Old `max()` averaged 0.580 → break-even for 6-flex is 0.5421 → EV is
    computed from 0.580. New contract averages 0.565 → EV is computed from
    0.565. The **summary line** 'Avg true prob' in the rendered report must
    reflect the lower (sharp) number, not the higher (fantasy).

    Per-leg tables still show both fantasy (UD True) and sharp columns —
    those are correct as-is. This test only pins the AVERAGED card-level
    numbers, which is where the overstatement lived.
    """
    lineup = [_make_ranked(picked_prob=0.58, sharp_prob=0.565) for _ in range(6)]
    md = _build_report(lineup)

    # The summary must show 56.50% (sharp-authoritative avg), not 58.00%.
    assert "**Avg true prob: 56.50%**" in md, (
        f"build_multi_report summary must show 56.50% (sharp avg), not "
        f"58.00% (fantasy max). Got report:\n{md}"
    )
    assert "**Avg true prob: 58.00%**" not in md, (
        f"build_multi_report must NOT show the fantasy max in the summary. "
        f"Got report:\n{md}"
    )


def test_build_report_uses_sharp_when_sharper():
    """Sharp bullish (sharp > fantasy): effective_true_prob picks sharp (0.61).
    Old max() also picks 0.61 here, so this case doesn't distinguish them —
    but it pins the contract on the bullish side too.
    """
    lineup = [_make_ranked(picked_prob=0.58, sharp_prob=0.61) for _ in range(6)]
    md = _build_report(lineup)

    assert "61.00%" in md, f"Expected 61.00% (sharp) in report. Got:\n{md}"


def test_build_report_uses_fantasy_when_no_sharp():
    """No sharp data → fantasy is the only signal. avg = 0.58."""
    lineup = [_make_ranked(picked_prob=0.58, sharp_prob=None) for _ in range(6)]
    md = _build_report(lineup)

    assert "58.00%" in md, f"Expected 58.00% (fantasy only) in report. Got:\n{md}"


def test_build_report_does_not_overstate_mixed_card():
    """Mixed card: 3 legs with sharp (slightly bearish), 3 without.

    Old `max()`: (3×0.58 + 3×0.58) / 6 = 0.580
    New contract: (3×0.565 + 3×0.58) / 6 = 0.5725

    The rendered avg must be the lower number.
    """
    lineup = (
        [_make_ranked(picked_prob=0.58, sharp_prob=0.565) for _ in range(3)]
        + [_make_ranked(picked_prob=0.58, sharp_prob=None) for _ in range(3)]
    )
    md = _build_report(lineup)

    # 0.5725 rounds to 57.25%
    assert "57.25%" in md, (
        f"Expected 57.25% avg (mixed card uses effective_true_prob). "
        f"Old max() would have shown 58.00%. Got:\n{md}"
    )


def test_build_report_no_max_overstate():
    """Guardrail: grep-style assertion that the source no longer contains
    the audit-flagged `max(...)` overstate pattern. If someone re-introduces
    the bug, this test fails.
    """
    import ud_edge.deliver as deliver_mod
    src = open(deliver_mod.__file__).read()
    # Look for the specific overstate pattern (picked, sharp with max).
    # `max(r.picked_true_prob, r.sharp_true_prob or 0.0)` is the bug.
    assert "max(r.picked_true_prob, r.sharp_true_prob" not in src, (
        "deliver.py still uses max(picked, sharp). This overstates EV when "
        "sharp is slightly bearish (within ±2pp band). Replace with "
        "effective_true_prob(picked, sharp) from ud_edge.matcher."
    )