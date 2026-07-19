"""Unit tests for PropLine payload parsing (no network)."""
import pytest

from ud_edge.propline_client import (
    BOOK_DABBLE,
    BOOK_PINNACLE,
    BOOK_PRIZEPICKS,
    BOOK_SLEEPER,
    BOOK_UNDERDOG,
    SPORT_MAP,
    parse_prop_outcomes_to_index_rows,
    propline_configured,
    PropLineClient,
)


SAMPLE_EVENT = {
    "id": "10",
    "sport_key": "baseball_mlb",
    "away_team": "Phillies",
    "home_team": "Rockies",
    "commence_time": "2026-07-19T23:00:00Z",
    "bookmakers": [
        {
            "key": "pinnacle",
            "title": "Pinnacle",
            "markets": [{
                "key": "pitcher_strikeouts",
                "outcomes": [
                    {"name": "Over", "description": "Zack Wheeler",
                     "price": -128, "point": 6.5},
                    {"name": "Under", "description": "Zack Wheeler",
                     "price": 108, "point": 6.5},
                ],
            }],
        },
        {
            "key": "prizepicks",
            "title": "PrizePicks",
            "markets": [{
                "key": "pitcher_strikeouts",
                "outcomes": [
                    {"name": "Over", "description": "Zack Wheeler",
                     "price": 100, "point": 6.5, "dfs_odds_type": "standard"},
                    {"name": "Under", "description": "Zack Wheeler",
                     "price": 100, "point": 6.5, "dfs_odds_type": "standard"},
                ],
            }],
        },
        {
            "key": "sleeper",
            "title": "Sleeper",
            "markets": [{
                "key": "pitcher_strikeouts",
                "outcomes": [
                    {"name": "Over", "description": "Zack Wheeler",
                     "price": -128, "point": 6.5},
                    {"name": "Under", "description": "Zack Wheeler",
                     "price": 102, "point": 6.5},
                ],
            }],
        },
        {
            "key": "dabble",
            "title": "Dabble",
            "markets": [{
                "key": "batter_hits",
                "outcomes": [
                    {"name": "Over", "description": "Kyle Schwarber",
                     "price": -175, "point": 0.5},
                    {"name": "Under", "description": "Kyle Schwarber",
                     "price": 130, "point": 0.5},
                ],
            }],
        },
        {
            "key": "underdog",
            "title": "Underdog Fantasy",
            "markets": [{
                "key": "pitcher_strikeouts",
                "outcomes": [
                    # Normal UD multiplier (~0.82) — keep
                    {"name": "Over", "description": "Zack Wheeler",
                     "price": -113, "point": 6.5, "payout_multiplier": 0.82},
                    {"name": "Under", "description": "Zack Wheeler",
                     "price": 101, "point": 6.5, "payout_multiplier": 1.0},
                ],
            }],
        },
        {
            "key": "underdog",
            "title": "Underdog Fantasy",
            "markets": [{
                "key": "batter_hits",
                "outcomes": [
                    # Extreme boost special — skip
                    {"name": "Over", "description": "Boosted Bat",
                     "price": -150, "point": 1.5, "payout_multiplier": 1.5},
                    {"name": "Under", "description": "Boosted Bat",
                     "price": 120, "point": 1.5, "payout_multiplier": None},
                ],
            }],
        },
    ],
}


class TestParsePropOutcomes:
    def test_sport_map_matches_live_propline_keys(self):
        assert SPORT_MAP["MLB"] == "baseball_mlb"
        assert SPORT_MAP["NBA"] == "basketball_nba"
        assert SPORT_MAP["NFL"] == "football_nfl"
        assert SPORT_MAP["NHL"] == "hockey_nhl"

    def test_true_prob_keeps_pinnacle_sleeper_dabble_underdog(self):
        rows = parse_prop_outcomes_to_index_rows(SAMPLE_EVENT, for_true_prob=True)
        books = {r["book_key"] for r in rows}
        assert BOOK_PINNACLE in books
        assert BOOK_SLEEPER in books
        assert BOOK_DABBLE in books
        assert BOOK_UNDERDOG in books
        assert BOOK_PRIZEPICKS not in books  # synthetic even-money excluded
        assert not any(r["player"] == "Boosted Bat" for r in rows)
        pin = next(r for r in rows if r["book_key"] == BOOK_PINNACLE)
        assert pin["stat"] == "strikeouts"
        assert pin["line"] == 6.5
        assert pin["over_decimal"] != 2.0

    def test_line_only_mode_includes_prizepicks(self):
        rows = parse_prop_outcomes_to_index_rows(SAMPLE_EVENT, for_true_prob=False)
        books = {r["book_key"] for r in rows}
        assert BOOK_PRIZEPICKS in books

    def test_client_requires_key(self):
        with pytest.raises(ValueError, match="PROPLINE_API_KEY"):
            PropLineClient(api_key="")

    def test_propline_configured_false_without_env(self, monkeypatch):
        monkeypatch.delenv("PROPLINE_API_KEY", raising=False)
        assert propline_configured() is False
        monkeypatch.setenv("PROPLINE_API_KEY", "test-key")
        assert propline_configured() is True
