"""Wave-0 research-only safety gate.

This module centralises the "is this system ready to make real-money
recommendations?" check.  It exposes a single source of truth used by:

- CLI reports (deliver.py)
- Dashboard API payload (compare.py)
- HONEST_STATUS.md (auto-generated)
- Future cron jobs (Wave 1, not scheduled here)

Audit baseline: 24 picks logged, 0 HIT, 0 MISS, 24 pending.
Wave 0 threshold: payout model must be independently verified AND ≥50
settled HIT/MISS outcomes must exist before lifting research-only mode.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

# ── Configuration ──────────────────────────────────────────────────────────

# Absolute minimum settled HIT/MISS legs before the tool can make anything
# beyond a "research estimate" claim.  Drawn from audit SB-P0-03.
MIN_SETTLED_LEGS = 50

# Path to the results tracker file.  Imported from results_tracker to avoid
# a second CWD-relative constant; results_tracker owns all writes.
# Overridden in tests via monkey-patching of safety_gate.RESULTS_PATH.
from ud_edge.results_tracker import RESULTS_PATH

# ── Payout-model verification ──────────────────────────────────────────────

# Wave 0: payout model is NOT independently verified.
# Set to True only when official Underdog payout rules have been obtained
# and archived, and break-even derivation tests pass (SB-P0-01 fix).
# TODO: flip to True once Wave 1 lands and the model is validated.
_PAYOUT_MODEL_VERIFIED = False


def is_payout_model_verified() -> bool:
    """Return True only when the payout model has been independently verified."""
    return _PAYOUT_MODEL_VERIFIED


# ── Calibration / settled-legs count ───────────────────────────────────────

def _load_results() -> dict:
    if not RESULTS_PATH.exists():
        return {"picks": [], "metadata": {"created": datetime.now(timezone.utc).isoformat()}}
    return json.loads(RESULTS_PATH.read_text())


def settled_legs_count() -> int:
    """Return the count of picks resolved as HIT or MISS (not STALE, not pending)."""
    data = _load_results()
    return sum(1 for p in data["picks"] if p.get("outcome") in {"HIT", "MISS"})


def hit_count() -> int:
    """Return the count of picks resolved as HIT."""
    data = _load_results()
    return sum(1 for p in data["picks"] if p.get("outcome") == "HIT")


def miss_count() -> int:
    """Return the count of picks resolved as MISS."""
    data = _load_results()
    return sum(1 for p in data["picks"] if p.get("outcome") == "MISS")


def pending_count() -> int:
    """Return the count of picks with no resolution yet."""
    data = _load_results()
    return sum(1 for p in data["picks"] if p.get("outcome") is None)


def picks_logged_count() -> int:
    """Return the total count of picks ever logged."""
    data = _load_results()
    return len(data.get("picks", []))


def is_calibration_sufficient() -> bool:
    """Return True when ≥ MIN_SETTLED_LEGS settled HIT/MISS legs exist."""
    return settled_legs_count() >= MIN_SETTLED_LEGS


# ── Research-mode gate ─────────────────────────────────────────────────────

def is_research_mode() -> bool:
    """Return True when the tool must not make actionable +EV / PLAY claims.

    Research mode is active when EITHER condition holds:
    1. The payout model has not been independently verified.
    2. Fewer than 50 settled HIT/MISS legs have been recorded.
    """
    return (not is_payout_model_verified()) or (not is_calibration_sufficient())


# ── Recommendation labels ──────────────────────────────────────────────────

# Labels used in research/unverified mode (no actionable PLAY / STRONG PLAY).
# These are the ONLY labels that should appear in user-facing output while
# the payout model is unverified or the sample is too small.
_RESEARCH_LABELS = {
    "strong": "🟢 RESEARCH ESTIMATE (unverified model)",   # EV > 10%
    "play":   "🟡 RESEARCH ESTIMATE",                     # EV > 3%
    "small":  "🟠 RESEARCH ESTIMATE",                     # EV > 0%
    "skip":   "🔴 SKIP (research only)",
}

# Verified-mode labels (Wave 1+).  Kept here so callers always go through
# recommendation_label() and get the right label for the current safety state.
_VERIFIED_LABELS = {
    "strong": "🟢 STRONG PLAY",
    "play":   "🟡 PLAY",
    "small":  "🟠 SMALL EDGE",
    "skip":   "🔴 SKIP",
}


def recommendation_label(ev_per_dollar: float, win_prob: float) -> str:
    """Return the display label appropriate to the current safety state.

    While is_research_mode() is True, all labels are downgraded to
    "RESEARCH ESTIMATE" variants regardless of EV magnitude.
    Numeric diagnostics (ev_per_dollar, win_prob) are still available
    internally for Wave 1 to consume.

    Args:
        ev_per_dollar: Expected value per $1 staked (positive = +EV).
        win_prob: Probability of returning any payout (>0 multiplier).

    Returns:
        A display string label, e.g. "🟡 RESEARCH ESTIMATE".
    """
    if ev_per_dollar > 0.10:
        raw = "strong"
    elif ev_per_dollar > 0.03:
        raw = "play"
    elif ev_per_dollar > 0.0:
        raw = "small"
    else:
        raw = "skip"

    if is_research_mode():
        return _RESEARCH_LABELS[raw]
    return _VERIFIED_LABELS[raw]


# ── Safety-status dict ─────────────────────────────────────────────────────

class SafetyStatus(TypedDict):
    is_research_mode: bool
    is_payout_model_verified: bool
    is_calibration_sufficient: bool
    settled_legs_count: int
    hit_count: int
    miss_count: int
    pending_count: int
    picks_logged_count: int
    min_settled_legs_required: int
    recommendation: str
    wave: int


def safety_status() -> SafetyStatus:
    """Return a complete safety-status snapshot for embedding in reports / APIs."""
    hits = hit_count()
    misses = miss_count()
    pending = pending_count()
    settled = hits + misses

    # Compute a research-only recommendation label for the status dict
    # (we don't have a real ev_per_dollar here, so use a neutral placeholder)
    rec = "RESEARCH ESTIMATE (unverified model)" if is_research_mode() else "ACTIVE"

    return SafetyStatus(
        is_research_mode=is_research_mode(),
        is_payout_model_verified=is_payout_model_verified(),
        is_calibration_sufficient=is_calibration_sufficient(),
        settled_legs_count=settled,
        hit_count=hits,
        miss_count=miss_count(),
        pending_count=pending,
        picks_logged_count=picks_logged_count(),
        min_settled_legs_required=MIN_SETTLED_LEGS,
        recommendation=rec,
        wave=0,
    )
