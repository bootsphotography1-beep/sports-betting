"""Tests for DFS-vs-sharp misprice scanner (no network)."""
from ud_edge.misprice import (
    clean_player_name,
    find_misprices,
    format_misprice_report,
    is_even_money_shell,
)


def test_clean_player_name_strips_team():
    assert clean_player_name("Anthony Volpe (NYY)") == "Anthony Volpe"
    assert clean_player_name("Juan Soto") == "Juan Soto"


def test_even_money_shell():
    assert is_even_money_shell(2.0, 2.0)
    assert not is_even_money_shell(1.91, 1.91)


def test_line_gap_misprice_over_easier_on_dfs():
    """DFS Over 0.5 vs sharp main 1.5 → +1.0 line gap on Over."""
    dfs = [{
        "player": "Juan Soto",
        "stat": "hits",
        "line": 0.5,
        "over_decimal": 2.0,
        "under_decimal": 2.0,
        "bookmaker": "prizepicks",
        "sport": "MLB",
    }]
    sharp = [{
        "player": "Juan Soto",
        "stat": "hits",
        "line": 1.5,
        "over_decimal": 1.91,
        "under_decimal": 1.91,
        "bookmaker": "pinnacle",
        "sport": "MLB",
    }]
    hits = find_misprices(dfs, sharp, min_line_gap=0.5, min_prob_edge_pp=99)
    overs = [m for m in hits if m.side == "over"]
    assert overs
    assert overs[0].line_gap == 1.0
    assert overs[0].dfs_book == "prizepicks"
    assert overs[0].sharp_book == "pinnacle"
    assert overs[0].kind == "line"


def test_ignores_extreme_alt_line_gaps():
    """BetMGM 6.5 vs DFS 1.5 is not a real soft edge — ignore (> max_line_gap)."""
    dfs = [{
        "player": "Jac Caglianone", "stat": "total_bases", "line": 1.5,
        "over_decimal": 2.0, "under_decimal": 2.0,
        "bookmaker": "prizepicks", "sport": "MLB",
    }]
    sharp = [{
        "player": "Jac Caglianone", "stat": "total_bases", "line": 6.5,
        "over_decimal": 1.87, "under_decimal": 1.87,
        "bookmaker": "betmgm", "sport": "MLB",
    }]
    hits = find_misprices(dfs, sharp, min_line_gap=0.5, max_line_gap=2.0, min_prob_edge_pp=99)
    assert hits == []


def test_prob_misprice_same_line():
    """Same line: sharp more confident on Over than Underdog."""
    # UD: over 2.10 (~47.6% raw), under 1.70 → after no-vig Over ~43%
    # Pin: over 1.50 (~66.7%), under 2.60 → after no-vig Over ~63%
    dfs = [{
        "player": "Shohei Ohtani",
        "stat": "total_bases",
        "line": 1.5,
        "over_decimal": 2.10,
        "under_decimal": 1.70,
        "bookmaker": "underdog",
        "sport": "MLB",
    }]
    sharp = [{
        "player": "Shohei Ohtani",
        "stat": "total_bases",
        "line": 1.5,
        "over_decimal": 1.50,
        "under_decimal": 2.60,
        "bookmaker": "pinnacle",
        "sport": "MLB",
    }]
    hits = find_misprices(dfs, sharp, min_line_gap=9.0, min_prob_edge_pp=2.0)
    overs = [m for m in hits if m.side == "over" and m.kind == "prob"]
    assert overs
    assert overs[0].prob_edge_pp is not None
    assert overs[0].prob_edge_pp > 2.0
    assert overs[0].dfs_book == "underdog"


def test_dabble_and_sleeper_included():
    dfs = [
        {
            "player": "Aaron Judge", "stat": "home_runs", "line": 0.5,
            "over_decimal": 2.5, "under_decimal": 1.5,
            "bookmaker": "dabble", "sport": "MLB",
        },
        {
            "player": "Aaron Judge", "stat": "home_runs", "line": 0.5,
            "over_decimal": 2.4, "under_decimal": 1.55,
            "bookmaker": "sleeper", "sport": "MLB",
        },
    ]
    sharp = [{
        "player": "Aaron Judge", "stat": "home_runs", "line": 0.5,
        "over_decimal": 1.70, "under_decimal": 2.20,
        "bookmaker": "draftkings", "sport": "MLB",
    }]
    hits = find_misprices(dfs, sharp, min_line_gap=9.0, min_prob_edge_pp=1.0)
    books = {m.dfs_book for m in hits}
    assert "dabble" in books
    assert "sleeper" in books


def test_flipped_betmgm_dropped_when_dk_disagrees():
    """BetMGM with inverted O/U must not create a fake DFS edge."""
    dfs = [{
        "player": "Caleb Durbin", "stat": "rbis", "line": 0.5,
        "over_decimal": 3.33, "under_decimal": 1.44,
        "bookmaker": "dabble", "sport": "MLB",
    }]
    sharp = [
        {
            "player": "Caleb Durbin", "stat": "rbis", "line": 0.5,
            "over_decimal": 3.20, "under_decimal": 1.32,
            "bookmaker": "draftkings", "sport": "MLB",
        },
        {
            "player": "Caleb Durbin", "stat": "rbis", "line": 0.5,
            "over_decimal": 1.44, "under_decimal": 2.65,
            "bookmaker": "betmgm", "sport": "MLB",
        },
    ]
    hits = find_misprices(dfs, sharp, min_line_gap=9.0, min_prob_edge_pp=2.0)
    # Consensus follows DK (~29% Over); Dabble is similar → no big Over edge
    overs = [m for m in hits if m.side == "over"]
    assert not overs or (overs[0].prob_edge_pp or 0) < 5.0


def test_format_report_empty():
    text = format_misprice_report([])
    assert "No misprices" in text

