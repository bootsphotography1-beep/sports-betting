"""Rank UD legs by no-vig edge against a break-even threshold.

Takes parsed Leg objects from ud_client, applies the no_vig math to each,
and returns RankedLeg objects sorted by edge descending.

Per-leg break-even is supplied by the caller — it's entry-type-dependent
(see flex_math.py). We default to 3-man-power (54.95%) which is the
recommended entry type per Derek @BTS methodology.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional
from ud_edge.models import Leg, RankedLeg
from ud_edge.no_vig import no_vig, edge_pp


# Audit P1 #6: line tolerance is now configurable via env var, CLI flag,
# and rank_legs(line_tolerance=...) parameter. Default stays 0.5 to preserve
# Wave 2A's exact-tolerance semantics; operators can opt up to 1.0+ via
# `UD_LINE_TOLERANCE` env var or `--line-tolerance` on the CLI.
import os as _os
_LINE_TOL_DEFAULT = float(_os.environ.get("UD_LINE_TOLERANCE", "0.5"))
# Module-level constant. Override at import time via UD_LINE_TOLERANCE env var,
# or per-call via rank_legs(line_tolerance=...), or via --line-tolerance CLI.
LINE_TOLERANCE: float = _LINE_TOL_DEFAULT


# Stat-name blacklist: lines where the favorite side is so obviously likely
# to hit that UD's underpricing isn't a real +EV signal — just an illiquid
# niche market. Examples: "Under 0 RBIs" hits ~80% of the time for any
# non-elite batter regardless of who's pitching; that's not information.
STAT_BLACKLIST_PATTERNS = [
    "under_0",  # any stat with line=0 and side=under is trivially ~80%
]

# Stat-name whitelist by sport: only include these stat types when filtering
# by sport. If empty for a sport, all non-blacklisted stats are allowed.
# Keys must match UD's exact stat names from /beta/v5/over_under_lines.
SPORT_STAT_WHITELIST: dict[str, set[str]] = {
    "NBA": {"points", "rebounds", "assists", "threes", "steals", "blocks",
            "turnovers", "fantasy_points", "pts_rebs_asts", "pts_rebs_asts_blocks_steals",
            "double_double", "triple_double"},
    "BASKETBALL": {"points", "pts_rebs_asts"},
    "WNBA": {"points", "rebounds", "assists", "threes", "fantasy_points"},
    "NFL": {"pass_yds", "pass_tds", "rush_yds", "rush_tds", "rec_yds", "rec_tds",
            "receptions", "fantasy_points", "interceptions", "rush_rec_yds",
            "rush_rec_tds", "longest_rush", "longest_rec", "longest_pass",
            "passing_yds", "rushing_yds", "receiving_yds", "passing_tds",
            "rushing_tds", "receiving_tds"},
    "CFB": {"season_pass_tds", "season_pass_yards", "season_rec_tds",
            "season_rec_yards", "season_rush_tds", "season_rush_yards",
            "passing_yds", "receiving_yds", "rushing_yds"},
    "CFL": {"passing_yds", "receiving_yds", "rushing_yds"},
    "MLB": {"hits", "runs", "rbis", "home_runs", "stolen_bases", "walks",
            "total_bases", "hits_runs_rbis", "strikeouts", "batter_strikeouts",
            "earned_runs", "singles", "doubles", "triples", "fantasy_points",
            "hits_allowed", "runs_allowed", "walks_allowed",
            "period_1_strikeouts", "period_1_hits_allowed",
            "period_1_total_runs_allowed", "period_1_batters_faced"},
    "NHL": {"goals", "assists", "points", "shots_on_goal", "saves",
            "fantasy_points", "power_play_points"},
    "PGA": {"birdies", "bogeys", "eagles", "pars", "strokes"},
    "MMA": {"significant_strikes", "takedowns", "fantasy_points"},
    "FIFA": {"period_1_2_goals", "period_1_2_assists", "period_1_2_shots_on_target",
             "period_1_2_shots_attempted", "period_1_2_goals_assists",
             "period_1_2_saves", "period_1_goals"},
    "ESPORTS": {"kills_on_game_1", "kills_on_game_2", "kills_on_game_3",
                "kills_on_game_1_2_3", "kills_on_maps_1_2",
                "fantasy_points_on_games_1_2_3", "fantasy_points_on_games_1_2",
                "headshots_on_maps_1_2"},
    "CS": {"kills_on_maps_1_2", "headshots_on_maps_1_2"},
    "LOL": {"kills_on_maps_1_2", "assists_on_maps_1_2",
            "period_1_2_fantasy_points"},
    "DOTA": {"kills_on_maps_1_2", "fantasy_points_on_games_1_2"},
}


def is_trivial_under_zero(leg: Leg) -> bool:
    """Return True if this leg is a trivially-underpriced niche market.

    Three patterns qualify as "trivial":

    1. "Under 0 X" (line=0.0) — Under 0 always hits ~75-85% regardless of
       matchup. UD prices these as 50/50 because they're illiquid.

    2. "Under 0.5 of rare counting stats" — any line=0.5 on the UNDER side
       where the stat is rare enough that the true hit rate is ~75%+. Examples:
       walks, RBIs, TDs, hits-allowed, saves, etc.

    3. "Over 0.5 of rare counting stats" — the mirror argument. Did player X
       record ≥1 of a rare event (first TD, first goal, any save, etc.) hits
       ~10-25% in reality, but UD's pricing is closer to 50/50 because these
       are illiquid.
    """
    if leg.line_value == 0.0:
        return True

    RARE_UNDER_HALF_STATS = {
        # MLB — rare at 0.5: hits/runs/RBIs/walks rarely go over 0.5 for most players
        "rbis", "walks", "home_runs", "stolen_bases",
        "hits", "runs",
        "hits_allowed", "runs_allowed", "walks_allowed",
        "period_1_total_runs_allowed",
        # NFL
        "pass_tds", "rush_tds", "rec_tds", "rush_rec_tds",
        "interceptions", "receiving_tds", "rushing_tds", "passing_tds",
        # NHL
        "goals", "saves", "power_play_points",
        # Soccer
        "period_1_2_goals", "period_1_2_assists",
        "period_1_2_first_goal_scorer", "period_1_2_last_goalscorer",
        # Tennis
        "tie_breakers_played", "sets_won",
        # MMA
        "takedowns",
    }
    if leg.line_value == 0.5 and leg.stat_name in RARE_UNDER_HALF_STATS:
        return True

    return False


# Statuses that mean "the player will not play" — these legs are unplayable.
# Day-To-Day / Questionable / Probable players can still play, so we keep
# their legs (with a flag in the report).
INJURY_OUT_STATUSES = {"OUT", "INJURY_RESERVE", "SUSPENDED", "DOUBTFUL"}


def is_player_out(leg: Leg, injury_index: Optional[dict] = None) -> bool:
    """Return True if the player on this leg is ruled OUT per ESPN injury feed.

    injury_index: {sport_id: {normalized_name: status}} — see injury_client.
    If None, returns False (no filter applied). Caller is responsible for
    pre-fetching the injury index once and passing it in.
    """
    if injury_index is None:
        return False
    from ud_edge.injury_client import normalize_name
    sport_data = injury_index.get(leg.sport_id or "", {})
    status = sport_data.get(normalize_name(leg.player_name), "ACTIVE")
    return status in INJURY_OUT_STATUSES


def get_player_status(leg: Leg, injury_index: Optional[dict] = None) -> str:
    """Return normalized injury status for the player on this leg. 'ACTIVE' if unknown."""
    if injury_index is None:
        return "ACTIVE"
    from ud_edge.injury_client import normalize_name
    sport_data = injury_index.get(leg.sport_id or "", {})
    return sport_data.get(normalize_name(leg.player_name), "ACTIVE")


def rank_legs(
    legs: list[Leg],
    break_even: float = 0.5495,
    min_true_prob: float = 0.55,
    min_edge_pp: float = 0.5,
    filter_trivial: bool = True,
    injury_index: Optional[dict] = None,
    sharp_book_index: Optional[dict] = None,
    full_game_only: bool = False,
    sharp_policy: str = "sharp_authoritative_quarantine",
    reject_started: bool = True,
    reject_live: bool = True,
    line_tolerance: float = LINE_TOLERANCE,
) -> list[RankedLeg]:
    """Rank legs by edge above break-even. Only include legs that clear min_true_prob.

    Args:
        legs: parsed Leg objects from ud_client
        break_even: per-leg break-even hit rate (entry-type dependent)
        min_true_prob: minimum true probability for the favorite side (default 55%)
        min_edge_pp: minimum edge in percentage points (default 0.5)
        filter_trivial: skip "Under 0 X" legs (trivially under-priced niche props)
        injury_index: optional {sport_id: {normalized_name: status}} from ESPN feed.
                      Legs where the player is OUT / IR / Suspended / Doubtful
                      are filtered out. Day-To-Day legs are kept (with a flag).
        sharp_book_index: optional sharp index from build_sharp_index().
        sharp_policy: policy for how to use sharp-book matches. Default
            "sharp_authoritative_quarantine" — when sharp disagrees with fantasy
            by more than -2.0pp (sharp lower), quarantine the leg; when sharp
            agrees (delta >= -2.0pp), use sharp's same-side probability for EV.
            Other policies may be added in future waves.
        full_game_only: skip mid-game / half props and obscure sports.
        reject_started: if True, skip legs whose scheduled_at is in the past
            (market has already started). Unknown scheduled_at is allowed.
            Default True.
        reject_live: if True, skip legs from live / in-progress events.
            Default True.

    Returns: list of RankedLeg sorted by picked_edge_pp descending.
    """
    ranked: list[RankedLeg] = []
    skipped_threshold = 0
    skipped_trivial = 0
    skipped_whitelist = 0
    skipped_injury = 0
    skipped_midgame = 0
    skipped_started = 0
    skipped_live = 0
    matched_sharp = 0

    # When --full-game-only is set, drop mid-game / first-half props that resolve
    # before the final whistle. These tend to have inflated true-prob estimates
    # because the event hasn't happened yet (pitcher not up, first set in progress),
    # so the "edge" is illusory. Restricting to full-game props gives more
    # stable, time-anchored edges.
    MIDGAME_STATS = {
        # Tennis mid-match props
        "period_1_games_won", "period_1_games_played",
        "period_1_sets_won", "period_1_breaks",
        # Soccer first-half props
        "period_1_2_goals", "period_1_2_assists",
        "period_1_2_shots_on_target", "period_1_2_shots_attempted",
        "period_1_2_goals_assists", "period_1_2_saves",
        "period_1_goals", "period_1_2_first_goal_scorer",
        "period_1_2_last_goalscorer",
        # MLB half-inning / mid-game props
        "period_1_strikeouts", "period_1_hits_allowed",
        "period_1_total_runs_allowed", "period_1_batters_faced",
        "period_1_total_bases", "period_1_hits",
        # Mid-quarter / mid-period NBA/NFL (none today, but defensive)
        "period_1_points", "period_1_rebounds",
    }
    # Obscure sports where liquidity is too thin for sharp pricing
    EXCLUDE_SPORTS = {"CS", "LOL", "DOTA", "VAL", "ESPORTS", "RACING", "CFL"}

    # Audit P1 #6: line_tolerance is now an explicit parameter (above) that
    # defaults to the module-level LINE_TOLERANCE constant. The old code had
    # a hard-coded LINE_TOLERANCE = 0.5 inside the function body, making it
    # impossible to opt up without editing source. Soft fantasy lines that
    # differed by 1.0+ from sharp silently fell through and ranked on
    # fantasy no-vig alone. Operators can now pass --line-tolerance=1.0 (or
    # higher) via the CLI / UD_LINE_TOLERANCE env var.

    for leg in legs:
        if filter_trivial and is_trivial_under_zero(leg):
            skipped_trivial += 1
            continue

        # ── Started / live-market rejection ─────────────────────────────────────
        if reject_started or reject_live:
            now = datetime.now(timezone.utc)
            parsed_scheduled: Optional[datetime] = None

            if leg.scheduled_at:
                try:
                    parsed_scheduled = datetime.fromisoformat(leg.scheduled_at)
                    # Normalize to UTC-aware
                    if parsed_scheduled.tzinfo is None:
                        parsed_scheduled = parsed_scheduled.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    # Malformed ISO8601 — treat as unknown, do not reject
                    parsed_scheduled = None

            if reject_started and parsed_scheduled is not None:
                if now > parsed_scheduled:
                    skipped_started += 1
                    continue

            # reject_live: if the leg has no scheduled_at and the market is live,
            # we can't determine — skip only if ud_client marked it as live.
            # The OverUnderLine model has a live_event field; if the leg's
            # match_title or a flag indicates in-progress, we reject.
            # For now, reject_live checks the OverUnderLine.liv_event flag that
            # was used during parsing. Since Leg doesn't carry that flag directly,
            # we use the presence of a very recent scheduled_at (within the last
            # 30 minutes) as a live-event proxy when reject_live is True.
            if reject_live and parsed_scheduled is not None:
                thirty_min = timedelta(minutes=30)
                if now > parsed_scheduled and now < parsed_scheduled + thirty_min:
                    # Started within the last 30 min — live / in-progress
                    skipped_live += 1
                    continue

        # Full-game-only mode: drop mid-game / obscure-sport legs
        if full_game_only:
            if leg.stat_name in MIDGAME_STATS:
                skipped_midgame += 1
                continue
            if leg.sport_id in EXCLUDE_SPORTS:
                skipped_midgame += 1
                continue

        # Sport-stat whitelist: only include known-meaningful stat types per sport
        whitelist = SPORT_STAT_WHITELIST.get(leg.sport_id or "")
        if whitelist and leg.stat_name not in whitelist:
            skipped_whitelist += 1
            continue

        # Injury filter: skip legs where the player is ruled OUT
        if is_player_out(leg, injury_index):
            skipped_injury += 1
            continue

        try:
            true_over, true_under, overround = no_vig(
                leg.higher_decimal, leg.lower_decimal
            )
        except ValueError:
            continue

        impl_over = 1.0 / leg.higher_decimal
        impl_under = 1.0 / leg.lower_decimal

        higher_edge = edge_pp(true_over, break_even)
        lower_edge = edge_pp(true_under, break_even)

        if true_over >= true_under:
            picked_side, picked_prob = "higher", true_over
        else:
            picked_side, picked_prob = "lower", true_under

        picked_edge = edge_pp(picked_prob, break_even)

        # ── Sharp-book cross-reference (sharp_authoritative_quarantine policy) ──
        sharp_true_prob: Optional[float] = None
        sharp_book: Optional[str] = None
        sharp_overround: Optional[float] = None
        mispricing_edge_pp: Optional[float] = None
        quarantined = False  # True when sharp disagrees with fantasy by > -2pp

        if sharp_book_index and sharp_policy == "sharp_authoritative_quarantine":
            from ud_edge.sharp_books_client import find_sharp_match
            sharp_match = find_sharp_match(
                sharp_book_index,
                leg.player_name,
                leg.stat_name,
                leg.line_value,
                line_tolerance=line_tolerance,
                event_title=leg.match_title,
                scheduled_at=leg.scheduled_at,
            )

            if sharp_match is not None:
                # sharp_match is a SharpMatch dataclass
                sharp_for_higher = sharp_match.sharp_for_higher
                sharp_for_lower = sharp_match.sharp_for_lower
                sharp_true_prob = (
                    sharp_for_higher if picked_side == "higher" else sharp_for_lower
                )
                sharp_book = sharp_match.bookmaker
                sharp_overround = None  # already no-vigged in SharpMatch

                # Mispricing = sharp_same_side - fantasy_same_side (percentage points)
                delta_pp = (sharp_true_prob - picked_prob) * 100
                mispricing_edge_pp = delta_pp

                # sharp_authoritative_quarantine: quarantine when delta < -2.0pp
                # (sharp is MORE BEARISH than fantasy by more than 2pp)
                if delta_pp < -2.0:
                    quarantined = True
                    matched_sharp += 1
                else:
                    # When delta >= -2.0pp: sharp agrees or is bullish → use sharp prob for EV
                    # (delta already computed above, just fall through to effective_prob)
                    matched_sharp += 1

        # ── Filter: quarantine when sharp-authoritative policy says to ──
        if quarantined:
            # Do not include this leg in ranked output
            continue

        # Compute effective probability for threshold filtering
        if sharp_true_prob is not None:
            effective_prob = sharp_true_prob
        else:
            effective_prob = picked_prob
        effective_edge = edge_pp(effective_prob, break_even)

        if effective_prob < min_true_prob or effective_edge < min_edge_pp:
            skipped_threshold += 1
            continue

        ranked.append(
            RankedLeg(
                leg=leg,
                higher_true_prob=true_over,
                higher_implied_prob=impl_over,
                higher_edge_pp=higher_edge,
                lower_true_prob=true_under,
                lower_implied_prob=impl_under,
                lower_edge_pp=lower_edge,
                picked_side=picked_side,
                picked_true_prob=picked_prob,
                picked_edge_pp=picked_edge,
                overround=overround,
                sharp_true_prob=sharp_true_prob,
                sharp_book=sharp_book,
                sharp_overround=sharp_overround,
                mispricing_edge_pp=mispricing_edge_pp,
            )
        )

    # Sort by mispricing-aware key: legs where sharp book gives more confidence first
    def _sort_key(r: RankedLeg):
        if r.mispricing_edge_pp is not None and r.mispricing_edge_pp > 0:
            # Fantasy line is soft vs sharp on the same side — boost to top
            return (1, r.mispricing_edge_pp)
        return (0, r.picked_edge_pp)

    ranked.sort(key=_sort_key, reverse=True)

    # Print a one-time diagnostic
    parts = []
    if skipped_trivial:
        parts.append(f"{skipped_trivial} trivial")
    if skipped_whitelist:
        parts.append(f"{skipped_whitelist} whitelisted")
    if skipped_injury:
        parts.append(f"{skipped_injury} OUT (injury)")
    if skipped_midgame:
        parts.append(f"{skipped_midgame} midgame")
    if skipped_started:
        parts.append(f"{skipped_started} started")
    if skipped_live:
        parts.append(f"{skipped_live} live")
    if matched_sharp:
        parts.append(f"{matched_sharp} matched-sharp")
    if parts:
        print(f"[matcher] skipped {' + '.join(parts)} → {len(ranked)} +EV legs kept")
    return ranked


def top_n_for_entry(ranked: list[RankedLeg], n_legs: int) -> list[RankedLeg]:
    """Pick the top N legs (typically n_legs = entry type leg count)."""
    return ranked[:n_legs]



def dedupe_lineups(ranked: list[RankedLeg]) -> list[RankedLeg]:
    """Remove legs that share the same canonical market, keeping the highest edge.

    Canonical key = (player_name, stat_name, line_value, picked_side, match_title)
    Case-insensitive and whitespace-tolerant.

    When the same market appears from multiple sources (e.g. Underdog + PrizePicks),
    the leg with the highest picked_edge_pp is retained.

    Args:
        ranked: pre-sorted list of RankedLeg (best edge first)

    Returns:
        list of RankedLeg with no duplicate canonical markets
    """
    seen: dict[tuple, RankedLeg] = {}

    for r in ranked:
        leg = r.leg
        # Normalize canonical key components
        player = " ".join(leg.player_name.lower().split())
        stat = " ".join(leg.stat_name.lower().split())
        match = " ".join((leg.match_title or "").lower().split())
        side = r.picked_side.lower()

        key = (player, stat, leg.line_value, side, match)

        if key not in seen:
            seen[key] = r
        else:
            # Keep the one with higher picked_edge_pp
            if r.picked_edge_pp > seen[key].picked_edge_pp:
                seen[key] = r

    return list(seen.values())



def build_lineups(
    ranked: list[RankedLeg],
    n_entries: int = 4,
    n_legs: int = 6,
) -> list[list[RankedLeg]]:
    """Partition ranked legs into N disjoint lineups of `n_legs` legs each.

    Fin's goal: deliver 3-4 distinct 6-man 6-flex entries per day so he
    can place multiple cards from the same edge pool without doubling up.

    Algorithm: simple top-N*K chunking. Entry #1 = top [0:n_legs], Entry #2
    = next [n_legs:2*n_legs], etc. Order is preserved from the ranked list
    (already sorted by edge desc, sharp-book-boosted mispricings first).

    Auto-fallback when the slate is thin: returns however many full
    lineups fit (1, 2, 3, or 4). If 0 lineups fit (ranked has fewer than
    `n_legs` legs), returns an empty list — caller should surface "no
    +EV slate today".

    Disjointness: guaranteed by construction (chunks don't overlap).

    Args:
        ranked: pre-sorted list of RankedLeg (best edge first)
        n_entries: maximum number of entries to build (e.g. 4)
        n_legs: legs per entry (e.g. 6 for a 6-flex)

    Returns: list of length <= n_entries, each a list of n_legs RankedLeg.
    """
    if n_entries < 1:
        raise ValueError(f"n_entries must be >= 1, got {n_entries}")
    if n_legs < 1:
        raise ValueError(f"n_legs must be >= 1, got {n_legs}")

    # Deduplicate canonical markets before building lineups
    ranked = dedupe_lineups(ranked)

    n_entries * n_legs
    if len(ranked) < n_legs:
        # Not even one full lineup possible
        return []

    # Cap n_entries to however many full lineups fit
    max_full = len(ranked) // n_legs
    actual = min(n_entries, max_full)

    lineups = []
    for i in range(actual):
        start = i * n_legs
        end = start + n_legs
        lineups.append(ranked[start:end])
    return lineups

def effective_true_prob(picked_prob: float, sharp_true_prob: float | None) -> float:
    """Return the best available true probability for a leg.

    Uses sharp_prob when it dominates (sharp_authoritative_quarantine policy:
    sharp agrees or is bullish — delta >= -2.0pp); falls back to UD's own
    no-vig probability when sharp is unavailable or divergent.
    """
    if sharp_true_prob is not None:
        return sharp_true_prob
    return picked_prob
