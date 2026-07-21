"""FastAPI dashboard for sharp-vs-fantasy mispricing opportunities.

Run:
    python -m ud_edge --serve
    # or
    uvicorn ud_edge.dashboard.app:app --reload --host 0.0.0.0 --port 8787
"""
from __future__ import annotations
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from ud_edge.compare import compare_fantasy_vs_sharp
from ud_edge.flex_math import UD_PAYOUTS

# Valid entry types — used to gate the /api/opportunities endpoint
_VALID_ENTRIES: set[str] = set(UD_PAYOUTS.keys())

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Edge Board",
    description="Sharp-book vs fantasy mispricing dashboard",
    version="0.3.0",
)

# ── CORS: Tailscale browsers (100.64.0.0/10) + localhost ───────────────────
_TAILSCALE_NETWORKS = [
    "http://100.64.0.0/10",
    "http://localhost",
    "http://127.0.0.1",
    "http://host.docker.internal",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighter in production; allow all for Tailscale discovery
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory cache of last comparison (avoids re-fetch on every tab click)
_CACHE: dict = {"payload": None, "key": None}
# Side-cache: stores the actual RankedLeg list keyed by cache_key so that
# /api/lineups can rebuild lineups without having to reconstruct Pydantic
# objects from the JSON payload (which loses fields like line_id,
# player_id, match_id, higher_american, etc.).
_RANKED_CACHE: dict[str, list] = {}

# Dashboard v2: cross-reference of fantasy legs by (player, stat, line) used by
# opportunities_to_dict() to attach the per-fantasy-book true_prob dict.
_FANTASY_LOOKUP_CACHE: dict[str, dict] = {}

# ── Raw props cache: keyed by sport, 60-second TTL ─────────────────────────
_RAW_PROPS_CACHE: dict = {"data": None, "key": None, "fetched_at": 0.0}


def _cache_key(**kwargs) -> str:
    return "|".join(f"{k}={kwargs[k]}" for k in sorted(kwargs))


@app.get("/api/health")
def health():
    return {"ok": True, "service": "edge-board"}


def _is_finite(v: float) -> bool:
    """Return True when v is a finite float (not NaN or ±Inf)."""
    try:
        return math.isfinite(v)
    except (TypeError, ValueError):
        return False


@app.get("/api/opportunities")
def opportunities(
    entry: str = Query("6-flex"),
    min_true_prob: float = Query(0.55),
    min_edge_pp: float = Query(0.5),
    sport: Optional[str] = Query(None, description="Comma-separated sports"),
    full_game_only: bool = Query(True),
    mispriced_only: bool = Query(False),
    refresh: bool = Query(False),
    n_entries: int = Query(4, ge=1, le=8),
    # Audit P1 #6 (remediation v3): allow the dashboard to forward a custom
    # line tolerance. None (default) means "use UD_LINE_TOLERANCE env var, then
    # fall back to the module constant".
    line_tolerance: Optional[float] = Query(
        None,
        ge=0.0,
        le=5.0,
        description="Fuzzy-match line gap (0.5 = exact, 1.0 = soft-line tolerant)",
    ),
):
    # Guard: reject NaN/inf values that would otherwise corrupt the JSON response
    # or cause degenerate ranking math (e.g. all legs pass/fail threshold).
    # FastAPI coerces literal "nan" / "inf" strings to float nan/inf, so we
    # validate after coercion rather than relying on 422 (which only fires for
    # un-coerceable strings like "foo").
    for name, val in [("min_true_prob", min_true_prob), ("min_edge_pp", min_edge_pp)]:
        if not _is_finite(val):
            return JSONResponse(
                {"error": f"Invalid value for '{name}': must be a finite number, got {val}"},
                status_code=400,
            )

    if entry not in _VALID_ENTRIES:
        return JSONResponse(
            {
                "error": f"Invalid entry type '{entry}'. Valid options: {sorted(_VALID_ENTRIES)}",
                "detail": f"entry must be one of {sorted(_VALID_ENTRIES)}",
            },
            status_code=400,
        )

    sport_filter = None
    if sport:
        sport_filter = {s.strip().upper() for s in sport.split(",") if s.strip()}

    key = _cache_key(
        entry=entry,
        min_true_prob=min_true_prob,
        min_edge_pp=min_edge_pp,
        sport=sport or "",
        full_game_only=full_game_only,
        mispriced_only=mispriced_only,
        n_entries=n_entries,
        line_tolerance=line_tolerance,
    )

    if not refresh and _CACHE.get("payload") is not None and _CACHE.get("key") == key:
        return JSONResponse(_CACHE["payload"])

    payload = compare_fantasy_vs_sharp(
        entry_type=entry,
        min_true_prob=min_true_prob,
        min_edge_pp=min_edge_pp,
        sport_filter=sport_list if sport else None,
        full_game_only=full_game_only,
        mispriced_only=mispriced_only,
        n_entries=n_entries,
        force_fetch=refresh or _CACHE.get("payload") is None,
        return_ranked=True,
        line_tolerance=line_tolerance,
    )
    # compare_fantasy_vs_sharp returns (payload_dict, ranked_list) when
    # return_ranked=True. The ranked list is stored separately for lineups.
    if isinstance(payload, tuple):
        payload, ranked = payload
    else:
        ranked = []
    _CACHE["payload"] = payload
    _CACHE["key"] = key
    _RANKED_CACHE[key] = ranked

    # Dashboard v2: cache the per-fantasy-book lookup so /api/lineups and
    # /api/props can attach fantasy_books {underdog, prizepicks, sleeper}
    # true_probs to each opp payload.
    from ud_edge.compare import collect_fantasy_legs
    import re as _re
    try:
        fantasy_legs, _ = collect_fantasy_legs(force_fetch=False)
        lookup: dict = {}
        for fl in fantasy_legs:
            name = _re.sub(r"[^\w\s]", "", (fl.player_name or "").lower()).strip()
            name = _re.sub(r"\s+", " ", name)
            key2 = (name, fl.stat_name, fl.line_value)
            lookup.setdefault(key2, []).append(fl)
        _FANTASY_LOOKUP_CACHE[key] = lookup
    except Exception:
        _FANTASY_LOOKUP_CACHE[key] = {}

    return JSONResponse(payload)


@app.get("/api/sports")
def sports_list():
    payload = _CACHE.get("payload")
    if not payload:
        # Lightweight empty response — client should call /api/opportunities first
        return {"sports": []}
    return {
        "sports": [
            {"sport": s["sport"], "count": s["count"], "mispriced_count": s["mispriced_count"]}
            for s in payload.get("sports", [])
        ]
    }


# ── /api/props: raw sharp + fantasy board ─────────────────────────────────────

@app.get("/api/props")
def raw_props(
    sport: Optional[str] = Query(None, description="Comma-separated sports to filter"),
):
    """Return the raw sharp + fantasy observations from PropLine — every
    player+stat+line for every sport.

    This is the "raw board" endpoint for the props wall. Results are cached
    for 60 seconds per sport to avoid burning PropLine budget on every tab click.

    Each observation dict contains:
      player, stat, line, over_decimal, under_decimal,
      bookmaker, book_type ('sharp'|'fantasy'), sport_id, event, commence
    """
    # Build sport filter
    sport_filter: Optional[set[str]] = None
    if sport:
        sport_filter = {s.strip().upper() for s in sport.split(",") if s.strip()}

    cache_key = f"props:{sport or 'all'}"

    # Check 60-second TTL cache
    now = time.time()
    if (
        _RAW_PROPS_CACHE.get("data") is not None
        and _RAW_PROPS_CACHE.get("key") == cache_key
        and (now - _RAW_PROPS_CACHE.get("fetched_at", 0)) < 60.0
    ):
        return JSONResponse(_RAW_PROPS_CACHE["data"])

    pl_key = os.environ.get("PROPLINE_API_KEY", "")
    if not pl_key:
        return JSONResponse(
            {"error": "PROPLINE_API_KEY not configured", "detail": "Set the PROPLINE_API_KEY environment variable to use /api/props"},
            status_code=503,
        )

    try:
        from ud_edge.propline_client import build_propline_indexes

        sports_list = list(sport_filter) if sport_filter else ["NBA", "NFL", "MLB", "NHL", "WNBA", "CFB", "MLS", "EPL"]
        sharp_index, fantasy_props, pl_meta = build_propline_indexes(
            api_key=pl_key,
            sports=sports_list,
            cache_path=Path("data/sharp_cache"),
        )

        # Build the raw board: every sharp observation + every fantasy observation
        observations: list[dict] = []

        # Sharp observations (best book per player/stat/line)
        for key, info in sharp_index.items():
            player = info.get("player_name", key.split("|")[0] if "|" in key else key)
            stat = info.get("stat_name", key.split("|")[1] if "|" in key else "points")
            observations.append({
                "player": player,
                "stat": stat,
                "line": info.get("line_value"),
                "over_decimal": info.get("over_decimal"),
                "under_decimal": info.get("under_decimal"),
                "bookmaker": info.get("bookmaker"),
                "book_type": "sharp",
                "sport_id": info.get("sport_id"),
                "event": info.get("event"),
                "commence": info.get("commence"),
                "source": info.get("source", "propline"),
            })

        # Fantasy observations (each fantasy book independently)
        for p in fantasy_props:
            observations.append({
                "player": p.get("player"),
                "stat": p.get("stat"),
                "line": p.get("line"),
                "over_decimal": p.get("over_decimal"),
                "under_decimal": p.get("under_decimal"),
                "bookmaker": p.get("bookmaker"),
                "book_type": "fantasy",
                "sport_id": p.get("sport_id"),
                "event": p.get("event"),
                "commence": p.get("commence"),
                "source": p.get("source", "propline"),
            })

        # Apply sport filter at observation level
        if sport_filter:
            observations = [
                o for o in observations
                if (o.get("sport_id") or "").upper() in sport_filter
            ]

        payload = {
            "observations": observations,
            "counts": {
                "sharp": len(sharp_index),
                "fantasy": len(fantasy_props),
                "total": len(observations),
            },
            "meta": pl_meta,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }

        _RAW_PROPS_CACHE["data"] = payload
        _RAW_PROPS_CACHE["key"] = cache_key
        _RAW_PROPS_CACHE["fetched_at"] = now
        return JSONResponse(payload)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            {"error": "Failed to fetch raw props", "detail": str(e)},
            status_code=500,
        )


# ── /api/lineups: correlation-aware 6-man + 4-man fallback ───────────────────

@app.get("/api/lineups")
def lineup_suggestions(
    entry: str = Query("6-flex"),
    n_entries: int = Query(4, ge=1, le=8),
    prefer_6man: bool = Query(True),
    # Audit residual v3 (remediation v3): forward line_tolerance so operators
    # can see lineups built from a tolerance they chose. The lineups endpoint
    # reads from _RANKED_CACHE populated by /api/opportunities, so we verify
    # the requested tolerance matches what was used in the cache. If not, we
    # 409 with instructions to refresh opportunities first (rather than
    # silently returning lineups built from a different tolerance).
    line_tolerance: Optional[float] = Query(
        None,
        ge=0.0,
        le=5.0,
        description="Must match the tolerance used in /api/opportunities. "
                    "If omitted, we use whatever the latest cache used.",
    ),
):
    """Return correlation-aware suggested lineups.

    Calls `select_lineups_for_card()` with the currently cached ranked legs
    to produce 6-flex lineups with correlation cleaning; falls back to 4-flex
    when fighting correlation is too high.

    Returns both the lineups and correlation warnings (avg_abs_rho,
    fighting_pairs, scenario probabilities).

    Audit (remediation v3): lineups consume `_RANKED_CACHE` from
    /api/opportunities. If line_tolerance is specified here but differs from
    the value used to populate the cache, return 409 to avoid silently
    returning mismatched data.
    """
    # Cross-check line_tolerance against the cache key the opportunities run used.
    if line_tolerance is not None:
        cache_key = _CACHE.get("key", "")
        # Cache key includes "line_tolerance=<value>" — extract it.
        m = re.search(r"line_tolerance=([^|]+)", cache_key)
        cached_lt: Optional[float] = None
        if m:
            try:
                cached_lt = float(m.group(1))
            except (TypeError, ValueError):
                cached_lt = None
        if cached_lt is not None and abs(cached_lt - line_tolerance) > 1e-9:
            return JSONResponse(
                {
                    "error": (
                        f"line_tolerance={line_tolerance} requested but the "
                        f"cached opportunities run used line_tolerance="
                        f"{cached_lt}. Refresh /api/opportunities with the "
                        f"desired tolerance first, then call /api/lineups."
                    ),
                    "cached_line_tolerance": cached_lt,
                    "requested_line_tolerance": line_tolerance,
                    "fix": (
                        f"GET /api/opportunities?line_tolerance={line_tolerance}"
                    ),
                },
                status_code=409,
            )

    payload = _CACHE.get("payload")
    if not payload:
        return JSONResponse(
            {"error": "No data yet — call /api/opportunities first"},
            status_code=404,
        )

    if entry not in _VALID_ENTRIES:
        return JSONResponse(
            {"error": f"Invalid entry type '{entry}'. Valid options: {sorted(_VALID_ENTRIES)}"},
            status_code=400,
        )

    try:
        from ud_edge.lineup_selector import select_lineups_for_card
        from ud_edge.copy_format import opportunities_to_dict, format_block
        from ud_edge.flex_math import UD_PAYOUTS

        # Pull the live ranked legs directly from the side-cache so we don't
        # have to reconstruct Pydantic objects from a JSON payload that has
        # already lost line_id / player_id / match_id / higher_american etc.
        # This is the round-trip-safe path.
        cache_key = _CACHE.get("key")
        ranked: list = _RANKED_CACHE.get(cache_key, [])
        if not ranked:
            # Fallback: derive from flat payload (best-effort)
            from ud_edge.models import RankedLeg, Leg

            flat = payload.get("flat", [])
            for d in flat:
                leg_dict = d.get("leg", {})
                leg = Leg(
                    line_id=leg_dict.get("line_id", ""),
                    player_id=leg_dict.get("player_id", ""),
                    player_name=leg_dict.get("player_name", ""),
                    sport_id=leg_dict.get("sport_id", ""),
                    match_title=leg_dict.get("match_title"),
                    match_id=leg_dict.get("match_id"),
                    scheduled_at=leg_dict.get("scheduled_at"),
                    stat_name=leg_dict.get("stat_name", "points"),
                    line_value=float(leg_dict.get("line_value") or 0),
                    line_type=leg_dict.get("line_type", "balanced"),
                    higher_american=leg_dict.get("higher_american", -110),
                    higher_decimal=float(leg_dict.get("higher_decimal") or 1.91),
                    higher_multiplier=leg_dict.get("higher_multiplier", 0.9),
                    lower_american=leg_dict.get("lower_american", -110),
                    lower_decimal=float(leg_dict.get("lower_decimal") or 1.91),
                    lower_multiplier=leg_dict.get("lower_multiplier", 0.9),
                    fantasy_source=leg_dict.get("fantasy_source") or "",
                    team_id=leg_dict.get("team_id"),
                )
                ranked.append(
                    RankedLeg(
                        leg=leg,
                        higher_true_prob=d.get("higher_true_prob"),
                        higher_implied_prob=d.get("higher_implied_prob") or 0.0,
                        higher_edge_pp=d.get("higher_edge_pp") or 0.0,
                        lower_true_prob=d.get("lower_true_prob"),
                        lower_implied_prob=d.get("lower_implied_prob") or 0.0,
                        lower_edge_pp=d.get("lower_edge_pp") or 0.0,
                        picked_side=d.get("picked_side", "higher"),
                        picked_true_prob=d.get("picked_true_prob", 0.5),
                        picked_edge_pp=d.get("picked_edge_pp", 0.0),
                        overround=d.get("overround", 1.0),
                        sharp_true_prob=d.get("sharp_true_prob"),
                        sharp_book=d.get("sharp_book"),
                        sharp_overround=d.get("sharp_overround"),
                        mispricing_edge_pp=d.get("mispricing_edge_pp"),
                    )
                )

        if not ranked:
            return JSONResponse({"error": "No ranked legs available"}, status_code=404)

        entry_obj = UD_PAYOUTS[entry]

        # Dashboard v2: pull the cached per-fantasy-book lookup so the lineup
        # payload can attach fantasy_books {underdog, prizepicks, sleeper}.
        _fantasy_lookup = _FANTASY_LOOKUP_CACHE.get(cache_key, {})

        result = select_lineups_for_card(
            ranked,
            prefer_6man=prefer_6man,
            max_entries=n_entries,
        )

        # Build correlation reports for each lineup
        from ud_edge.correlation import analyze_slip
        from ud_edge.flex_math import expected_value_per_card
        from ud_edge.matcher import effective_true_prob

        lineups_payload = []
        all_warnings: list[dict] = []
        for i, lineup in enumerate(result.lineups, 1):
            report = analyze_slip(lineup)
            # Audit P0: avg uses effective_true_prob (sharp when matched);
            # win_prob/ev/median_payout use expected_value_per_card (heterogeneous exact).
            per_leg = [effective_true_prob(r.picked_true_prob, r.sharp_true_prob) for r in lineup]
            avg_prob = sum(per_leg) / len(per_leg)
            ev, win_prob, med = expected_value_per_card(entry_obj, per_leg)
            lineups_payload.append({
                "entry": i,
                "n_legs": len(lineup),
                "avg_true_prob": round(avg_prob, 4),
                "win_prob": round(win_prob, 4) if win_prob else None,
                "median_payout": med,
                "ev": round(ev, 4) if ev is not None else None,
                "opportunities": [
                    opportunities_to_dict(
                        r,
                        break_even=entry_obj.break_even,
                        fantasy_books_lookup=_fantasy_lookup,
                    )
                    for r in lineup
                ],
                "copy": {
                    "prizepicks": format_block(lineup, "prizepicks", include_header=True),
                    "sleeper": format_block(lineup, "sleeper", include_header=True),
                    "underdog": format_block(lineup, "underdog", include_header=True),
                },
                "correlation": {
                    "avg_abs_rho": round(report.avg_abs_rho, 4),
                    "fighting_pairs": report.fighting_pairs,
                    "positive_pairs": report.positive_pairs,
                    "recommend_entry": report.recommend_entry,
                    "recommend_reason": report.recommend_reason,
                },
            })
            if report.fighting_pairs > 0 or report.avg_abs_rho > 0.3:
                all_warnings.append({
                    "entry": i,
                    "fighting_pairs": report.fighting_pairs,
                    "avg_abs_rho": round(report.avg_abs_rho, 4),
                    "recommend_entry": report.recommend_entry,
                })

        # Dashboard v2: sort lineups by win_prob DESC (most-likely-first).
        # Tie-break by avg_true_prob DESC, then by EV DESC.
        lineups_payload.sort(
            key=lambda lu: (
                -(lu.get("win_prob") or 0.0),
                -(lu.get("avg_true_prob") or 0.0),
                -(lu.get("ev") or 0.0),
            )
        )
        for i, lu in enumerate(lineups_payload, 1):
            lu["entry"] = i

        dropped_payload = []
        for dropped_leg, reason in result.dropped_legs:
            dropped_payload.append({
                "player": dropped_leg.leg.player_name,
                "stat": dropped_leg.leg.stat_name,
                "reason": reason,
            })

        return JSONResponse({
            "entry_type": entry,
            "n_entries": len(result.lineups),
            "lineups": lineups_payload,
            "dropped_legs": dropped_payload,
            "correlation_warnings": all_warnings,
            "total_ranked_pool": len(ranked),
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            {"error": "Failed to build lineups", "detail": str(e)},
            status_code=500,
        )


# ── /api/alerts/recent: last N alerts from data/alerts.jsonl ─────────────────

@app.get("/api/alerts/recent")
def recent_alerts(
    limit: int = Query(10, ge=1, le=200),
):
    """Return the last N alert entries from data/alerts.jsonl (newest first)."""
    alerts_path = Path("data/alerts.jsonl")
    if not alerts_path.exists():
        return JSONResponse({"alerts": [], "total": 0, "limit": limit})

    try:
        lines = alerts_path.read_text(encoding="utf-8").splitlines()
        # Parse all JSON lines
        parsed: list[dict] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(parsed) >= limit:
                break

        return JSONResponse({
            "alerts": parsed[:limit],
            "total": len(parsed[:limit]),
            "limit": limit,
            "source": str(alerts_path),
        })
    except Exception as e:
        return JSONResponse(
            {"error": "Failed to read alerts", "detail": str(e)},
            status_code=500,
        )


# ── /api/budget: PropLine daily call budget snapshot ──────────────────────────

@app.get("/api/budget")
def budget_snapshot():
    """Return the current PropLine daily call budget state.

    Reads from data/propline_budget.json and returns a BudgetSnapshot dict
    with: day, used, limit, reserve, remaining_scheduled, remaining_total, exhausted.
    """
    try:
        from ud_edge.budget import CallBudget
        budget = CallBudget(path=Path("data/propline_budget.json"))
        snap = budget.snapshot()
        return JSONResponse({
            "day": snap.day,
            "used": snap.used,
            "limit": snap.limit,
            "reserve": snap.reserve,
            "remaining_scheduled": snap.remaining_scheduled,
            "remaining_total": snap.remaining_total,
            "exhausted": snap.exhausted,
            "pct_used": round(snap.used / snap.limit * 100, 2) if snap.limit > 0 else 0,
        })
    except Exception as e:
        return JSONResponse(
            {"error": "Failed to read budget", "detail": str(e)},
            status_code=500,
        )


@app.get("/api/export/{platform}")
def export_platform(
    platform: str,
    sport: Optional[str] = Query(None),
):
    payload = _CACHE.get("payload")
    if not payload:
        return JSONResponse({"error": "No data yet — refresh opportunities first"}, status_code=404)

    platform = platform.lower()
    if platform not in ("prizepicks", "sleeper", "underdog", "generic"):
        return JSONResponse({"error": f"Unknown platform: {platform}"}, status_code=400)

    if sport:
        for block in payload.get("sports", []):
            if block["sport"].upper() == sport.upper():
                text = block.get("copy", {}).get(platform, "")
                return JSONResponse({"platform": platform, "sport": sport.upper(), "text": text})
        return JSONResponse({"error": f"Sport not found: {sport}"}, status_code=404)

    text = payload.get("copy_all", {}).get(platform, "")
    return JSONResponse({"platform": platform, "sport": None, "text": text})


@app.get("/")
def index():
    index_path = STATIC_DIR / "index.html"
    return FileResponse(index_path)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Convert FastAPI's default 422 validation errors to 400 with a clear JSON body."""
    return JSONResponse(
        status_code=400,
        content={
            "error": "Invalid request parameters",
            "detail": exc.errors(),
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Return JSON errors for unexpected HTTP exceptions instead of HTML."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """Catch-all: any unhandled Python exception returns a 500 JSON response.

    This prevents raw Python tracebacks from leaking into API responses and
    crashes the JSON encoder (e.g. from NaN/inf floats that slipped past
    earlier guards).
    """
    import sys
    import traceback

    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(f"[Unhandled exception] {exc}\n{tb}", file=sys.stderr)

    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": "An unexpected error occurred. Please try again or refresh the slate.",
        },
    )


@app.get("/HONEST_STATUS.md")
def honest_status():
    """Serve HONEST_STATUS.md from the project root as plain text."""
    from ud_edge.compare import _project_root

    md_path = _project_root() / "HONEST_STATUS.md"
    if not md_path.exists():
        return JSONResponse({"error": "HONEST_STATUS.md not found"}, status_code=404)
    return FileResponse(md_path, media_type="text/plain")


# Mount static assets (css/js) under /static
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
