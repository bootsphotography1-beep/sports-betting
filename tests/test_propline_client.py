"""Unit tests for PropLine payload parsing (no network)."""
import pytest

from ud_edge.propline_client import (
    BOOK_PINNACLE,
    BOOK_PRIZEPICKS,
    BOOK_UNDERDOG,
    parse_prop_outcomes_to_index_rows,
    propline_configured,
    PropLineClient,
)


SAMPLE_EVENT = {
    "id": "10",
    "sport_key": "baseball_mlb",
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
            "key": "underdog",
            "title": "Underdog Fantasy",
            "markets": [{
                "key": "pitcher_strikeouts",
                "outcomes": [
                    {"name": "Over", "description": "Zack Wheeler",
                     "price": -113, "point": 6.5, "payout_multiplier": None},
                    {"name": "Under", "description": "Zack Wheeler",
                     "price": 101, "point": 6.5, "payout_multiplier": None},
                ],
            }],
        },
        {
            "key": "underdog",
            "title": "Underdog Fantasy",
            "markets": [{
                "key": "batter_hits",
                "outcomes": [
                    # Boosted special — must be skipped for true-prob index
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
    def test_true_prob_keeps_pinnacle_and_clean_underdog(self):
        rows = parse_prop_outcomes_to_index_rows(SAMPLE_EVENT, for_true_prob=True)
        books = {r["book_key"] for r in rows}
        assert BOOK_PINNACLE in books
        assert BOOK_UNDERDOG in books
        assert BOOK_PRIZEPICKS not in books  # synthetic even-money excluded
        # Boosted Underdog special skipped
        assert not any(r["player"] == "Boosted Bat" for r in rows)
        # Zack Wheeler strikeouts from Pinnacle present with real decimals
        pin = next(r for r in rows if r["book_key"] == BOOK_PINNACLE)
        assert pin["stat"] == "strikeouts"
        assert pin["line"] == 6.5
        assert pin["over_decimal"] != 2.0
        assert pin["under_decimal"] != 2.0

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
