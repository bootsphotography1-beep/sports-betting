"""Audit P1 #6 completion (remediation v3): line_tolerance plumbed through the
full pipeline + match_distance surfaced on RankedLeg + dashboard JSON.

Original P1 #6 (remediation v2) wired line_tolerance into rank_legs and CLI but
left three gaps:
1. compare_fantasy_vs_sharp didn't accept/forward line_tolerance
2. Poller didn't forward line_tolerance
3. SharpMatch.match_distance was computed but never copied onto RankedLeg or
   serialized into dashboard JSON.

This file pins all three.
"""
from __future__ import annotations

from pathlib import Path
import inspect

ROOT = Path(__file__).resolve().parents[1]


# ── 1. compare_fantasy_vs_sharp accepts line_tolerance ───────────────────────


def test_compare_fantasy_vs_sharp_accepts_line_tolerance():
    """compare_fantasy_vs_sharp must accept line_tolerance as a kwarg so the
    dashboard / poller / API can forward the operator's choice."""
    from ud_edge.compare import compare_fantasy_vs_sharp
    sig = inspect.signature(compare_fantasy_vs_sharp)
    assert "line_tolerance" in sig.parameters, (
        "compare_fantasy_vs_sharp does not accept line_tolerance kwarg. "
        "Dashboard / poller / API are stuck on the module default."
    )
    # Default should be None (meaning "use LINE_TOLERANCE constant")
    default = sig.parameters["line_tolerance"].default
    assert default is None, (
        f"line_tolerance default should be None (so we fall back to "
        f"LINE_TOLERANCE constant), got {default!r}."
    )


# ── 2. Poller forwards line_tolerance ────────────────────────────────────────


def test_poller_forwards_line_tolerance_env():
    """The poller must read UD_LINE_TOLERANCE env var and forward it."""
    from pathlib import Path as _P
    text = (_P(__file__).resolve().parents[1] / "ud_edge" / "poller.py").read_text(
        encoding="utf-8"
    )
    assert "UD_LINE_TOLERANCE" in text, (
        "poller.py does not read UD_LINE_TOLERANCE env var."
    )
    assert "line_tolerance=" in text, (
        "poller.py does not forward line_tolerance= to compare_fantasy_vs_sharp."
    )


# ── 3. match_distance on RankedLeg + dashboard JSON ──────────────────────────


def test_ranked_leg_has_match_distance_field():
    """RankedLeg must carry match_distance so the dashboard can surface it."""
    from ud_edge.models import RankedLeg
    assert "match_distance" in RankedLeg.model_fields, (
        "RankedLeg is missing match_distance. Dashboard cannot surface "
        "fuzzy-match gap to operators."
    )


def test_matcher_propagates_match_distance_to_ranked_leg():
    """When find_sharp_match returns a SharpMatch with match_distance > 0
    (fuzzy match), rank_legs must copy that onto the RankedLeg.
    """
    from unittest.mock import patch
    from ud_edge.matcher import rank_legs
    from ud_edge.models import Leg
    from ud_edge.sharp_books_client import SharpMatch

    # Build a fantasy leg with a non-integer line that won't have an exact
    # sharp match.
    # scheduled_at must be strictly in the future so Wave 2B's reject_started
    # filter does not skip the leg before fuzzy matching runs.
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    leg = Leg(
        line_id="line_md", player_id="p_md", player_name="Player MD",
        sport_id="NBA", match_title="A @ B", match_id=1,
        scheduled_at=future,
        stat_name="points", line_value=27.5, line_type="balanced",
        higher_american=-110, higher_decimal=1.909, higher_multiplier=0.95,
        lower_american=-110, lower_decimal=1.909, lower_multiplier=0.95,
    )

    # Build a fake SharpMatch at a fuzzy gap of 1.5 (line_value=29.0 in sharp)
    fake_match = SharpMatch(
        sharp_for_higher=0.55, sharp_for_lower=0.45,
        both_sides_within_tolerance=False,  # fuzzy match (audit-relevant)
        over_decimal=1.909, under_decimal=1.909,
        bookmaker="Pinnacle", line_value=29.0,
        match_distance=1.5,  # <-- the audit-relevant field
    )

    # Build a sharp_book_index keyed under the normalized key for Player MD + points.
    # We don't care about the key shape — find_sharp_match is mocked.
    sharp_index = {"_ignored_": fake_match}

    with patch(
        "ud_edge.sharp_books_client.find_sharp_match",
        return_value=fake_match,
    ):
        ranked = rank_legs(
            [leg],
            break_even=0.5421,
            min_true_prob=0.0,
            min_edge_pp=-100.0,  # accept everything
            sharp_book_index=sharp_index,
            line_tolerance=2.0,  # allow the 1.5 gap
        )

    assert ranked, "rank_legs returned no legs"
    rl = ranked[0]
    assert rl.match_distance is not None, (
        "rank_legs did not propagate match_distance from SharpMatch to RankedLeg."
    )
    assert rl.match_distance == 1.5, (
        f"Expected match_distance=1.5 (fuzzy gap), got {rl.match_distance}."
    )


def test_opportunities_to_dict_emits_match_distance():
    """opportunities_to_dict must include match_distance in the dashboard JSON."""
    from ud_edge.copy_format import opportunities_to_dict
    from ud_edge.models import Leg, RankedLeg

    leg = Leg(
        line_id="L", player_id="P", player_name="P",
        sport_id="NBA", match_title="A @ B", match_id=1,
        scheduled_at="2026-07-20T20:00:00+00:00",
        stat_name="points", line_value=27.5, line_type="balanced",
        higher_american=-110, higher_decimal=1.909, higher_multiplier=0.95,
        lower_american=-110, lower_decimal=1.909, lower_multiplier=0.95,
    )
    rl = RankedLeg(
        leg=leg,
        higher_true_prob=0.55, higher_implied_prob=0.524, higher_edge_pp=0.6,
        lower_true_prob=0.45, lower_implied_prob=0.524, lower_edge_pp=-7.4,
        picked_side="higher", picked_true_prob=0.55, picked_edge_pp=0.6,
        overround=1.05,
        sharp_true_prob=0.60, sharp_book="Pinnacle",
        match_distance=1.5,
    )

    d = opportunities_to_dict(rl, break_even=0.5421)
    assert "match_distance" in d, (
        f"opportunities_to_dict output missing match_distance. "
        f"Keys: {sorted(d.keys())}"
    )
    assert d["match_distance"] == 1.5, (
        f"Expected match_distance=1.5 in dict, got {d.get('match_distance')}"
    )


def test_opportunities_to_dict_match_distance_is_none_when_unmatched():
    """match_distance should be None when no sharp match exists (not silently 0)."""
    from ud_edge.copy_format import opportunities_to_dict
    from ud_edge.models import Leg, RankedLeg

    leg = Leg(
        line_id="L", player_id="P", player_name="P",
        sport_id="NBA", match_title="A @ B", match_id=1,
        scheduled_at="2026-07-20T20:00:00+00:00",
        stat_name="points", line_value=27.5, line_type="balanced",
        higher_american=-110, higher_decimal=1.909, higher_multiplier=0.95,
        lower_american=-110, lower_decimal=1.909, lower_multiplier=0.95,
    )
    rl = RankedLeg(
        leg=leg,
        higher_true_prob=0.55, higher_implied_prob=0.524, higher_edge_pp=0.6,
        lower_true_prob=0.45, lower_implied_prob=0.524, lower_edge_pp=-7.4,
        picked_side="higher", picked_true_prob=0.55, picked_edge_pp=0.6,
        overround=1.05,
        sharp_true_prob=None, sharp_book=None,
        # match_distance left as None (default)
    )

    d = opportunities_to_dict(rl, break_even=0.5421)
    assert d.get("match_distance") is None, (
        f"match_distance should be None when no sharp match, got "
        f"{d.get('match_distance')}"
    )


# ── 4. Dashboard /api/opportunities accepts line_tolerance query param ───────


def test_dashboard_opportunities_endpoint_has_line_tolerance_param():
    """The dashboard /api/opportunities endpoint must accept line_tolerance."""
    text = (ROOT / "ud_edge" / "dashboard" / "app.py").read_text(encoding="utf-8")
    # Find the opportunities() function signature up to the line containing
    # just "):" at the function's own indent level.
    import re
    m = re.search(r"def opportunities\((.*?)^    \):", text, re.DOTALL | re.MULTILINE)
    assert m, "Could not locate opportunities() signature"
    sig = m.group(1)
    assert "line_tolerance" in sig, (
        "opportunities() signature does not include line_tolerance. "
        "Dashboard cannot forward operator's choice."
    )