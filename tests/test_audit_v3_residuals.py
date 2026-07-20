"""Audit v3 residuals (remediation v3 closes):

1. results_tracker.log_picks() must store effective_true_prob alongside
   picked_true_prob; calibration math must use effective_true_prob.
2. __main__.py single-entry entry-type comparison must guard
   expected_value_per_card when len(per_leg) != entry.n_legs.
3. __main__.py single-entry entry-type comparison must label the rec
   probability clearly so it can't silently disagree with the per-card EV.
4. /api/lineups must accept line_tolerance query param and 409 on mismatch.
"""
from __future__ import annotations

from pathlib import Path
import json
import re


ROOT = Path(__file__).resolve().parents[1]


# ── Residual 1: results_tracker uses effective_true_prob for calibration ────


def test_results_tracker_stores_effective_true_prob(tmp_path: Path, monkeypatch):
    """log_picks must write effective_true_prob alongside picked_true_prob
    so calibration can measure the probability the board EV used.
    """
    # Redirect RESULTS_PATH to tmp_path
    import ud_edge.results_tracker as rt
    monkeypatch.setattr(rt, "RESULTS_PATH", tmp_path / "results.json")

    from ud_edge.models import Leg, RankedLeg
    leg = Leg(
        line_id="L1", player_id="P1", player_name="Player",
        sport_id="NBA", match_title="A@B", match_id=1,
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
        sharp_true_prob=0.62, sharp_book="Pinnacle",
    )
    rt.log_picks([[rl]], entry_type="6-flex", n_entries=1)
    data = json.loads((tmp_path / "results.json").read_text())
    pick = data["picks"][0]
    assert "effective_true_prob" in pick, (
        f"results.json pick missing effective_true_prob. "
        f"Calibration can't measure the board's contract. Keys: "
        f"{sorted(pick.keys())}"
    )
    # effective = sharp when matched (0.62); fantasy fallback is 0.55.
    # effective_true_prob should be 0.62 (sharp-authoritative).
    assert abs(pick["effective_true_prob"] - 0.62) < 1e-6, (
        f"Expected effective_true_prob=0.62 (sharp), got "
        f"{pick['effective_true_prob']}"
    )
    # Legacy field kept for backward-compat reads.
    assert "picked_true_prob" in pick
    assert "sharp_true_prob" in pick


def test_calibration_stats_use_effective_prob(tmp_path: Path, monkeypatch):
    """calibration_stats must bucket by _predicted_prob (= effective when
    present, picked as fallback). The pin: a sharp-aware pick (effective=0.62)
    must land in the 60-65% bucket, NOT in the 55-60% bucket where
    picked_true_prob=0.55 would put it.
    """
    import ud_edge.results_tracker as rt
    results_file = tmp_path / "results.json"
    monkeypatch.setattr(rt, "RESULTS_PATH", results_file)

    # Seed: 100 picks with effective=0.62, all hits (Brier should be very low
    # because the predicted prob is well-calibrated against a 100% hit rate —
    # we just care about WHICH bucket they land in).
    seeded_picks = []
    for i in range(100):
        seeded_picks.append({
            "date": "2026-07-20",
            "entry": 1,
            "leg": 1,
            "_key": f"k{i}",
            "line_id": f"L{i}",
            "sport": "NBA",
            "player_id": f"P{i}",
            "player_name": f"P{i}",
            "match_title": "X@Y",
            "stat": "points",
            "line_value": 27.5,
            "picked_side": "higher",
            "picked_true_prob": 0.55,
            "sharp_true_prob": 0.62,
            "effective_true_prob": 0.62,  # the prob the board used
            "picked_american": -110,
            "entry_type": "6-flex",
            "outcome": "HIT",
            "actual_stat": 30.0,
            "resolved_at": "2026-07-21T00:00:00+00:00",
        })
    results_file.write_text(json.dumps({
        "picks": seeded_picks,
        "metadata": {"created": "2026-07-20T00:00:00+00:00"},
    }))

    stats = rt.calibration_stats()
    buckets = stats["by_prob_bucket"]
    # The picks have effective_true_prob=0.62, which buckets to 60-65%.
    # If calibration still used picked_true_prob, they'd bucket to 55-60%.
    assert "60-65%" in buckets, (
        f"Expected picks with effective_true_prob=0.62 to bucket into "
        f"'60-65%'. Got buckets: {sorted(buckets.keys())}. "
        f"Calibration is using the wrong probability."
    )
    assert "55-60%" not in buckets or buckets.get("55-60%", {}).get("n", 0) == 0, (
        f"Picks with effective=0.62 ended up in 55-60% bucket — calibration "
        f"is using picked_true_prob (fantasy) instead of effective_true_prob. "
        f"Buckets: {buckets}"
    )


def test_legacy_picks_fall_back_to_picked_true_prob(tmp_path: Path, monkeypatch):
    """Legacy picks (no effective_true_prob field) must fall back to
    picked_true_prob so old data still flows through.
    """
    import ud_edge.results_tracker as rt
    results_file = tmp_path / "results.json"
    monkeypatch.setattr(rt, "RESULTS_PATH", results_file)

    # Seed: a legacy pick without effective_true_prob.
    results_file.write_text(json.dumps({
        "picks": [{
            "date": "2026-07-19",
            "entry": 1,
            "leg": 1,
            "_key": "legacy",
            "line_id": "L_legacy",
            "sport": "NBA",
            "player_id": "P_legacy",
            "player_name": "Legacy",
            "match_title": "X@Y",
            "stat": "points",
            "line_value": 25.5,
            "picked_side": "higher",
            "picked_true_prob": 0.58,
            "picked_american": -110,
            "entry_type": "6-flex",
            "outcome": "HIT",
            "actual_stat": 27.0,
            "resolved_at": "2026-07-19T20:00:00+00:00",
            # NB: no effective_true_prob field
        }],
        "metadata": {"created": "2026-07-19T00:00:00+00:00"},
    }))

    # Should not raise; should bucket the legacy pick into 55-60%.
    stats = rt.calibration_stats()
    assert stats["total_resolved"] == 1, "Legacy pick not picked up"
    # The legacy pick with picked_true_prob=0.58 should land in 55-60%.
    assert "55-60%" in stats["by_prob_bucket"], (
        f"Legacy pick fallback broken. Buckets: "
        f"{sorted(stats['by_prob_bucket'].keys())}"
    )


# ── Residual 3: __main__ single-entry guard for expected_value_per_card ────


def test_main_single_entry_handles_thin_slate():
    """__main__.py's single-entry entry-type comparison must guard the
    expected_value_per_card call when len(per_leg) < entry.n_legs.

    The audit re-score flagged that calling expected_value_per_card(et, per_leg)
    with per_leg shorter than et.n_legs raises ValueError. The fix is a
    `len(per_leg) == et.n_legs` guard with a homogeneous fallback.
    """
    text = (ROOT / "ud_edge" / "__main__.py").read_text(encoding="utf-8")

    # Find the single-entry entry-type comparison loop body
    block_match = re.search(
        r"--- Entry-type comparison \(same top legs\) ---.*?return 0",
        text,
        re.DOTALL,
    )
    assert block_match, "Could not locate entry-type comparison block"
    block = block_match.group()
    assert "len(per_leg) == et.n_legs" in block, (
        f"Single-entry entry-type loop does not guard expected_value_per_card. "
        f"A thin slate would ValueError. Block:\n{block}"
    )
    # And there must be a fallback to homogeneous expected_value()
    assert "expected_value(et, avg_prob)" in block, (
        f"Single-entry entry-type loop does not have homogeneous fallback. "
        f"Block:\n{block}"
    )


# ── Residual 4: __main__ single-entry labels the rec probability clearly ────


def test_main_single_entry_labels_recommendation_prob():
    """The single-entry entry-type comparison must clearly label what the
    recommendation probability is. Without this, recommend_entry (scalar)
    can silently disagree with the printed per-card EV.
    """
    text = (ROOT / "ud_edge" / "__main__.py").read_text(encoding="utf-8")
    block_match = re.search(
        r"--- Entry-type comparison \(same top legs\) ---.*?return 0",
        text,
        re.DOTALL,
    )
    assert block_match, "Could not locate entry-type comparison block"
    block = block_match.group()
    # The print line should mention avg_prob or board to make it explicit.
    assert (
        "avg_prob_on_board" in block
        or "avg_prob" in block.lower()
    ), (
        f"Single-entry entry-type loop does not label the recommendation "
        f"probability clearly. Operators could miss a scalar-vs-per-card "
        f"disagreement on mixed cards. Block:\n{block}"
    )


# ── Residual 2: /api/lineups accepts line_tolerance ─────────────────────────


def test_dashboard_lineups_endpoint_has_line_tolerance_param():
    """The /api/lineups endpoint must accept line_tolerance query param."""
    text = (ROOT / "ud_edge" / "dashboard" / "app.py").read_text(encoding="utf-8")
    # Find the lineup_suggestions() signature. The closing "):" can sit at
    # column 0 (after a multi-line Query that includes nested parens), so we
    # match either 0-indent or 4-indent terminator.
    m = re.search(r"def lineup_suggestions\((.*?)^\):", text, re.DOTALL | re.MULTILINE)
    assert m, "Could not locate lineup_suggestions() signature"
    sig = m.group(1)
    assert "line_tolerance" in sig, (
        "lineup_suggestions() signature does not include line_tolerance. "
        "Operators can't see lineups built from their chosen tolerance."
    )


def test_dashboard_lineups_409_on_tolerance_mismatch():
    """If line_tolerance on /api/lineups disagrees with the cached
    opportunities run, the endpoint must 409 with a fix instruction rather
    than silently returning mismatched data.
    """
    text = (ROOT / "ud_edge" / "dashboard" / "app.py").read_text(encoding="utf-8")
    # Find the lineup_suggestions function body
    block_match = re.search(
        r"def lineup_suggestions\(.*?recommend_entry_for_audit.*?\n",
        text,
        re.DOTALL,
    )
    if not block_match:
        # Fallback: find the lineups function body up to a reasonable length
        block_match = re.search(
            r"def lineup_suggestions\((.*?)^    payload = _CACHE\.get",
            text,
            re.DOTALL | re.MULTILINE,
        )
    assert block_match, "Could not locate lineup_suggestions body"
    body = block_match.group()
    assert "status_code=409" in body, (
        "/api/lineups does not return 409 on line_tolerance mismatch. "
        "Would silently return mismatched lineups."
    )
    assert "/api/opportunities" in body, (
        "/api/lineups 409 message should direct user to refresh "
        "/api/opportunities first."
    )