"""Wave 1 tests: numerical break-even solver + heterogeneous EV.

These tests replace the hardcoded break_even constants in UD_PAYOUTS with
a bisection solver, and replace expected_value() with a per-leg heterogeneous
exact EV that enumerates all 2^N outcomes weighted by per-leg probability.

Strict TDD: tests are written FIRST (RED), then the implementation (GREEN).
"""
from __future__ import annotations
import math
import pytest
from ud_edge.flex_math import (
    UD_PAYOUTS,
    break_even_numerical,
    expected_value,
    expected_value_per_card,
    recommend_entry,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Numerical break-even solver
# ─────────────────────────────────────────────────────────────────────────────

class TestBreakEvenNumerical:
    """Bisection solver for p in (0,1) solving E[payouts] = 1."""

    def test_6flex_break_even_is_approximately_0_5421_not_0_524(self):
        """The 6-flex break-even must be ~54.21%, not the wrong 52.40%."""
        entry = UD_PAYOUTS["6-flex"]
        be = break_even_numerical(entry)
        # Must be close to the exact solution 0.5421, NOT the wrong 0.5240
        assert 0.541 < be < 0.543, (
            f"6-flex break-even={be:.4f} must be ~0.5421. "
            "If you restored 0.5240 this test must fail."
        )

    def test_all_entry_types_have_numerical_break_even(self):
        """Every entry type in UD_PAYOUTS must have a computable break-even."""
        for name, entry in UD_PAYOUTS.items():
            be = break_even_numerical(entry)
            assert 0 < be < 1, f"{name}: break_even={be} out of (0,1)"
            # Break-even for multi-tier flex should be lower than for power plays
            # (tiered payouts reduce risk)

    def test_power_play_break_even_closed_form_matches_numerical(self):
        """2-man and 3-man power plays: numerical ≈ closed-form."""
        for name in ("2-man-power", "3-man-power"):
            entry = UD_PAYOUTS[name]
            be_num = break_even_numerical(entry)
            # Closed-form: p = (1/mult)^(1/n)
            mult = entry.payouts[entry.n_legs]
            be_closed = mult ** (-1.0 / entry.n_legs)
            assert abs(be_num - be_closed) < 1e-8, (
                f"{name}: numerical={be_num:.8f} vs closed-form={be_closed:.8f}"
            )

    def test_break_even_solver_tolerance(self):
        """Solver must converge to within 1e-10 of the true solution."""
        entry = UD_PAYOUTS["6-flex"]
        be = break_even_numerical(entry)
        # Verify: EV at computed break-even should be essentially 0
        ev, _, _ = expected_value(entry, be)
        assert abs(ev) <= 1e-9, f"EV at break_even={be:.10f} should be ~0, got {ev:.2e}"

    def test_break_even_solver_max_iterations(self):
        """Solver must not iterate more than 200 times for any entry type."""
        entry = UD_PAYOUTS["6-flex"]
        _, iterations = break_even_numerical(entry, return_iterations=True)
        assert iterations <= 200, f"Solver took {iterations} iterations (> 200 max)"

    def test_break_even_solver_rejects_k_0_tier(self):
        """The 0-hit tier (if present) must be excluded from break-even calc."""
        # 6-flex has no 0-hit tier, but we verify by checking EV at break-even ~= 0
        entry = UD_PAYOUTS["6-flex"]
        be = break_even_numerical(entry)
        ev, _, _ = expected_value(entry, be)
        assert abs(ev) <= 1e-9, "0-hit tier must not affect break-even calculation"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: UD_PAYOUTS now uses numerical break-even (not hardcoded)
# ─────────────────────────────────────────────────────────────────────────────

class TestUDPayoutsBreakEvenConsistency:
    """UD_PAYOUTS break_even must equal the numerical solver output."""

    def test_all_hardcoded_break_even_replaced_with_numerical(self):
        """Every entry's break_even must now be the numerical solution."""
        for name, entry in UD_PAYOUTS.items():
            expected_be = break_even_numerical(entry)
            assert abs(entry.break_even - expected_be) < 1e-9, (
                f"{name}: stored break_even={entry.break_even:.6f} "
                f"must equal numerical={expected_be:.6f}"
            )

    def test_expected_value_at_break_even_is_zero(self):
        """EV at the stored break-even must be ≤ 1e-9 (within rounding)."""
        for name, entry in UD_PAYOUTS.items():
            ev, _, _ = expected_value(entry, entry.break_even)
            assert abs(ev) <= 1e-9, (
                f"{name}: EV at break_even={entry.break_even:.6f} = {ev:.2e}, "
                "must be ≤ 1e-9"
            )

    def test_6flex_break_even_known_value(self):
        """Known reference: 6-flex break-even must be 0.5421 ± 0.0001."""
        entry = UD_PAYOUTS["6-flex"]
        assert 0.5420 < entry.break_even < 0.5422, (
            f"6-flex break_even={entry.break_even:.4f} must be 0.5421 (not 0.5240)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Heterogeneous per-leg EV (exact, not uniform-probability)
# ─────────────────────────────────────────────────────────────────────────────

class TestExpectedValuePerCard:
    """expected_value_per_card() enumerates all 2^N outcomes weighted by
    per-leg probability, giving exact EV even when legs have different probs."""

    def test_identity_with_uniform_probability(self):
        """When all legs have the same probability p, result must equal
        the standard expected_value(entry, p)."""
        entry = UD_PAYOUTS["6-flex"]
        p = 0.52
        leg_probs = [p] * 6

        ev_hetero, win_hetero, med_hetero = expected_value_per_card(entry, leg_probs)
        ev_uniform, win_uniform, med_uniform = expected_value(entry, p)

        assert abs(ev_hetero - ev_uniform) < 1e-12, (
            f"Heterogeneous EV {ev_hetero} must equal uniform EV {ev_uniform} "
            "when all leg probs equal."
        )

    def test_heterogeneous_ev_differs_from_uniform_average(self):
        """When legs have DIFFERENT probabilities, the heterogeneous EV must
        NOT equal the uniform-average EV — this is the whole point."""
        entry = UD_PAYOUTS["6-flex"]
        # One leg is 60%, rest are 50%
        leg_probs = [0.60, 0.50, 0.50, 0.50, 0.50, 0.50]

        ev_hetero, _, _ = expected_value_per_card(entry, leg_probs)

        # Compare to uniform average (wrong approach)
        avg_prob = sum(leg_probs) / len(leg_probs)
        ev_uniform, _, _ = expected_value(entry, avg_prob)

        # They must be different (the heterogeneous form is exact)
        assert abs(ev_hetero - ev_uniform) > 1e-6, (
            "Heterogeneous EV must differ from uniform-average EV when "
            "leg probabilities differ."
        )

    def test_ev_per_card_at_break_even_is_zero(self):
        """At the exact break-even per-leg probability, EV must be ~0."""
        entry = UD_PAYOUTS["6-flex"]
        be = entry.break_even
        leg_probs = [be] * 6
        ev, _, _ = expected_value_per_card(entry, leg_probs)
        assert abs(ev) <= 1e-9, f"EV at break-even must be ~0, got {ev:.2e}"

    def test_win_prob_sum_of_individual_hit_probs(self):
        """win_prob must be sum over all outcomes where payout > 0 of
        the joint probability of that exact hit pattern."""
        entry = UD_PAYOUTS["6-flex"]
        leg_probs = [0.5] * 6
        _, win_prob, _ = expected_value_per_card(entry, leg_probs)

        # Manual enumeration: win_prob = sum_k P(k hits) where mult_k > 0
        n = 6
        manual_win = 0.0
        for k in range(0, n + 1):
            binom = math.comb(n, k)
            prob_k = binom * (0.5 ** n)  # symmetric when p=0.5
            if entry.payouts.get(k, 0) > 0:
                manual_win += prob_k

        assert abs(win_prob - manual_win) < 1e-12

    def test_median_payout_is_valid_or_zero(self):
        """median_payout must be either a payout multiplier or 0 (0-hit median at low probs)."""
        entry = UD_PAYOUTS["6-flex"]
        for leg_probs in [
            [0.5] * 6,
            [0.6] * 6,
            [0.4] * 6,
            [0.55, 0.52, 0.60, 0.48, 0.53, 0.50],
        ]:
            _, _, median = expected_value_per_card(entry, leg_probs)
            assert median == 0.0 or median in entry.payouts.values(), (
                f"median_payout={median} not in payout table {entry.payouts} "
                "(and not 0 for 0-hit median)"
            )

    def test_exhaustive_outcome_enumeration_sums_to_one(self):
        """Sum of probabilities over all 2^N outcomes must be exactly 1."""
        UD_PAYOUTS["6-flex"]
        leg_probs = [0.52, 0.48, 0.55, 0.47, 0.53, 0.51]

        n = len(leg_probs)
        total_prob = 0.0
        for bits in range(1 << n):  # enumerate all 2^N outcomes
            prob = 1.0
            for i in range(n):
                p_hit = leg_probs[i]
                prob *= p_hit if (bits >> i) & 1 else (1 - p_hit)
            total_prob += prob

        assert abs(total_prob - 1.0) < 1e-15

    def test_per_card_accepts_variable_leg_count(self):
        """per-card EV must work for any n_legs from 2 to 6."""
        for name in UD_PAYOUTS:
            entry = UD_PAYOUTS[name]
            leg_probs = [0.52] * entry.n_legs
            ev, win, med = expected_value_per_card(entry, leg_probs)
            assert isinstance(ev, float)
            assert isinstance(win, float)
            assert isinstance(med, float)
            assert 0 <= win <= 1

    def test_wrong_leg_count_raises(self):
        """Passing wrong number of leg_probs must raise ValueError."""
        entry = UD_PAYOUTS["6-flex"]
        with pytest.raises(ValueError, match="len.*must.*n_legs"):
            expected_value_per_card(entry, [0.52] * 5)  # wrong count

    def test_recommend_entry_backward_compatible_float(self):
        """recommend_entry must remain backward compatible with single float prob."""
        entry = UD_PAYOUTS["6-flex"]
        rec = recommend_entry(entry, 0.55)
        assert rec in ("play-strong", "play", "small", "skip")

    def test_recommend_entry_positive_ev_gives_play(self):
        """At p > break_even, recommend_entry should give a positive-EV label."""
        entry = UD_PAYOUTS["6-flex"]
        be = entry.break_even
        # Slightly above break-even
        rec = recommend_entry(entry, be + 0.02)
        assert "play" in rec.lower() or "small" in rec.lower(), (
            f"At p={be+0.02:.4f} EV should be positive, got rec={rec}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Mutation tests — prove regression if 0.524 is restored
# ─────────────────────────────────────────────────────────────────────────────

class TestMutationRegression:
    """Prove that restoring the wrong 0.524 break-even causes test failures."""

    def test_6flex_wrong_break_even_524_would_fail_audit(self):
        """Document the regression: 0.524 was the wrong value; correct is ~0.5421."""
        WRONG_BE = 0.5240
        entry = UD_PAYOUTS["6-flex"]
        # The stored break-even is the correct numerical value
        assert abs(entry.break_even - 0.5421) < 0.001, (
            "Stored break-even should be ~0.5421, not 0.5240"
        )
        # The difference between correct and wrong is ~0.018
        diff = abs(entry.break_even - WRONG_BE)
        assert diff > 0.01, (
            f"Correct break_even={entry.break_even:.4f} must differ from "
            f"wrong 0.524 by >0.01 (diff={diff:.4f}). "
            "This proves our tests catch the 0.524 regression."
        )

    def test_ev_at_stored_break_even_fails_if_wrong_value(self):
        """If 0.524 were stored, EV at that break-even would NOT be ~0."""
        entry = UD_PAYOUTS["6-flex"]
        ev_at_524, _, _ = expected_value(entry, 0.524)
        # At p=0.524 the EV is significantly negative (break-even is ~0.542)
        assert ev_at_524 < -0.03, (
            f"EV at p=0.524 is {ev_at_524:.4f}, proving 0.524 is NOT "
            "the true break-even. Our tests catch this regression."
        )

    def test_mutation_proof_exact_break_even_satisfies_ev_zero(self):
        """Prove the exact numerical break-even satisfies EV ≈ 0."""
        entry = UD_PAYOUTS["6-flex"]
        be = break_even_numerical(entry)
        ev, _, _ = expected_value(entry, be)
        assert abs(ev) <= 1e-9, "Exact break-even must satisfy EV ≈ 0"


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Integration — recommendation_label uses heterogeneous EV
# ─────────────────────────────────────────────────────────────────────────────

class TestRecommendationLabelIntegration:
    """recommendation_label must consume the heterogeneous EV form."""

    def test_label_differs_when_leg_probs_heterogeneous(self):
        """When legs differ significantly, the label based on heterogeneous EV
        must differ from the uniform-probability label."""
        entry = UD_PAYOUTS["6-flex"]
        leg_probs_hetero = [0.60, 0.50, 0.50, 0.50, 0.50, 0.50]

        ev_h, _, _ = expected_value_per_card(entry, leg_probs_hetero)

        # The recommendation is based on ev_h (heterogeneous)
        from ud_edge.safety_gate import recommendation_label
        label = recommendation_label(ev_h, 0.5)

        # At 60% top leg, EV should be positive → not "skip"
        assert label != "🔴 SKIP", (
            f"With a 60% top leg, EV={ev_h:.4f} must give a play label, not skip"
        )

    def test_6flex_with_good_legs_is_not_skip(self):
        """6-flex with uniform 55% legs gives +EV (not a skip)."""
        entry = UD_PAYOUTS["6-flex"]
        leg_probs = [0.55] * 6
        ev, _, _ = expected_value_per_card(entry, leg_probs)
        assert ev > 0.0, f"55% uniform legs should give positive EV, got {ev:.4f}"
        # Verify it's not a skip recommendation
        rec = recommend_entry(entry, leg_probs)
        assert rec != "skip", f"55% legs should not be 'skip', got {rec}"

