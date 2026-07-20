"""Unified fantasy-vs-sharp comparison pipeline for the dashboard + CLI.

Pulls Underdog (live) + optional CSV fantasy boards (PrizePicks / Sleeper),
pulls sharp books (manual CSV / Odds API / SportsGameOdds), ranks mispricings,
and returns sport-grouped opportunities ready for the white frontend.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ud_edge.copy_format import format_block, opportunities_to_dict
from ud_edge.flex_math import UD_PAYOUTS, expected_value_per_card
from ud_edge.matcher import effective_true_prob, rank_legs, build_lineups
from ud_edge.models import Leg, RankedLeg
from ud_edge.safety_gate import safety_status
from ud_edge.sharp_books_client import build_sharp_index
from ud_edge.ud_client import UDClient, resolve_sport_filter


DEFAULT_SPORTS = ["NBA", "NFL", "MLB", "NHL", "WNBA", "CFB", "MLS", "EPL"]


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_fantasy_csv_legs(csv_path: Path, source_name: str = "prizepicks") -> list[Leg]:
    """Convert a PrizePicks/Sleeper-style CSV into Leg objects for ranking."""
    from ud_edge.pp_clipboard import parse_prizepicks_csv

    result = parse_prizepicks_csv(csv_path, source_name=source_name)
    # New (non-strict) API: returns (observations, diagnostics) tuple
    # Old (strict) API: returns list
    if isinstance(result, tuple):
        observations = result[0]
    else:
        observations = result
    legs: list[Leg] = []
    for i, obs in enumerate(observations):
        higher = float(obs.get("higher_decimal") or 0) or 1.91
        lower = float(obs.get("lower_decimal") or 0) or 1.91
        # Guard: decimal odds must be > 1
        if higher <= 1.0:
            higher = 1.91
        if lower <= 1.0:
            lower = 1.91
        legs.append(
            Leg(
                line_id=f"{source_name}-{i}",
                player_id=f"{source_name}-p{i}",
                player_name=obs.get("player_name") or "Unknown",
                sport_id=(obs.get("sport_id") or "UNK").upper(),
                match_title=obs.get("match_title") or None,
                scheduled_at=obs.get("scheduled_at") or None,
                stat_name=(obs.get("stat_name") or "points").lower(),
                line_value=float(obs.get("line_value") or 0),
                line_type="balanced",
                higher_american=-110,
                higher_decimal=higher,
                higher_multiplier=0.9,
                lower_american=-110,
                lower_decimal=lower,
                lower_multiplier=0.9,
            )
        )
    return legs


def collect_fantasy_legs(
    *,
    cache_path: Optional[Path] = None,
    sport_filter: Optional[set[str]] = None,
    resolved_sport_filter: Optional[set[str]] = None,
    fantasy_csvs: Optional[list[tuple[Path, str]]] = None,
    force_fetch: bool = True,
) -> tuple[list[Leg], dict]:
    """Fetch Underdog + optional fantasy CSV boards.

    Returns (legs, meta) where meta describes source counts.
    """
    root = _project_root()
    cache_path = cache_path or (root / "data" / "ud_lines_cache.json")
    meta = {"sources": {}, "errors": []}

    # Use resolved sport filter if provided; otherwise fall back to raw filter
    effective_filter = resolved_sport_filter if resolved_sport_filter is not None else sport_filter

    legs: list[Leg] = []
    try:
        client = UDClient(cache_path=cache_path)
        data = client.fetch(force=force_fetch)
        ud_legs = client.parse_legs(data, sport_filter=sport_filter)
        legs.extend(ud_legs)
        meta["sources"]["underdog"] = len(ud_legs)
    except Exception as e:
        meta["errors"].append(f"underdog: {e}")

    for path, source in fantasy_csvs or []:
        try:
            if path.exists():
                csv_legs = load_fantasy_csv_legs(path, source_name=source)
                if effective_filter:
                    csv_legs = [csv_lg for csv_lg in csv_legs if (csv_lg.sport_id or "") in effective_filter]
                legs.extend(csv_legs)
                meta["sources"][source] = meta["sources"].get(source, 0) + len(csv_legs)
        except Exception as e:
            meta["errors"].append(f"{source}: {e}")

    return legs, meta


def collect_sharp_index(
    *,
    data_dir: Optional[Path] = None,
    sports: Optional[list[str]] = None,
    skip_propline: bool = False,
) -> tuple[dict, dict]:
    """Build sharp-book index from CSV + PropLine / Odds API / SGO keys."""
    root = _project_root()
    data_dir = data_dir or (root / "data")
    sports = sports or DEFAULT_SPORTS
    meta = {"count": 0, "sources": [], "errors": []}

    sharp_csv = data_dir / "sharp_lines.csv"
    sgo_key = os.environ.get("SPORTSGAMEODDS_KEY", "") or None
    odds_key = os.environ.get("ODDS_API_KEY", "") or None
    propline_key = None if skip_propline else (os.environ.get("PROPLINE_API_KEY", "") or None)

    try:
        index, _build_meta = build_sharp_index(
            manual_csv=sharp_csv if sharp_csv.exists() else None,
            sgo_key=sgo_key,
            sgo_sports=sports if sgo_key else None,
            odds_api_key=odds_key,
            odds_api_sports=sports if odds_key else None,
            propline_key=propline_key,
            propline_sports=sports if propline_key else None,
            cache_path=data_dir / "sharp_cache",
        )
        meta["count"] = len(index)
        meta["sources"] = sorted({v.get("source", "?") for v in index.values()})
        meta["stale_lines_rejected"] = _build_meta.get("stale_lines_rejected", 0)
        meta["missing_captured_at"] = _build_meta.get("missing_captured_at", 0)
        return index, meta
    except Exception as e:
        meta["errors"].append(str(e))
        return {}, meta


def compare_fantasy_vs_sharp(
    *,
    entry_type: str = "6-flex",
    min_true_prob: float = 0.55,
    min_edge_pp: float = 0.5,
    sport_filter: Optional[set[str]] = None,
    full_game_only: bool = True,
    mispriced_only: bool = False,
    fantasy_csvs: Optional[list[tuple[Path, str]]] = None,
    n_entries: int = 4,
    force_fetch: bool = True,
    return_ranked: bool = False,
    line_tolerance: Optional[float] = None,
):
    """Run the full comparison and return a dashboard-ready payload.

    When return_ranked=True, returns a tuple (payload, ranked_list) so the
    dashboard can hand the live RankedLeg objects to the lineup selector
    without losing fields like line_id / player_id / match_id.

    Audit P1 #6 (remediation v3): accept line_tolerance so the dashboard /
    poller / API can forward the operator's choice instead of being hard-wired
    to the module default. None means "use LINE_TOLERANCE constant".
    """
    root = _project_root()
    data_dir = root / "data"
    entry = UD_PAYOUTS[entry_type]

    injury_index = None
    try:
        from ud_edge.injury_client import ESPNInjuryClient
        injury_client = ESPNInjuryClient(
            cache_path=data_dir / "injury_cache",
            ttl_seconds=1800,
        )
        injury_index = injury_client.fetch_all_sports()
    except Exception:
        injury_index = None

    # Default fantasy CSVs if present
    if fantasy_csvs is None:
        fantasy_csvs = []
        for name, source in (
            ("prizepicks_demo.csv", "prizepicks"),
            ("sleeper_board.csv", "sleeper"),
            ("prizepicks_board.csv", "prizepicks"),
        ):
            p = data_dir / name
            if p.exists() and "demo" not in name:
                fantasy_csvs.append((p, source))

    sports_list = sorted(sport_filter) if sport_filter else DEFAULT_SPORTS
    # Resolve sport aliases once so filtering also matches aliased sport_ids
    resolved_sport_filter = resolve_sport_filter(sport_filter)

    legs, fantasy_meta = collect_fantasy_legs(
        cache_path=data_dir / "ud_lines_cache.json",
        sport_filter=sport_filter,
        fantasy_csvs=fantasy_csvs,
        force_fetch=force_fetch,
    )

    # Single PropLine pull → sharp index + fantasy boards (PP/UD/Sleeper)
    # Audit P1 #8: always attempt PropLine (live or disk cache). Missing key
    # still loads sharp_cache via load_cached_indexes so a 429 day / no-key
    # laptop can emit mispriced legs from yesterday's pull.
    pl_key = os.environ.get("PROPLINE_API_KEY", "")
    pl_sharp: dict = {}
    pl_meta: dict = {}
    fantasy_props: list = []
    sharp_cache_path = data_dir / "sharp_cache"
    try:
        from ud_edge.propline_client import (
            build_propline_indexes,
            fantasy_props_to_legs,
            load_cached_indexes,
        )
        if pl_key:
            pl_sharp, fantasy_props, pl_meta = build_propline_indexes(
                api_key=pl_key,
                sports=sports_list,
                cache_path=sharp_cache_path,
            )
        else:
            pl_sharp, fantasy_props, pl_meta = load_cached_indexes(
                cache_path=sharp_cache_path,
                sports=sports_list,
            )
            if pl_sharp or fantasy_props:
                fantasy_meta.setdefault("errors", []).append(
                    "PROPLINE_API_KEY unset; served sharp_cache fallback"
                )
        # If live path returned nothing usable, try disk once more.
        if not pl_sharp and sharp_cache_path.exists():
            cached_sharp, cached_fantasy, cached_meta = load_cached_indexes(
                cache_path=sharp_cache_path,
                sports=sports_list,
            )
            if cached_sharp or cached_fantasy:
                pl_sharp, fantasy_props = cached_sharp, cached_fantasy
                pl_meta = {**pl_meta, **cached_meta, "from_cache": True}
                fantasy_meta.setdefault("errors", []).append(
                    "live PropLine empty; served sharp_cache fallback"
                )
        pl_legs = fantasy_props_to_legs(fantasy_props)
        if resolved_sport_filter:
            pl_legs = [pl_lg for pl_lg in pl_legs if (pl_lg.sport_id or "") in resolved_sport_filter]
        legs.extend(pl_legs)
        fantasy_meta.setdefault("sources", {})
        fantasy_meta["sources"]["propline_fantasy"] = len(pl_legs)
        for src in sorted({p.get("bookmaker", "?") for p in fantasy_props}):
            fantasy_meta["sources"][f"propline-{src}"] = sum(
                1 for p in fantasy_props if p.get("bookmaker") == src
            )
        fantasy_meta.setdefault("errors", []).extend(pl_meta.get("errors", []))
    except Exception as e:
        fantasy_meta.setdefault("errors", []).append(f"propline: {e}")

    sharp_index, sharp_meta = collect_sharp_index(
        data_dir=data_dir,
        sports=sports_list,
        skip_propline=True,  # already pulled above
    )
    # PropLine sharp wins over CSV / other sources
    if pl_sharp:
        sharp_index.update(pl_sharp)
        sharp_meta["count"] = len(sharp_index)
        sharp_meta["sources"] = sorted({
            *sharp_meta.get("sources", []),
            *(v.get("source", "propline") for v in pl_sharp.values()),
        })
        if pl_meta.get("from_cache"):
            sharp_meta["from_cache"] = True
            sharp_meta["cache_files_loaded"] = pl_meta.get("cache_files_loaded", 0)
    # Audit P1 #4: surface the actual PropLine HTTP-call count so the poller
    # can advance its budget by the real number (was hard-coded to 1 before,
    # under-counting cycles that hit ~60-80 endpoints for 6 sports).
    sharp_meta["propline_calls"] = int(pl_meta.get("propline_calls", 0) or 0)

    # Audit P1 #6 (remediation v3): forward line_tolerance so the dashboard /
    # poller / API all use the operator's chosen value instead of the module
    # default. None here means "use LINE_TOLERANCE constant" inside rank_legs.
    from ud_edge.matcher import LINE_TOLERANCE as _LT_DEFAULT
    _effective_lt = line_tolerance if line_tolerance is not None else _LT_DEFAULT
    ranked = rank_legs(
        legs,
        break_even=entry.break_even,
        min_true_prob=min_true_prob,
        min_edge_pp=min_edge_pp,
        injury_index=injury_index,
        sharp_book_index=sharp_index or None,
        full_game_only=full_game_only,
        sharp_policy="sharp_authoritative_quarantine",
        line_tolerance=_effective_lt,
    )
    if mispriced_only:
        ranked = [
            r for r in ranked
            if r.mispricing_edge_pp is not None and r.mispricing_edge_pp >= 2.0
        ]

    # Group by sport
    by_sport: dict[str, list[RankedLeg]] = {}
    for r in ranked:
        sport = (r.leg.sport_id or "UNK").upper()
        by_sport.setdefault(sport, []).append(r)

    sports_payload = []
    for sport in sorted(by_sport.keys()):
        sport_legs = by_sport[sport]
        sports_payload.append({
            "sport": sport,
            "count": len(sport_legs),
            "mispriced_count": sum(
                1 for r in sport_legs
                if r.mispricing_edge_pp is not None and r.mispricing_edge_pp >= 2.0
            ),
            "opportunities": [opportunities_to_dict(r, break_even=entry.break_even) for r in sport_legs],
            "copy": {
                "prizepicks": format_block(sport_legs, "prizepicks", sport=sport),
                "sleeper": format_block(sport_legs, "sleeper", sport=sport),
                "underdog": format_block(sport_legs, "underdog", sport=sport),
                "generic": format_block(sport_legs, "generic", sport=sport),
            },
        })

    lineups = build_lineups(ranked, n_entries=n_entries, n_legs=entry.n_legs)
    lineup_payload = []
    for i, lineup in enumerate(lineups, 1):
        # Audit P0: avg_true_prob uses effective_true_prob (sharp when matched);
        # win_prob/ev/median_payout use expected_value_per_card (heterogeneous exact).
        per_leg = [effective_true_prob(r.picked_true_prob, r.sharp_true_prob) for r in lineup]
        avg_prob = sum(per_leg) / len(per_leg)
        ev, win_prob, median_payout = expected_value_per_card(entry, per_leg)
        lineup_payload.append({
            "entry": i,
            "n_legs": len(lineup),
            "avg_true_prob": round(avg_prob, 4),
            "win_prob": round(win_prob, 4) if win_prob else None,
            "median_payout": median_payout,
            "ev": round(ev, 4) if ev is not None else None,
            "opportunities": [opportunities_to_dict(r, break_even=entry.break_even) for r in lineup],
            "copy": {
                "prizepicks": format_block(lineup, "prizepicks", include_header=True),
                "sleeper": format_block(lineup, "sleeper", include_header=True),
                "underdog": format_block(lineup, "underdog", include_header=True),
            },
        })

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "entry_type": entry_type,
        "min_true_prob": min_true_prob,
        "min_edge_pp": min_edge_pp,
        "full_game_only": full_game_only,
        "mispriced_only": mispriced_only,
        "safety_status": safety_status(),
        "totals": {
            "legs_scanned": len(legs),
            "opportunities": len(ranked),
            "mispriced": sum(
                1 for r in ranked
                if r.mispricing_edge_pp is not None and r.mispricing_edge_pp >= 2.0
            ),
            "sports": len(by_sport),
            "lineups": len(lineups),
        },
        "fantasy_meta": fantasy_meta,
        "sharp_meta": sharp_meta,
        "sports": sports_payload,
        "lineups": lineup_payload,
        "flat": [opportunities_to_dict(r, break_even=entry.break_even) for r in ranked],
        "copy_all": {
            "prizepicks": format_block(ranked, "prizepicks"),
            "sleeper": format_block(ranked, "sleeper"),
            "underdog": format_block(ranked, "underdog"),
            "generic": format_block(ranked, "generic"),
        },
        "methodology": {
            "title": "How Edge Board picks props",
            "steps": [
                "Pull fantasy boards (Underdog live + PropLine PrizePicks/Sleeper when keyed).",
                "Pull sharp sportsbook props (PropLine Pinnacle/DK/FD preferred).",
                "Strip vig from two-sided prices to recover true Over/Under probabilities.",
                "Pick the side with edge vs the entry break-even; boost soft fantasy lines where sharp same-side prob is higher.",
                "Group by sport and build disjoint lineups you can copy into apps.",
            ],
            "break_even": entry.break_even,
            "entry_type": entry_type,
        },
    }
    if return_ranked:
        return payload, ranked
    return payload
