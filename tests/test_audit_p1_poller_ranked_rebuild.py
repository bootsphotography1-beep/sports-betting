"""Audit P1 #5 fix: poller must use the live RankedLeg list returned by
compare_fantasy_vs_sharp(return_ranked=True) instead of reconstructing
RankedLeg from the JSON-serialized 'flat' list.

The reconstruction path drops fields (match_id, team_id, fantasy_source, etc.)
and can silently under-alert or mis-key cooldown. The dashboard already
fixed this in b045758; the poller still has the bug.

These tests pin:
1. poller._run_poll_cycle passes return_ranked=True to compare_fantasy_vs_sharp
2. poller uses the returned RankedLeg list (no JSON reconstruction)
3. all fields on RankedLeg are preserved end-to-end (especially sharp_true_prob,
   sharp_book, mispricing_edge_pp which are needed for alert logic)
"""
from __future__ import annotations

from pathlib import Path



def _make_ranked_with_extras() -> object:
    """RankedLeg with all the fields the poller rebuild was dropping."""
    from ud_edge.models import Leg, RankedLeg

    leg = Leg(
        line_id="line_xyz", player_id="player_xyz",
        player_name="Player XYZ", sport_id="NBA",
        match_title="BOS @ NYK", match_id=42,
        scheduled_at="2026-07-20T23:00:00+00:00",
        stat_name="points", line_value=27.5, line_type="balanced",
        higher_american=-160, higher_decimal=1.625, higher_multiplier=0.95,
        lower_american=135, lower_decimal=2.35, lower_multiplier=0.95,
        fantasy_source="underdog", team_id="BOS",
    )
    return RankedLeg(
        leg=leg,
        higher_true_prob=0.59, higher_implied_prob=0.615, higher_edge_pp=4.0,
        lower_true_prob=0.41, lower_implied_prob=0.425, lower_edge_pp=-14.0,
        picked_side="higher", picked_true_prob=0.59,
        picked_edge_pp=4.0, overround=1.04,
        sharp_true_prob=0.62, sharp_book="Pinnacle",
        sharp_overround=1.02, mispricing_edge_pp=3.0,
    )


# ── Poller uses return_ranked path ─────────────────────────────────────────────


def test_poller_passes_return_ranked_true(tmp_path: Path, monkeypatch):
    """compare_fantasy_vs_sharp must be called with return_ranked=True.

    Without it, compare returns just the dict (no live RankedLeg list).
    """
    from ud_edge import poller
    from ud_edge.budget import CallBudget

    # Audit remediation (test hygiene): bypass the PROPLINE_API_KEY early-return
    # so our stubbed compare_fantasy_vs_sharp is actually invoked.
    monkeypatch.setenv("PROPLINE_API_KEY", "testkey-not-real")

    captured = {"return_ranked": None}

    def fake_compare(**kwargs):
        captured["return_ranked"] = kwargs.get("return_ranked")
        return {"flat": [], "lineups": [], "sharp_meta": {"propline_calls": 0}}

    monkeypatch.setattr(poller, "compare_fantasy_vs_sharp", fake_compare)
    budget = CallBudget(path=tmp_path / "b.json", daily_limit=5000)

    poller._run_poll_cycle(budget=budget, min_mispricing_pp=1.5, cache_path=tmp_path)

    assert captured["return_ranked"] is True, (
        f"poller._run_poll_cycle must call compare_fantasy_vs_sharp with "
        f"return_ranked=True (so it gets the live RankedLeg list back). "
        f"Got: {captured['return_ranked']}. This is the audit P1 #5 bug."
    )


def test_poller_uses_returned_ranked_list_not_flat(tmp_path: Path, monkeypatch):
    """The poller must consume the ranked list returned via return_ranked,
    not rebuild RankedLeg from the JSON-serialized flat list.

    Stub compare to return a tuple (payload, ranked_list). Verify the
    poller uses that exact ranked_list (no reconstruction, no field loss).
    """
    from ud_edge import poller
    from ud_edge.budget import CallBudget

    # Audit remediation (test hygiene): bypass the PROPLINE_API_KEY early-return.
    monkeypatch.setenv("PROPLINE_API_KEY", "testkey-not-real")

    live_ranked = [_make_ranked_with_extras() for _ in range(3)]

    fake_payload = {
        "flat": [],  # intentionally empty — poller must NOT use this
        "lineups": [],
        "sharp_meta": {"propline_calls": 0, "count": 3, "sources": ["Pinnacle"], "errors": []},
        "totals": {"opportunities": 3, "mispriced": 1, "sports": 1, "lineups": 1},
    }

    def fake_compare(**kwargs):
        if kwargs.get("return_ranked"):
            return (fake_payload, live_ranked)
        return fake_payload

    monkeypatch.setattr(poller, "compare_fantasy_vs_sharp", fake_compare)
    budget = CallBudget(path=tmp_path / "b.json", daily_limit=5000)

    mispriced, nearest, meta = poller._run_poll_cycle(
        budget=budget, min_mispricing_pp=1.5, cache_path=tmp_path
    )

    # The 3 legs have mispricing_edge_pp=3.0 (>= 2.0), so all 3 should be mispriced.
    assert len(mispriced) == 3, (
        f"Poller must surface all 3 mispriced legs from the live ranked list. "
        f"Got {len(mispriced)}. If 0, it's still using the empty flat[] and "
        f"ignoring the live list — the audit P1 #5 bug is still present."
    )

    # Verify field preservation: sharp_book and mispricing_edge_pp must survive
    for r in mispriced:
        assert r.sharp_book == "Pinnacle", (
            f"sharp_book field lost across poller boundary: {r.sharp_book}. "
            f"This is the audit P1 #5 bug — flat-JSON reconstruction dropped it."
        )
        assert r.mispricing_edge_pp == 3.0, (
            f"mispricing_edge_pp field lost across poller boundary: "
            f"{r.mispricing_edge_pp}."
        )


def test_poller_preserves_sharp_book_on_alerts(tmp_path: Path, monkeypatch):
    """End-to-end: the alert logic (which uses sharp_book for cooldown keys)
    must see the original field. If the poller reconstructs RankedLeg from
    flat JSON, sharp_book will be None for every leg and alerts will
    collide on the same cooldown bucket.
    """
    from ud_edge import poller
    from ud_edge.budget import CallBudget

    # Audit remediation (test hygiene): bypass the PROPLINE_API_KEY early-return.
    monkeypatch.setenv("PROPLINE_API_KEY", "testkey-not-real")

    live_ranked = [_make_ranked_with_extras() for _ in range(2)]
    # Make them mispriced
    for r in live_ranked:
        r.mispricing_edge_pp = 5.0

    fake_payload = {
        "flat": [],
        "lineups": [],
        "sharp_meta": {"propline_calls": 0, "count": 2, "sources": ["Pinnacle"], "errors": []},
    }

    monkeypatch.setattr(
        poller, "compare_fantasy_vs_sharp",
        lambda **kw: (fake_payload, live_ranked) if kw.get("return_ranked") else fake_payload,
    )

    budget = CallBudget(path=tmp_path / "b.json", daily_limit=5000)

    mispriced, _, _ = poller._run_poll_cycle(
        budget=budget, min_mispricing_pp=1.5, cache_path=tmp_path
    )

    # Both legs must carry sharp_book through the poller boundary
    assert all(r.sharp_book == "Pinnacle" for r in mispriced), (
        f"All mispriced legs must carry sharp_book='Pinnacle'. Got: "
        f"{[r.sharp_book for r in mispriced]}"
    )