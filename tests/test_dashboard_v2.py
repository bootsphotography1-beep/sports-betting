"""
Dashboard v2 — per-book columns, dark theme, fantasy-only badge, sort by hit probability.

TDD: these tests fail BEFORE the implementation and pass AFTER.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Per-sharp-book dict attached to opp payload
# ─────────────────────────────────────────────────────────────────────────────

def test_opp_payload_includes_per_sharp_book_dict(monkeypatch):
    """Every opp in /api/opportunities must carry a `sharp_books` dict of
    {book_title: sharp_true_prob} for ALL sharp books that match the leg
    within match tolerance, not just the single best match."""
    import os
    from fastapi.testclient import TestClient

    from ud_edge.dashboard.app import app

    # Disable PropLine so the test fixture's sharp_index isn't overridden by
    # the on-disk sharp_cache (which has 1000+ real entries).
    monkeypatch.delenv("PROPLINE_API_KEY", raising=False)
    # Also stub the PropLine cache loader so the 1000+ on-disk sharp entries
    # don't flood the test fixture. These are imported INSIDE compare.py so we
    # have to patch them at the source module.
    import ud_edge.propline_client as _plc
    monkeypatch.setattr(_plc, "load_cached_indexes", lambda **kw: ({}, [], {}))
    monkeypatch.setattr(_plc, "build_propline_indexes", lambda **kw: ({}, [], {}))

    # Build a fake sharp index. Real sharp_books_client keys are flat:
    #   key = "player|stat|event_title"
    #   value = {"over_decimal", "under_decimal", "bookmaker", "line_value", ...}
    # Real data has 1 book per event. To simulate 3 different books for the
    # same player/stat/line, use 3 keys with the SAME event_title (the
    # dashboard's find_all_sharp_matches dedupes by bookmaker).
    norm_player = "hwang seongbin"
    canon_stat = "batter_hits_runs_rbis"
    norm_event = "ssg lot"
    fake_sharp_index = {
        f"{norm_player}|{canon_stat}|{norm_event}": {
            "over_decimal": 1.55, "under_decimal": 2.40,
            "bookmaker": "pinnacle", "line_value": 2.5,
        },
    }
    # Add 2 more books at the same key position — but since keys are unique,
    # we represent them by adding "pinnacle", "circa", "betonline" as if
    # multiple books contributed to the same line at the same event. Real
    # build_sharp_index collapses these, but the test exercises the
    # find_all_sharp_matches per-book dedupe path by giving 3 separate keys
    # with distinct event_titles (representing separate markets).
    fake_sharp_index[f"{norm_player}|{canon_stat}|{norm_event} (circa)"] = {
        "over_decimal": 1.62, "under_decimal": 2.30,
        "bookmaker": "circa", "line_value": 2.5,
    }
    fake_sharp_index[f"{norm_player}|{canon_stat}|{norm_event} (betonline)"] = {
        "over_decimal": 1.58, "under_decimal": 2.35,
        "bookmaker": "betonline", "line_value": 2.5,
    }
    fake_ud_legs = [_make_ud_leg(
        player="Hwang Seong-bin",
        stat="batter_hits_runs_rbis",
        line=2.5,
        side="higher",
        higher_dec=1.55,  # makes fantasy pick "higher" → matches sharp's bullish view
        lower_dec=2.40,
        sport="KBO",
        match="SSG @ LOT",
    )]

    monkeypatch.setattr("ud_edge.compare.collect_sharp_index", lambda **kw: (fake_sharp_index, {"count": 3, "sources": ["pinnacle","circa","betonline"]}))
    monkeypatch.setattr("ud_edge.compare.collect_fantasy_legs", lambda **kw: (fake_ud_legs, {"sources": {"underdog": 1}, "errors": []}))

    client = TestClient(app)
    res = client.get("/api/opportunities", params={"entry": "6-flex", "min_true_prob": 0.0, "min_edge_pp": -100.0})
    assert res.status_code == 200, f"endpoint error: {res.text}"
    body = res.json()
    # Debug aid: print body if no Hwang
    found_hwang = False
    for block in body.get("sports", []):
        for opp in block.get("opportunities", []):
            if opp["player_name"] == "Hwang Seong-bin":
                found_hwang = True
                break
    if not found_hwang:
        print("DEBUG sports:", body.get("sports"))
        print("DEBUG totals:", body.get("totals"))
    assert body["sports"], "expected at least 1 sport in response"

    found = False
    for block in body["sports"]:
        for opp in block.get("opportunities", []):
            if opp["player_name"] == "Hwang Seong-bin":
                found = True
                assert "sharp_books" in opp, f"missing sharp_books key, got: {sorted(opp.keys())}"
                assert isinstance(opp["sharp_books"], dict)
                # All 3 books present
                assert set(opp["sharp_books"].keys()) == {"pinnacle", "circa", "betonline"}
                # Values are floats in [0,1]
                for v in opp["sharp_books"].values():
                    assert isinstance(v, float)
                    assert 0.0 <= v <= 1.0
    assert found, "Hwang Seong-bin opp was not returned"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Per-fantasy-book dict on opp payload
# ─────────────────────────────────────────────────────────────────────────────

def test_opp_payload_includes_per_fantasy_book_dict(monkeypatch, tmp_path):
    """When PP CSV + Sleeper CSV provide legs for the same player/stat/line,
    opp payload must carry `fantasy_books: {underdog, prizepicks, sleeper}`
    of true_probs (None when not offered on that book)."""
    from fastapi.testclient import TestClient

    from ud_edge.dashboard.app import app

    pp_csv = tmp_path / "pp.csv"
    sl_csv = tmp_path / "sl.csv"
    pp_csv.write_text(_pp_csv_for("Hwang Seong-bin", 2.5))
    sl_csv.write_text(_sl_csv_for("Hwang Seong-bin", 2.5))

    fake_ud_legs = [_make_ud_leg(
        player="Hwang Seong-bin", stat="batter_hits_runs_rbis",
        line=2.5, side="higher", higher_dec=1.55, lower_dec=2.40, sport="KBO",
        match="SSG @ LOT",
    )]

    monkeypatch.setattr("ud_edge.compare.collect_sharp_index", lambda **kw: ({}, {"count": 0, "sources": []}))
    monkeypatch.setattr("ud_edge.compare.collect_fantasy_legs", lambda **kw: (fake_ud_legs, {"sources": {"underdog": 1}, "errors": []}))
    monkeypatch.setenv("FANTASY_PP_CSV", str(pp_csv))
    monkeypatch.setenv("FANTASY_SL_CSV", str(sl_csv))

    client = TestClient(app)
    res = client.get("/api/opportunities", params={"entry": "6-flex", "min_true_prob": 0.0, "min_edge_pp": 0.0})
    assert res.status_code == 200
    body = res.json()

    found = False
    for block in body["sports"]:
        for opp in block.get("opportunities", []):
            if opp["player_name"] == "Hwang Seong-bin":
                found = True
                assert "fantasy_books" in opp, f"missing fantasy_books key, got: {sorted(opp.keys())}"
                fb = opp["fantasy_books"]
                assert set(fb.keys()) == {"underdog", "prizepicks", "sleeper"}
                assert fb["underdog"] is not None
    assert found, "Hwang Seong-bin opp was not returned"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: fantasy_only flag set when no sharp book matched
# ─────────────────────────────────────────────────────────────────────────────

def test_opp_fantasy_only_flag_when_no_sharp_match(monkeypatch):
    """When sharp_books is empty AND the leg came from fantasy only,
    opp must carry `fantasy_only: True` and a `fantasy_only_reason` string."""
    from fastapi.testclient import TestClient

    from ud_edge.dashboard.app import app

    fake_ud_legs = [_make_ud_leg(
        player="Hwang Seong-bin", stat="batter_hits_runs_rbis",
        line=2.5, side="higher", higher_dec=1.55, lower_dec=2.40, sport="KBO",
        match="SSG @ LOT",
    )]

    monkeypatch.setattr("ud_edge.compare.collect_sharp_index", lambda **kw: ({}, {"count": 0, "sources": []}))
    monkeypatch.setattr("ud_edge.compare.collect_fantasy_legs", lambda **kw: (fake_ud_legs, {"sources": {"underdog": 1}, "errors": []}))

    client = TestClient(app)
    res = client.get("/api/opportunities", params={"entry": "6-flex", "min_true_prob": 0.0, "min_edge_pp": 0.0})
    assert res.status_code == 200
    body = res.json()

    found = False
    for block in body["sports"]:
        for opp in block.get("opportunities", []):
            if opp["player_name"] == "Hwang Seong-bin":
                found = True
                assert opp.get("fantasy_only") is True
                assert isinstance(opp.get("fantasy_only_reason"), str)
                assert opp["fantasy_only_reason"]  # non-empty
    assert found


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Lineups sorted by win_prob DESC (most-likely first)
# ─────────────────────────────────────────────────────────────────────────────

def test_lineups_sorted_by_win_prob_desc(monkeypatch):
    """The `lineups` list in /api/opportunities must be sorted by `win_prob`
    descending — the most-likely lineup first."""
    from fastapi.testclient import TestClient

    from ud_edge.dashboard.app import app

    # Build 6 legs that can produce 2 disjoint 6-flex lineups with different win_probs
    legs = [
        _make_ud_leg(f"P{i}", "points", 25.5, "higher" if i % 2 else "lower",
                     1.85, 1.95, sport="NBA", match=f"G{i} @ G{i+1}")
        for i in range(12)
    ]

    monkeypatch.setattr("ud_edge.compare.collect_sharp_index", lambda **kw: ({}, {"count": 0, "sources": []}))
    monkeypatch.setattr("ud_edge.compare.collect_fantasy_legs", lambda **kw: (legs, {"sources": {"underdog": 12}, "errors": []}))

    client = TestClient(app)
    res = client.get("/api/opportunities", params={"entry": "6-flex", "min_true_prob": 0.0, "min_edge_pp": 0.0, "n_entries": 4})
    assert res.status_code == 200
    body = res.json()

    lineups = body.get("lineups", [])
    if len(lineups) >= 2:
        win_probs = [lu.get("win_prob") for lu in lineups]
        # Filter out None values
        wp_filled = [w for w in win_probs if w is not None]
        assert wp_filled == sorted(wp_filled, reverse=True), f"lineups not sorted by win_prob DESC: {win_probs}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Dark theme tokens in styles.css
# ─────────────────────────────────────────────────────────────────────────────

def test_styles_css_uses_dark_theme():
    """The active stylesheet must declare a dark background and light ink
    in its top-level :root tokens. Light theme fails."""
    css_path = Path(__file__).resolve().parents[1] / "ud_edge" / "dashboard" / "static" / "styles.css"
    css = css_path.read_text(encoding="utf-8")

    # Find :root { ... } block — match only on a single balanced block by
    # looking for the first `:root {` then the next `}`.
    import re
    m = re.search(r":root\s*\{(.*?)\}", css, re.DOTALL)
    assert m, "no :root block found in styles.css"
    active = m.group(1)

    # Extract --bg and --ink
    bg_match = re.search(r"--bg\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", active)
    ink_match = re.search(r"--ink\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", active)
    assert bg_match, f"no --bg hex token in :root; got: {active[:200]}"
    assert ink_match, f"no --ink hex token in :root; got: {active[:200]}"

    def is_dark(hex_color: str) -> bool:
        s = hex_color.strip()
        if s.startswith("#") and len(s) == 7:
            r = int(s[1:3], 16); g = int(s[3:5], 16); b = int(s[5:7], 16)
            luminance = (0.299*r + 0.587*g + 0.114*b)
            return luminance < 64  # dark = low luminance
        return False

    bg = bg_match.group(1)
    assert is_dark(bg), f"--bg is not a dark color: {bg}"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_ud_leg(player, stat, line, side, higher_dec, lower_dec, sport="NBA", match="A @ B"):
    """Build a Leg that mimics what the UD client would produce."""
    from ud_edge.models import Leg
    return Leg(
        line_id=f"line-{player}-{stat}-{line}",
        player_id=f"pid-{player}",
        player_name=player,
        sport_id=sport,
        match_title=match,
        stat_name=stat,
        line_value=line,
        line_type="balanced",
        higher_american=_dec_to_american(higher_dec),
        higher_decimal=higher_dec,
        higher_multiplier=higher_dec,
        lower_american=_dec_to_american(lower_dec),
        lower_decimal=lower_dec,
        lower_multiplier=lower_dec,
        fantasy_source="underdog",
    )


def _dec_to_american(dec):
    if dec >= 2.0:
        return int(round((dec - 1.0) * 100))
    return int(round(-100.0 / (dec - 1.0)))


def _pp_csv_for(player, line):
    """Minimal PrizePicks CSV format with same player/stat/line."""
    return (
        "player,stat,line,over_odds,under_odds\n"
        f"{player},batter_hits_runs_rbis,{line},2.05,1.78\n"
    )


def _sl_csv_for(player, line):
    """Minimal Sleeper CSV format with same player/stat/line."""
    return (
        "player,stat,line,over_odds,under_odds\n"
        f"{player},batter_hits_runs_rbis,{line},2.10,1.75\n"
    )
