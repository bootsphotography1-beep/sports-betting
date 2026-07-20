"""Tests for Wave 3B: empty-slate crash guard in run_once."""
from unittest.mock import patch, MagicMock
from pathlib import Path


class TestEmptySlateGuard:
    def test_rank_legs_returns_empty_when_all_fail_min_true_prob(self):
        """If all legs fail min_true_prob, ranked list is empty."""
        from ud_edge.models import Leg
        from ud_edge.matcher import rank_legs

        # A leg with even-money odds (true prob = 50%) that fails min_true_prob=0.55
        bad_leg = Leg(
            line_id="bad1", player_id="p1", player_name="Test Player",
            sport_id="NBA", match_id=1, match_title="TEST@TEST",
            stat_name="points", line_value=10.0, line_type="balanced",
            higher_american=-110, higher_decimal=1.10, higher_multiplier=0.5,
            lower_american=-110, lower_decimal=1.10, lower_multiplier=0.5,
        )
        ranked = rank_legs([bad_leg], break_even=0.5495, min_true_prob=0.55)
        assert ranked == [], f"Expected empty, got {ranked}"

    @patch('ud_edge.__main__.UDClient')
    @patch('ud_edge.injury_client.ESPNInjuryClient')
    @patch('ud_edge.sharp_books_client.build_sharp_index')
    def test_run_once_no_crash_on_empty_ranked(self, mock_sharp, mock_injury, mock_ud):
        """run_once must not crash when top is empty."""
        from ud_edge.__main__ import run_once

        # Mock data: leg with even-money odds (true prob 50%) → fails min_true_prob=0.70
        mock_data = {
            "players": [{"id": "p1", "first_name": "Test", "last_name": "Player",
                         "sport_id": "NBA", "team_id": "T1", "position_id": "1"}],
            "appearances": [{"id": "a1", "player_id": "p1", "match_id": 1,
                            "match_type": "full_game", "team_id": "T1"}],
            "games": [{"id": 1, "abbreviated_title": "TEST@TS2",
                       "full_team_names_title": "Test vs TS2",
                       "matchup_text": "TEST@TS2", "scheduled_at": "2027-01-01T00:00:00Z"}],
            "over_under_lines": [{
                "id": "l1", "line_type": "balanced",
                "over_under": {"appearance_stat": {"stat": "points", "appearance_id": "a1"}},
                "options": [
                    {"choice": "higher", "choice_display_name_shorter": "10+",
                     "american_price": -110, "decimal_price": 1.10, "payout_multiplier": 0.5},
                    {"choice": "lower", "choice_display_name_shorter": "10-",
                     "american_price": -110, "decimal_price": 1.10, "payout_multiplier": 0.5},
                ],
            }],
        }

        mock_client = MagicMock()
        mock_client.fetch.return_value = mock_data
        mock_ud.return_value = mock_client

        mock_injury_instance = MagicMock()
        mock_injury_instance.fetch_all_sports.return_value = {}
        mock_injury.return_value = mock_injury_instance

        mock_sharp.return_value = ({}, {})

        cache = Path("/tmp/test_cache.json")
        result = run_once(
            sport_filter=None,
            entry_type="6-flex",
            top_n=6,
            min_true_prob=0.70,  # intentionally high so nothing qualifies
            min_edge_pp=0.5,
            cache_path=cache,
            save_path=None,
            quiet=True,
            use_apisports=False,
            n_entries=1,
            full_game_only=False,
        )
        # Should exit cleanly with code 0, not crash
        assert result == 0

    @patch('ud_edge.__main__.UDClient')
    @patch('ud_edge.injury_client.ESPNInjuryClient')
    @patch('ud_edge.sharp_books_client.build_sharp_index')
    def test_run_once_per_entry_loop_handles_empty_top(self, mock_sharp, mock_injury, mock_ud):
        """Per-entry comparison loop must not crash when top is empty."""
        from ud_edge.__main__ import run_once

        mock_data = {
            "players": [{"id": "p1", "first_name": "Test", "last_name": "Player",
                         "sport_id": "NBA", "team_id": "T1", "position_id": "1"}],
            "appearances": [{"id": "a1", "player_id": "p1", "match_id": 1,
                            "match_type": "full_game", "team_id": "T1"}],
            "games": [{"id": 1, "abbreviated_title": "TEST@TS2",
                       "full_team_names_title": "Test vs TS2",
                       "matchup_text": "TEST@TS2", "scheduled_at": "2027-01-01T00:00:00Z"}],
            "over_under_lines": [{
                "id": "l1", "line_type": "balanced",
                "over_under": {"appearance_stat": {"stat": "points", "appearance_id": "a1"}},
                "options": [
                    {"choice": "higher", "choice_display_name_shorter": "10+",
                     "american_price": -110, "decimal_price": 1.10, "payout_multiplier": 0.5},
                    {"choice": "lower", "choice_display_name_shorter": "10-",
                     "american_price": -110, "decimal_price": 1.10, "payout_multiplier": 0.5},
                ],
            }],
        }

        mock_client = MagicMock()
        mock_client.fetch.return_value = mock_data
        mock_ud.return_value = mock_client

        mock_injury_instance = MagicMock()
        mock_injury_instance.fetch_all_sports.return_value = {}
        mock_injury.return_value = mock_injury_instance

        mock_sharp.return_value = ({}, {})

        cache = Path("/tmp/test_cache2.json")
        # Use n_entries > 1 to exercise the per-entry loop path
        result = run_once(
            sport_filter=None,
            entry_type="6-flex",
            top_n=6,
            min_true_prob=0.70,
            min_edge_pp=0.5,
            cache_path=cache,
            save_path=None,
            quiet=True,
            use_apisports=False,
            n_entries=4,  # multi-entry path
            full_game_only=False,
        )
        # Should not crash; graceful exit
        assert result in (0, 1)
