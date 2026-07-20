"""Tests for PropLine client parsing (no network)."""
from __future__ import annotations

from ud_edge.propline_client import parse_event_odds, fantasy_props_to_legs


SAMPLE_EVENT = {
    "id": "evt1",
    "sport_key": "basketball_nba",
    "home_team": "Knicks",
    "away_team": "Celtics",
    "commence_time": "2026-07-20T00:00:00Z",
    "bookmakers": [
        {
            "key": "pinnacle",
            "title": "Pinnacle",
            "markets": [
                {
                    "key": "player_points",
                    "last_update": "2026-07-20T00:00:00Z",
                    "outcomes": [
                        {"name": "Over", "description": "Jayson Tatum", "price": -115, "point": 27.5},
                        {"name": "Under", "description": "Jayson Tatum", "price": -105, "point": 27.5},
                    ],
                }
            ],
        },
        {
            "key": "draftkings",
            "title": "DraftKings",
            "markets": [
                {
                    "key": "player_points",
                    "last_update": "2026-07-20T00:00:00Z",
                    "outcomes": [
                        {"name": "Over", "description": "Jayson Tatum", "price": -120, "point": 27.5},
                        {"name": "Under", "description": "Jayson Tatum", "price": 100, "point": 27.5},
                    ],
                }
            ],
        },
        {
            "key": "prizepicks",
            "title": "PrizePicks",
            "markets": [
                {
                    "key": "player_points",
                    "last_update": "2026-07-20T00:00:00Z",
                    "outcomes": [
                        {"name": "Over", "description": "Jayson Tatum", "price": 100, "point": 28.5,
                         "dfs_odds_type": "standard"},
                        {"name": "Under", "description": "Jayson Tatum", "price": 100, "point": 28.5,
                         "dfs_odds_type": "standard"},
                        {"name": "Over", "description": "Jayson Tatum", "price": 100, "point": 25.5,
                         "dfs_odds_type": "goblin"},
                        {"name": "Under", "description": "Jayson Tatum", "price": 100, "point": 25.5,
                         "dfs_odds_type": "goblin"},
                    ],
                }
            ],
        },
        {
            "key": "underdog",
            "title": "Underdog",
            "markets": [
                {
                    "key": "player_points",
                    "last_update": "2026-07-20T00:00:00Z",
                    "outcomes": [
                        {"name": "Over", "description": "Jayson Tatum", "price": -130, "point": 27.5,
                         "payout_multiplier": None},
                        {"name": "Under", "description": "Jayson Tatum", "price": 110, "point": 27.5,
                         "payout_multiplier": None},
                    ],
                }
            ],
        },
    ],
}


class TestParseEventOdds:
    def test_prefers_pinnacle_over_draftkings(self):
        props = parse_event_odds(SAMPLE_EVENT, "NBA")
        sharp = [p for p in props if p["book_type"] == "sharp"]
        assert len(sharp) == 1
        assert sharp[0]["bookmaker"] == "pinnacle"
        assert sharp[0]["stat"] == "points"
        assert sharp[0]["line"] == 27.5
        # American -115 → decimal ≈ 1.87
        assert abs(sharp[0]["over_decimal"] - (1 + 100 / 115)) < 0.01

    def test_keeps_fantasy_books_separately(self):
        props = parse_event_odds(SAMPLE_EVENT, "NBA")
        fantasy = [p for p in props if p["book_type"] == "fantasy"]
        books = {p["bookmaker"] for p in fantasy}
        assert "prizepicks" in books
        assert "underdog" in books

    def test_skips_prizepicks_goblin(self):
        props = parse_event_odds(SAMPLE_EVENT, "NBA")
        pp = [p for p in props if p["bookmaker"] == "prizepicks"]
        assert len(pp) == 1
        assert pp[0]["line"] == 28.5  # standard, not goblin 25.5


class TestFantasyPropsToLegs:
    def test_builds_legs(self):
        props = parse_event_odds(SAMPLE_EVENT, "NBA")
        fantasy = [p for p in props if p["book_type"] == "fantasy"]
        legs = fantasy_props_to_legs(fantasy)
        assert len(legs) == len(fantasy)
        assert all(leg.player_name == "Jayson Tatum" for leg in legs)
        assert all(leg.higher_decimal > 1.0 for leg in legs)
