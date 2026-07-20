"""Wave 3A tests: source/platform identity, canonical dedupe, valid copy targets (TDD).
Strict TDD - tests written FIRST (RED), then implementation (GREEN).
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
import pytest
from ud_edge.models import Leg, RankedLeg
from ud_edge.matcher import rank_legs, dedupe_lineups, build_lineups


def make_leg(
    *,
    player_name: str = "Jayson Tatum",
    stat_name: str = "points",
    line_value: float = 27.5,
    higher_dec: float = 1.82,
    lower_dec: float = 2.25,
    sport_id: str = "NBA",
    match_title: str = "BOS vs NYK",
    fantasy_source: str = "",
    scheduled_at: str | None = None,
    **overrides,
) -> Leg:
    if scheduled_at is None:
        scheduled_at = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    return Leg(
        line_id=f"test-{player_name[:3]}-{stat_name}-{line_value}",
        appearance_id="a1", player_id="p1", player_name=player_name,
        sport_id=sport_id, match_id=1, match_title=match_title,
        scheduled_at=scheduled_at, stat_name=stat_name, line_value=line_value,
        line_type="balanced", higher_american=-130, higher_decimal=higher_dec,
        higher_multiplier=0.9, lower_american=110, lower_decimal=lower_dec,
        lower_multiplier=0.9, fantasy_source=fantasy_source, **overrides,
    )


def make_ranked(
    *,
    player_name: str = "Jayson Tatum",
    stat_name: str = "points",
    line_value: float = 27.5,
    higher_dec: float = 1.82,
    lower_dec: float = 2.25,
    sport_id: str = "NBA",
    match_title: str = "BOS vs NYK",
    fantasy_source: str = "",
    picked_edge_pp: float = 2.0,
    scheduled_at: str | None = None,
    **overrides,
) -> RankedLeg:
    if scheduled_at is None:
        scheduled_at = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    leg = make_leg(
        player_name=player_name, stat_name=stat_name, line_value=line_value,
        higher_dec=higher_dec, lower_dec=lower_dec, sport_id=sport_id,
        match_title=match_title, fantasy_source=fantasy_source,
        scheduled_at=scheduled_at, **overrides,
    )
    picked_side = "higher" if picked_edge_pp >= 0 else "lower"
    picked_prob = 0.55 if picked_side == "higher" else 0.45
    return RankedLeg(
        leg=leg, higher_true_prob=0.55, higher_implied_prob=1.0 / higher_dec,
        higher_edge_pp=picked_edge_pp + 0.5, lower_true_prob=0.45,
        lower_implied_prob=1.0 / lower_dec, lower_edge_pp=picked_edge_pp - 0.5,
        picked_side=picked_side, picked_true_prob=picked_prob,
        picked_edge_pp=picked_edge_pp, overround=1.05,
    )


class TestLegFantasySourceField:
    def test_leg_has_fantasy_source_field_default_empty(self):
        leg = make_leg()
        assert hasattr(leg, "fantasy_source"), "Leg must have fantasy_source field"
        assert leg.fantasy_source == "", "fantasy_source must default to ''"

    def test_leg_accepts_fantasy_source_value(self):
        leg = make_leg(fantasy_source="underdog")
        assert leg.fantasy_source == "underdog"

    def test_leg_fantasy_source_set_via_constructor(self):
        leg = Leg(
            line_id="x", appearance_id="a", player_id="p",
            player_name="Test Player", sport_id="NBA",
            stat_name="points", line_value=25.0, line_type="balanced",
            higher_american=-130, higher_decimal=1.82, higher_multiplier=0.9,
            lower_american=110, lower_decimal=2.25, lower_multiplier=0.9,
            fantasy_source="prizepicks",
        )
        assert leg.fantasy_source == "prizepicks"


class TestCanonicalMarketKey:
    def test_canonical_key_same_leg_no_dedupe(self):
        leg1 = make_ranked(player_name="Jayson Tatum", stat_name="points",
                           line_value=27.5, match_title="BOS vs NYK",
                           fantasy_source="underdog", picked_edge_pp=3.0)
        result = dedupe_lineups([leg1])
        assert len(result) == 1

    def test_canonical_key_identical_legs_dedupe_to_one(self):
        leg1 = make_ranked(player_name="Jayson Tatum", stat_name="points",
                           line_value=27.5, match_title="BOS vs NYK",
                           fantasy_source="underdog", picked_edge_pp=3.0)
        leg2 = make_ranked(player_name="Jayson Tatum", stat_name="points",
                           line_value=27.5, match_title="BOS vs NYK",
                           fantasy_source="prizepicks", picked_edge_pp=1.5)
        result = dedupe_lineups([leg1, leg2])
        assert len(result) == 1, "Duplicates must reduce to 1 leg"
        assert result[0].picked_edge_pp == 3.0, "Highest edge should be kept"

    def test_canonical_key_6_legs_same_market_leaves_1(self):
        base = dict(player_name="Jayson Tatum", stat_name="points",
                     line_value=27.5, match_title="BOS vs NYK")
        legs = [
            make_ranked(fantasy_source="underdog", picked_edge_pp=3.0, **base),
            make_ranked(fantasy_source="prizepicks", picked_edge_pp=2.5, **base),
            make_ranked(fantasy_source="sleeper", picked_edge_pp=2.0, **base),
            make_ranked(fantasy_source="underdog", picked_edge_pp=1.5, **base),
            make_ranked(fantasy_source="prizepicks", picked_edge_pp=1.0, **base),
            make_ranked(fantasy_source="sleeper", picked_edge_pp=0.5, **base),
        ]
        result = dedupe_lineups(legs)
        assert len(result) == 1
        assert result[0].picked_edge_pp == 3.0

    def test_canonical_key_different_player_no_dedupe(self):
        leg1 = make_ranked(player_name="Jayson Tatum", stat_name="points",
                           line_value=27.5, match_title="BOS vs NYK", picked_edge_pp=3.0)
        leg2 = make_ranked(player_name="Jaylen Brown", stat_name="points",
                           line_value=27.5, match_title="BOS vs NYK", picked_edge_pp=2.0)
        result = dedupe_lineups([leg1, leg2])
        assert len(result) == 2

    def test_canonical_key_different_stat_no_dedupe(self):
        leg1 = make_ranked(player_name="Jayson Tatum", stat_name="points",
                           line_value=27.5, match_title="BOS vs NYK", picked_edge_pp=3.0)
        leg2 = make_ranked(player_name="Jayson Tatum", stat_name="rebounds",
                           line_value=27.5, match_title="BOS vs NYK", picked_edge_pp=2.0)
        result = dedupe_lineups([leg1, leg2])
        assert len(result) == 2

    def test_canonical_key_different_line_value_no_dedupe(self):
        leg1 = make_ranked(player_name="Jayson Tatum", stat_name="points",
                           line_value=27.5, match_title="BOS vs NYK", picked_edge_pp=3.0)
        leg2 = make_ranked(player_name="Jayson Tatum", stat_name="points",
                           line_value=28.5, match_title="BOS vs NYK", picked_edge_pp=2.0)
        result = dedupe_lineups([leg1, leg2])
        assert len(result) == 2

    def test_canonical_key_case_insensitive(self):
        leg1 = make_ranked(player_name="JAYSON TATUM", stat_name="POINTS",
                           line_value=27.5, match_title="bos vs nyk", picked_edge_pp=3.0)
        leg2 = make_ranked(player_name="jayson tatum", stat_name="points",
                           line_value=27.5, match_title="BOS vs NYK", picked_edge_pp=2.0)
        result = dedupe_lineups([leg1, leg2])
        assert len(result) == 1

    def test_canonical_key_whitespace_tolerant(self):
        leg1 = make_ranked(player_name="  Jayson  Tatum ", stat_name="  points ",
                           line_value=27.5, match_title="BOS  vs  NYK", picked_edge_pp=3.0)
        leg2 = make_ranked(player_name="Jayson Tatum", stat_name="points",
                           line_value=27.5, match_title="BOS vs NYK", picked_edge_pp=2.0)
        result = dedupe_lineups([leg1, leg2])
        assert len(result) == 1

    def test_canonical_key_different_picked_side_no_dedupe(self):
        leg1 = make_ranked(player_name="Jayson Tatum", stat_name="points",
                           line_value=27.5, match_title="BOS vs NYK", picked_edge_pp=3.0)
        leg2 = make_ranked(player_name="Jayson Tatum", stat_name="points",
                           line_value=27.5, match_title="BOS vs NYK", picked_edge_pp=-1.0)
        result = dedupe_lineups([leg1, leg2])
        assert len(result) == 2

    def test_canonical_key_none_match_title_treated_equal(self):
        leg1 = make_ranked(player_name="Jayson Tatum", stat_name="points",
                           line_value=27.5, match_title=None, picked_edge_pp=3.0)
        leg2 = make_ranked(player_name="Jayson Tatum", stat_name="points",
                           line_value=27.5, match_title=None, picked_edge_pp=2.0)
        result = dedupe_lineups([leg1, leg2])
        assert len(result) == 1


class TestBuildLineupsCallsDedupe:
    def test_build_lineups_dedupe_before_chunking(self):
        base = dict(player_name="Jayson Tatum", stat_name="points",
                     line_value=27.5, match_title="BOS vs NYK")
        legs = [
            make_ranked(fantasy_source="underdog", picked_edge_pp=3.0, **base),
            make_ranked(fantasy_source="prizepicks", picked_edge_pp=2.5, **base),
            make_ranked(fantasy_source="sleeper", picked_edge_pp=2.0, **base),
            make_ranked(fantasy_source="underdog", picked_edge_pp=1.5, **base),
            make_ranked(fantasy_source="prizepicks", picked_edge_pp=1.0, **base),
            make_ranked(fantasy_source="sleeper", picked_edge_pp=0.5, **base),
        ]
        lineups = build_lineups(legs, n_entries=4, n_legs=6)
        assert lineups == []

    def test_build_lineups_6_unique_legs_produces_1_lineup(self):
        legs = [
            make_ranked(player_name="Jayson Tatum", stat_name="points",
                        line_value=27.5, match_title="BOS vs NYK", picked_edge_pp=3.0),
            make_ranked(player_name="Jaylen Brown", stat_name="rebounds",
                        line_value=7.5, match_title="BOS vs NYK", picked_edge_pp=2.5),
            make_ranked(player_name="Derrick White", stat_name="assists",
                        line_value=5.5, match_title="BOS vs NYK", picked_edge_pp=2.0),
            make_ranked(player_name="Jrue Holiday", stat_name="steals",
                        line_value=1.5, match_title="BOS vs NYK", picked_edge_pp=1.5),
            make_ranked(player_name="Kristaps Porzingis", stat_name="blocks",
                        line_value=1.5, match_title="BOS vs NYK", picked_edge_pp=1.0),
            make_ranked(player_name="Sam Hauser", stat_name="threes",
                        line_value=3.5, match_title="BOS vs NYK", picked_edge_pp=0.5),
        ]
        lineups = build_lineups(legs, n_entries=4, n_legs=6)
        assert len(lineups) == 1
        assert len(lineups[0]) == 6

    def test_build_lineups_12_unique_legs_produces_2_lineups(self):
        legs = [
            make_ranked(player_name=f"Player{i}", stat_name="points",
                        line_value=20.0 + i, match_title=f"Team{i} vs Team{i+1}",
                        picked_edge_pp=3.0 - i * 0.1)
            for i in range(12)
        ]
        lineups = build_lineups(legs, n_entries=4, n_legs=6)
        assert len(lineups) == 2
        assert len(lineups[0]) == 6
        assert len(lineups[1]) == 6


class TestOpportunitiesCopyTargets:
    def test_opportunities_includes_fantasy_source(self):
        from ud_edge.copy_format import opportunities_to_dict
        leg = make_ranked(fantasy_source="prizepicks", picked_edge_pp=2.0)
        d = opportunities_to_dict(leg)
        assert "fantasy_source" in d
        assert d["fantasy_source"] == "prizepicks"

    def test_opportunities_includes_available_copy_platforms(self):
        from ud_edge.copy_format import opportunities_to_dict
        leg = make_ranked(fantasy_source="prizepicks", picked_edge_pp=2.0)
        d = opportunities_to_dict(leg)
        assert "available_copy_platforms" in d

    def test_underdog_leg_available_copy_is_underdog_only(self):
        from ud_edge.copy_format import opportunities_to_dict
        leg = make_ranked(fantasy_source="underdog", picked_edge_pp=2.0)
        d = opportunities_to_dict(leg)
        platforms = d.get("available_copy_platforms", [])
        assert platforms == ["underdog"], f"expected ['underdog'], got {platforms}"

    def test_prizepicks_leg_available_copy_is_prizepicks_only(self):
        from ud_edge.copy_format import opportunities_to_dict
        leg = make_ranked(fantasy_source="prizepicks", picked_edge_pp=2.0)
        d = opportunities_to_dict(leg)
        platforms = d.get("available_copy_platforms", [])
        assert platforms == ["prizepicks"], f"expected ['prizepicks'], got {platforms}"

    def test_sleeper_leg_available_copy_is_sleeper_only(self):
        from ud_edge.copy_format import opportunities_to_dict
        leg = make_ranked(fantasy_source="sleeper", picked_edge_pp=2.0)
        d = opportunities_to_dict(leg)
        platforms = d.get("available_copy_platforms", [])
        assert platforms == ["sleeper"], f"expected ['sleeper'], got {platforms}"

    def test_unknown_source_leg_available_copy_empty(self):
        from ud_edge.copy_format import opportunities_to_dict
        leg = make_ranked(fantasy_source="", picked_edge_pp=2.0)
        d = opportunities_to_dict(leg)
        platforms = d.get("available_copy_platforms", [])
        assert platforms == [], f"expected [], got {platforms}"

    def test_copy_text_only_generated_for_observed_platform(self):
        from ud_edge.copy_format import opportunities_to_dict
        leg = make_ranked(fantasy_source="underdog", picked_edge_pp=2.0)
        d = opportunities_to_dict(leg)
        copy_dict = d.get("copy", {})
        available = d.get("available_copy_platforms", [])
        for platform in available:
            assert platform in copy_dict
        assert "underdog" in copy_dict
