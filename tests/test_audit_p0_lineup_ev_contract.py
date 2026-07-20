"""Audit P0 fix: compare.py + dashboard/app.py lineup EV must use
effective_true_prob (sharp-authoritative) and expected_value_per_card
(heterogeneous exact EV), NOT a fantasy-prob average.

The dashboard reads `lineups[i].avg_true_prob`, `lineups[i].win_prob`,
`lineups[i].ev`, `lineups[i].median_payout`. These were computed from
the fantasy average — which overstates edge whenever sharp is slightly
bearish-but-in-band. They also collapsed heterogeneity (one averaged
probability used for all six legs), which underestimates variance and
breaks flex-payout assumptions.

These tests pin:
1. `avg_true_prob` is `effective_true_prob` averaged (sharp when matched)
2. `win_prob`, `ev`, `median_payout` come from `expected_value_per_card`
   with the same per-leg effective probs
3. The contract applies to both compare.py's compare_fantasy_vs_sharp
   and dashboard/app.py's /api/lineups handler
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from ud_edge.flex_math import UD_PAYOUTS, expected_value_per_card
from ud_edge.matcher import effective_true_prob


# ── expected_value_per_card contract (heterogeneous EV) ───────────────────────


def test_expected_value_per_card_handles_heterogeneous_probs():
    """6-flex with leg probs [0.58, 0.58, 0.58, 0.58, 0.58, 0.565] vs all 0.58.

    The two versions should NOT match — heterogeneous probabilities change
    the win-prob and EV calculations because the joint distribution is
    different. A card with one leg at 56.5% (sharp-corrected) and the rest
    at 58% has lower win probability than a uniform 58% card.
    """
    entry = UD_PAYOUTS["6-flex"]
    uniform = [0.58] * 6
    heterogeneous = [0.58, 0.58, 0.58, 0.58, 0.58, 0.565]

    ev_u, win_u, med_u = expected_value_per_card(entry, uniform)
    ev_h, win_h, med_h = expected_value_per_card(entry, heterogeneous)

    # Heterogeneous should have lower win_prob because one leg is worse
    assert win_h < win_u, (
        f"Heterogeneous card (one leg at 56.5%) should have lower win_prob "
        f"than uniform 58%. Got win_u={win_u:.4f}, win_h={win_h:.4f}"
    )
    # And lower EV
    assert ev_h < ev_u, (
        f"Heterogeneous card should have lower EV. Got ev_u={ev_u:.4f}, "
        f"ev_h={ev_h:.4f}"
    )


def test_expected_value_per_card_matches_uniform_when_legs_identical():
    """Sanity: heterogeneous with all-identical probs must equal uniform.

    expected_value_per_card is the exact heterogeneous EV; for a uniform
    card it must produce the same numbers as the analytical expected_value.
    """
    from ud_edge.flex_math import expected_value
    entry = UD_PAYOUTS["6-flex"]
    probs = [0.58] * 6
    ev_h, win_h, med_h = expected_value_per_card(entry, probs)
    ev_a, win_a, med_a = expected_value(entry, 0.58)

    assert abs(ev_h - ev_a) < 1e-9, f"EV mismatch: {ev_h} vs {ev_a}"
    assert abs(win_h - win_a) < 1e-9, f"Win prob mismatch: {win_h} vs {win_a}"


# ── compare.py contract ───────────────────────────────────────────────────────


def test_compare_fantasy_vs_sharp_lineup_avg_uses_effective_prob(monkeypatch):
    """compare_fantasy_vs_sharp must compute lineup avg_true_prob from
    effective_true_prob, not raw picked_true_prob.

    Stub the live pipeline so we can supply a known ranked list, then
    inspect the returned payload's `lineups[i].avg_true_prob` value.
    """
    from ud_edge import compare
    from ud_edge.models import Leg, RankedLeg

    # Six legs: fantasy 58%, sharp 56.5% (sharp inside ±2pp band)
    leg = Leg(
        player_id="p_x", player_name="X", sport_id="NBA",
        stat_name="points", line_value=20.5, line_id="l_x",
        line_type="balanced",
        higher_american=-110, higher_decimal=1.91, higher_multiplier=0.95,
        lower_american=-110, lower_decimal=1.91, lower_multiplier=0.95,
        fantasy_source="underdog",
    )
    ranked = [
        RankedLeg(
            leg=leg,
            higher_true_prob=0.42, higher_implied_prob=0.44, higher_edge_pp=-12.0,
            lower_true_prob=0.58, lower_implied_prob=0.60, lower_edge_pp=3.79,
            picked_side="lower", picked_true_prob=0.58,
            picked_edge_pp=3.79, overround=1.05,
            sharp_true_prob=0.565, sharp_book="manual-csv",
            mispricing_edge_pp=-1.5,
        )
        for _ in range(6)
    ]

    # Stub the live pipeline to return our fixture
    monkeypatch.setattr(compare, "collect_fantasy_legs", lambda **kw: ([], {}))
    monkeypatch.setattr(compare, "build_sharp_index", lambda **kw: ({}, {}))
    monkeypatch.setattr(compare, "rank_legs", lambda *a, **kw: ranked)
    monkeypatch.setattr(compare, "build_lineups", lambda r, **kw: [ranked[:6]])

    payload = compare.compare_fantasy_vs_sharp(
        entry_type="6-flex",
        n_entries=1,
        sport_filter=set(),
        force_fetch=False,
    )

    lineup = payload["lineups"][0]
    # Effective avg = 0.565 (sharp wins). Old fantasy-only avg = 0.58.
    assert lineup["avg_true_prob"] == pytest.approx(0.565, abs=1e-4), (
        f"compare_fantasy_vs_sharp lineup avg_true_prob must use "
        f"effective_true_prob. Expected 0.565 (sharp), got "
        f"{lineup['avg_true_prob']} (raw fantasy would be 0.58)."
    )


def test_compare_fantasy_vs_sharp_lineup_ev_uses_per_card(monkeypatch):
    """compare_fantasy_vs_sharp must compute win_prob/ev/median_payout
    from expected_value_per_card (heterogeneous), not expected_value
    (uniform average).
    """
    from ud_edge import compare
    from ud_edge.models import Leg, RankedLeg

    leg = Leg(
        player_id="p_x", player_name="X", sport_id="NBA",
        stat_name="points", line_value=20.5, line_id="l_x",
        line_type="balanced",
        higher_american=-110, higher_decimal=1.91, higher_multiplier=0.95,
        lower_american=-110, lower_decimal=1.91, lower_multiplier=0.95,
        fantasy_source="underdog",
    )
    # Heterogeneous: 5 legs at 58%, 1 leg at 56.5%
    legs_list = []
    for i in range(6):
        sharp = 0.58 if i < 5 else 0.565
        legs_list.append(RankedLeg(
            leg=leg,
            higher_true_prob=1 - sharp, higher_implied_prob=1 - sharp + 0.02,
            higher_edge_pp=(1 - sharp - 0.5421) * 100,
            lower_true_prob=sharp, lower_implied_prob=sharp + 0.02,
            lower_edge_pp=(sharp - 0.5421) * 100,
            picked_side="lower", picked_true_prob=sharp,
            picked_edge_pp=(sharp - 0.5421) * 100, overround=1.05,
            sharp_true_prob=sharp, sharp_book="manual-csv",
            mispricing_edge_pp=0.0,
        ))

    monkeypatch.setattr(compare, "collect_fantasy_legs", lambda **kw: ([], {}))
    monkeypatch.setattr(compare, "build_sharp_index", lambda **kw: ({}, {}))
    monkeypatch.setattr(compare, "rank_legs", lambda *a, **kw: legs_list)
    monkeypatch.setattr(compare, "build_lineups", lambda r, **kw: [legs_list[:6]])

    payload = compare.compare_fantasy_vs_sharp(
        entry_type="6-flex",
        n_entries=1,
        sport_filter=set(),
        force_fetch=False,
    )

    lineup = payload["lineups"][0]
    # Expected per-card EV with heterogeneous probs
    expected_ev, expected_win, expected_med = expected_value_per_card(
        UD_PAYOUTS["6-flex"], [0.58, 0.58, 0.58, 0.58, 0.58, 0.565]
    )
    assert lineup["win_prob"] == pytest.approx(round(expected_win, 4), abs=1e-4)
    assert lineup["ev"] == pytest.approx(round(expected_ev, 4), abs=1e-4)
    assert lineup["median_payout"] == pytest.approx(expected_med, abs=1e-4)


# ── dashboard/app.py contract ─────────────────────────────────────────────────


def test_dashboard_lineup_handler_uses_effective_prob(monkeypatch):
    """The /api/lineups handler in dashboard/app.py must apply the same
    effective_true_prob contract for avg_true_prob.
    """
    from fastapi.testclient import TestClient

    from ud_edge.dashboard import app as dash_app
    from ud_edge.models import Leg, RankedLeg

    leg = Leg(
        player_id="p_x", player_name="X", sport_id="NBA",
        stat_name="points", line_value=20.5, line_id="l_x",
        line_type="balanced",
        higher_american=-110, higher_decimal=1.91, higher_multiplier=0.95,
        lower_american=-110, lower_decimal=1.91, lower_multiplier=0.95,
        fantasy_source="underdog",
    )
    ranked = [
        RankedLeg(
            leg=leg,
            higher_true_prob=0.42, higher_implied_prob=0.44, higher_edge_pp=-12.0,
            lower_true_prob=0.58, lower_implied_prob=0.60, lower_edge_pp=3.79,
            picked_side="lower", picked_true_prob=0.58,
            picked_edge_pp=3.79, overround=1.05,
            sharp_true_prob=0.565, sharp_book="manual-csv",
            mispricing_edge_pp=-1.5,
        )
        for _ in range(6)
    ]

    # Stub select_lineups_for_card to return our ranked list
    from ud_edge.lineup_selector import LineupSelectorResult
    from ud_edge import lineup_selector
    monkeypatch.setattr(
        lineup_selector, "select_lineups_for_card",
        lambda *a, **kw: LineupSelectorResult(lineups=[ranked], dropped_legs=[])
    )

    # Pre-populate the dashboard's _RANKED_CACHE AND _CACHE so /api/lineups
    # returns our fixture instead of "no data" 404.
    from ud_edge.dashboard.app import _CACHE, _RANKED_CACHE, _cache_key
    cache_key = _cache_key(
        entry="6-flex", min_true_prob=0.6, min_edge_pp=0.5,
        sport="", full_game_only=True, mispriced_only=False, n_entries=1,
    )
    _RANKED_CACHE[cache_key] = ranked
    _CACHE["key"] = cache_key
    _CACHE["payload"] = {"lineups": [], "flat": []}
    client = TestClient(dash_app.app)
    resp = client.get("/api/lineups?n_entries=1&entry_type=6-flex&min_true_prob=0.6")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    lineup = body["lineups"][0]
    assert lineup["avg_true_prob"] == pytest.approx(0.565, abs=1e-4), (
        f"Dashboard /api/lineups avg_true_prob must use effective_true_prob. "
        f"Expected 0.565 (sharp), got {lineup['avg_true_prob']}."
    )


def test_dashboard_lineup_handler_uses_per_card_ev(monkeypatch):
    """The /api/lineups handler must compute win_prob/ev from
    expected_value_per_card, not the homogeneous expected_value.
    """
    from fastapi.testclient import TestClient

    from ud_edge.dashboard import app as dash_app
    from ud_edge.models import Leg, RankedLeg

    leg = Leg(
        player_id="p_x", player_name="X", sport_id="NBA",
        stat_name="points", line_value=20.5, line_id="l_x",
        line_type="balanced",
        higher_american=-110, higher_decimal=1.91, higher_multiplier=0.95,
        lower_american=-110, lower_decimal=1.91, lower_multiplier=0.95,
        fantasy_source="underdog",
    )
    # Heterogeneous: 5 legs at 58%, 1 leg at 56.5%
    legs_list = []
    for i in range(6):
        sharp = 0.58 if i < 5 else 0.565
        legs_list.append(RankedLeg(
            leg=leg,
            higher_true_prob=1 - sharp, higher_implied_prob=1 - sharp + 0.02,
            higher_edge_pp=(1 - sharp - 0.5421) * 100,
            lower_true_prob=sharp, lower_implied_prob=sharp + 0.02,
            lower_edge_pp=(sharp - 0.5421) * 100,
            picked_side="lower", picked_true_prob=sharp,
            picked_edge_pp=(sharp - 0.5421) * 100, overround=1.05,
            sharp_true_prob=sharp, sharp_book="manual-csv",
            mispricing_edge_pp=0.0,
        ))

    from ud_edge.lineup_selector import LineupSelectorResult
    from ud_edge import lineup_selector
    monkeypatch.setattr(
        lineup_selector, "select_lineups_for_card",
        lambda *a, **kw: LineupSelectorResult(lineups=[legs_list[:6]], dropped_legs=[])
    )

    from ud_edge.dashboard.app import _CACHE, _RANKED_CACHE, _cache_key
    cache_key = _cache_key(
        entry="6-flex", min_true_prob=0.6, min_edge_pp=0.5,
        sport="", full_game_only=True, mispriced_only=False, n_entries=1,
    )
    _RANKED_CACHE[cache_key] = legs_list[:6]
    _CACHE["key"] = cache_key
    _CACHE["payload"] = {"lineups": [], "flat": []}
    client = TestClient(dash_app.app)
    resp = client.get("/api/lineups?n_entries=1&entry_type=6-flex&min_true_prob=0.6")
    body = resp.json()
    lineup = body["lineups"][0]

    expected_ev, expected_win, _ = expected_value_per_card(
        UD_PAYOUTS["6-flex"], [0.58, 0.58, 0.58, 0.58, 0.58, 0.565]
    )
    assert lineup["win_prob"] == pytest.approx(round(expected_win, 4), abs=1e-4)
    assert lineup["ev"] == pytest.approx(round(expected_ev, 4), abs=1e-4)