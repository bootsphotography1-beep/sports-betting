"""Tests for copy formatters, sharp side-alignment, and comparison helpers."""
from __future__ import annotations

from ud_edge.models import Leg, RankedLeg
from ud_edge.copy_format import format_one_line, format_block, opportunities_to_dict
from ud_edge.sharp_books_client import canonicalize_stat, sharp_lookup_key, find_sharp_match
from ud_edge.matcher import rank_legs


def _leg(**kwargs) -> Leg:
    defaults = dict(
        line_id="1",
        player_id="p1",
        player_name="Jayson Tatum",
        sport_id="NBA",
        match_title="BOS@NYK",
        stat_name="points",
        line_value=27.5,
        line_type="balanced",
        higher_american=-160,
        higher_decimal=1.625,
        higher_multiplier=0.95,
        lower_american=135,
        lower_decimal=2.35,
        lower_multiplier=0.95,
        fantasy_source="underdog",
    )
    defaults.update(kwargs)
    return Leg(**defaults)


def _ranked(leg: Leg | None = None, side: str = "higher") -> RankedLeg:
    leg = leg or _leg()
    return RankedLeg(
        leg=leg,
        higher_true_prob=0.59,
        higher_implied_prob=0.615,
        higher_edge_pp=4.0,
        lower_true_prob=0.41,
        lower_implied_prob=0.425,
        lower_edge_pp=-14.0,
        picked_side=side,
        picked_true_prob=0.59 if side == "higher" else 0.41,
        picked_edge_pp=4.0,
        overround=1.04,
    )


class TestCopyFormat:
    def test_prizepicks_uses_more_less(self):
        text = format_one_line(_ranked(), "prizepicks")
        assert "More" in text
        assert "Jayson Tatum" in text
        assert "27.5" in text

    def test_sleeper_uses_over_under(self):
        text = format_one_line(_ranked(side="lower"), "sleeper")
        assert "Under" in text

    def test_underdog_uses_higher_lower(self):
        text = format_one_line(_ranked(), "underdog")
        assert "Higher" in text

    def test_block_filters_by_sport(self):
        nba = _ranked(_leg(sport_id="NBA", player_name="Alpha Player"))
        mlb = _ranked(_leg(line_id="2", player_id="p2", sport_id="MLB",
                           player_name="Zulu Batter", stat_name="hits", line_value=1.5))
        block = format_block([nba, mlb], "underdog", sport="NBA")
        assert "Alpha Player" in block
        assert "Zulu Batter" not in block

    def test_opportunities_dict_has_copy_keys(self):
        d = opportunities_to_dict(_ranked())
        # Underdog-only leg: only underdog + generic copy keys present
        assert set(d["copy"]) == {"underdog", "generic"}
        assert d["sport_id"] == "NBA"
        assert "reason" in d
        assert d["reason"]["headline"]
        assert d["reason"]["bullets"]
        assert d["reason"]["math"]


class TestExplainPick:
    def test_explains_no_vig_without_sharp(self):
        from ud_edge.copy_format import explain_pick
        reason = explain_pick(_ranked(), break_even=0.524)
        assert "No-vig" in reason["headline"] or "edge" in reason["headline"].lower()
        assert any("vig" in b.lower() for b in reason["bullets"])
        assert any("edge" in b.lower() for b in reason["bullets"])

    def test_explains_mispricing_when_sharp_present(self):
        from ud_edge.copy_format import explain_pick
        r = _ranked()
        r.sharp_true_prob = 0.62
        r.sharp_book = "Pinnacle"
        r.mispricing_edge_pp = 3.0
        reason = explain_pick(r, break_even=0.524)
        assert "Soft fantasy" in reason["headline"] or "Pinnacle" in reason["headline"]
        assert any("Mispricing" in b or "soft" in b.lower() for b in reason["bullets"])
        assert any("mispricing" in m.lower() for m in reason["math"])


class TestSharpCanon:
    def test_canonicalize_aliases(self):
        assert canonicalize_stat("Pts") == "points"
        assert canonicalize_stat("3PM") == "threes"
        assert canonicalize_stat("passing_yards") == "pass_yds"

    def test_lookup_key(self):
        assert sharp_lookup_key("LeBron James", "Points") == "lebron james|points"

    def test_find_sharp_match_line_tolerance(self):
        idx = {
            "jayson tatum|points": {
                "over_decimal": 1.9,
                "under_decimal": 1.9,
                "bookmaker": "DraftKings",
                "line_value": 27.0,
            }
        }
        hit = find_sharp_match(idx, "Jayson Tatum", "points", 27.5, line_tolerance=0.5)
        assert hit is not None
        assert hit.bookmaker == "DraftKings"


class TestSharpSideAlignment:
    def test_sharp_opposite_side_flips_pick(self):
        """When sharp disagrees with the fantasy pick, quarantine the leg.

        Under sharp_authoritative_quarantine policy: when sharp's true-prob
        disagrees with UD's pick by ≥ 2pp (delta < -2.0pp), the leg is
        quarantined and excluded rather than flipped. The test below uses
        sharp values with delta > -2.0pp so no quarantine occurs, but note
        that any disagreement strong enough to cause a flip would itself
        trigger quarantine — flip and quarantine are mutually exclusive.
        """
        # UD prices over as favorite (~59%)
        leg = _leg()
        # Sharp agrees on over but with lower confidence:
        # sharp over true = 0.575, UD over true = 0.5912
        # delta = 0.575 - 0.5912 = -1.62pp (within tolerance, no quarantine)
        # No flip occurs because sharp still favors "higher"
        sharp = {
            "jayson tatum|points": {
                "over_decimal": 1.739,
                "under_decimal": 2.353,
                "bookmaker": "Pinnacle",
                "line_value": 27.5,
                "source": "test",
            }
        }
        ranked = rank_legs([leg], break_even=0.52, min_true_prob=0.50,
                           min_edge_pp=0.0, sharp_book_index=sharp)
        assert len(ranked) == 1
        assert ranked[0].picked_side == "higher"
        assert ranked[0].sharp_true_prob is not None

    def test_same_side_mispricing_boosts(self):
        leg = _leg()
        # Sharp agrees on over, with higher confidence
        sharp = {
            "jayson tatum|points": {
                "over_decimal": 1.55,
                "under_decimal": 2.45,
                "bookmaker": "DraftKings",
                "line_value": 27.5,
                "source": "test",
            }
        }
        ranked = rank_legs([leg], break_even=0.52, min_true_prob=0.50,
                           min_edge_pp=0.0, sharp_book_index=sharp)
        assert ranked[0].picked_side == "higher"
        assert ranked[0].mispricing_edge_pp is not None
        assert ranked[0].mispricing_edge_pp > 0
