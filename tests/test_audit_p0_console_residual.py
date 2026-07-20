"""Audit P0 residual (remediation v3): __main__.py console summaries must use
effective_true_prob + expected_value_per_card so they agree with /api/lineups
and the Markdown deliver.

The original fix (#4875acb #4eef2e6) wired these into deliver.py / compare.py /
/api/lineups but left the operator-facing CLI console on the old homogeneous
expected_value() + raw picked_true_prob path. This pins that the CLI board is
now consistent with the rest of the system.

Pins:
1. Multi-entry per-entry console loop calls effective_true_prob + per_card.
2. Entry-type comparison console loop calls effective_true_prob + per_card.
3. No remaining raw `picked_true_prob` averaging in __main__.py's print paths.
4. No remaining homogeneous `expected_value(entry, avg_prob)` calls in __main__.py
   print paths (allowed inside self_test() where it's used to *test* that function).
"""
from __future__ import annotations

from pathlib import Path
import re


MAIN = Path(__file__).resolve().parents[1] / "ud_edge" / "__main__.py"


def test_main_console_per_entry_uses_effective_true_prob():
    """Multi-entry per-entry console loop must call effective_true_prob."""
    text = MAIN.read_text(encoding="utf-8")
    # Locate the per-entry comparison block
    assert "Per-entry comparison" in text, "Per-entry comparison block missing"
    # The block must call effective_true_prob (not just picked_true_prob averaging)
    block_match = re.search(
        r"--- Per-entry comparison ---.*?return 0",
        text,
        re.DOTALL,
    )
    assert block_match, "Could not locate Per-entry comparison block"
    block = block_match.group()
    assert "effective_true_prob" in block, (
        f"Per-entry console block does not call effective_true_prob. "
        f"Operators would still see raw fantasy-prob EV (audit P0 residual). "
        f"Block content:\n{block}"
    )
    assert "expected_value_per_card" in block, (
        f"Per-entry console block does not call expected_value_per_card. "
        f"Operators would still see homogeneous EV (audit P0 residual). "
        f"Block content:\n{block}"
    )


def test_main_console_entry_type_uses_effective_true_prob():
    """Entry-type comparison console loop must call effective_true_prob."""
    text = MAIN.read_text(encoding="utf-8")
    assert "Entry-type comparison (same top legs)" in text, "entry-type block missing"
    block_match = re.search(
        r"# Audit P0 residual \(remediation v3\): use effective_true_prob.*?return 0",
        text,
        re.DOTALL,
    )
    # Fallback: search for the second occurrence of entry-type comparison
    if not block_match:
        # Locate by structural anchor
        block_match = re.search(
            r"Entry-type comparison \(same top legs\).*?return 0",
            text,
            re.DOTALL,
        )
    assert block_match, "Could not locate entry-type comparison block"
    block = block_match.group()
    assert "effective_true_prob" in block, (
        f"Entry-type console block does not call effective_true_prob. "
        f"Block content:\n{block}"
    )
    assert "expected_value_per_card" in block, (
        f"Entry-type console block does not call expected_value_per_card. "
        f"Block content:\n{block}"
    )


def test_main_no_homogeneous_expected_value_in_console_paths():
    """__main__.py's *console* print paths must not call expected_value()
    on an averaged probability (homogeneous EV). The function is still allowed
    inside self_test() where it's the unit-under-test.
    """
    text = MAIN.read_text(encoding="utf-8")
    # Look for the pattern: expected_value(<entry>, <single_prob>)
    # inside the operator-facing print() blocks (not inside self_test())
    # Self-test is defined with `def _self_test(` or `def self_test(`.
    # Strip everything inside self_test first.
    stripped = re.sub(
        r"def (?:_?self_test)\(.*?(?=\ndef |\Z)",
        "SELFTEST_STRIPPED ",
        text,
        flags=re.DOTALL,
    )
    # Look for homogeneous calls OUTSIDE self_test
    bad = re.findall(
        r"expected_value\s*\(\s*\w+\s*,\s*avg_prob\s*\)",
        stripped,
    )
    assert not bad, (
        f"__main__.py still calls homogeneous expected_value(entry, avg_prob) "
        f"outside self_test (audit P0 residual). Count: {len(bad)}. "
        f"Use expected_value_per_card(entry, per_leg) instead."
    )


def test_main_no_raw_picked_true_prob_averaging_in_console_paths():
    """__main__.py console paths must not average raw picked_true_prob
    without effective_true_prob wrapping.
    """
    text = MAIN.read_text(encoding="utf-8")
    # Search for the pattern: sum(r.picked_true_prob for r in ...) / len(...)
    # inside console-print code (not inside report builders or rank_legs)
    bad = re.findall(
        r"sum\(r\.picked_true_prob for r in (?:lineup|top)\)\s*/\s*len\(",
        text,
    )
    assert not bad, (
        f"__main__.py still averages raw picked_true_prob without "
        f"effective_true_prob wrapping. Count: {len(bad)}. "
        f"Audit P0 residual: operators see fantasy-only EV on CLI."
    )