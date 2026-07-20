"""Audit P1 fix: docs/METHODOLOGY.md (and the README entry-type cheat sheet)
must report break-even values that match `UD_PAYOUTS` exactly.

These docs drifted: the old METHODOLOGY.md said 6-flex break-even was
52.40% when the code solved it to 54.21%. Agents following the docs
think 55% legs are "easy +EV" by ~2pp more than reality.

Fix: this test loads `UD_PAYOUTS` at runtime and asserts the docs quote
the same values. If `UD_PAYOUTS` ever changes, the docs must change too.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ud_edge.flex_math import UD_PAYOUTS, expected_value


ROOT = Path(__file__).resolve().parent.parent
METHODOLOGY = ROOT / "docs" / "METHODOLOGY.md"
README = ROOT / "README.md"


def _pct_str(p: float) -> str:
    """Render a 0-1 break-even as 'XX.XX%' to match the docs' format."""
    return f"{p * 100:.2f}%"


def _ev_at_prob(entry_name: str, p: float) -> float:
    """Return EV / $1 for a uniform card at probability p."""
    entry = UD_PAYOUTS[entry_name]
    ev, _, _ = expected_value(entry, p)
    return ev


# ── UD_PAYOUTS sanity (so any drift is caught here first) ─────────────────────


def test_ud_payouts_6flex_break_even_is_5421():
    """Pin the Wave 1 fix. If this changes, audit the change carefully."""
    assert abs(UD_PAYOUTS["6-flex"].break_even - 0.5421) < 1e-4


def test_ud_payouts_3man_power_break_even_is_5503():
    """Audit used 55.03% (vs the docs' wrong 54.95%)."""
    assert abs(UD_PAYOUTS["3-man-power"].break_even - 0.5503) < 1e-4


def test_ud_payouts_4flex_break_even_is_5503():
    """Same as 3-man-power analytically — different payout structure."""
    assert abs(UD_PAYOUTS["4-flex"].break_even - 0.5503) < 1e-4


def test_ud_payouts_5flex_break_even_is_4216():
    """Audit caught the docs using 57.81% — that's wrong because 3/5 → 2× pulls the BE way down."""
    assert abs(UD_PAYOUTS["5-flex"].break_even - 0.4216) < 1e-4


# ── METHODOLOGY.md doc-text checks ─────────────────────────────────────────────


@pytest.mark.parametrize("entry_name", ["3-man-power", "4-flex", "5-flex", "6-flex"])
def test_methodology_md_quotes_correct_break_even(entry_name: str):
    """METHODOLOGY.md's entry-type table must show the exact break-even
    percentage from `UD_PAYOUTS`. The old version had:
        - 3-man-power 54.95% (actual 55.03%)
        - 4-flex      57.81% (actual 55.03%)
        - 5-flex      57.81% (actual 42.16%)
        - 6-flex      52.40% (actual 54.21%)
    """
    text = METHODOLOGY.read_text(encoding="utf-8")
    expected_pct = _pct_str(UD_PAYOUTS[entry_name].break_even)
    # Match a row like "| 6-flex | 6 | ... | 54.21% | ..." (multiple pipes).
    pattern = rf"\|\s*{re.escape(entry_name)}\b.*?\|\s*{re.escape(expected_pct)}\s*\|"
    assert re.search(pattern, text), (
        f"METHODOLOGY.md does not show {entry_name} break-even as {expected_pct}. "
        f"The row in the file says something else. Audit the entry-type table:\n"
        f"{text}"
    )


# ── README doc-text checks ─────────────────────────────────────────────────────


@pytest.mark.parametrize("entry_name", ["3-man-power", "4-man-power", "4-flex", "5-flex", "6-flex"])
def test_readme_quotes_correct_break_even(entry_name: str):
    """README's entry-type cheat sheet table must show exact break-even."""
    text = README.read_text(encoding="utf-8")
    expected_pct = _pct_str(UD_PAYOUTS[entry_name].break_even)
    # README bold-wraps the row: `| **6-flex** | **...** | **54.21%** | ...`
    pattern = rf"\|\s*\**\s*{re.escape(entry_name)}\b.*?\|\s*\**\s*{re.escape(expected_pct)}\s*\**\s*\|"
    assert re.search(pattern, text), (
        f"README.md does not show {entry_name} break-even as {expected_pct}. "
        f"Found rows:\n"
        + "\n".join(line for line in text.splitlines() if entry_name in line)
    )


# ── Stale-number guardrails (prevent re-introduction) ────────────────────────


@pytest.mark.parametrize("stale", ["52.40%", "54.95%", "57.81%"])
def test_methodology_md_no_stale_break_evens(stale: str):
    """Guardrail: the audit-flagged stale numbers must not appear in docs.

    52.40% — old 6-flex BE (now 54.21%)
    54.95% — old 3-man-power BE (now 55.03%)
    57.81% — old 4-flex / 5-flex BE (now 55.03% / 42.16%)
    """
    text = METHODOLOGY.read_text(encoding="utf-8")
    assert stale not in text, (
        f"METHODOLOGY.md still contains stale break-even '{stale}'. "
        f"This is the audit-flagged drift."
    )


@pytest.mark.parametrize("stale", ["52.40%", "298 tests"])
def test_readme_no_stale_break_evens_or_test_counts(stale: str):
    """README's entry-type table had 52.40% (wrong) and the architecture
    tree said '~298 tests' when the real count is 365+ now. Neither must
    reappear.
    """
    text = README.read_text(encoding="utf-8")
    assert stale not in text, (
        f"README.md still contains stale value '{stale}'."
    )