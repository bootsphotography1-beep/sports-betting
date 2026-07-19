"""Correlation analyzer for Underdog flex/power slips.

Framing (OddsJuice-style, from correlation / 6-man variance content):
  • Positive correlation = same latent script pushes legs the SAME direction
    → raises P(sweep) and P(wipeout); mid-bin flex partials get rarer.
  • Negative / fighting = legs need contradictory scripts → avoid.
  • If average pairwise corr is high on 5–6 legs → prefer POWER over FLEX
    (you're buying sweeps, not 5/6 insurance).
  • Always show joint vs independent probability so the user sees the boost.

This module is rule-based (no trained ρ). It labels pairs, approximates a
joint-hit boost via pairwise ρ, and enumerates outcome scenarios.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Optional

from ud_edge.flex_math import UD_PAYOUTS, expected_value
from ud_edge.matcher import effective_true_prob
from ud_edge.models import RankedLeg


# Market families for rule matching
HITTING = frozenset({
    "hits", "total_bases", "runs", "rbis", "home_runs", "stolen_bases",
    "walks", "singles", "doubles", "triples", "hits_runs_rbis", "fantasy_points",
})
PITCHING_K = frozenset({"strikeouts", "pitcher_strikeouts"})
PITCHING_ALLOWED = frozenset({
    "hits_allowed", "runs_allowed", "earned_runs", "walks_allowed",
})
PASSING = frozenset({
    "pass_yds", "passing_yds", "pass_tds", "passing_tds", "longest_pass",
})
RECEIVING = frozenset({
    "rec_yds", "receiving_yds", "receptions", "rec_tds", "receiving_tds",
    "longest_rec",
})
RUSHING = frozenset({
    "rush_yds", "rushing_yds", "rush_tds", "rushing_tds", "longest_rush",
    "rush_rec_yds",
})
SCORING_BASKET = frozenset({
    "points", "pts_rebs_asts", "rebounds", "assists", "threes", "fantasy_points",
})


@dataclass
class PairCorr:
    i: int
    j: int
    player_a: str
    player_b: str
    pick_a: str
    pick_b: str
    label: str            # e.g. same_game_same_direction
    direction: str        # "positive" | "negative" | "neutral"
    rho: float            # signed correlation prior used in joint boost
    script: str           # human script tag
    note: str


@dataclass
class OutcomeScenario:
    hits: int
    prob_indep: float
    prob_corr: float
    payout_flex: float
    payout_power: float
    label: str


@dataclass
class SlipCorrelationReport:
    n_legs: int
    pairs: list[PairCorr]
    avg_abs_rho: float
    avg_signed_rho: float
    p_sweep_indep: float
    p_sweep_corr: float
    joint_boost: float          # p_corr / p_indep
    fighting_pairs: int
    positive_pairs: int
    recommend_entry: str        # "6-flex" / "6-man-power-style" / keep current
    recommend_reason: str
    scenarios: list[OutcomeScenario] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stack_score: float = 1.0    # joint_boost (payout drag left = 1 for now)


def _side(r: RankedLeg) -> str:
    return r.picked_side  # "higher" | "lower"


def _same_side(a: RankedLeg, b: RankedLeg) -> bool:
    return _side(a) == _side(b)


def _family(stat: str) -> str:
    s = (stat or "").lower()
    if s in HITTING:
        return "hitting"
    if s in PITCHING_K:
        return "pitching_k"
    if s in PITCHING_ALLOWED:
        return "pitching_allowed"
    if s in PASSING:
        return "passing"
    if s in RECEIVING:
        return "receiving"
    if s in RUSHING:
        return "rushing"
    if s in SCORING_BASKET:
        return "scoring"
    return "other"


def classify_pair(a: RankedLeg, b: RankedLeg, i: int, j: int) -> PairCorr:
    """Rule-based pairwise correlation label (OddsJuice-style scripts)."""
    la, lb = a.leg, b.leg
    fa, fb = _family(la.stat_name), _family(lb.stat_name)
    same_game = (
        la.match_id is not None and la.match_id == lb.match_id
    ) or (
        (la.match_title or "") != "" and la.match_title == lb.match_title
    )
    same_team = bool(la.team_id and lb.team_id and la.team_id == lb.team_id)
    same_player = la.player_id == lb.player_id
    same_dir = _same_side(a, b)
    pick_a = f"{'Over' if _side(a)=='higher' else 'Under'} {la.line_value:g} {la.stat_name}"
    pick_b = f"{'Over' if _side(b)=='higher' else 'Under'} {lb.line_value:g} {lb.stat_name}"

    # Same player two stats — usually positive if same direction
    if same_player:
        rho = 0.45 if same_dir else -0.35
        return PairCorr(
            i, j, la.player_name, lb.player_name, pick_a, pick_b,
            label="same_player",
            direction="positive" if same_dir else "negative",
            rho=rho,
            script="same_player_counting",
            note="Same player props almost always move together.",
        )

    # NFL-style pass stack
    if same_team and same_game and {fa, fb} == {"passing", "receiving"}:
        rho = 0.55 if same_dir else -0.50
        return PairCorr(
            i, j, la.player_name, lb.player_name, pick_a, pick_b,
            label="qb_wr_stack",
            direction="positive" if same_dir else "negative",
            rho=rho,
            script="pass_game",
            note="QB pass yards/TDs and WR receiving move with the same pass script.",
        )

    # Rush vs pass on same team — often mild negative on overs
    if same_team and same_game and {fa, fb} == {"passing", "rushing"}:
        if same_dir and _side(a) == "higher":
            rho = -0.25
            direction = "negative"
            note = "Pass-heavy and rush-heavy scripts fight on the same team."
        else:
            rho = 0.15 if same_dir else -0.10
            direction = "positive" if same_dir else "neutral"
            note = "Mild link via game script / clock."
        return PairCorr(
            i, j, la.player_name, lb.player_name, pick_a, pick_b,
            label="pass_rush_conflict",
            direction=direction, rho=rho, script="offensive_split", note=note,
        )

    # MLB same-team hitters
    if same_team and same_game and fa == "hitting" and fb == "hitting":
        rho = 0.30 if same_dir else -0.28
        return PairCorr(
            i, j, la.player_name, lb.player_name, pick_a, pick_b,
            label="mlb_same_team_hitters",
            direction="positive" if same_dir else "negative",
            rho=rho,
            script="lineup_production",
            note="Same-team bats share pitcher, park, and inning opportunities.",
        )

    # Pitcher Ks vs opposing batter contact (same game, different teams)
    if same_game and not same_team and (
        (fa == "pitching_k" and fb == "hitting")
        or (fb == "pitching_k" and fa == "hitting")
    ):
        # K OVER fights batter OVER; K OVER aligns with batter UNDER
        pitcher_over = (_side(a) == "higher" and fa == "pitching_k") or (
            _side(b) == "higher" and fb == "pitching_k"
        )
        batter_over = (_side(a) == "higher" and fa == "hitting") or (
            _side(b) == "higher" and fb == "hitting"
        )
        if pitcher_over and batter_over:
            rho, direction = -0.40, "negative"
            note = "Pitcher strikeouts OVER fights opposing batter production OVER."
        elif pitcher_over and not batter_over:
            rho, direction = 0.35, "positive"
            note = "Pitcher Ks OVER aligns with opposing batter UNDER."
        else:
            rho, direction = 0.20 if same_dir else -0.15, (
                "positive" if same_dir else "negative"
            )
            note = "Pitcher-allowed / batter contact share the same AB outcomes."
        return PairCorr(
            i, j, la.player_name, lb.player_name, pick_a, pick_b,
            label="pitcher_batter",
            direction=direction, rho=rho, script="matchup_script", note=note,
        )

    # Hits allowed OVER ↔ opposing batter OVER
    if same_game and not same_team and (
        (fa == "pitching_allowed" and fb == "hitting")
        or (fb == "pitching_allowed" and fa == "hitting")
    ):
        rho = 0.40 if same_dir else -0.35
        return PairCorr(
            i, j, la.player_name, lb.player_name, pick_a, pick_b,
            label="hits_allowed_batter",
            direction="positive" if same_dir else "negative",
            rho=rho,
            script="contact_script",
            note="Pitcher hits/runs allowed and opposing bats move together.",
        )

    # Same-game, same direction, different teams — mild pace correlation
    if same_game and same_dir and fa == fb and fa in {"hitting", "scoring"}:
        rho = 0.18
        return PairCorr(
            i, j, la.player_name, lb.player_name, pick_a, pick_b,
            label="same_game_pace",
            direction="positive",
            rho=rho,
            script="game_environment",
            note="High-scoring / low-scoring environments lift both sides similarly.",
        )

    # Same game, opposite directions on same family — mild negative
    if same_game and not same_dir and fa == fb:
        rho = -0.12
        return PairCorr(
            i, j, la.player_name, lb.player_name, pick_a, pick_b,
            label="same_game_mixed",
            direction="negative",
            rho=rho,
            script="mixed_script",
            note="Opposite sides in the same game can fight the same environment.",
        )

    # Default: independent if different games
    if not same_game:
        return PairCorr(
            i, j, la.player_name, lb.player_name, pick_a, pick_b,
            label="cross_game",
            direction="neutral",
            rho=0.0,
            script="independent",
            note="Different games — treated as independent.",
        )

    # Same game leftover
    rho = 0.08 if same_dir else -0.08
    return PairCorr(
        i, j, la.player_name, lb.player_name, pick_a, pick_b,
        label="same_game_weak",
        direction="positive" if same_dir else "negative",
        rho=rho,
        script="shared_game",
        note="Same game, weak residual correlation.",
    )


def _probs(legs: list[RankedLeg]) -> list[float]:
    return [
        max(0.01, min(0.99, effective_true_prob(r.picked_true_prob, r.sharp_true_prob)))
        for r in legs
    ]


def indep_sweep_prob(probs: list[float]) -> float:
    p = 1.0
    for x in probs:
        p *= x
    return p


def corr_sweep_prob(probs: list[float], pairs: list[PairCorr]) -> float:
    """Approximate P(all hit) with pairwise correlation adjustment.

    Starts from ∏p_i and multiplies a boost from average positive ρ among pairs.
    Uses a simple Frechet-style blend toward min(p) for strong positive corr.
    """
    p_indep = indep_sweep_prob(probs)
    if len(probs) < 2 or not pairs:
        return p_indep
    # Weight by |rho| of positive pairs only for sweep boost
    pos = [pr for pr in pairs if pr.rho > 0]
    if not pos:
        # Negative corr → pull toward max(0, sum p - (n-1)) lower bound-ish
        neg_avg = sum(pr.rho for pr in pairs) / len(pairs)
        # Shrink toward independent (negative corr reduces sweeps)
        return max(1e-9, p_indep * (1.0 + neg_avg * 0.5))

    avg_rho = sum(pr.rho for pr in pos) / len(pos)
    # Upper Frechet-ish bound for all-hit: min(p_i)
    p_upper = min(probs)
    # Blend independent → upper bound by avg_rho
    return p_indep + avg_rho * (p_upper - p_indep)


def _binom_indep_scenarios(
    probs: list[float],
    flex_payouts: dict[int, float],
    n: int,
) -> list[tuple[int, float]]:
    """Exact distribution of hit-count under independence (2^n enumeration)."""
    dist = {k: 0.0 for k in range(n + 1)}
    for bits in range(1 << n):
        pr = 1.0
        hits = 0
        for i in range(n):
            if bits & (1 << i):
                pr *= probs[i]
                hits += 1
            else:
                pr *= 1.0 - probs[i]
        dist[hits] += pr
    return [(k, dist[k]) for k in range(n + 1)]


def _corr_adjust_hit_dist(
    indep_dist: list[tuple[int, float]],
    joint_boost: float,
    n: int,
) -> list[tuple[int, float]]:
    """Move probability mass toward extremes when joint_boost > 1.

    Positive correlation → more 0 and n, fewer middles (OddsJuice variance shape).
    """
    if n == 0:
        return indep_dist
    d = {k: p for k, p in indep_dist}
    boost = max(0.0, joint_boost - 1.0)  # 0 if no boost
    if boost <= 1e-9:
        # Mild negative: flatten extremes slightly
        return indep_dist
    # Take from middle bins, give to 0 and n
    middle_keys = [k for k in range(1, n) if d.get(k, 0) > 0]
    take = 0.0
    for k in middle_keys:
        steal = d[k] * min(0.55, boost * 0.8)
        d[k] -= steal
        take += steal
    # Split stolen mass to wipeout vs sweep proportional to boost intent
    d[0] = d.get(0, 0.0) + take * 0.45
    d[n] = d.get(n, 0.0) + take * 0.55
    # Renormalize
    s = sum(d.values()) or 1.0
    return [(k, d.get(k, 0.0) / s) for k in range(n + 1)]


def analyze_slip(
    legs: list[RankedLeg],
    *,
    entry_type: str = "6-flex",
) -> SlipCorrelationReport:
    """Full correlation report for a candidate slip."""
    n = len(legs)
    probs = _probs(legs)
    pairs = [
        classify_pair(legs[i], legs[j], i, j)
        for i, j in itertools.combinations(range(n), 2)
    ]
    fighting = sum(1 for p in pairs if p.direction == "negative")
    positive = sum(1 for p in pairs if p.direction == "positive")
    avg_signed = sum(p.rho for p in pairs) / len(pairs) if pairs else 0.0
    avg_abs = sum(abs(p.rho) for p in pairs) / len(pairs) if pairs else 0.0

    p_indep = indep_sweep_prob(probs)
    p_corr = corr_sweep_prob(probs, pairs)
    boost = (p_corr / p_indep) if p_indep > 0 else 1.0

    entry = UD_PAYOUTS.get(entry_type) or UD_PAYOUTS["6-flex"]
    flex_entry = UD_PAYOUTS.get(f"{n}-flex") or entry
    # Power analog: only perfect cash (use n-man-power if present)
    power_key = f"{n}-man-power"
    power_entry = UD_PAYOUTS.get(power_key)

    indep_dist = _binom_indep_scenarios(probs, flex_entry.payouts, n)
    corr_dist = _corr_adjust_hit_dist(indep_dist, boost, n)
    # Force sweep bin to match p_corr estimate
    corr_map = {k: p for k, p in corr_dist}
    # Rescale so P(n) ~= p_corr, keep relative shape of rest
    old_pn = corr_map.get(n, 0.0)
    if old_pn > 0 and p_corr > 0:
        scale_rest = (1.0 - p_corr) / max(1e-12, (1.0 - old_pn))
        for k in range(n):
            corr_map[k] = corr_map.get(k, 0.0) * scale_rest
        corr_map[n] = p_corr
    corr_dist = [(k, corr_map.get(k, 0.0)) for k in range(n + 1)]

    scenarios: list[OutcomeScenario] = []
    indep_map = {k: p for k, p in indep_dist}
    for k in range(n + 1):
        scenarios.append(OutcomeScenario(
            hits=k,
            prob_indep=indep_map.get(k, 0.0),
            prob_corr=corr_map.get(k, 0.0),
            payout_flex=flex_entry.payouts.get(k, 0.0),
            payout_power=(
                power_entry.payouts.get(k, 0.0) if power_entry
                else (entry.payouts.get(n, 0.0) if k == n else 0.0)
            ),
            label=_scenario_label(k, n),
        ))

    warnings: list[str] = []
    if fighting:
        warnings.append(
            f"{fighting} fighting pair(s) — legs need opposite game scripts."
        )
    same_game_ids = {}
    for r in legs:
        mid = r.leg.match_id or r.leg.match_title
        if mid:
            same_game_ids[mid] = same_game_ids.get(mid, 0) + 1
    for mid, c in same_game_ids.items():
        if c >= 2:
            warnings.append(
                f"{c} legs share game '{mid}' — treat as one script, not independent."
            )

    # Entry recommendation (Video 2): high corr + many legs → power
    if n >= 5 and avg_signed >= 0.20 and fighting == 0:
        recommend = power_key if power_key in UD_PAYOUTS else "power-style (max sweep)"
        reason = (
            "High positive correlation: sweeps (& wipeouts) more likely; "
            "flex 5/6 insurance is weaker — prefer power / higher perfect multiplier."
        )
    elif fighting >= 1:
        recommend = "avoid / rebuild"
        reason = "Fighting legs detected — rebuild so one script explains the slip."
    elif avg_signed <= 0.05:
        recommend = entry_type
        reason = "Low correlation — flex/insured entries harvest partials better."
    else:
        recommend = entry_type
        reason = (
            "Moderate correlation — flex OK, but don't treat EV as fully independent."
        )

    return SlipCorrelationReport(
        n_legs=n,
        pairs=pairs,
        avg_abs_rho=avg_abs,
        avg_signed_rho=avg_signed,
        p_sweep_indep=p_indep,
        p_sweep_corr=p_corr,
        joint_boost=boost,
        fighting_pairs=fighting,
        positive_pairs=positive,
        recommend_entry=recommend,
        recommend_reason=reason,
        scenarios=scenarios,
        warnings=warnings,
        stack_score=boost,
    )


def _scenario_label(k: int, n: int) -> str:
    if k == n:
        return "SWEEP (all hit)"
    if k == 0:
        return "WIPEOUT (all miss)"
    if k == n - 1:
        return "HOOK (one miss)"
    return f"{k}/{n} hit"


def format_correlation_report(
    report: SlipCorrelationReport,
    legs: list[RankedLeg],
) -> str:
    """Plain-text / markdown-ish report for CLI and alerts."""
    lines = []
    lines.append("## Correlation analysis")
    lines.append("")
    lines.append(
        f"Legs: {report.n_legs} · positive pairs: {report.positive_pairs} · "
        f"fighting: {report.fighting_pairs} · avg ρ: {report.avg_signed_rho:+.2f}"
    )
    lines.append(
        f"P(sweep) independent: {report.p_sweep_indep:.2%} → "
        f"correlated: **{report.p_sweep_corr:.2%}** "
        f"(boost **{report.joint_boost:.2f}×**)"
    )
    lines.append(f"Recommend: **{report.recommend_entry}** — {report.recommend_reason}")
    lines.append("")

    if report.warnings:
        lines.append("### Warnings")
        for w in report.warnings:
            lines.append(f"- ⚠ {w}")
        lines.append("")

    # Pair table (skip pure cross-game neutrals unless verbose)
    interesting = [p for p in report.pairs if p.direction != "neutral" or abs(p.rho) >= 0.1]
    if interesting:
        lines.append("### Pair links")
        for p in sorted(interesting, key=lambda x: -abs(x.rho)):
            arrow = "+" if p.direction == "positive" else ("−" if p.direction == "negative" else "·")
            lines.append(
                f"- [{arrow}] {p.player_a} `{p.pick_a}` ↔ {p.player_b} `{p.pick_b}` "
                f"— {p.label} (ρ={p.rho:+.2f}, {p.script})"
            )
            lines.append(f"  _{p.note}_")
        lines.append("")

    lines.append("### Possible outcomes (flex payout)")
    lines.append("| Result | P indep | P corr | Flex pays | Power pays |")
    lines.append("|--------|---------|--------|-----------|------------|")
    for s in report.scenarios:
        if s.prob_indep < 0.005 and s.prob_corr < 0.005 and s.hits not in (0, report.n_legs, report.n_legs - 1):
            continue
        lines.append(
            f"| {s.label} | {s.prob_indep:.1%} | {s.prob_corr:.1%} | "
            f"{s.payout_flex:g}x | {s.payout_power:g}x |"
        )
    lines.append("")
    lines.append(
        "_Positive corr moves mass to SWEEP and WIPEOUT — fewer mid flex cashes. "
        "Place on Underdog; this is structural, not a second book._"
    )
    return "\n".join(lines)


def analyze_and_format(legs: list[RankedLeg], entry_type: str = "6-flex") -> str:
    return format_correlation_report(analyze_slip(legs, entry_type=entry_type), legs)
