"""Rank UD legs by no-vig edge against a break-even threshold.

Takes parsed Leg objects from ud_client, applies the no_vig math to each,
and returns RankedLeg objects sorted by edge descending.

Per-leg break-even is supplied by the caller — it's entry-type-dependent
(see flex_math.py). We default to 3-man-power (54.95%) which is the
recommended entry type per Derek @BTS methodology.
"""
from __future__ import annotations
from typing import Optional
from ud_edge.models import Leg, RankedLeg
from ud_edge.no_vig import no_vig, edge_pp


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
        # MLB
        "rbis", "walks", "home_runs", "stolen_bases",
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


def effective_true_prob(
    picked_prob: float,
    sharp_true_prob: Optional[float] = None,
) -> float:
    """Gate/EV probability that respects same-side sharp agreement.

    - No sharp data → UD picked_prob
    - Sharp same-side ≥ 0.5 (agrees) → max(UD, sharp) so confirmed mispricings clear the gate
    - Sharp same-side < 0.5 (disagrees) → sharp prob (conservative; often filters the leg out)
    """
    if sharp_true_prob is None:
        return picked_prob
    if sharp_true_prob >= 0.5:
        return max(picked_prob, sharp_true_prob)
    return sharp_true_prob


def rank_legs(
    legs: list[Leg],
    break_even: float = 0.5495,
    min_true_prob: float = 0.55,
    min_edge_pp: float = 0.5,
    filter_trivial: bool = True,
    injury_index: Optional[dict] = None,
    sharp_book_index: Optional[dict] = None,
    full_game_only: bool = False,
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
        sharp_book_index: optional {f"{norm_player}|{stat}": {over_decimal,
                          under_decimal, bookmaker, line_value}} from sharp books.
                          When matched, sharp_true_prob is the sharp book's
                          probability for UD's picked side (same-side). Agreements
                          with higher sharp prob are boosted; disagreements
                          (same-side < 50%) are demoted and often filtered out.

    Returns: list of RankedLeg sorted by mispricing-aware edge descending.
    """
    ranked: list[RankedLeg] = []
    skipped_threshold = 0
    skipped_trivial = 0
    skipped_whitelist = 0
    skipped_injury = 0
    skipped_midgame = 0
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

    # Pre-compile line tolerance: a sharp-book line within ±0.5 of UD's counts as same line
    LINE_TOLERANCE = 0.5

    for leg in legs:
        if filter_trivial and is_trivial_under_zero(leg):
            skipped_trivial += 1
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

        # ── Sharp-book cross-reference ──
        sharp_true_prob: Optional[float] = None
        sharp_book: Optional[str] = None
        sharp_overround: Optional[float] = None
        mispricing_edge_pp: Optional[float] = None

        if sharp_book_index:
            from ud_edge.injury_client import normalize_name
            # Try direct match first
            sharp = sharp_book_index.get(
                f"{normalize_name(leg.player_name)}|{leg.stat_name}"
            )
            # If direct match fails, try fuzzy match (same player, similar line)
            if sharp is None:
                for k, v in sharp_book_index.items():
                    if not k.endswith(f"|{leg.stat_name}"):
                        continue
                    if normalize_name(k.split("|")[0]) == normalize_name(leg.player_name):
                        if abs(v.get("line_value", 0) - leg.line_value) <= LINE_TOLERANCE:
                            sharp = v
                            break

            if sharp is not None:
                try:
                    s_over, s_under, s_overround = no_vig(
                        sharp["over_decimal"], sharp["under_decimal"]
                    )
                    # Same-side probability: sharp's true prob for UD's picked side.
                    # Never use the sharp favorite when it is the opposite side.
                    sharp_true_prob = s_over if picked_side == "higher" else s_under
                    sharp_book = sharp.get("bookmaker", "sharp")
                    sharp_overround = s_overround
                    # Mispricing = sharp_same_side - ud_picked (positive = UD underprices this side)
                    mispricing_edge_pp = (sharp_true_prob - picked_prob) * 100
                    matched_sharp += 1
                except (ValueError, KeyError):
                    pass

        # ── Filter: only keep legs where the favorite side clears thresholds ──
        # Agree (sharp same-side ≥ 50%): may use the sharper of UD vs sharp for the gate.
        # Disagree (sharp same-side < 50%): use sharp's lower estimate (conservative demote).
        effective_prob = effective_true_prob(picked_prob, sharp_true_prob)
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

    # Sort: boost same-side sharp agreements with higher confidence; demote disagreements
    def _sort_key(r: RankedLeg):
        if r.mispricing_edge_pp is not None and r.sharp_true_prob is not None:
            if r.sharp_true_prob >= 0.5 and r.mispricing_edge_pp > 0:
                # Sharp agrees on side AND assigns higher probability → boost
                return (2, r.mispricing_edge_pp)
            if r.sharp_true_prob < 0.5:
                # Sharp disagrees on side → demote below UD-only legs
                return (0, r.mispricing_edge_pp)
        return (1, r.picked_edge_pp)

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
    if matched_sharp:
        parts.append(f"{matched_sharp} matched-sharp")
    if parts:
        print(f"[matcher] skipped {' + '.join(parts)} → {len(ranked)} +EV legs kept")
    return ranked


def top_n_for_entry(ranked: list[RankedLeg], n_legs: int) -> list[RankedLeg]:
    """Pick the top N legs (typically n_legs = entry type leg count)."""
    return ranked[:n_legs]


def _lineup_floor_prob(lineup: list[RankedLeg]) -> float:
    """Minimum effective true-prob across legs in a lineup."""
    return min(
        effective_true_prob(r.picked_true_prob, r.sharp_true_prob) for r in lineup
    )


def _select_diversified_lineup(
    pool: list[RankedLeg],
    n_legs: int,
    max_per_game: int = 1,
) -> list[RankedLeg]:
    """Greedily pick `n_legs` from `pool` (already best-first), diversifying.

    Soft constraints (relaxed only if a full lineup cannot otherwise be filled):
      1. At most one leg per player_id
      2. At most `max_per_game` legs per match_id (None match_ids unrestricted)
    """
    if len(pool) < n_legs:
        return []

    def _try(max_game: int, unique_players: bool) -> list[RankedLeg]:
        chosen: list[RankedLeg] = []
        used_players: set[str] = set()
        game_counts: dict[int, int] = {}
        for r in pool:
            pid = r.leg.player_id
            mid = r.leg.match_id
            if unique_players and pid in used_players:
                continue
            if mid is not None and game_counts.get(mid, 0) >= max_game:
                continue
            chosen.append(r)
            used_players.add(pid)
            if mid is not None:
                game_counts[mid] = game_counts.get(mid, 0) + 1
            if len(chosen) == n_legs:
                return chosen
        return chosen if len(chosen) == n_legs else []

    # Tightest → loosest diversification
    for max_game, unique_players in (
        (max_per_game, True),
        (2, True),
        (n_legs, True),
        (n_legs, False),
    ):
        picked = _try(max_game, unique_players)
        if picked:
            return picked
    return []


def build_lineups(
    ranked: list[RankedLeg],
    n_entries: int = 4,
    n_legs: int = 6,
    min_floor_prob: Optional[float] = None,
    diversify: bool = True,
) -> list[list[RankedLeg]]:
    """Partition ranked legs into N disjoint lineups of `n_legs` legs each.

    Fin's goal: deliver 3-4 distinct 6-man 6-flex entries per day so he
    can place multiple cards from the same edge pool without doubling up.

    Algorithm: greedy best-first selection with optional same-player /
    same-game diversification. Entry #1 takes the best diversified set;
    subsequent entries draw from the remaining pool.

    Quality gate: if `min_floor_prob` is set, an entry whose weakest leg
    (effective true-prob) falls below that floor is discarded and no further
    (weaker) entries are emitted.

    Auto-fallback when the slate is thin: returns however many full
    lineups fit. If 0 lineups fit, returns an empty list.

    Args:
        ranked: pre-sorted list of RankedLeg (best edge first)
        n_entries: maximum number of entries to build (e.g. 4)
        n_legs: legs per entry (e.g. 6 for a 6-flex)
        min_floor_prob: optional minimum effective true-prob for every leg
            in an emitted entry (e.g. break_even + 0.01)
        diversify: when True, prefer unique players and ≤1 leg per game

    Returns: list of length <= n_entries, each a list of n_legs RankedLeg.
    """
    if n_entries < 1:
        raise ValueError(f"n_entries must be >= 1, got {n_entries}")
    if n_legs < 1:
        raise ValueError(f"n_legs must be >= 1, got {n_legs}")

    if len(ranked) < n_legs:
        return []

    pool = list(ranked)
    lineups: list[list[RankedLeg]] = []

    for _ in range(n_entries):
        if diversify:
            lineup = _select_diversified_lineup(pool, n_legs=n_legs)
        else:
            lineup = pool[:n_legs] if len(pool) >= n_legs else []

        if not lineup:
            break

        if min_floor_prob is not None and _lineup_floor_prob(lineup) < min_floor_prob:
            # Weaker remaining legs will only be worse — stop emitting entries
            break

        lineups.append(lineup)
        used_ids = {r.leg.line_id for r in lineup}
        pool = [r for r in pool if r.leg.line_id not in used_ids]

    return lineups