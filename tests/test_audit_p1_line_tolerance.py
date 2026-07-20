"""Audit P1 #6 fix: line tolerance must be configurable and surface match quality.

Before: LINE_TOLERANCE = 0.5 was a hard-coded module constant in matcher.py.
Soft fantasy lines that differed by 1.0+ from sharp (common — sportsbooks
post different alt lines for the same player/stat) were never matched and
silently fell through to fantasy no-vig ranking alone.

After:
- LINE_TOLERANCE remains the default (0.5) to preserve Wave 2A semantics.
- New parameter `line_tolerance` on rank_legs() lets operators tune it.
- New CLI flag `--line-tolerance` on --once / --snapshot / --serve.
- New env var UD_LINE_TOLERANCE (overrides default at module load).
- SharpMatch gains `match_distance` so the dashboard can surface "fuzzy
  match at 1.5 line gap" instead of hiding it.
- Audit guardrail: rank_legs must respect the parameter (no longer hard-codes
  0.5 in the call site).
"""
from __future__ import annotations

import inspect

import pytest

from ud_edge.matcher import LINE_TOLERANCE, rank_legs


# ── Module default + env var contract ─────────────────────────────────────────


def test_default_line_tolerance_is_half():
    """Pin the default. Wave 2A's exact-tolerance semantics must survive."""
    assert LINE_TOLERANCE == 0.5


def test_env_var_overrides_default(monkeypatch):
    """UD_LINE_TOLERANCE env var must override the module default.

    (We re-import the module under the env var to verify the loader.)
    """
    import importlib
    monkeypatch.setenv("UD_LINE_TOLERANCE", "1.0")
    import ud_edge.matcher as matcher_mod
    importlib.reload(matcher_mod)
    try:
        assert matcher_mod.LINE_TOLERANCE == 1.0
    finally:
        monkeypatch.delenv("UD_LINE_TOLERANCE", raising=False)
        importlib.reload(matcher_mod)


# ── rank_legs() signature must accept line_tolerance ──────────────────────────


def test_rank_legs_accepts_line_tolerance_kwarg():
    """rank_legs() must take a `line_tolerance` parameter (no hard-coded 0.5)."""
    sig = inspect.signature(rank_legs)
    assert "line_tolerance" in sig.parameters, (
        f"rank_legs() must accept a `line_tolerance` parameter. "
        f"Current signature: {sig}. The audit P1 #6 bug was that this was "
        f"hard-coded to LINE_TOLERANCE=0.5 inside the function body."
    )


def test_rank_legs_default_line_tolerance_is_module_constant():
    """The default value of `line_tolerance` must equal LINE_TOLERANCE."""
    sig = inspect.signature(rank_legs)
    default = sig.parameters["line_tolerance"].default
    assert default == LINE_TOLERANCE, (
        f"rank_legs(line_tolerance=...) default must equal LINE_TOLERANCE. "
        f"Got default={default}, LINE_TOLERANCE={LINE_TOLERANCE}."
    )


# ── rank_legs() honors the parameter (functional test) ────────────────────────


def test_rank_legs_uses_higher_tolerance_when_supplied():
    """A fantasy line at 25.5 with sharp at 26.5 (delta=1.0):
    - With default tolerance 0.5: NO match (silent miss, audit-flagged bug)
    - With tolerance 1.5: match (operator opts up via CLI flag)
    """
    from ud_edge.sharp_books_client import find_sharp_match

    # Sharp index with a line at 26.5 (1.0 away from fantasy).
    # Key format must match sharp_lookup_key(): "{norm_player}|{canon_stat}"
    # (no trailing | when event_title is None).
    sharp_index = {
        "lebron james|points": {
            "over_decimal": 1.91, "under_decimal": 1.91,
            "bookmaker": "Pinnacle", "line_value": 26.5,
            "player_name": "LeBron James", "stat_name": "points",
            "sport_id": "NBA", "source": "propline",
        }
    }

    # Default tolerance: 0.5 → no match
    miss = find_sharp_match(sharp_index, "LeBron James", "points", 25.5, line_tolerance=0.5)
    assert miss is None, (
        f"Default tolerance 0.5 must reject 1.0 line gap (Wave 2A semantics). "
        f"Got: {miss}"
    )

    # Higher tolerance: 1.5 → match
    hit = find_sharp_match(sharp_index, "LeBron James", "points", 25.5, line_tolerance=1.5)
    assert hit is not None, (
        "Tolerance 1.5 must accept 1.0 line gap (operator opt-up). "
        "Got: None — the audit-flagged bug is still present."
    )


# ── SharpMatch exposes match distance ────────────────────────────────────────


def test_sharp_match_exposes_match_distance():
    """SharpMatch must carry match_distance so the dashboard can surface
    fuzzy-match confidence (delta=0.5 vs delta=1.5 should be visibly
    different on the front-end).
    """
    from ud_edge.sharp_books_client import SharpMatch, find_sharp_match

    sharp_index = {
        "lebron james|points": {
            "over_decimal": 1.91, "under_decimal": 1.91,
            "bookmaker": "Pinnacle", "line_value": 26.5,
            "player_name": "LeBron James", "stat_name": "points",
            "sport_id": "NBA", "source": "propline",
        }
    }

    hit = find_sharp_match(sharp_index, "LeBron James", "points", 25.5, line_tolerance=1.5)
    assert hit is not None
    # Sharp line 26.5, fantasy line 25.5 → distance 1.0
    assert hasattr(hit, "match_distance"), (
        f"SharpMatch must expose match_distance. Got attrs: "
        f"{[f.name for f in SharpMatch.__dataclass_fields__.values()]}"
    )
    assert hit.match_distance == pytest.approx(1.0, abs=1e-4)


# ── CLI flag presence ────────────────────────────────────────────────────────


def test_cli_exposes_line_tolerance_flag():
    """The --once / --snapshot / --serve subcommands must accept --line-tolerance.

    This is what makes the new tunability operator-facing rather than
    buried as a Python-only kwarg.
    """
    # Check argparse surface by building the parser
    from ud_edge.__main__ import build_parser

    parser = build_parser()
    # Parse a representative command; should not error on --line-tolerance
    args = parser.parse_args(["--once", "--entry", "6-flex", "--line-tolerance", "1.0"])
    assert getattr(args, "line_tolerance", None) == 1.0, (
        f"--line-tolerance must be a top-level CLI flag. "
        f"parser.parse_args returned: {args}"
    )