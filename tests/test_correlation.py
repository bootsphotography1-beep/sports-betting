"""Correlation analyzer unit tests (no network)."""
from ud_edge.correlation import analyze_slip, classify_pair, format_correlation_report
from ud_edge.models import Leg, RankedLeg


def _rl(name, pid, team, match, stat, side, prob, line=1.5, mid=1):
    leg = Leg(
        line_id=f"l-{pid}-{stat}",
        player_id=pid,
        player_name=name,
        sport_id="MLB",
        team_id=team,
        match_id=mid,
        match_title="LAD @ NYY",
        stat_name=stat,
        line_value=line,
        line_type="balanced",
        higher_american=-120,
        higher_decimal=1.83,
        higher_multiplier=0.9,
        lower_american=100,
        lower_decimal=2.0,
        lower_multiplier=1.0,
    )
    return RankedLeg(
        leg=leg,
        higher_true_prob=prob if side == "higher" else 1 - prob,
        higher_implied_prob=0.55,
        higher_edge_pp=5.0,
        lower_true_prob=prob if side == "lower" else 1 - prob,
        lower_implied_prob=0.45,
        lower_edge_pp=-5.0,
        picked_side=side,
        picked_true_prob=prob,
        picked_edge_pp=5.0,
        overround=1.05,
    )


def test_same_team_hitters_positive_same_direction():
    a = _rl("Muncy", "p1", "LAD", "g", "hits", "higher", 0.55)
    b = _rl("Betts", "p2", "LAD", "g", "total_bases", "lower", 0.58)
    # Opposite directions on same team -> negative / fighting
    pair = classify_pair(a, b, 0, 1)
    assert pair.label == "mlb_same_team_hitters"
    assert pair.direction == "negative"
    assert pair.rho < 0


def test_same_team_hitters_both_under_positive():
    a = _rl("Muncy", "p1", "LAD", "g", "hits", "lower", 0.55)
    b = _rl("Betts", "p2", "LAD", "g", "total_bases", "lower", 0.58)
    pair = classify_pair(a, b, 0, 1)
    assert pair.direction == "positive"
    assert pair.rho > 0


def test_cross_game_neutral():
    a = _rl("A", "p1", "T1", "g1", "hits", "higher", 0.55, mid=1)
    a.leg.match_title = "A @ B"
    b = _rl("B", "p2", "T2", "g2", "hits", "higher", 0.55, mid=2)
    b.leg.match_title = "C @ D"
    pair = classify_pair(a, b, 0, 1)
    assert pair.direction == "neutral"
    assert pair.rho == 0.0


def test_qb_wr_stack():
    a = _rl("Mahomes", "q1", "KC", "g", "pass_yds", "higher", 0.56, mid=9)
    a.leg.sport_id = "NFL"
    b = _rl("Kelce", "w1", "KC", "g", "rec_yds", "higher", 0.55, mid=9)
    b.leg.sport_id = "NFL"
    pair = classify_pair(a, b, 0, 1)
    assert pair.label == "qb_wr_stack"
    assert pair.direction == "positive"


def test_slip_boost_and_scenarios():
    # Two same-team unders -> positive corr -> sweep boost > 1
    legs = [
        _rl("Betts", "p2", "LAD", "g", "total_bases", "lower", 0.58),
        _rl("Freeman", "p3", "LAD", "g", "total_bases", "lower", 0.56),
    ]
    report = analyze_slip(legs, entry_type="6-flex")
    assert report.positive_pairs >= 1
    assert report.joint_boost > 1.0
    assert report.p_sweep_corr > report.p_sweep_indep
    assert any(s.hits == 2 for s in report.scenarios)
    text = format_correlation_report(report, legs)
    assert "Correlation analysis" in text
    assert "SWEEP" in text


def test_fighting_recommends_avoid():
    legs = [
        _rl("Muncy", "p1", "LAD", "g", "hits", "higher", 0.55),
        _rl("Betts", "p2", "LAD", "g", "total_bases", "lower", 0.58),
    ]
    report = analyze_slip(legs)
    assert report.fighting_pairs >= 1
    assert "avoid" in report.recommend_entry or "rebuild" in report.recommend_entry
