"""Unit tests for owned sportsbook scrapers (no network)."""
from ud_edge.book_scrapers import (
    milestone_label_to_line,
    _synthetic_under_decimal,
)
from ud_edge.no_vig import no_vig
from ud_edge.sharp_books_client import _index_prop_rows, _to_decimal


def test_milestone_label_to_line():
    assert milestone_label_to_line("1+") == 0.5
    assert milestone_label_to_line("2+") == 1.5
    assert milestone_label_to_line("3+") == 2.5
    assert milestone_label_to_line("Over") is None
    assert milestone_label_to_line("") is None


def test_synthetic_under_forces_unit_overround():
    over_dec = _to_decimal(-150)  # ~1.6667
    assert over_dec is not None
    under_dec = _synthetic_under_decimal(over_dec)
    true_over, true_under, overround = no_vig(over_dec, under_dec)
    assert abs(overround - 1.0) < 1e-9
    assert abs(true_over - (1.0 / over_dec)) < 1e-9
    assert abs(true_over + true_under - 1.0) < 1e-9


def test_index_prop_rows_prefers_draftkings():
    index: dict = {}
    props = [
        {
            "player": "Juan Soto",
            "stat": "hits",
            "line": 1.5,
            "over_decimal": 1.80,
            "under_decimal": 2.25,
            "bookmaker": "fanduel",
            "source": "scraper-fd-milestone",
        },
        {
            "player": "Juan Soto",
            "stat": "hits",
            "line": 1.5,
            "over_decimal": 1.91,
            "under_decimal": 2.10,
            "bookmaker": "draftkings",
            "source": "scraper-dk-milestone",
        },
    ]
    n = _index_prop_rows(
        index, props,
        source_default="scraper",
        book_priority=["draftkings", "fanduel"],
    )
    assert n == 2
    assert "juan soto|hits|1.5" in index
    # Last write wins for line-specific key
    assert index["juan soto|hits|1.5"]["bookmaker"] == "draftkings"
    # Base key prefers higher-priority book (draftkings)
    assert index["juan soto|hits"]["bookmaker"] == "draftkings"
