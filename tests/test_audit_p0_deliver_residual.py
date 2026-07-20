"""Audit P0 residual (remediation v3): deliver.py Markdown report must use
expected_value_per_card (heterogeneous exact) — not the homogeneous
expected_value(entry, avg_prob) which overstates EV by a few cents per $1 on
mixed-confidence cards.

Pins:
1. No remaining `expected_value(entry, avg_prob)` calls in deliver.py outside
   docstrings/comments.
2. deliver.py imports expected_value_per_card.
"""
from __future__ import annotations

from pathlib import Path
import re


DELIVER = Path(__file__).resolve().parents[1] / "ud_edge" / "deliver.py"


def test_deliver_imports_expected_value_per_card():
    """deliver.py must import expected_value_per_card so the per-card contract
    is the same as compare.py and /api/lineups."""
    text = DELIVER.read_text(encoding="utf-8")
    assert "expected_value_per_card" in text, (
        "deliver.py does not import expected_value_per_card. "
        "Markdown EV will silently fall back to the homogeneous contract."
    )


def test_deliver_no_homogeneous_expected_value_calls():
    """deliver.py must not call the homogeneous expected_value(entry, avg_prob)
    in the per-card code path.

    Exception (allowed): the partial-card fallback inside build_report when
    top_n < entry.n_legs and we don't have enough legs to fill a full card.
    In that case expected_value_per_card is undefined (it requires exactly
    n_legs probs), and we fall back to the homogeneous formula so the report
    still renders. That's an information-degraded scenario, not the
    homogeneous-overstate bug.
    """
    text = DELIVER.read_text(encoding="utf-8")
    # Strip comments
    code_only = re.sub(r"#[^\n]*", "", text)
    # Find all expected_value() call sites (we'll filter manually)
    all_calls = re.findall(
        r"expected_value\s*\([^)]*\)",
        code_only,
    )
    # The only allowed site is the partial-card fallback in build_report.
    # That call uses `avg_prob` as the per-leg prob — which is what we're
    # guarding against in the multi-entry per-leg path. So we want to allow
    # exactly one call, the one inside `if per_leg and len(per_leg) == entry.n_legs: ... else:`.
    # The pin: assert there's at most ONE call site, and it's in the fallback branch.
    expected_value_calls = [
        c for c in all_calls if "expected_value_per_card" not in c
    ]
    assert len(expected_value_calls) <= 1, (
        f"deliver.py still calls homogeneous expected_value(...) {len(expected_value_calls)} "
        f"time(s). Only the partial-card fallback inside build_report should "
        f"use it. Audit P0 residual: Markdown deliver overstates "
        f"EV by ~4c/$1 on mixed-confidence full cards. Calls:\n"
        + "\n".join(expected_value_calls)
    )
    # And assert that call is the fallback — i.e., it's in the `else:` branch
    # of `if per_leg and len(per_leg) == entry.n_legs:`.
    if expected_value_calls:
        # Look at the surrounding code
        call = expected_value_calls[0]
        idx = code_only.find(call)
        before = code_only[max(0, idx - 600):idx]
        assert "Partial card" in before or "else:" in before[-100:], (
            f"Homogeneous expected_value() call is not inside the partial-card "
            f"fallback branch:\n{before[-300:]}\n... {call}"
        )


def test_deliver_multi_entry_uses_per_card():
    """The multi-entry table and per-entry detail sections must call
    expected_value_per_card.
    """
    text = DELIVER.read_text(encoding="utf-8")
    # Locate both loops
    for_loop_count = text.count("for i, lineup in enumerate(lineups, 1):")
    assert for_loop_count == 2, (
        f"Expected 2 lineup loops in deliver.py (multi-entry table + per-entry "
        f"detail), found {for_loop_count}."
    )
    # Each loop body must call expected_value_per_card
    # We assert at least 2 occurrences of expected_value_per_card (one per loop)
    assert text.count("expected_value_per_card(entry, per_leg)") >= 2, (
        f"deliver.py should call expected_value_per_card(entry, per_leg) at "
        f"least twice (once per loop). Found: "
        f"{text.count('expected_value_per_card(entry, per_leg)')}"
    )