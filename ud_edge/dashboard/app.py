"""FastAPI dashboard for sharp-vs-fantasy mispricing opportunities.

Run:
    python -m ud_edge --serve
    # or
    uvicorn ud_edge.dashboard.app:app --reload --host 127.0.0.1 --port 8787
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

import math

from ud_edge.compare import compare_fantasy_vs_sharp
from ud_edge.flex_math import UD_PAYOUTS

# Valid entry types — used to gate the /api/opportunities endpoint
_VALID_ENTRIES: set[str] = set(UD_PAYOUTS.keys())

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Edge Board",
    description="Sharp-book vs fantasy mispricing dashboard",
    version="0.2.0",
)

# In-memory cache of last comparison (avoids re-fetch on every tab click)
_CACHE: dict = {"payload": None, "key": None}


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
    )

    if not refresh and _CACHE.get("payload") is not None and _CACHE.get("key") == key:
        return JSONResponse(_CACHE["payload"])

    payload = compare_fantasy_vs_sharp(
        entry_type=entry,
        min_true_prob=min_true_prob,
        min_edge_pp=min_edge_pp,
        sport_filter=sport_filter,
        full_game_only=full_game_only,
        mispriced_only=mispriced_only,
        n_entries=n_entries,
        force_fetch=refresh or _CACHE.get("payload") is None,
    )
    _CACHE["payload"] = payload
    _CACHE["key"] = key
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
