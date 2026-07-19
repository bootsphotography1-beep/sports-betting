"""FastAPI dashboard for sharp-vs-fantasy mispricing opportunities.

Run:
    python -m ud_edge --serve
    # or
    uvicorn ud_edge.dashboard.app:app --reload --host 127.0.0.1 --port 8787
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ud_edge.compare import compare_fantasy_vs_sharp

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


# Mount static assets (css/js) under /static
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
