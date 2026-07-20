"""Tests for the Wave-0 safety gate: research-only mode when payout model is unverified.

RED: These tests define the desired behavior. They fail on the current codebase
because no safety gate exists yet.
GREEN: After adding ud_edge/safety_gate.py, all tests pass.
"""
import pytest
from pathlib import Path
import tempfile
import json

# ── Test helpers ────────────────────────────────────────────────────────────

def write_results(path: Path, picks: list[dict]) -> None:
    """Write a results.json file with the given picks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "picks": picks,
        "metadata": {"created": "2026-07-19T00:00:00Z"}
    }
    path.write_text(json.dumps(data))


def write_temp_results(picks: list[dict]) -> Path:
    """Write a temporary results.json and return its path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir) / "data" / "results.json"
        write_results(p, picks)
        yield p


# ── Module import tests ─────────────────────────────────────────────────────

def test_safety_gate_module_exists():
    """The safety_gate module must exist and be importable."""
    from ud_edge import safety_gate
    assert hasattr(safety_gate, "is_payout_model_verified")
    assert hasattr(safety_gate, "settled_legs_count")
    assert hasattr(safety_gate, "is_calibration_sufficient")
    assert hasattr(safety_gate, "is_research_mode")
    assert hasattr(safety_gate, "safety_status")
    assert hasattr(safety_gate, "recommendation_label")


def test_is_payout_model_verified_returns_bool():
    """Must return True only when the payout model has been independently verified."""
    from ud_edge.safety_gate import is_payout_model_verified
    result = is_payout_model_verified()
    assert isinstance(result, bool)


def test_settled_legs_count_returns_int():
    """Must return the count of settled (HIT/MISS) legs in results.json."""
    from ud_edge.safety_gate import settled_legs_count
    result = settled_legs_count()
    assert isinstance(result, int)
    assert result >= 0


def test_is_calibration_sufficient_returns_bool():
    """Must return True only when ≥50 HIT/MISS legs exist."""
    from ud_edge.safety_gate import is_calibration_sufficient
    result = is_calibration_sufficient()
    assert isinstance(result, bool)


def test_is_research_mode_is_true_when_payout_unverified():
    """Research mode must be True when payout model is unverified."""
    from ud_edge.safety_gate import is_payout_model_verified, is_research_mode
    if not is_payout_model_verified():
        assert is_research_mode() is True


def test_is_research_mode_is_true_when_fewer_than_50_settled():
    """Research mode must be True when fewer than 50 settled legs exist."""
    from ud_edge.safety_gate import is_calibration_sufficient, is_research_mode
    if not is_calibration_sufficient():
        assert is_research_mode() is True


def test_safety_status_returns_dict_with_required_keys():
    """safety_status() must return a dict with all required status keys."""
    from ud_edge.safety_gate import safety_status
    status = safety_status()
    assert isinstance(status, dict)
    required_keys = {
        "is_research_mode",
        "is_payout_model_verified",
        "is_calibration_sufficient",
        "settled_legs_count",
        "hit_count",
        "miss_count",
        "pending_count",
        "picks_logged_count",
        "recommendation",
        "wave",
    }
    assert required_keys.issubset(status.keys()), f"Missing keys: {required_keys - status.keys()}"


def test_safety_status_recommendation_is_research_when_unverified():
    """When payout model is unverified, recommendation must be research-only."""
    from ud_edge.safety_gate import safety_status, is_payout_model_verified
    if not is_payout_model_verified():
        status = safety_status()
        rec = status["recommendation"].lower()
        # Must NOT contain actionable claims
        assert "play" not in rec or "research" in rec or "unverified" in rec or "estimate" in rec


def test_recommendation_label_in_research_mode():
    """recommendation_label() must return a research-only label in research mode."""
    from ud_edge.safety_gate import recommendation_label, is_research_mode
    if is_research_mode():
        label = recommendation_label(ev_per_dollar=0.15, win_prob=0.30)
        label_lower = label.lower()
        # Must NOT say PLAY or STRONG PLAY
        assert "play" not in label_lower or "research" in label_lower or "unverified" in label_lower


def test_recommendation_label_in_verified_mode():
    """recommendation_label() must still work (not error) in verified mode."""
    from ud_edge.safety_gate import recommendation_label
    # Should not raise regardless of mode
    label = recommendation_label(ev_per_dollar=0.15, win_prob=0.30)
    assert isinstance(label, str)
    assert len(label) > 0


# ── Payout model verified flag ──────────────────────────────────────────────

def test_payout_model_not_verified_by_default():
    """By default (no verification artifact), payout model must NOT be verified."""
    from ud_edge.safety_gate import is_payout_model_verified
    # The audit found the payout model is unverified, so this should be False
    assert is_payout_model_verified() is False


# ── Display-label scrubbing in report output ─────────────────────────────────

def test_deliver_no_play_labels_in_research_mode(tmp_path, monkeypatch):
    """In research mode, deliver.py output must NOT contain raw PLAY/STRONG PLAY labels.

    This is an integration test: it patches the results path and calls build_report.
    """
    from ud_edge.safety_gate import is_research_mode

    if not is_research_mode():
        pytest.skip("Payout model is verified — test only applies in research mode")

    # Patch results path to a temp file with 0 settled legs
    with tempfile.TemporaryDirectory() as tmpdir:
        results_path = Path(tmpdir) / "data" / "results.json"
        results_path.parent.mkdir(parents=True)
        results_path.write_text(json.dumps({
            "picks": [],  # no settled legs
            "metadata": {"created": "2026-07-19T00:00:00Z"}
        }))

        # Monkey-patch the results path in safety_gate
        import ud_edge.safety_gate as sg
        original_results_path = sg.RESULTS_PATH
        sg.RESULTS_PATH = results_path

        try:
            from ud_edge.deliver import build_report
            from ud_edge.models import RankedLeg, Leg

            # Build a minimal synthetic RankedLeg
            synthetic_leg = Leg(
                line_id="test-1",
                player_id="p1",
                player_name="Test Player",
                sport_id="NBA",
                match_id=1,
                match_title="TEST@VS",
                stat_name="points",
                line_value=25.5,
                line_type="balanced",
                higher_american=-130,
                higher_decimal=1.77,
                higher_multiplier=0.85,
                lower_american=110,
                lower_decimal=2.10,
                lower_multiplier=1.05,
            )
            ranked = [
                RankedLeg(
                    leg=synthetic_leg,
                    higher_true_prob=0.57,
                    higher_implied_prob=0.565,
                    higher_edge_pp=2.5,
                    lower_true_prob=0.43,
                    lower_implied_prob=0.435,
                    lower_edge_pp=-10.0,
                    picked_side="higher",
                    picked_true_prob=0.57,
                    picked_edge_pp=2.5,
                    overround=1.0,
                )
            ]

            report = build_report(ranked, entry_type="6-flex", top_n=1)

            # In research mode, must not show raw "PLAY" or "STRONG PLAY" without "research" qualifier
            for line in report.split("\n"):
                raw_play = any(
                    badge in line and "research" not in line.lower() and "unverified" not in line.lower()
                    for badge in ["STRONG PLAY", "🟢", "🟡 PLAY", "PLAY"]
                )
                assert not raw_play, f"Found actionable label without research qualifier in: {line}"
        finally:
            sg.RESULTS_PATH = original_results_path


# ── Dashboard API exposes safety status ─────────────────────────────────────

def test_dashboard_payload_includes_safety_status(tmp_path, monkeypatch):
    """The dashboard API payload must include a top-level safety_status field."""
    # This tests the compare.py → dashboard pipeline
    # We check the structure by running compare_fantasy_vs_sharp and verifying
    # the returned dict has a 'safety_status' key.
    from ud_edge.compare import compare_fantasy_vs_sharp

    # Patch the results path to a known state
    with tempfile.TemporaryDirectory() as tmpdir:
        results_path = Path(tmpdir) / "data" / "results.json"
        results_path.parent.mkdir(parents=True)
        results_path.write_text(json.dumps({
            "picks": [],
            "metadata": {"created": "2026-07-19T00:00:00Z"}
        }))

        import ud_edge.safety_gate as sg
        original_results_path = sg.RESULTS_PATH
        sg.RESULTS_PATH = results_path

        # Also patch the root data dir used by compare
        import ud_edge.compare as cmp
        original_root = cmp._project_root
        cmp._project_root = lambda: Path(tmpdir)

        try:
            payload = compare_fantasy_vs_sharp(
                entry_type="6-flex",
                min_true_prob=0.55,
                min_edge_pp=0.5,
                full_game_only=False,
                force_fetch=False,
            )
            assert "safety_status" in payload, "Dashboard payload must include safety_status"
            status = payload["safety_status"]
            assert isinstance(status, dict)
            assert "is_research_mode" in status
            assert "recommendation" in status
        finally:
            sg.RESULTS_PATH = original_results_path
            cmp._project_root = original_root


# ── HONEST_STATUS.md exists ──────────────────────────────────────────────────

def test_honest_status_md_exists():
    """HONEST_STATUS.md must exist in the repo root."""
    from ud_edge import safety_gate
    root = Path(safety_gate.__file__).resolve().parent.parent
    status_file = root / "HONEST_STATUS.md"
    assert status_file.exists(), f"HONEST_STATUS.md not found at {status_file}"


def test_honest_status_md_mentions_research_and_current_wave():
    """HONEST_STATUS.md must surface the current wave and the research-only safety case.

    Wave 0+ all keep `is_research_mode: TRUE` because the calibration sample is
    too small; the document must therefore reference both the active research
    mode and the current wave number. The wave label evolves (Wave 0, Wave 1, …)
    so assert the *family* of waves, not a literal value.
    """
    from ud_edge import safety_gate
    root = Path(safety_gate.__file__).resolve().parent.parent
    status_file = root / "HONEST_STATUS.md"
    content = status_file.read_text()
    assert "research" in content.lower(), "Status doc must keep the research-only safety case"
    assert "Wave " in content or "wave " in content.lower(), "Status doc must identify a current wave"


# ── Wave 0 review-gap tests ────────────────────────────────────────────────────

def test_multi_lineup_header_uses_recommendation_label_not_raw_ev_labels(tmp_path, monkeypatch):
    """Multi-lineup header table must use recommendation_label(), never _recommend_from_ev.

    This was the Wave 0 reviewer finding: build_multi_report was calling the raw
    internal helper directly, bypassing is_research_mode() downgrading.
    """
    from ud_edge.deliver import build_multi_report
    from ud_edge.models import Leg, RankedLeg

    # Monkey-patch so we don't need real data
    import ud_edge.safety_gate as sg
    orig_results_path = sg.RESULTS_PATH
    results_path = tmp_path / "data" / "results.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps({
        "picks": [],
        "metadata": {"created": "2026-07-19T00:00:00Z"}
    }))
    sg.RESULTS_PATH = results_path

    try:
        # Build two synthetic lineups (need >1 for the at-a-glance header table)
        def make_ranked():
            leg = Leg(
                line_id="t1", player_id="p1", player_name="Test Player", sport_id="NBA",
                match_id=1, match_title="A@B", stat_name="points", line_value=25.5,
                line_type="balanced",
                higher_american=-130, higher_decimal=1.77, higher_multiplier=0.85,
                lower_american=110, lower_decimal=2.10, lower_multiplier=1.05,
            )
            return RankedLeg(
                leg=leg,
                higher_true_prob=0.57, higher_implied_prob=0.565, higher_edge_pp=2.5,
                lower_true_prob=0.43, lower_implied_prob=0.435, lower_edge_pp=-10.0,
                picked_side="higher", picked_true_prob=0.57, picked_edge_pp=2.5,
                overround=1.0,
            )

        lineups = [[make_ranked() for _ in range(6)] for _ in range(2)]
        report = build_multi_report(lineups, entry_type="6-flex", n_legs=6)

        # The header table must contain recommendation_label() output (🟡 RESEARCH ESTIMATE
        # or similar), not raw _recommend_from_ev hardcoded thresholds
        for line in report.split("\n"):
            # Bad: any raw badge emoji followed by PLAIN CAPS label without research qualifier
            # (the _recommend_from_ev output: "🟢 STRONG PLAY", "🟡 PLAY", etc.)
            for badge in ["🟢 STRONG PLAY", "🟡 PLAY", "🟠 SMALL", "🔴 SKIP"]:
                if badge in line and "research" not in line.lower() and "estimate" not in line.lower():
                    # Allow if it's also marked unverified
                    if "unverified" not in line.lower():
                        assert False, f"Raw actionable label found (not via recommendation_label): {line.strip()}"
    finally:
        sg.RESULTS_PATH = orig_results_path


def test_report_inline_unverified_warning_in_header(tmp_path, monkeypatch):
    """In research mode, build_report must show UNVERIFIED RESEARCH ESTIMATES banner inline,
    not only in a footer note.
    """
    import ud_edge.safety_gate as sg
    orig_results_path = sg.RESULTS_PATH
    results_path = tmp_path / "data" / "results.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps({
        "picks": [],
        "metadata": {"created": "2026-07-19T00:00:00Z"}
    }))
    sg.RESULTS_PATH = results_path

    try:
        from ud_edge.deliver import build_report
        from ud_edge.models import Leg, RankedLeg

        leg = Leg(
            line_id="t1", player_id="p1", player_name="Test Player", sport_id="NBA",
            match_id=1, match_title="A@B", stat_name="points", line_value=25.5,
            line_type="balanced",
            higher_american=-130, higher_decimal=1.77, higher_multiplier=0.85,
            lower_american=110, lower_decimal=2.10, lower_multiplier=1.05,
        )
        ranked = [RankedLeg(
            leg=leg,
            higher_true_prob=0.57, higher_implied_prob=0.565, higher_edge_pp=2.5,
            lower_true_prob=0.43, lower_implied_prob=0.435, lower_edge_pp=-10.0,
            picked_side="higher", picked_true_prob=0.57, picked_edge_pp=2.5,
            overround=1.0,
        )]
        report = build_report(ranked, entry_type="6-flex", top_n=1)

        # Must have an inline banner (not only footer) containing "UNVERIFIED"
        assert "UNVERIFIED" in report or "unverified" in report.lower(), \
            "build_report must show an inline unverified banner, not only footer"
        # The banner must appear before the per-leg table (i.e., in the header section)
        lines = report.split("\n")
        table_idx = next((i for i, l in enumerate(lines) if "|" in l and "Sport" in l), -1)
        banner_idx = next((i for i, l in enumerate(lines)
                          if "unverified" in l.lower() or "UNVERIFIED" in l), -1)
        assert banner_idx != -1, "No UNVERIFIED banner found in report"
        assert banner_idx < table_idx, \
            f"UNVERIFIED banner (line {banner_idx}) must appear BEFORE the table (line {table_idx})"
    finally:
        sg.RESULTS_PATH = orig_results_path


def test_safety_gate_results_path_from_results_tracker():
    """safety_gate.RESULTS_PATH must be imported from results_tracker to avoid
    a second CWD-relative constant."""
    from ud_edge import safety_gate
    from ud_edge import results_tracker
    # The object must be the same (identity check, not just equal value)
    assert safety_gate.RESULTS_PATH is results_tracker.RESULTS_PATH, \
        "safety_gate.RESULTS_PATH must be the exact same object as results_tracker.RESULTS_PATH"


def test_deliver_no_unused_matcher_imports():
    """deliver.py must not import rank_legs, top_n_for_entry, or build_lineups from matcher."""
    import ast
    import sys
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "ud_edge" / "deliver.py").read_text()
    tree = ast.parse(src)
    aliases = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and "matcher" in node.module:
                for alias in node.names:
                    aliases.append(alias.name)

    forbidden = {"rank_legs", "top_n_for_entry", "build_lineups"}
    conflicts = forbidden & set(aliases)
    assert not conflicts, \
        f"deliver.py still imports unused matcher symbols: {conflicts}"


def test_recommendation_label_deterministic_both_states(monkeypatch, tmp_path):
    """recommendation_label() must return correct label for both research and
    verified modes when each is explicitly forced via monkeypatch."""
    import ud_edge.safety_gate as sg

    # --- Force verified mode ---
    monkeypatch.setattr(sg, "_PAYOUT_MODEL_VERIFIED", True)
    results_path = tmp_path / "data" / "results.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps({
        "picks": [{"outcome": "HIT"}] * 60,  # 60 settled > 50 threshold
        "metadata": {"created": "2026-07-19T00:00:00Z"}
    }))
    orig_path = sg.RESULTS_PATH
    sg.RESULTS_PATH = results_path
    try:
        # In verified mode, STRONG PLAY is allowed (no "research" in label)
        label_strong = sg.recommendation_label(0.15, 0.30)
        assert isinstance(label_strong, str)
        assert len(label_strong) > 0
        # Must NOT contain "research" or "unverified" when payout is verified
        assert "research" not in label_strong.lower(), \
            f"Verified mode label must not say 'research': {label_strong}"

        label_play = sg.recommendation_label(0.05, 0.30)
        assert "research" not in label_play.lower()
    finally:
        sg.RESULTS_PATH = orig_path
        sg._PAYOUT_MODEL_VERIFIED = False  # restore default

    # --- Force research mode (default) ---
    monkeypatch.setattr(sg, "_PAYOUT_MODEL_VERIFIED", False)
    results_path2 = tmp_path / "data2" / "results.json"
    results_path2.parent.mkdir(parents=True, exist_ok=True)
    results_path2.write_text(json.dumps({
        "picks": [{"outcome": "HIT"}] * 5,  # < 50 threshold
        "metadata": {"created": "2026-07-19T00:00:00Z"}
    }))
    sg.RESULTS_PATH = results_path2
    try:
        label = sg.recommendation_label(0.15, 0.30)
        assert isinstance(label, str)
        assert len(label) > 0
        # Must contain "research" or "unverified" in research mode for non-skip labels
        assert "research" in label.lower() or "unverified" in label.lower(), \
            f"Research mode label must say 'research' or 'unverified': {label}"
    finally:
        sg.RESULTS_PATH = orig_path
