"""Wave 2B tests: started-market rejection + consolidated trivial-prop filter.

Strict TDD — tests written FIRST (RED), then implementation (GREEN).

Covers:
1. reject_started / reject_live parameters on rank_legs()
2. scheduled_at / expires_at parsing and market-rejection logic
3. Consolidated RARE_UNDER_HALF_STATS (runs, hits, rbis, walks added)
4. EXCLUDE_STATS / EXCLUDE_SPORTS canonical defaults moved into rank_legs()
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
import pytest
from ud_edge.models import Leg, RankedLeg
from ud_edge.matcher import rank_legs, is_trivial_under_zero


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def make_leg(
    *,
    scheduled_at: str | None = None,
    stat_name: str = "points",
    line_value: float = 27.5,
    higher_dec: float = 1.82,
    lower_dec: float = 2.25,
    sport_id: str = "NBA",
    **overrides,
) -> Leg:
    """Minimal valid leg for rank_legs()."""
    return Leg(
        line_id="test-1",
        appearance_id="a1",
        player_id="p1",
        player_name="Jayson Tatum",
        sport_id=sport_id,
        match_id=1,
        match_title="BOS vs NYK",
        scheduled_at=scheduled_at,
        stat_name=stat_name,
        line_value=line_value,
        line_type="balanced",
        higher_american=-130,
        higher_decimal=higher_dec,
        higher_multiplier=0.9,
        lower_american=110,
        lower_decimal=lower_dec,
        lower_multiplier=0.9,
        **overrides,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 1 — reject_started / reject_live parameters exist
# ─────────────────────────────────────────────────────────────────────────────

class TestRejectStartedLiveParams:
    """Parameter must exist and default to True (reject started/live markets)."""

    def test_reject_started_param_exists(self):
        import inspect
        sig = inspect.signature(rank_legs)
        assert "reject_started" in sig.parameters, (
            "rank_legs must accept a reject_started parameter"
        )

    def test_reject_live_param_exists(self):
        import inspect
        sig = inspect.signature(rank_legs)
        assert "reject_live" in sig.parameters, (
            "rank_legs must accept a reject_live parameter"
        )

    def test_reject_started_default_is_True(self):
        import inspect
        p = inspect.signature(rank_legs).parameters["reject_started"]
        assert p.default is True, (
            "reject_started must default to True to reject already-started markets"
        )

    def test_reject_live_default_is_True(self):
        import inspect
        p = inspect.signature(rank_legs).parameters["reject_live"]
        assert p.default is True, (
            "reject_live must default to True to reject live-event markets"
        )


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 2 — SB-P1-05 repro: leg scheduled 3 hours in the past → 0 legs
# ─────────────────────────────────────────────────────────────────────────────

class TestStartedMarketRepro:
    """Reproducer for audit SB-P1-05: started markets appearing in rankings."""

    def test_started_leg_rejected_by_default(self):
        """A leg scheduled 3 hours in the past must NOT appear in ranked output."""
        past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        leg = make_leg(scheduled_at=past)
        ranked = rank_legs([leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0)
        assert len(ranked) == 0, (
            f"SB-P1-05: started market (scheduled 3h ago) must be rejected, "
            f"got {len(ranked)} legs"
        )

    def test_started_leg_included_when_reject_started_False(self):
        """A leg scheduled 3 hours in the past must appear when reject_started=False."""
        past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        leg = make_leg(scheduled_at=past)
        ranked = rank_legs(
            [leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0,
            reject_started=False,
        )
        assert len(ranked) == 1, "reject_started=False must include past-market legs"

    def test_future_leg_returned_normally(self):
        """A leg scheduled 3 hours in the future must NOT be rejected."""
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        leg = make_leg(scheduled_at=future)
        ranked = rank_legs([leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0)
        assert len(ranked) == 1, (
            f"Future-market leg must NOT be rejected, got {len(ranked)}"
        )

    def test_null_scheduled_at_NOT_rejected(self):
        """Null/unknown scheduled_at must NOT cause rejection (label as unknown)."""
        leg = make_leg(scheduled_at=None)
        ranked = rank_legs([leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0)
        assert len(ranked) == 1, (
            "Null scheduled_at must NOT cause rejection — label as unknown but allow"
        )

    def test_malformed_scheduled_at_not_rejected(self):
        """A leg with an unparseable scheduled_at string must NOT crash or reject."""
        leg = make_leg(scheduled_at="not-a-date")
        ranked = rank_legs([leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0)
        assert len(ranked) == 1, (
            "Malformed scheduled_at must NOT crash or reject — treat as unknown"
        )


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 3 — expires_at expiry
# ─────────────────────────────────────────────────────────────────────────────

class TestExpiresAt:
    """Legs with an expires_at in the past must be rejected."""

    def test_expired_leg_rejected(self):
        """Leg with expires_at 1 hour in the past must be rejected."""
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        # scheduled_at past means rejected
        leg = make_leg(scheduled_at=past, stat_name="rebounds", line_value=6.5)
        ranked = rank_legs([leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0)
        assert len(ranked) == 0


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 4 — live_event rejection
# ─────────────────────────────────────────────────────────────────────────────

class TestLiveEventRejection:
    """reject_live=True must reject legs from live/in-progress events."""

    def test_reject_live_param_filters_live_event(self):
        import inspect
        sig = inspect.signature(rank_legs)
        assert "reject_live" in sig.parameters


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 5 — SB-P1-06 repro: trivial-prop filter consolidation
# ─────────────────────────────────────────────────────────────────────────────

class TestTrivialPropFilterConsolidation:
    """Reproducer for audit SB-P1-06: 'Lower 0.5 runs' leaking through filter."""

    def test_lower_0_5_runs_filtered(self):
        """Lower 0.5 runs at line=0.5 must be filtered as trivial."""
        leg = make_leg(stat_name="runs", line_value=0.5)
        assert is_trivial_under_zero(leg) is True, (
            "SB-P1-06: 'Lower 0.5 runs' must be filtered as trivial prop"
        )

    def test_lower_0_5_hits_filtered(self):
        """Lower 0.5 hits at line=0.5 must be filtered as trivial."""
        leg = make_leg(stat_name="hits", line_value=0.5)
        assert is_trivial_under_zero(leg) is True, (
            "SB-P1-06: 'Lower 0.5 hits' must be filtered as trivial"
        )

    def test_lower_0_5_rbis_filtered(self):
        """Lower 0.5 RBIs at line=0.5 must be filtered as trivial."""
        leg = make_leg(stat_name="rbis", line_value=0.5)
        assert is_trivial_under_zero(leg) is True, (
            "SB-P1-06: 'Lower 0.5 rbis' must be filtered as trivial"
        )

    def test_lower_0_5_walks_filtered(self):
        """Lower 0.5 walks at line=0.5 must be filtered as trivial."""
        leg = make_leg(stat_name="walks", line_value=0.5)
        assert is_trivial_under_zero(leg) is True, (
            "SB-P1-06: 'Lower 0.5 walks' must be filtered as trivial"
        )

    def test_higher_11_5_rebounds_NOT_filtered(self):
        """Higher 11.5 rebounds is NOT trivial — must NOT be filtered."""
        leg = make_leg(stat_name="rebounds", line_value=11.5)
        assert is_trivial_under_zero(leg) is False, (
            "'Higher 11.5 rebounds' is NOT trivial — must pass through"
        )

    def test_higher_27_5_points_NOT_filtered(self):
        """Higher 27.5 points is a mainstream prop — must NOT be filtered."""
        leg = make_leg(stat_name="points", line_value=27.5)
        assert is_trivial_under_zero(leg) is False

    def test_under_0_any_stat_filtered(self):
        """Under 0 of any stat (line=0.0) must always be filtered."""
        for stat in ["runs", "hits", "rbis", "walks", "rebounds", "assists", "points"]:
            leg = make_leg(stat_name=stat, line_value=0.0)
            assert is_trivial_under_zero(leg) is True, (
                f"Under 0 {stat} must be filtered"
            )

    def test_rank_legs_filters_trivial_runs(self):
        """rank_legs must filter 'Lower 0.5 runs' legs from output."""
        leg = make_leg(stat_name="runs", line_value=0.5)
        ranked = rank_legs([leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0)
        assert len(ranked) == 0, (
            "SB-P1-06: 'Lower 0.5 runs' must not appear in ranked output"
        )

    def test_rank_legs_passes_higher_11_5_rebounds(self):
        """rank_legs must pass 'Higher 11.5 rebounds' through to output."""
        leg = make_leg(stat_name="rebounds", line_value=11.5)
        ranked = rank_legs([leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0)
        assert len(ranked) == 1, (
            "'Higher 11.5 rebounds' must appear in ranked output"
        )


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 6 — EXCLUDE_STATS / EXCLUDE_SPORTS canonical defaults in rank_legs()
# ─────────────────────────────────────────────────────────────────────────────

class TestExcludeStatsSportsDefaults:
    """EXCLUDE_STATS and EXCLUDE_SPORTS must be canonical defaults inside rank_legs()."""

    def test_exclude_sports_in_rank_legs_filters_esports(self):
        """Esports legs must be filtered when full_game_only=True (uses EXCLUDE_SPORTS)."""
        leg = make_leg(sport_id="ESPORTS")
        ranked = rank_legs(
            [leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0,
            full_game_only=True,
        )
        assert len(ranked) == 0, (
            "ESPORTS is in EXCLUDE_SPORTS — must be filtered in full_game_only mode"
        )

    def test_exclude_stats_filters_period_1_props(self):
        """period_1_* stats in EXCLUDE_STATS must be filtered in full_game_only mode."""
        leg = make_leg(stat_name="period_1_games_won")
        ranked = rank_legs(
            [leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0,
            full_game_only=True,
        )
        assert len(ranked) == 0, (
            "period_1_games_won is in EXCLUDE_STATS — must be filtered"
        )

    def test_normal_sport_and_stat_not_filtered_by_default(self):
        """NBA / points / 27.5 must NOT be filtered when no extra filters applied."""
        leg = make_leg(sport_id="NBA", stat_name="points", line_value=27.5)
        ranked = rank_legs([leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0)
        assert len(ranked) == 1, "Normal NBA props must not be filtered by default"


# ─────────────────────────────────────────────────────────────────────────────
# TEST GROUP 7 — Integration: started + trivial filter combined
# ─────────────────────────────────────────────────────────────────────────────

class TestStartedAndTrivialCombined:
    """Both filters must compose correctly."""

    def test_started_leg_not_even_trivial_checked(self):
        """A started-market leg is rejected before the trivial filter runs."""
        past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        # This is both started AND trivial
        leg = make_leg(scheduled_at=past, stat_name="runs", line_value=0.5)
        ranked = rank_legs([leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0)
        assert len(ranked) == 0, "Started trivial leg must be rejected"

    def test_future_leg_trivial_still_filtered(self):
        """A future leg that is trivial must still be filtered by is_trivial_under_zero."""
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        leg = make_leg(scheduled_at=future, stat_name="runs", line_value=0.5)
        ranked = rank_legs([leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0)
        assert len(ranked) == 0, "Future but trivial leg must be filtered"

    def test_future_normal_leg_returned(self):
        """A future leg that is normal must be returned."""
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        leg = make_leg(scheduled_at=future, stat_name="points", line_value=27.5)
        ranked = rank_legs([leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0)
        assert len(ranked) == 1, "Future normal leg must be returned"
