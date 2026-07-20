"""Tests for Wave 3B: sport aliases in ud_client."""
import pytest
from ud_edge.ud_client import resolve_sport_filter, ALIASES


class TestSportAliases:
    def test_nba_alias_resolves_both_ways(self):
        """Passing {'NBA'} should match legs with sport_id='NBA' AND 'BASKETBALL'."""
        expanded = resolve_sport_filter({'NBA'})
        assert 'NBA' in expanded
        assert 'BASKETBALL' in expanded

    def test_basketball_alias_expands_to_nba(self):
        """Passing {'BASKETBALL'} should also include NBA legs."""
        expanded = resolve_sport_filter({'BASKETBALL'})
        assert 'BASKETBALL' in expanded
        assert 'NBA' in expanded

    def test_wnba_alias_includes_basketball(self):
        """WNBA games on UD may return sport_id='BASKETBALL', so alias includes it."""
        expanded = resolve_sport_filter({'WNBA'})
        assert 'WNBA' in expanded
        assert 'BASKETBALL' in expanded

    def test_wnba_alias_not_just_nba(self):
        """WNBA should NOT map to NBA — they are separate leagues."""
        expanded = resolve_sport_filter({'WNBA'})
        assert 'NBA' not in expanded

    def test_multiple_sports_expanded(self):
        """Passing NFL and MLB should expand correctly."""
        expanded = resolve_sport_filter({'NFL', 'MLB'})
        assert 'NFL' in expanded
        assert 'MLB' in expanded

    def test_empty_filter_returns_empty(self):
        """No filter applied returns empty set."""
        assert resolve_sport_filter(None) == set()
        assert resolve_sport_filter(set()) == set()

    def test_aliases_defined_for_common_leagues(self):
        """ALIASES should contain entries for NBA, WNBA, NFL, MLB, NHL."""
        assert 'NBA' in ALIASES
        assert 'WNBA' in ALIASES
        assert 'NFL' in ALIASES
        assert 'MLB' in ALIASES
        assert 'NHL' in ALIASES
