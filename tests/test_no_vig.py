"""Pytest unit tests for the no-vig math + flex_math payout tables."""
import pytest
from ud_edge.no_vig import (
    american_to_implied, decimal_to_implied, no_vig, edge_pp, pick_side
)
from ud_edge.flex_math import UD_PAYOUTS, expected_value, recommend_entry
from ud_edge.matcher import rank_legs, build_lineups
from ud_edge.deliver import build_multi_report
from ud_edge.results_tracker import (
    log_picks, settle_pick, print_calibration, calibration_stats,
    _leg_key, _load, _save, RESULTS_PATH,
)


# ── american_to_implied ────────────────────────────────────────────────────
class TestAmericanToImplied:
    def test_minus_110_is_52_38_pct(self):
        # Favorite: -110 = 110/(110+100) = 0.52381
        assert abs(american_to_implied(-110) - 0.52381) < 0.001

    def test_plus_110_is_47_62_pct(self):
        # Underdog: +110 = 100/(110+100) = 0.47619
        assert abs(american_to_implied(110) - 0.47619) < 0.001

    def test_minus_200_is_66_67_pct(self):
        # Favorite: -200 = 200/(200+100) = 0.66667
        assert abs(american_to_implied(-200) - 0.66667) < 0.001

    def test_plus_200_is_33_33_pct(self):
        # Underdog: +200 = 100/(200+100) = 0.33333
        assert abs(american_to_implied(200) - 0.33333) < 0.001

    def test_zero_raises(self):
        with pytest.raises(ValueError):
            american_to_implied(0)


# ── decimal_to_implied ─────────────────────────────────────────────────────
class TestDecimalToImplied:
    def test_1_91_implies_52_4_pct(self):
        # 1/1.9091 ≈ 0.52381 (American -110 equivalent)
        assert abs(decimal_to_implied(1.9091) - 0.52381) < 0.001

    def test_2_0_implies_50_pct(self):
        assert abs(decimal_to_implied(2.0) - 0.5) < 0.001

    def test_below_1_raises(self):
        with pytest.raises(ValueError):
            decimal_to_implied(0.99)


# ── no_vig (the core math) ────────────────────────────────────────────────
class TestNoVig:
    def test_symmetric_even_money_no_vig(self):
        # True even money (decimal 2.0/2.0) — no vig, exactly 50/50
        t_over, t_under, over = no_vig(2.0, 2.0)
        assert abs(t_over - 0.5) < 1e-9
        assert abs(t_under - 0.5) < 1e-9
        assert abs(over - 1.0) < 1e-9

    def test_minus_110_implies_4_76_pct_vig(self):
        # -110/-110 in American = decimal 1.9091 = 4.76% overround
        # This is NOT true even money — there's a typical bookmaker vig.
        t_over, t_under, over = no_vig(1.9091, 1.9091)
        # After stripping vig, true prob normalizes back to exactly 50/50
        assert abs(t_over - 0.5) < 1e-9
        assert abs(t_under - 0.5) < 1e-9
        # But the overround is 4.76% (each side 52.38%, sum 104.76%)
        assert abs(over - 1.0476) < 0.001

    def test_derek_gobert(self):
        # -112/-118 → over 49.39%, under 50.61% (UNDER is slight favorite)
        # Decimal derivation: 1 + 100/|am| for negative, 1 + am/100 for positive
        # am -112 → 1 + 100/112 = 1.8929
        # am -118 → 1 + 100/118 = 1.8475
        t_over, t_under, _ = no_vig(1.8929, 1.8475)
        assert abs(t_over - 0.4939) < 0.005
        assert abs(t_under - 0.5061) < 0.005

    def test_asymmetric_signal(self):
        # -136/+110 → over ~54.75%, under ~45.25% (OVER is +EV favorite)
        # am -136 → 1 + 100/136 = 1.7353
        # am +110 → 1 + 110/100 = 2.10
        t_over, t_under, over = no_vig(1.7353, 2.10)
        assert abs(t_over - 0.5475) < 0.005
        assert abs(t_under - 0.4525) < 0.005
        assert 1.04 < over < 1.06  # ~5% overround

    def test_heavy_favorite(self):
        # -300/+200 → over 69.23%, under 30.77% (NOT 75/25 — heavy vig on extremes)
        # am -300 → 1 + 100/300 = 1.3333
        # am +200 → 1 + 200/100 = 3.0
        t_over, t_under, _ = no_vig(1.3333, 3.0)
        assert abs(t_over - 0.6923) < 0.005
        assert abs(t_under - 0.3077) < 0.005

    def test_soft_book_negative_vig(self):
        # Soft book — under is over-priced (decimal=2.30 instead of ~2.0)
        # implied_over=0.5747, implied_under=0.4348, overround=1.0095 (negative-vig-ish)
        t_over, t_under, over = no_vig(1.74, 2.30)
        # Soft overround just means tighter pricing; should still normalize
        assert t_over > 0.55
        assert t_under < 0.45
        assert over < 1.05  # very low overround (sharp pricing)

    def test_invalid_decimal_raises(self):
        with pytest.raises(ValueError):
            no_vig(0.5, 2.0)
        with pytest.raises(ValueError):
            no_vig(2.0, 1.0)

    def test_sum_to_one(self):
        # The two true probs must sum to exactly 1.0 (definition)
        t_over, t_under, _ = no_vig(1.85, 1.95)
        assert abs((t_over + t_under) - 1.0) < 1e-9


# ── edge_pp ────────────────────────────────────────────────────────────────
class TestEdgePp:
    def test_basic(self):
        assert abs(edge_pp(0.55, 0.5495) - 0.05) < 0.001

    def test_negative_edge(self):
        assert abs(edge_pp(0.54, 0.5495) - (-0.95)) < 0.001


# ── pick_side ──────────────────────────────────────────────────────────────
class TestPickSide:
    def test_picks_favorite_over(self):
        side, prob = pick_side(0.55, 0.45, 0.50)
        assert side == "higher"
        assert abs(prob - 0.55) < 0.001

    def test_picks_favorite_under(self):
        side, prob = pick_side(0.45, 0.55, 0.50)
        assert side == "lower"
        assert abs(prob - 0.55) < 0.001

    def test_rejects_below_threshold(self):
        with pytest.raises(ValueError):
            pick_side(0.51, 0.49, 0.55)


# ── flex_math / payout tables ─────────────────────────────────────────────
class TestFlexMath:
    def test_3_man_power_break_even(self):
        # 3-man-power: 6x payout → break-even = 6^(-1/3) ≈ 0.5503
        # The "54.95%" in published tables is rounded to 2dp from 0.5503
        import math
        assert abs(UD_PAYOUTS["3-man-power"].break_even - (1/6)**(1/3)) < 0.001
        # The stored value should be the rounded-to-2dp version
        assert abs(UD_PAYOUTS["3-man-power"].break_even - 0.5495) < 0.001

    def test_6_flex_payout_tiers(self):
        e = UD_PAYOUTS["6-flex"]
        assert e.payouts[6] == 25.0
        assert e.payouts[5] == 2.0
        assert e.payouts[4] == 0.4
        assert e.n_legs == 6

    def test_ev_at_exact_break_even_is_zero(self):
        # At the mathematically exact break-even (6^(-1/3) ≈ 0.5503),
        # EV should be ~0 for a power play. The stored 0.5495 is the
        # rounded-to-2dp version, so EV there is slightly negative.
        import math
        exact_be = (1/6) ** (1/3)
        entry = UD_PAYOUTS["3-man-power"]
        ev_at_exact, _, _ = expected_value(entry, exact_be)
        assert abs(ev_at_exact) < 1e-9
        # At the stored (rounded) 0.5495, EV is slightly negative
        ev_at_stored, _, _ = expected_value(entry, entry.break_even)
        assert ev_at_stored < 0  # confirmed: -0.0045
        assert abs(ev_at_stored) < 0.01  # but within rounding tolerance

    def test_ev_positive_above_break_even(self):
        entry = UD_PAYOUTS["3-man-power"]
        ev, _, _ = expected_value(entry, 0.60)
        assert ev > 0

    def test_ev_negative_below_break_even(self):
        entry = UD_PAYOUTS["3-man-power"]
        ev, _, _ = expected_value(entry, 0.50)
        assert ev < 0

    def test_win_prob_decreases_with_more_legs(self):
        # 3-power @ 60% vs 6-flex @ 60% — 6-flex has more ways to win partial
        p3, win3, _ = expected_value(UD_PAYOUTS["3-man-power"], 0.60)
        p6, win6, _ = expected_value(UD_PAYOUTS["6-flex"], 0.60)
        assert win6 > win3  # 6-flex wins on 4/5/6, 3-power only on 3/3
        # But 3-power pays more on full hit
        assert p3 > 0 and p6 > 0

    def test_recommend_entry_says_play_at_clear_edge(self):
        e = UD_PAYOUTS["3-man-power"]
        rec = recommend_entry(e, 0.65)
        assert rec in ("play-strong", "play")

    def test_recommend_entry_says_skip_at_no_edge(self):
        e = UD_PAYOUTS["3-man-power"]
        rec = recommend_entry(e, 0.50)
        assert rec == "skip"


# ── Injury client ─────────────────────────────────────────────────────────
class TestInjuryClient:
    def test_normalize_name_lowercases_and_strips_punct(self):
        from ud_edge.injury_client import normalize_name
        assert normalize_name("Jayson Tatum") == "jayson tatum"
        assert normalize_name("D'Angelo Russell") == "dangelo russell"
        assert normalize_name("  P.J.  Washington  ") == "pj washington"

    def test_normalize_status_out(self):
        from ud_edge.injury_client import normalize_status
        assert normalize_status("Out") == "OUT"
        assert normalize_status("OUT") == "OUT"
        assert normalize_status("Out Indefinitely") == "OUT"

    def test_normalize_status_ruled_out_in_comment(self):
        from ud_edge.injury_client import normalize_status
        # Status says "Day-To-Day" but comment says "ruled out" — comment wins
        assert normalize_status(
            "Day-To-Day",
            "Tatum (knee) has been ruled out for Saturday's game."
        ) == "OUT"

    def test_normalize_status_day_to_day(self):
        from ud_edge.injury_client import normalize_status
        assert normalize_status("Day-To-Day") == "DAY_TO_DAY"
        assert normalize_status("Day to Day") == "DAY_TO_DAY"

    def test_normalize_status_questionable_probable(self):
        from ud_edge.injury_client import normalize_status
        assert normalize_status("Questionable") == "QUESTIONABLE"
        assert normalize_status("Probable") == "PROBABLE"
        assert normalize_status("Doubtful") == "DOUBTFUL"

    def test_normalize_status_ir_and_suspended(self):
        from ud_edge.injury_client import normalize_status
        assert normalize_status("Injury Reserve") == "INJURY_RESERVE"
        assert normalize_status("IR") == "INJURY_RESERVE"
        assert normalize_status("Suspended") == "SUSPENDED"

    def test_injury_out_statuses_filters_correctly(self):
        from ud_edge.matcher import is_player_out, INJURY_OUT_STATUSES
        from ud_edge.models import Leg
        # Build a tiny index: Jayson Tatum is OUT
        idx = {"NBA": {"jayson tatum": "OUT"}}
        leg_out = Leg(line_id="1", player_id="p", player_name="Jayson Tatum",
                      sport_id="NBA", stat_name="points", line_value=27.5,
                      line_type="balanced",
                      higher_american=-110, higher_decimal=1.91, higher_multiplier=0.95,
                      lower_american=-110, lower_decimal=1.91, lower_multiplier=0.95)
        leg_dtd = Leg(line_id="2", player_id="p", player_name="Jayson Tatum",
                      sport_id="NBA", stat_name="points", line_value=27.5,
                      line_type="balanced",
                      higher_american=-110, higher_decimal=1.91, higher_multiplier=0.95,
                      lower_american=-110, lower_decimal=1.91, lower_multiplier=0.95)
        # Without index: don't filter
        assert is_player_out(leg_out) is False
        # With index and OUT status: filter
        assert is_player_out(leg_out, idx) is True
        # With DTD status: don't filter
        idx_dtd = {"NBA": {"jayson tatum": "DAY_TO_DAY"}}
        assert is_player_out(leg_out, idx_dtd) is False
        # Active player from another team: don't filter
        idx_other = {"NBA": {"lamelo ball": "OUT"}}
        assert is_player_out(leg_out, idx_other) is False

    def test_get_player_status_returns_active_when_unknown(self):
        from ud_edge.matcher import get_player_status
        from ud_edge.models import Leg
        leg = Leg(line_id="1", player_id="p", player_name="Nobody Real",
                  sport_id="NBA", stat_name="points", line_value=27.5,
                  line_type="balanced",
                  higher_american=-110, higher_decimal=1.91, higher_multiplier=0.95,
                  lower_american=-110, lower_decimal=1.91, lower_multiplier=0.95)
        # No index
        assert get_player_status(leg) == "ACTIVE"
        # With index, player not in it
        assert get_player_status(leg, {"NBA": {"jayson tatum": "OUT"}}) == "ACTIVE"
        # With index, player is OUT
        idx = {"NBA": {"nobody real": "OUT"}}
        assert get_player_status(leg, idx) == "OUT"


# ── Sharp-book client ──────────────────────────────────────────────────────
class TestSharpBooksClient:
    def test_manual_csv_load(self, tmp_path):
        from ud_edge.sharp_books_client import ManualSharpBookClient
        from datetime import datetime, timezone
        csv_path = tmp_path / "sharp.csv"
        now = datetime.now(timezone.utc).isoformat()
        csv_path.write_text(
            "player_name,stat_name,line_value,over_decimal,under_decimal,bookmaker,captured_at\n"
            f"LeBron James,points,27.5,1.91,1.91,Pinnacle,{now}\n"
            f"Jayson Tatum,rebounds,8.5,1.85,1.95,DraftKings,{now}\n"
        )
        client = ManualSharpBookClient(csv_path)
        idx, _meta = client.load()
        assert len(idx) == 2
        assert "lebron james|points" in idx
        assert idx["lebron james|points"]["over_decimal"] == 1.91
        assert idx["lebron james|points"]["bookmaker"] == "Pinnacle"

    def test_to_decimal_handles_american(self):
        from ud_edge.sharp_books_client import _to_decimal
        assert abs(_to_decimal("+110") - 2.10) < 0.01
        assert abs(_to_decimal("-110") - 1.909) < 0.01
        assert abs(_to_decimal("-150") - 1.667) < 0.01
        assert abs(_to_decimal(1.85) - 1.85) < 0.01
        assert _to_decimal(None) is None
        assert _to_decimal("") is None

    def test_build_sharp_index_with_only_csv(self, tmp_path):
        from ud_edge.sharp_books_client import build_sharp_index
        from datetime import datetime, timezone
        csv_path = tmp_path / "sharp.csv"
        now = datetime.now(timezone.utc).isoformat()
        csv_path.write_text(
            "player_name,stat_name,line_value,over_decimal,under_decimal,bookmaker,captured_at\n"
            f"Test Player,points,20.5,1.85,1.95,Pinnacle,{now}\n"
        )
        idx, _meta = build_sharp_index(manual_csv=csv_path, sgo_key=None)
        assert "test player|points" in idx
        assert idx["test player|points"]["source"] == "manual-csv"

    def test_rank_legs_with_sharp_book_boosts_mispricings(self):
        """Legs where the sharp book gives a higher true prob should rank higher."""
        from ud_edge.matcher import rank_legs
        from ud_edge.models import Leg

        # Use strong asymmetric pricing so both legs clear 0.55 threshold
        # -145/+125 implies ~55.6% over → 0.5324/0.4676 after vig — too low
        # -160/+135 implies ~61.5% over → ~58.4% true after vig (clears threshold)
        leg_normal = Leg(line_id="1", player_id="p1", player_name="Player A",
                         sport_id="NBA", stat_name="points", line_value=27.5,
                         line_type="balanced",
                         higher_american=-160, higher_decimal=1.625, higher_multiplier=0.95,
                         lower_american=135, lower_decimal=2.35, lower_multiplier=0.95)
        leg_mispriced = Leg(line_id="2", player_id="p2", player_name="Player B",
                            sport_id="NBA", stat_name="points", line_value=27.5,
                            line_type="balanced",
                            higher_american=-160, higher_decimal=1.625, higher_multiplier=0.95,
                            lower_american=135, lower_decimal=2.35, lower_multiplier=0.95)
        legs = [leg_normal, leg_mispriced]
        # UD: -160/+135 → implied 0.6154+0.4255 = 1.0409 → true 0.5912/0.4088
        # Pinnacle: -180/+155 → implied 0.6429+0.6077 = 1.0861... wait that's wrong
        # Let's instead make Pinnacle use a tighter spread (lower overround):
        # Pinnacle: -170/+145 → 1.588/2.45 → implied 0.6296+0.4082 = 1.0378 → true 0.6066/0.3934
        # Now UD best=0.5912, Pinnacle best=0.6066 → mispricing edge = +1.54pp ✓
        sharp = {
            "player a|points": {"over_decimal": 1.588, "under_decimal": 2.45,
                                "bookmaker": "Pinnacle", "line_value": 27.5,
                                "source": "test"},
            # Player B is NOT in the sharp index — no cross-ref
        }
        ranked = rank_legs(legs, break_even=0.5495, injury_index=None, sharp_book_index=sharp)
        # Both legs should be present
        assert len(ranked) == 2, f"expected 2 legs, got {len(ranked)}: {ranked}"
        # Player A should rank first because the sharp book boosts it (mispricing signal)
        assert ranked[0].leg.player_name == "Player A"
        assert ranked[0].sharp_true_prob is not None
        assert ranked[0].mispricing_edge_pp is not None
        assert ranked[0].mispricing_edge_pp > 0  # sharp book gives higher prob
        # Player B has no sharp entry — falls back to UD-implied edge
        assert ranked[1].leg.player_name == "Player B"
        assert ranked[1].sharp_true_prob is None


# ── build_lineups (multi-entry 6-flex builder) ──────────────────────────────
class TestBuildLineups:
    """Tests for build_lineups() — the disjoint lineup partitioner."""

    def _mk_ranked(self, n: int, base_prob: float = 0.55, edge_step: float = 0.005):
        """Build N synthetic RankedLeg with descending edge."""
        from ud_edge.models import Leg, RankedLeg
        ranked = []
        for i in range(n):
            prob = base_prob + edge_step * (n - i)  # highest prob first
            leg = Leg(
                line_id=f"l{i}", player_id=f"p{i}", player_name=f"Player {i}",
                sport_id="NBA", match_id=i, match_title=f"M{i}",
                stat_name="points", line_value=27.5, line_type="balanced",
                higher_american=-145, higher_decimal=1.69, higher_multiplier=0.86,
                lower_american=125, lower_decimal=2.25, lower_multiplier=1.10,
            )
            # picked_side = higher (true_over > true_under for this pricing)
            ranked.append(RankedLeg(
                leg=leg,
                higher_true_prob=prob, higher_implied_prob=0.55,
                higher_edge_pp=(prob - 0.5495) * 100,
                lower_true_prob=1 - prob, lower_implied_prob=0.45,
                lower_edge_pp=((1 - prob) - 0.5495) * 100,
                picked_side="higher", picked_true_prob=prob,
                picked_edge_pp=(prob - 0.5495) * 100,
                overround=1.05,
            ))
        return ranked

    def test_four_lineups_of_six_from_24_legs(self):
        ranked = self._mk_ranked(24)
        lineups = build_lineups(ranked, n_entries=4, n_legs=6)
        assert len(lineups) == 4
        assert all(len(l) == 6 for l in lineups)

    def test_lineups_are_disjoint(self):
        ranked = self._mk_ranked(24)
        lineups = build_lineups(ranked, n_entries=4, n_legs=6)
        all_line_ids = []
        for l in lineups:
            for r in l:
                all_line_ids.append(r.leg.line_id)
        assert len(all_line_ids) == 24
        assert len(set(all_line_ids)) == 24, "expected 24 unique line_ids"

    def test_lineups_preserve_rank_order(self):
        """Entry #1 = top 6, Entry #2 = next 6, etc."""
        ranked = self._mk_ranked(24)
        lineups = build_lineups(ranked, n_entries=4, n_legs=6)
        # Entry #1 should be the top-6 ranked legs (Player 0-5)
        assert [r.leg.player_name for r in lineups[0]] == \
               [f"Player {i}" for i in range(6)]
        # Entry #2 = ranks 6-11
        assert [r.leg.player_name for r in lineups[1]] == \
               [f"Player {i}" for i in range(6, 12)]
        # Entry #4 = ranks 18-23 (floor)
        assert [r.leg.player_name for r in lineups[3]] == \
               [f"Player {i}" for i in range(18, 24)]

    def test_fallback_when_slate_thin(self):
        """12 ranked legs = max 2 full 6-flexes; 3rd & 4th should be omitted."""
        ranked = self._mk_ranked(12)
        lineups = build_lineups(ranked, n_entries=4, n_legs=6)
        assert len(lineups) == 2, f"expected 2 lineups, got {len(lineups)}"
        assert all(len(l) == 6 for l in lineups)

    def test_empty_when_not_enough_for_one_lineup(self):
        """4 legs < 6 needed → 0 lineups (caller handles 'no +EV slate today')."""
        ranked = self._mk_ranked(4)
        lineups = build_lineups(ranked, n_entries=4, n_legs=6)
        assert lineups == []

    def test_exactly_one_lineup(self):
        ranked = self._mk_ranked(6)
        lineups = build_lineups(ranked, n_entries=4, n_legs=6)
        assert len(lineups) == 1
        assert len(lineups[0]) == 6

    def test_invalid_inputs_raise(self):
        ranked = self._mk_ranked(24)
        with pytest.raises(ValueError):
            build_lineups(ranked, n_entries=0, n_legs=6)
        with pytest.raises(ValueError):
            build_lineups(ranked, n_entries=4, n_legs=0)

    def test_three_lineups_explicit(self):
        """--entries 3 path: 18 ranked legs → 3 disjoint 6-flexes."""
        ranked = self._mk_ranked(18)
        lineups = build_lineups(ranked, n_entries=3, n_legs=6)
        assert len(lineups) == 3
        # 3 × 6 = 18 unique legs
        all_ids = sum([[r.leg.line_id for r in l] for l in lineups], [])
        assert len(set(all_ids)) == 18


# ── build_multi_report (multi-entry markdown) ───────────────────────────────
class TestBuildMultiReport:
    """Tests for the multi-entry Markdown report builder."""

    def _mk_lineups(self, n_entries=4, n_legs=6):
        from ud_edge.matcher import build_lineups
        ranked = TestBuildLineups()._mk_ranked(n_entries * n_legs)
        return build_lineups(ranked, n_entries=n_entries, n_legs=n_legs)

    def test_header_contains_all_entry_markers(self):
        lineups = self._mk_lineups(4)
        md = build_multi_report(lineups, entry_type="6-flex", n_legs=6)
        # Header
        assert "Underdog Edge Bot" in md
        assert "4 lineups" in md
        assert "24 unique legs" in md
        # Per-entry sections
        for i in range(1, 5):
            assert f"Entry #{i} — 6-flex" in md, f"missing Entry #{i} header"

    def test_disjoint_footer_disclaimer(self):
        lineups = self._mk_lineups(3)
        md = build_multi_report(lineups, entry_type="6-flex", n_legs=6)
        assert "disjoint" in md
        assert "Entry #1 has the highest-edge" in md

    def test_at_a_glance_summary_table(self):
        lineups = self._mk_lineups(4)
        md = build_multi_report(lineups, entry_type="6-flex", n_legs=6)
        assert "## At-a-glance" in md
        # 4 rows of the summary table + the header row
        # The pattern "| **#" should appear 4 times
        import re
        matches = re.findall(r"\| \*\*#\d+\*\*", md)
        assert len(matches) == 4

    def test_single_entry_omits_at_a_glance(self):
        """With only 1 lineup, the summary table would be redundant."""
        lineups = self._mk_lineups(1)
        md = build_multi_report(lineups, entry_type="6-flex", n_legs=6)
        assert "## At-a-glance" not in md

    def test_unknown_entry_type_raises(self):
        lineups = self._mk_lineups(2)
        with pytest.raises(ValueError):
            build_multi_report(lineups, entry_type="9-flex", n_legs=6)


# ── rank_legs full_game_only mode ───────────────────────────────────────────
class TestFullGameOnly:
    """Verify that --full-game-only drops mid-game props and obscure sports."""

    def _mk_legs(self):
        from ud_edge.models import Leg
        return [
            # Tennis full-game (KEEP)
            Leg(line_id="l1", player_id="p1", player_name="A", sport_id="TENNIS",
                stat_name="sets_played", line_value=2.5, line_type="balanced",
                higher_american=-150, higher_decimal=1.67, higher_multiplier=0.82,
                lower_american=130, lower_decimal=2.30, lower_multiplier=1.10),
            # Tennis mid-match (DROP)
            Leg(line_id="l2", player_id="p2", player_name="B", sport_id="TENNIS",
                stat_name="period_1_games_won", line_value=5.5, line_type="balanced",
                higher_american=-140, higher_decimal=1.71, higher_multiplier=0.83,
                lower_american=120, lower_decimal=2.20, lower_multiplier=1.05),
            # MLB full-game (KEEP)
            Leg(line_id="l3", player_id="p3", player_name="C", sport_id="MLB",
                stat_name="hits", line_value=1.5, line_type="balanced",
                higher_american=-160, higher_decimal=1.625, higher_multiplier=0.80,
                lower_american=140, lower_decimal=2.40, lower_multiplier=1.15),
            # MLB mid-game (DROP) — non-trivial to test the full_game_only filter
            Leg(line_id="l4", player_id="p4", player_name="D", sport_id="MLB",
                stat_name="period_1_strikeouts", line_value=3.5, line_type="balanced",
                higher_american=-140, higher_decimal=1.71, higher_multiplier=0.83,
                lower_american=120, lower_decimal=2.20, lower_multiplier=1.05),
            # NBA full-game (KEEP)
            Leg(line_id="l5", player_id="p5", player_name="E", sport_id="NBA",
                stat_name="points", line_value=27.5, line_type="balanced",
                higher_american=-145, higher_decimal=1.69, higher_multiplier=0.86,
                lower_american=125, lower_decimal=2.25, lower_multiplier=1.10),
            # CS obscure sport (DROP) — slightly stronger edge to clear threshold
            Leg(line_id="l6", player_id="p6", player_name="F", sport_id="CS",
                stat_name="kills_on_maps_1_2", line_value=15.5, line_type="balanced",
                higher_american=-200, higher_decimal=1.50, higher_multiplier=0.75,
                lower_american=170, lower_decimal=2.70, lower_multiplier=1.25),
        ]

    def test_full_game_only_drops_midgame(self):
        legs = self._mk_legs()
        ranked_full = rank_legs(legs, break_even=0.524, full_game_only=True)
        ranked_default = rank_legs(legs, break_even=0.524)
        # Default: all 6 may rank (assuming they pass threshold)
        # full_game_only: only 3 should pass (TENNIS sets_played, MLB hits, NBA points)
        ids_full = {r.leg.line_id for r in ranked_full}
        assert "l2" not in ids_full  # period_1_games_won dropped
        assert "l4" not in ids_full  # period_1_strikeouts dropped
        assert "l6" not in ids_full  # CS sport dropped
        # And the keepers should remain
        assert "l1" in ids_full
        assert "l3" in ids_full
        assert "l5" in ids_full

    def test_full_game_only_off_keeps_all(self):
        """Without the flag, mid-game and obscure-sport legs are kept."""
        legs = self._mk_legs()
        ranked = rank_legs(legs, break_even=0.524, full_game_only=False)
        ids = {r.leg.line_id for r in ranked}
        # All 6 should pass (assuming thresholds met for each)
        assert "l2" in ids
        assert "l4" in ids
        assert "l6" in ids


# ── results_tracker (calibration) ───────────────────────────────────────────
class TestResultsTracker:
    """Test the picks-logging + calibration module."""

    def _mk_ranked(self, n: int = 6):
        from ud_edge.models import Leg, RankedLeg
        ranked = []
        for i in range(n):
            leg = Leg(
                line_id=f"log_l{i}", player_id=f"log_p{i}",
                player_name=f"Player {i}", sport_id="NBA",
                match_id=i, match_title=f"M{i}",
                stat_name="points", line_value=27.5, line_type="balanced",
                higher_american=-145, higher_decimal=1.69, higher_multiplier=0.86,
                lower_american=125, lower_decimal=2.25, lower_multiplier=1.10,
            )
            ranked.append(RankedLeg(
                leg=leg, higher_true_prob=0.65, higher_implied_prob=0.55,
                higher_edge_pp=12.0, lower_true_prob=0.35, lower_implied_prob=0.45,
                lower_edge_pp=-15.0, picked_side="higher", picked_true_prob=0.65,
                picked_edge_pp=12.0, overround=1.05,
            ))
        return ranked

    def setup_method(self):
        """Backup existing results.json before each test."""
        self._backup = None
        if RESULTS_PATH.exists():
            self._backup = RESULTS_PATH.read_text()
        RESULTS_PATH.unlink(missing_ok=True)

    def teardown_method(self):
        """Restore the original results.json."""
        if self._backup is not None:
            RESULTS_PATH.write_text(self._backup)
        else:
            RESULTS_PATH.unlink(missing_ok=True)

    def test_log_picks_creates_file(self):
        ranked = self._mk_ranked(6)
        lineups = [ranked]
        added = log_picks(lineups, entry_type="6-flex", n_entries=1)
        assert added == 6
        assert RESULTS_PATH.exists()
        data = _load()
        assert len(data["picks"]) == 6

    def test_log_picks_dedupes_same_day(self):
        """Running twice the same day should not double-log."""
        ranked = self._mk_ranked(6)
        lineups = [ranked]
        added1 = log_picks(lineups, entry_type="6-flex", n_entries=1)
        added2 = log_picks(lineups, entry_type="6-flex", n_entries=1)
        assert added1 == 6
        assert added2 == 0
        data = _load()
        assert len(data["picks"]) == 6

    def test_log_picks_with_multiple_entries(self):
        ranked = self._mk_ranked(12)
        lineups = [ranked[:6], ranked[6:]]
        added = log_picks(lineups, entry_type="6-flex", n_entries=2)
        assert added == 12
        data = _load()
        # Entries are tagged 1, 2
        entries = {p["entry"] for p in data["picks"]}
        assert entries == {1, 2}

    def test_settle_pick_marks_outcome(self):
        ranked = self._mk_ranked(2)
        lineups = [ranked]
        log_picks(lineups, entry_type="6-flex", n_entries=1)
        ok = settle_pick(0, hit=True, actual_stat=29.0)
        assert ok
        data = _load()
        assert data["picks"][0]["outcome"] == "HIT"
        assert data["picks"][0]["actual_stat"] == 29.0
        # Cannot settle twice
        ok2 = settle_pick(0, hit=False, actual_stat=20.0)
        assert not ok2

    def test_calibration_empty(self):
        """No picks → informative empty report."""
        out = print_calibration()
        assert "No resolved picks" in out

    def test_calibration_with_picks(self):
        """Log picks, settle half as HIT, check Brier score."""
        ranked = self._mk_ranked(4)
        lineups = [ranked]
        log_picks(lineups, entry_type="6-flex", n_entries=1)
        settle_pick(0, hit=True, actual_stat=29.0)
        settle_pick(1, hit=False, actual_stat=22.0)
        stats = calibration_stats()
        assert stats["total_resolved"] == 2
        assert stats["total_pending"] == 2
        # 4 picks at pred 0.65: 1 HIT, 1 MISS, 2 pending
        # Brier = ((0.65-1)^2 + (0.65-0)^2) / 2 = (0.1225 + 0.4225) / 2 = 0.2725
        assert 0.20 < stats["brier_score"] < 0.30

    def test_settle_invalid_index_returns_false(self):
        ranked = self._mk_ranked(2)
        log_picks([ranked], entry_type="6-flex", n_entries=1)
        assert not settle_pick(99, hit=True)
        assert not settle_pick(-1, hit=True)

    def test_leg_key_format(self):
        from ud_edge.models import Leg
        leg = Leg(
            line_id="x", player_id="abc", player_name="X", sport_id="NBA",
            stat_name="points", line_value=27.5, line_type="balanced",
            higher_american=-145, higher_decimal=1.69, higher_multiplier=0.86,
            lower_american=125, lower_decimal=2.25, lower_multiplier=1.10,
        )
        assert _leg_key(leg) == "NBA|abc|points|27.5"