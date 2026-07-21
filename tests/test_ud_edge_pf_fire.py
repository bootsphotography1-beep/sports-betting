"""Tests for `scripts/ud_edge_pf_fire.py` — PF-specific mispricing fire."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import ud_edge_pf_fire
from scripts.ud_edge_pf_fire import reroute_to_pf, format_pf_message


def _make_leg(
    sport: str = "MLB",
    player: str = "Test Player",
    *,
    ev: float = 5.0,
    edge_kind: str = "vs_sharp",
    sharp_book: str = "pinnacle",
    sharp_prob: float = 60.0,
    fantasy_book: str = "sleeper",
    fantasy_prob: float = 55.0,
    side: str = "Under",
    stat: str = "hits",
    line: float = 1.5,
):
    """Helper to build a leg dict for reroute_to_pf tests."""
    return {
        "player": player,
        "sport": sport,
        "stat": stat,
        "line": line,
        "side_label": side,
        "ev": ev,
        "edge_kind": edge_kind,
        "sharp_book": sharp_book,
        "sharp_prob": sharp_prob,
        "fantasy_book": fantasy_book,
        "fantasy_prob": fantasy_prob,
        "all_fantasy_books": {
            "prizepicks": fantasy_prob / 100,  # default; tests can override
            "sleeper": fantasy_prob / 100,
            "underdog": (fantasy_prob + 5) / 100,
        },
    }


# ── reroute_to_pf: vs_sharp legs ──────────────────────────────────────


def test_reroute_to_pf_vs_sharp_keeps_pf_legs():
    """A vs_sharp leg with PF coverage → routed to PF, EV recomputed."""
    legs = [_make_leg(player="Bo Bichette", sharp_prob=63.7, fantasy_prob=60.5)]
    pf_legs = reroute_to_pf(legs)
    assert len(pf_legs) == 1
    leg = pf_legs[0]
    assert leg["fantasy_book"] == "prizepicks"
    assert leg["fantasy_prob"] == 60.5
    # Edge = sharp - PF - friction = 63.7 - 60.5 - 1 = +2.2pp
    assert leg["ev"] == 2.2
    assert leg["edge_kind"] == "vs_sharp_pf"


def test_reroute_to_pf_drops_legs_without_pf_coverage():
    """A leg with all_fantasy_books[prizepicks] = None → dropped."""
    leg = _make_leg(player="Bo Bichette")
    leg["all_fantasy_books"]["prizepicks"] = None
    pf_legs = reroute_to_pf([leg])
    assert len(pf_legs) == 0


def test_reroute_to_pf_drops_negative_edge_legs():
    """PF overpriced vs sharp → negative edge → dropped."""
    # Sharp 50%, PF 60% (overpriced) → edge = 50 - 60 - 1 = -11pp → dropped
    leg = _make_leg(player="Bo Bichette", sharp_prob=50.0, fantasy_prob=60.0)
    pf_legs = reroute_to_pf([leg])
    assert len(pf_legs) == 0


def test_reroute_to_pf_flags_synthetic_default():
    """PF=50.0% (the parser's synthetic default when side is absent) gets a
    pf_synthetic_default flag set to True.
    """
    leg = _make_leg(player="Jake McCarthy", sharp_prob=59.2, fantasy_prob=50.0)
    pf_legs = reroute_to_pf([leg])
    assert len(pf_legs) == 1
    assert pf_legs[0]["pf_synthetic_default"] is True


def test_reroute_to_pf_no_synthetic_flag_for_real_pf_price():
    """PF != 50.0% (a real price) → flag is False."""
    leg = _make_leg(player="Bo Bichette", sharp_prob=63.7, fantasy_prob=60.5)
    pf_legs = reroute_to_pf([leg])
    assert pf_legs[0]["pf_synthetic_default"] is False


# ── reroute_to_pf: fantasy-only legs ──────────────────────────────────


def test_reroute_to_pf_drops_fantasy_only_by_default():
    """Fantasy-only legs (no sharp) are dropped unless include_fantasy_only=True."""
    leg = _make_leg(
        edge_kind="vs_breakeven",
        sharp_prob=None,
        sharp_book="?",
        fantasy_prob=58.0,  # > 54.21 break-even
    )
    pf_legs = reroute_to_pf([leg])
    assert len(pf_legs) == 0


def test_reroute_to_pf_includes_fantasy_only_when_flagged():
    """With include_fantasy_only=True, fantasy-only PF legs above break-even pass."""
    leg = _make_leg(
        edge_kind="vs_breakeven",
        sharp_prob=None,
        sharp_book="?",
        fantasy_prob=58.0,
    )
    pf_legs = reroute_to_pf([leg], include_fantasy_only=True)
    assert len(pf_legs) == 1
    # Edge = PF - break_even - friction = 58 - 54.21 - 1 = +2.79pp
    assert abs(pf_legs[0]["ev"] - 2.79) < 0.01
    assert pf_legs[0]["edge_kind"] == "vs_breakeven_pf"


# ── format_pf_message ────────────────────────────────────────────────


def test_format_pf_message_appends_synthetic_warning_section():
    """When any leg has pf_synthetic_default=True, a PF-SYNTHETIC warning
    section is appended to the message body.
    """
    legs = [_make_leg(player="Jake McCarthy", sharp_prob=59.2, fantasy_prob=50.0)]
    pf_legs = reroute_to_pf(legs)
    msg = format_pf_message("evening", pf_legs, min_edge_pp=0.5)
    assert "PF-SYNTHETIC" in msg
    assert "Jake McCarthy" in msg


def test_format_pf_message_no_synthetic_warning_for_clean_legs():
    """Legs with PF != 50.0% do NOT trigger the synthetic warning section."""
    legs = [_make_leg(player="Bo Bichette", sharp_prob=63.7, fantasy_prob=60.5)]
    pf_legs = reroute_to_pf(legs)
    msg = format_pf_message("evening", pf_legs, min_edge_pp=0.5)
    assert "PF-SYNTHETIC" not in msg


def test_format_pf_message_restores_tier_threshold():
    """After calling format_pf_message, the original tier threshold is restored
    so subsequent calls in the same process aren't affected.
    """
    from scripts.ud_edge_fire import TIERS
    orig_evening = TIERS["evening"]
    legs = [_make_leg(player="Bo Bichette", sharp_prob=63.7, fantasy_prob=60.5)]
    pf_legs = reroute_to_pf(legs)
    format_pf_message("evening", pf_legs, min_edge_pp=0.5)
    # Threshold must be back to the original
    assert TIERS["evening"] == orig_evening