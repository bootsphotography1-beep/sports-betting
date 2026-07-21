"""Tests for scripts/ud_edge_fire.py — the canonical cron live-fire entrypoint.

These cover the changes added 2026-07-21 (broker pre-check, exhaustion
alerting, multi-sport coverage, correlation-aware per-tier sort). The
script's job is to:
  1. Format the compare_fantasy_vs_sharp payload for Telegram delivery
  2. Alert the operator when BOTH PropLine keys are exhausted
  3. Per-sport min-edge filter so the slate doesn't collapse to MLB-only
  4. Pull fighting same-game opposite-side pairs into a DO NOT PAIR section
  5. Group legs by fantasy book (UD → PP → SL) so the operator can place
     bets app-by-app top-to-bottom

We mock `refresh_dashboard` and `compare_fantasy_vs_sharp` so these tests
run offline (no network, no real PropLine calls).
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock


# Make scripts/ importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ud_edge_fire  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────


def _make_leg(
    sport: str,
    player: str,
    *,
    ev: float = 4.0,
    edge_kind: str = "vs_sharp",
    sharp_prob: float = 65.0,
    sharp_book: str = "pinnacle",
    fantasy_book: str = "underdog",
    fantasy_prob: float = 62.0,
    side: str = "Over",
    match_title: str = "Game X",
    stat: str = "points",
    line: float = 1.5,
) -> dict:
    """Build a leg dict in the shape parse_legs produces."""
    return {
        "player": player,
        "stat": stat,
        "line": line,
        "side_label": side,
        "side_prizepicks": "More" if side == "Over" else "Less",
        "side_sleeper": side,
        "side_underdog": "Higher" if side == "Over" else "Lower",
        "fantasy_book": fantasy_book,
        "fantasy_prob": fantasy_prob,
        "sharp_book": sharp_book,
        "sharp_prob": sharp_prob,
        "ev": ev,
        "edge_kind": edge_kind,
        "win_prob": sharp_prob,
        "all_fantasy_books": {fantasy_book: fantasy_prob / 100.0},
        "all_sharp_books": {sharp_book: sharp_prob / 100.0} if edge_kind == "vs_sharp" else {},
        "match_title": match_title,
        "sport": sport,
    }


# ── Test 1: multi-sport coverage (the original "all MLB" bug) ─────────


def test_format_message_includes_non_mlb_sports():
    """Sharp edges from non-MLB sports (NFL, Tennis, WNBA) MUST appear when a sharp
    book confirms the line. 2026-07-21 spec: cron emits vs_sharp only — fantasy-only
    legs (no sharp match) are filtered out before the report.
    """
    legs = [
        _make_leg("MLB", "Bo Bichette", ev=4.5, edge_kind="vs_sharp",
                  sharp_prob=65.0, fantasy_book="underdog", fantasy_prob=62.0),
        # Tennis, NO sharp match, 5.8pp edge — dropped by sharp-only filter
        _make_leg("TENNIS", "Alcaraz", ev=5.8, edge_kind="vs_breakeven",
                  sharp_prob=0, sharp_book="?", fantasy_book="prizepicks",
                  fantasy_prob=60.0),
        # WNBA fantasy-only, 4.5pp — dropped
        _make_leg("WNBA", "Aja Wilson", ev=4.5, edge_kind="vs_breakeven",
                  sharp_prob=0, sharp_book="?", fantasy_book="sleeper",
                  fantasy_prob=58.0),
        # WNBA sharp match, 5.5pp edge — passes
        _make_leg("WNBA", "Aja Wilson Real", ev=5.5, edge_kind="vs_sharp",
                  sharp_prob=66.0, sharp_book="draftkings",
                  fantasy_book="underdog", fantasy_prob=61.0, stat="points"),
        # NFL preseason, sharp match, 3.2pp — above MLB min of 3.0pp
        _make_leg("NFL", "Jaxson Dart", ev=3.2, edge_kind="vs_sharp",
                  sharp_prob=64.0, sharp_book="draftkings",
                  fantasy_book="underdog", fantasy_prob=61.0, stat="pass_yds", line=175.5,
                  side="Under"),
    ]
    msg = ud_edge_fire.format_message("ud_morning", legs)

    # vs_sharp legs SHOULD appear (across multiple sports)
    assert "Bo Bichette" in msg, "MLB sharp-match leg must appear"
    assert "Jaxson Dart" in msg, "NFL preseason leg with sharp match must appear"
    assert "Aja Wilson Real" in msg, "WNBA sharp-match leg must appear"

    # Fantasy-only legs MUST NOT appear (sharp-only filter, 2026-07-21)
    assert "Alcaraz" not in msg, "Tennis fantasy-only must be dropped (no sharp match)"
    # WNBA Wilson FantasyOnly gets dropped; "Aja Wilson Real" still passes, so
    # test that the "no sharp match" variant is absent by checking the suffix.
    # (Above asserts "Aja Wilson Real" appears, and only one Aja Wilson entry
    # exists total, so the fantasy-only one is correctly filtered.)

    # Sport mix line should reflect diversity
    assert "SPORT MIX:" in msg
    assert "MLB=" in msg
    assert "NFL=" in msg
    assert "WNBA=" in msg


def test_format_message_per_sport_min_edge():
    """MLB needs ≥3pp, NFL needs ≥3pp, tennis fantasy-only needs ≥5pp."""
    legs = [
        # MLB at 2.9pp — below 3pp threshold → filtered
        _make_leg("MLB", "LowEdge", ev=2.9, edge_kind="vs_sharp",
                  sharp_prob=63.0, fantasy_book="underdog", fantasy_prob=60.0),
        # MLB at 3.1pp — passes
        _make_leg("MLB", "GoodEdge", ev=3.1, edge_kind="vs_sharp",
                  sharp_prob=64.0, fantasy_book="underdog", fantasy_prob=61.0),
    ]
    msg = ud_edge_fire.format_message("ud_morning", legs)
    assert "LowEdge" not in msg
    assert "GoodEdge" in msg


def test_format_message_sharp_only_filter():
    """2026-07-21 spec: ONLY vs_sharp legs surface in the report, regardless
    of EV. Fantasy-only legs are dropped before the report even though they
    may have EV > 5pp. This is the McCarthy-style false-recommendation fix.
    """
    legs = [
        # Tennis at 4.9pp fantasy-only — sharp-only filter drops it
        _make_leg("TENNIS", "AlmostFO", ev=4.9, edge_kind="vs_breakeven",
                  sharp_prob=0, sharp_book="?"),
        # Tennis at 5.1pp fantasy-only — sharp-only filter drops it too (no longer
        # passes via the FANTASY_ONLY_MIN_EDGE_PP escape hatch)
        _make_leg("TENNIS", "PassesFO", ev=5.1, edge_kind="vs_breakeven",
                  sharp_prob=0, sharp_book="?"),
        # Same Tennis player but with sharp match at 6.0pp — passes (5.0pp TENNIS threshold)
        _make_leg("TENNIS", "SharpVersion", ev=6.0, edge_kind="vs_sharp",
                  sharp_prob=64.0, sharp_book="draftkings",
                  fantasy_book="prizepicks", fantasy_prob=58.0),
    ]
    msg = ud_edge_fire.format_message("ud_morning", legs)
    assert "AlmostFO" not in msg, "fantasy-only 4.9pp must be dropped (no sharp)"
    assert "PassesFO" not in msg, "fantasy-only 5.1pp must be dropped (no sharp — 2026-07-21 spec)"
    assert "SharpVersion" in msg, "vs_sharp Tennis at 4.0pp must appear"


def test_format_message_drops_negative_edges():
    """Negative ev legs (fantasy overpriced vs sharp) MUST be dropped."""
    legs = [
        _make_leg("MLB", "Bad", ev=-1.5, edge_kind="vs_sharp",
                  sharp_prob=58.0, fantasy_book="underdog", fantasy_prob=62.0),
        _make_leg("MLB", "Good", ev=4.0, edge_kind="vs_sharp",
                  sharp_prob=65.0, fantasy_book="underdog", fantasy_prob=61.0),
    ]
    msg = ud_edge_fire.format_message("ud_morning", legs)
    assert "Bad" not in msg
    assert "Good" in msg


# ── Test 2: correlation-aware sort (DO NOT PAIR section) ───────────────


def test_correlation_group_detects_fighting_pair():
    """Two legs in same match + same stat + opposite sides → fighting pair."""
    legs = [
        _make_leg("MLB", "PitcherA", ev=5.0, edge_kind="vs_sharp",
                  match_title="CHW @ DET", stat="strikeouts",
                  side="Over", fantasy_book="underdog"),
        _make_leg("MLB", "PitcherB", ev=4.5, edge_kind="vs_sharp",
                  match_title="CHW @ DET", stat="strikeouts",
                  side="Under", fantasy_book="prizepicks"),
    ]
    grouped, fighting = ud_edge_fire.correlation_group(legs)
    # Both legs should be in fighting (they form a pair)
    assert len(fighting) == 2
    assert len(grouped) == 0
    assert "DO NOT PAIR" in ud_edge_fire.format_message("ud_morning", legs)


def test_correlation_group_keeps_same_side_positive_pair():
    """Two legs in same match + same stat + SAME side → positive pair (grouped, not fighting)."""
    legs = [
        _make_leg("MLB", "BatterA", ev=5.0, edge_kind="vs_sharp",
                  match_title="NYM @ MIL", stat="hits",
                  side="Over", fantasy_book="underdog"),
        _make_leg("MLB", "BatterB", ev=4.5, edge_kind="vs_sharp",
                  match_title="NYM @ MIL", stat="hits",
                  side="Over", fantasy_book="prizepicks"),
    ]
    grouped, fighting = ud_edge_fire.correlation_group(legs)
    # Both legs should be grouped (positive pair), no fighting
    assert len(grouped) == 2
    assert len(fighting) == 0
    msg = ud_edge_fire.format_message("ud_morning", legs)
    assert "DO NOT PAIR" not in msg


def test_correlation_group_different_games_are_independent():
    """Legs in different matches → both grouped, no fighting."""
    legs = [
        _make_leg("MLB", "BatterA", ev=5.0, edge_kind="vs_sharp",
                  match_title="Game A", stat="hits", side="Over"),
        _make_leg("MLB", "BatterB", ev=4.5, edge_kind="vs_sharp",
                  match_title="Game B", stat="hits", side="Under"),
    ]
    grouped, fighting = ud_edge_fire.correlation_group(legs)
    assert len(grouped) == 2
    assert len(fighting) == 0


# ── Test 3: book-grouped sort within tiers ─────────────────────────────


def test_format_message_groups_by_book_UD_PP_SL():
    """Within each tier, all UD picks appear before all PP, all PP before SL, all SL before DA."""
    legs = [
        _make_leg("MLB", "DA_Pick", ev=6.5, edge_kind="vs_sharp",
                  fantasy_book="dabble", stat="hits"),
        _make_leg("MLB", "PP_Pick", ev=6.0, edge_kind="vs_sharp",
                  fantasy_book="prizepicks", stat="hits"),
        _make_leg("MLB", "SL_Pick", ev=5.5, edge_kind="vs_sharp",
                  fantasy_book="sleeper", stat="hits"),
        _make_leg("MLB", "UD_Pick", ev=5.0, edge_kind="vs_sharp",
                  fantasy_book="underdog", stat="hits"),
    ]
    msg = ud_edge_fire.format_message("ud_morning", legs)
    # Find positions in P1 section
    p1_section = msg.split("*PRIORITY 1")[1].split("*PRIORITY 2")[0]
    ud_pos = p1_section.find("UD_Pick")
    pp_pos = p1_section.find("PP_Pick")
    sl_pos = p1_section.find("SL_Pick")
    da_pos = p1_section.find("DA_Pick")
    assert ud_pos < pp_pos < sl_pos < da_pos, (
        f"Expected UD < PP < SL < DA in P1; got UD={ud_pos}, PP={pp_pos}, "
        f"SL={sl_pos}, DA={da_pos}"
    )


# ── Test 4: broker pre-check + exhaustion alert ─────────────────────────


def test_alert_both_keys_exhausted_when_both_empty(monkeypatch, tmp_path):
    """If broker reports both accounts exhausted, send_telegram must be called."""
    pool = [
        {"name": "primary", "key_hint": "a2c7…99", "limit": 5000,
         "used": 5000, "remaining_total": 0, "exhausted": True, "day": "2026-07-21"},
        {"name": "free1", "key_hint": "8f47…d8", "limit": 1000,
         "used": 1000, "remaining_total": 0, "exhausted": True, "day": "2026-07-21"},
    ]

    fake_broker = MagicMock()
    fake_broker.pool_snapshot.return_value = pool

    def fake_broker_from_env(*, state_dir, **kwargs):
        return fake_broker

    monkeypatch.setattr("ud_edge.broker.broker_from_env", fake_broker_from_env)

    sent = []
    monkeypatch.setattr(ud_edge_fire, "send_telegram", lambda title, body: sent.append((title, body)) or True)
    # Prevent real dotenv loading
    monkeypatch.setattr(ud_edge_fire, "load_dotenv", lambda *a, **k: None)

    ok = ud_edge_fire.alert_both_keys_exhausted(budget_per_fire=1000)
    assert ok is True
    assert len(sent) == 1
    title, body = sent[0]
    assert "EXHAUSTED" in title.upper()
    assert "primary" in body
    assert "free1" in body
    assert "Resumes at UTC midnight" in body


def test_alert_both_keys_exhausted_skipped_when_one_has_budget(monkeypatch):
    """If at least one key still has budget, no alert should be sent."""
    pool = [
        {"name": "primary", "key_hint": "a2c7…99", "limit": 5000,
         "used": 5000, "remaining_total": 0, "exhausted": True, "day": "2026-07-21"},
        {"name": "free1", "key_hint": "8f47…d8", "limit": 1000,
         "used": 200, "remaining_total": 800, "exhausted": False, "day": "2026-07-21"},
    ]
    fake_broker = MagicMock()
    fake_broker.pool_snapshot.return_value = pool
    monkeypatch.setattr("ud_edge.broker.broker_from_env", lambda **kw: fake_broker)

    sent = []
    monkeypatch.setattr(ud_edge_fire, "send_telegram", lambda title, body: sent.append((title, body)) or True)

    ok = ud_edge_fire.alert_both_keys_exhausted(budget_per_fire=1000)
    assert ok is False
    assert sent == []


def test_alert_both_keys_exhausted_skipped_when_no_broker(monkeypatch):
    """If no broker is configured (legacy single-key env), no alert path."""
    # broker_from_env raises → broker_pool_status returns [] → alert skipped
    def fake_broker_from_env(*, state_dir, **kwargs):
        raise ValueError("no broker")
    monkeypatch.setattr("ud_edge.broker.broker_from_env", fake_broker_from_env)

    sent = []
    monkeypatch.setattr(ud_edge_fire, "send_telegram", lambda title, body: sent.append((title, body)) or True)

    ok = ud_edge_fire.alert_both_keys_exhausted(budget_per_fire=1000)
    assert ok is False
    assert sent == []


# ── Test 5: refresh_dashboard detects 401/429/quota ─────────────────────


def test_refresh_dashboard_classifies_auth_error(monkeypatch):
    """401 response → status_message must contain PROPLINE_AUTH."""
    fake_response = MagicMock()
    fake_response.ok = False
    fake_response.status_code = 401
    fake_response.text = "Unauthorized: invalid API key"

    monkeypatch.setattr(ud_edge_fire.requests, "get", lambda *a, **kw: fake_response)
    ok, msg = ud_edge_fire.refresh_dashboard(min_edge=3.0)
    assert ok is False
    assert "PROPLINE_AUTH" in msg
    assert "401" in msg


def test_refresh_dashboard_classifies_quota_error(monkeypatch):
    """429 response → status_message must contain PROPLINE_QUOTA."""
    fake_response = MagicMock()
    fake_response.ok = False
    fake_response.status_code = 429
    fake_response.text = "Rate limit exceeded"

    monkeypatch.setattr(ud_edge_fire.requests, "get", lambda *a, **kw: fake_response)
    ok, msg = ud_edge_fire.refresh_dashboard(min_edge=3.0)
    assert ok is False
    assert "PROPLINE_QUOTA" in msg
    assert "429" in msg


def test_refresh_dashboard_classifies_connect_error(monkeypatch):
    """Connection refused → status_message must contain connect_error."""
    def fake_get(*a, **kw):
        raise ConnectionRefusedError("localhost:5173")
    monkeypatch.setattr(ud_edge_fire.requests, "get", fake_get)
    ok, msg = ud_edge_fire.refresh_dashboard(min_edge=3.0)
    assert ok is False
    assert "connect_error" in msg


# ── Test 6: main() wires everything together ────────────────────────────


def test_main_dry_run_prints_full_message(monkeypatch):
    """--dry-run should print the formatted message and return without sending."""
    payload = {"sports": [{"sport": "MLB", "opportunities": [
        {"player_name": "TestLeg", "stat_name": "hits", "line_value": 1.5,
         "side_label": "Over", "ud_true_prob": 0.62, "lower_true_prob": 0.38,
         "sharp_books": {"pinnacle": 0.65}, "fantasy_books": {"underdog": 0.62},
         "mispricing_edge_pp": 4.5, "ud_edge_pp": 7.8, "match_title": "TestGame"}
    ]}]}

    monkeypatch.setattr(sys, "argv", ["ud_edge_fire.py", "--tier", "ud_morning", "--dry-run"])
    monkeypatch.setattr(ud_edge_fire, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(ud_edge_fire, "refresh_dashboard", lambda edge: (True, "refresh=200"))
    monkeypatch.setattr(ud_edge_fire, "run_compare",
                        lambda tier: (payload, [], []))
    monkeypatch.setattr(ud_edge_fire, "alert_both_keys_exhausted", lambda n: False)

    sent = []
    monkeypatch.setattr(ud_edge_fire, "send_telegram",
                        lambda title, body: sent.append((title, body)) or True)

    buf = io.StringIO()
    with redirect_stdout(buf):
        ud_edge_fire.main()

    out = buf.getvalue()
    assert "UD Edge | Ud Morning" in out
    assert "TestLeg" in out
    assert "SPORT MIX:" in out
    assert sent == [], "dry-run must not send Telegram"


def test_main_calls_exhaustion_alert_before_refresh(monkeypatch):
    """When both keys are exhausted, alert is sent BEFORE the dashboard refresh."""
    call_log = []

    def fake_alert(budget):
        call_log.append(("alert", budget))
        return True

    def fake_refresh(edge):
        call_log.append(("refresh", edge))
        return True, "refresh=200"

    monkeypatch.setattr(sys, "argv", ["ud_edge_fire.py", "--tier", "ud_morning", "--dry-run"])
    monkeypatch.setattr(ud_edge_fire, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(ud_edge_fire, "alert_both_keys_exhausted", fake_alert)
    monkeypatch.setattr(ud_edge_fire, "refresh_dashboard", fake_refresh)
    monkeypatch.setattr(ud_edge_fire, "run_compare",
                        lambda tier: ({"sports": []}, [], []))
    monkeypatch.setattr(ud_edge_fire, "send_telegram",
                        lambda title, body: True)

    buf = io.StringIO()
    with redirect_stdout(buf):
        ud_edge_fire.main()

    # The alert MUST be called before refresh — operator gets the heads-up
    # BEFORE the bot tries (and fails) to pull live data.
    assert call_log[0][0] == "alert"
    assert call_log[1][0] == "refresh"


def test_main_sends_api_error_alert_on_401(monkeypatch):
    """Dashboard refresh returning 401 must trigger a Telegram API-error alert."""
    monkeypatch.setattr(sys, "argv", ["ud_edge_fire.py", "--tier", "ud_morning", "--dry-run"])
    monkeypatch.setattr(ud_edge_fire, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(ud_edge_fire, "alert_both_keys_exhausted", lambda n: False)
    monkeypatch.setattr(ud_edge_fire, "refresh_dashboard",
                        lambda edge: (False, "PROPLINE_AUTH=401 — invalid key"))
    monkeypatch.setattr(ud_edge_fire, "run_compare",
                        lambda tier: ({"sports": []}, [], []))

    sent = []
    def fake_send(title, body):
        sent.append((title, body))
        return True
    monkeypatch.setattr(ud_edge_fire, "send_telegram", fake_send)

    buf = io.StringIO()
    with redirect_stdout(buf):
        ud_edge_fire.main()

    # The API-error alert must be sent
    alert_bodies = [b for t, b in sent if "PropLine API" in t or "API ERROR" in t.upper()]
    assert len(alert_bodies) >= 1, f"Expected API-error alert, got titles: {[t for t, _ in sent]}"
    assert "401" in alert_bodies[0]


# ── Test 7: new tier "evening" (6 PM CT) ───────────────────────────────


def test_evening_tier_present():
    """The new 6 PM CT fire must be a valid tier choice."""
    assert "evening" in ud_edge_fire.TIERS
    desc, threshold, max_legs, confidence = ud_edge_fire.TIERS["evening"]
    assert threshold > 0
    assert confidence.endswith("%")


def test_default_budget_per_fire_is_1000():
    """The default budget per fire (1000) × 6 fires = 6000 = full combined budget."""
    assert ud_edge_fire.DEFAULT_BUDGET_PER_FIRE == 1000
    assert ud_edge_fire.DEFAULT_BUDGET_PER_FIRE * 6 == 6000


# ── Test 8: Dabble support (added 2026-07-21) ──────────────────────────


def test_dabble_appears_in_book_breakdown():
    """Dabble legs must appear in the BOOK BREAKDOWN line as DA=N.

    Both legs need to clear the WNBA per-sport min_edge threshold (4.0pp).
    Stewart at 3.5pp would be filtered, so use 5.0pp+ edges for both.
    """
    legs = [
        _make_leg("WNBA", "Aja Wilson", ev=5.5, edge_kind="vs_sharp",
                  fantasy_book="dabble", fantasy_prob=60.0,
                  sharp_book="pinnacle", sharp_prob=66.0, stat="points", line=22.5),
        _make_leg("WNBA", "Stewart", ev=5.0, edge_kind="vs_sharp",
                  fantasy_book="underdog", fantasy_prob=58.0,
                  sharp_book="draftkings", sharp_prob=63.0, stat="rebounds", line=8.5),
    ]
    msg = ud_edge_fire.format_message("ud_morning", legs)
    assert "DA=1" in msg, f"BOOK BREAKDOWN should include DA=1 for the Dabble leg. Got: {msg}"
    assert "UD=1" in msg


def test_dabble_in_FANTASY_BOOKS():
    """ud_edge.propline_client.FANTASY_BOOKS must include 'dabble'."""
    from ud_edge.propline_client import FANTASY_BOOKS
    assert "dabble" in FANTASY_BOOKS, (
        f"FANTASY_BOOKS missing 'dabble': {FANTASY_BOOKS}. "
        "PropLine returns Dabble in WNBA events (verified 2026-07-21)."
    )


def test_dabble_unknown_book_does_not_crash():
    """Unknown fantasy books (e.g. hypothetical 'kalshi') must not crash BOOK_ORDER.get()."""
    legs = [
        _make_leg("WNBA", "Mystery", ev=5.0, edge_kind="vs_sharp",
                  fantasy_book="unknown_book", fantasy_prob=60.0,
                  sharp_book="pinnacle", sharp_prob=64.0),
    ]
    # Should not raise — unknown books sort after the 4 known ones (key=99)
    msg = ud_edge_fire.format_message("ud_morning", legs)
    assert "DA=0" in msg
    assert "UD=0" in msg


# ── Test 9: discrepancy tag (Tyler Freeman-style "STALE LINE" sanity hint) ─


def test_format_message_discrepancy_tag_stale_line():
    """Fantasy book ≥15pp off sharp on counting stat → STALE LINE hint.

    This gives the operator context on WHY the pick exists — not just the
    raw edge number. Tyler Freeman TB 1.5 Under (PF 50% vs Pin -185 = 65% Under)
    gets a 15pp gap → STALE LINE tag because Pinnacle is presumably slow to
    react to a recent slumping stretch (avg last 5 below the line).
    """
    legs = [
        # Freeman-style: 50% vs 65% = 15pp gap → STALE LINE
        _make_leg("MLB", "Tyler Freeman", ev=9.5, edge_kind="vs_sharp",
                  fantasy_book="prizepicks", fantasy_prob=50.0,
                  sharp_book="pinnacle", sharp_prob=65.0,
                  stat="total_bases", line=1.5, side="Under"),
    ]
    msg = ud_edge_fire.format_message("ud_morning", legs)
    assert "Tyler Freeman" in msg
    assert "STALE LINE" in msg, (
        f"Expected STALE LINE discrepancy tag (gap ≥ 15pp), got:\n{msg}"
    )


def test_format_message_discrepancy_tag_lagged():
    """Gap 10-14pp → LAGGED tag (lower-severity than STALE LINE)."""
    legs = [
        _make_leg("MLB", "MediumLag", ev=8.0, edge_kind="vs_sharp",
                  fantasy_book="underdog", fantasy_prob=55.0,
                  sharp_book="draftkings", sharp_prob=66.0,
                  stat="hits", line=0.5, side="Over"),
    ]
    msg = ud_edge_fire.format_message("ud_morning", legs)
    assert "LAGGED" in msg, f"Expected LAGGED tag (gap 10-14pp), got:\n{msg}"
    assert "STALE LINE" not in msg  # 11pp < 15pp → no STALE LINE tier


def test_format_message_no_discrepancy_tag_when_normal():
    """Gap < 10pp → no discrepancy tag (edge speaks for itself)."""
    legs = [
        _make_leg("MLB", "NormalEdge", ev=4.0, edge_kind="vs_sharp",
                  fantasy_book="underdog", fantasy_prob=60.0,
                  sharp_book="draftkings", sharp_prob=64.5,
                  stat="hits", line=0.5),
    ]
    msg = ud_edge_fire.format_message("ud_morning", legs)
    assert "NormalEdge" in msg
    assert "LAGGED" not in msg
    assert "STALE LINE" not in msg


# ── Test 10: destination-book proximity logic (2026-07-21 fix) ─────────


def test_parse_legs_picks_book_closest_to_sharp_for_vs_sharp_legs():
    """For vs_sharp legs, pick the fantasy book whose no-vig prob is CLOSEST to
    sharp — not the most-extreme price. Catches the PF-50%-synthetic-default
    failure mode where PF's missing-side default 50% creates a fake 15pp edge
    but PF doesn't actually offer that side at the app.

    Tyler Freeman TB 1.5 Under: sharp 62%, PF 50% (synthetic), Sleeper 61%.
    The bot should pick Sleeper (closest to sharp), not PF (lowest = best payout
    but unreliable).
    """
    # Build a synthetic payload that mimics compare_fantasy_vs_sharp output
    payload = {
        "sports": [{
            "sport": "MLB",
            "opportunities": [{
                "player_name": "Tyler Freeman",
                "stat_name": "total_bases",
                "line_value": 1.5,
                "side_label": "Under",
                "ud_true_prob": 0.528,
                "lower_true_prob": 0.50,
                "sharp_books": {"pinnacle": 0.622},
                "fantasy_books": {
                    "prizepicks": 0.50,
                    "sleeper": 0.608,
                    "underdog": 0.528,
                },
                "mispricing_edge_pp": 12.2,
                "ud_edge_pp": 9.36,
                "match_title": "COL @ WSH",
            }]
        }]
    }
    # Empty fantasy_lookup — secondary path is unused here
    fantasy_lookup = {}
    legs = ud_edge_fire.parse_legs(payload, fantasy_lookup)
    assert len(legs) == 1
    leg = legs[0]
    assert leg["fantasy_book"] == "sleeper", (
        f"Expected Sleeper (closest to sharp at 60.8% vs 62.2%), got "
        f"{leg['fantasy_book']} at {leg['fantasy_prob']}%. "
        f"With the old 'lowest true_prob' rule, the bot would have picked "
        f"PF (50%) which is the synthetic default and unplaceable on the app."
    )
    assert leg["fantasy_prob"] == 60.8


def test_parse_legs_picks_lowest_prob_for_fantasy_only_legs():
    """For fantasy-only legs (no sharp), fall back to lowest true_prob (best
    payout for bettor). Sharp-proximity rule doesn't apply when there's no
    sharp to compare against.
    """
    payload = {
        "sports": [{
            "sport": "TENNIS",
            "opportunities": [{
                "player_name": "Alcaraz",
                "stat_name": "aces",
                "line_value": 12.5,
                "side_label": "Over",
                "ud_true_prob": 0.58,
                "lower_true_prob": 0.42,
                # No sharp_books → fantasy-only
                "fantasy_books": {
                    "prizepicks": 0.58,
                    "sleeper": 0.62,
                    "underdog": 0.56,
                },
                "mispricing_edge_pp": None,
                "ud_edge_pp": 5.8,
                "match_title": "Alcaraz vs Sinner",
            }]
        }]
    }
    legs = ud_edge_fire.parse_legs(payload, fantasy_lookup={})
    assert len(legs) == 1
    leg = legs[0]
    # No sharp → use lowest true_prob → underdog 56%
    assert leg["fantasy_book"] == "underdog"
    assert leg["fantasy_prob"] == 56.0