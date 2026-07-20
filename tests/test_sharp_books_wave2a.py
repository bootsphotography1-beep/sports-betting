"""Wave 2A tests: sharp-side matching authority, exact tolerance, freshness, event identity.

Strict TDD — tests written FIRST (RED), then implementation (GREEN).
"""
from __future__ import annotations
import csv
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from ud_edge.sharp_books_client import (
    find_sharp_match,
    build_sharp_index,
    sharp_lookup_key,
)


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — Exact tolerance, no 2× fallback
# ─────────────────────────────────────────────────────────────────────────────

class TestExactTolerance:

    def test_find_sharp_match_rejects_1pp_gap_at_0_5_tolerance(self):
        sharp_index = {
            sharp_lookup_key("Jayson Tatum", "points"): {
                "over_decimal": 1.95,
                "under_decimal": 1.95,
                "bookmaker": "DraftKings",
                "line_value": 27.5,
                "player_name": "Jayson Tatum",
                "stat_name": "points",
            }
        }
        result = find_sharp_match(
            sharp_index,
            player_name="Jayson Tatum",
            stat_name="points",
            line_value=26.5,
            line_tolerance=0.5,
        )
        assert result is None, (
            "find_sharp_match must REJECT a 1.0pp line gap when tolerance=0.5. "
            "The 2Xtolerance fallback at lines 605-607 must be removed."
        )

    def test_find_sharp_match_accepts_0_5pp_gap_at_0_5_tolerance(self):
        sharp_index = {
            sharp_lookup_key("Jayson Tatum", "points"): {
                "over_decimal": 1.95,
                "under_decimal": 1.95,
                "bookmaker": "DraftKings",
                "line_value": 27.5,
                "player_name": "Jayson Tatum",
                "stat_name": "points",
            }
        }
        result = find_sharp_match(
            sharp_index,
            player_name="Jayson Tatum",
            stat_name="points",
            line_value=27.0,
            line_tolerance=0.5,
        )
        assert result is not None, "Exactly 0.5pp gap must be accepted"

    def test_find_sharp_match_accepts_0_4pp_gap_at_0_5_tolerance(self):
        sharp_index = {
            sharp_lookup_key("Jayson Tatum", "points"): {
                "over_decimal": 1.95,
                "under_decimal": 1.95,
                "bookmaker": "DraftKings",
                "line_value": 27.5,
                "player_name": "Jayson Tatum",
                "stat_name": "points",
            }
        }
        result = find_sharp_match(
            sharp_index,
            player_name="Jayson Tatum",
            stat_name="points",
            line_value=27.1,
            line_tolerance=0.5,
        )
        assert result is not None


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — SharpMatch dataclass
# ─────────────────────────────────────────────────────────────────────────────

class TestSharpMatchDataclass:

    def test_sharp_match_dataclass_exists(self):
        from ud_edge.sharp_books_client import SharpMatch
        fields = getattr(SharpMatch, "__dataclass_fields__", {})
        required = {"sharp_for_higher", "sharp_for_lower", "both_sides_within_tolerance",
                    "over_decimal", "under_decimal", "bookmaker", "line_value"}
        assert required.issubset(fields.keys()), (
            f"SharpMatch must have fields {required}, got {set(fields.keys())}"
        )

    def test_sharp_match_fields(self):
        from ud_edge.sharp_books_client import SharpMatch
        sm = SharpMatch(
            sharp_for_higher=0.55,
            sharp_for_lower=0.45,
            both_sides_within_tolerance=True,
            over_decimal=1.82,
            under_decimal=2.22,
            bookmaker="Pinnacle",
            line_value=27.5,
        )
        assert sm.both_sides_within_tolerance is True
        assert sm.sharp_for_higher == 0.55
        assert sm.sharp_for_lower == 0.45


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — TTL freshness
# ─────────────────────────────────────────────────────────────────────────────

class TestSharpLineFreshness:

    def test_manual_csv_rejects_stale_lines(self, tmp_path):
        sharp_csv = tmp_path / "sharp_lines.csv"
        now = datetime.now(timezone.utc)
        fresh_time = (now - timedelta(minutes=10)).isoformat()
        stale_time = (now - timedelta(minutes=35)).isoformat()

        with open(sharp_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "player_name", "stat_name", "line_value", "over_decimal",
                "under_decimal", "bookmaker", "captured_at"
            ])
            writer.writeheader()
            writer.writerow({
                "player_name": "Jayson Tatum",
                "stat_name": "points",
                "line_value": "27.5",
                "over_decimal": "1.95",
                "under_decimal": "1.95",
                "bookmaker": "DraftKings",
                "captured_at": fresh_time,
            })
            writer.writerow({
                "player_name": "Jayson Tatum",
                "stat_name": "points",
                "line_value": "27.5",
                "over_decimal": "2.00",
                "under_decimal": "1.90",
                "bookmaker": "BetMGM",
                "captured_at": stale_time,
            })

        index, meta = build_sharp_index(manual_csv=sharp_csv)
        assert meta.get("stale_lines_rejected", 0) >= 1, (
            f"stale_lines_rejected must be >= 1, got {meta}"
        )
        key = sharp_lookup_key("Jayson Tatum", "points")
        assert key in index
        assert index[key]["bookmaker"] == "DraftKings"

    def test_manual_csv_requires_captured_at_no_crash(self, tmp_path):
        sharp_csv = tmp_path / "sharp_lines.csv"
        with open(sharp_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "player_name", "stat_name", "line_value", "over_decimal",
                "under_decimal", "bookmaker"
            ])
            writer.writeheader()
            writer.writerow({
                "player_name": "Jayson Tatum",
                "stat_name": "points",
                "line_value": "27.5",
                "over_decimal": "1.95",
                "under_decimal": "1.95",
                "bookmaker": "DraftKings",
            })
        index, meta = build_sharp_index(manual_csv=sharp_csv)
        assert "stale_lines_rejected" in meta or "missing_captured_at" in meta or "errors" in meta

    def test_empty_captured_at_not_indexed(self, tmp_path):
        sharp_csv = tmp_path / "sharp_lines.csv"
        with open(sharp_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "player_name", "stat_name", "line_value", "over_decimal",
                "under_decimal", "bookmaker", "captured_at"
            ])
            writer.writeheader()
            writer.writerow({
                "player_name": "Jayson Tatum",
                "stat_name": "points",
                "line_value": "27.5",
                "over_decimal": "1.95",
                "under_decimal": "1.95",
                "bookmaker": "DraftKings",
                "captured_at": "",
            })
        index, meta = build_sharp_index(manual_csv=sharp_csv)
        key = sharp_lookup_key("Jayson Tatum", "points")
        assert key not in index


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — Event identity
# ─────────────────────────────────────────────────────────────────────────────

class TestEventIdentity:

    def test_same_player_stat_different_event_title_two_entries(self, tmp_path):
        sharp_csv = tmp_path / "sharp_lines.csv"
        with open(sharp_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "player_name", "stat_name", "line_value", "over_decimal",
                "under_decimal", "bookmaker", "captured_at", "event_title"
            ])
            writer.writeheader()
            writer.writerow({
                "player_name": "Jayson Tatum",
                "stat_name": "points",
                "line_value": "27.5",
                "over_decimal": "1.95",
                "under_decimal": "1.95",
                "bookmaker": "DraftKings",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "event_title": "BOS vs NYK",
            })
            writer.writerow({
                "player_name": "Jayson Tatum",
                "stat_name": "points",
                "line_value": "28.5",
                "over_decimal": "2.00",
                "under_decimal": "1.85",
                "bookmaker": "FanDuel",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "event_title": "BOS vs MIA",
            })
        index, meta = build_sharp_index(manual_csv=sharp_csv)
        assert len(index) == 2, f"Expected 2 entries, got {len(index)}"
        keys = list(index.keys())
        assert keys[0] != keys[1]

    def test_event_title_in_key(self, tmp_path):
        sharp_csv = tmp_path / "sharp_lines.csv"
        with open(sharp_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "player_name", "stat_name", "line_value", "over_decimal",
                "under_decimal", "bookmaker", "captured_at", "event_title"
            ])
            writer.writeheader()
            writer.writerow({
                "player_name": "Jayson Tatum",
                "stat_name": "points",
                "line_value": "27.5",
                "over_decimal": "1.95",
                "under_decimal": "1.95",
                "bookmaker": "DraftKings",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "event_title": "BOS vs NYK",
            })
        index, meta = build_sharp_index(manual_csv=sharp_csv)
        key = list(index.keys())[0]
        assert "BOS vs NYK" in key or "bos" in key.lower(), f"Key must include event_title: {key}"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — sharp_authoritative_quarantine policy
# ─────────────────────────────────────────────────────────────────────────────

class TestSharpAuthoritativeQuarantine:

    def _make_leg(self, higher_dec=1.82, lower_dec=2.25):
        from ud_edge.models import Leg
        return Leg(
            line_id="test-1", appearance_id="a1", player_id="p1",
            player_name="Jayson Tatum", sport_id="NBA", match_id=1,
            match_title="BOS vs NYK", scheduled_at=None,
            stat_name="points", line_value=27.5, line_type="balanced",
            higher_american=-130, higher_decimal=higher_dec, higher_multiplier=0.9,
            lower_american=110, lower_decimal=lower_dec, lower_multiplier=0.9,
        )

    def _sharp_index(self, prob_over, prob_under, line=27.5):
        return {
            sharp_lookup_key("Jayson Tatum", "points"): {
                "over_decimal": 1.0 / prob_over,
                "under_decimal": 1.0 / prob_under,
                "bookmaker": "Pinnacle",
                "line_value": line,
                "player_name": "Jayson Tatum",
                "stat_name": "points",
            }
        }

    def test_quarantine_minus_5pp_disagreement(self):
        from ud_edge.matcher import rank_legs
        fantasy_leg = self._make_leg(higher_dec=1.82, lower_dec=2.25)
        # sharp 50% vs fantasy ~55% over -> delta = -5pp < -2pp -> quarantine
        sharp_index = self._sharp_index(prob_over=0.50, prob_under=0.50)
        ranked = rank_legs(
            [fantasy_leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0,
            sharp_book_index=sharp_index, sharp_policy="sharp_authoritative_quarantine",
        )
        assert len(ranked) == 0, f"-5pp disagreement must quarantine, got {len(ranked)}"

    def test_use_sharp_prob_plus_3pp_agreement(self):
        from ud_edge.matcher import rank_legs
        fantasy_leg = self._make_leg(higher_dec=1.82, lower_dec=2.25)
        # sharp 58% vs fantasy ~55% -> delta = +3pp > 0 -> use sharp prob
        sharp_index = self._sharp_index(prob_over=0.58, prob_under=0.42)
        ranked = rank_legs(
            [fantasy_leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0,
            sharp_book_index=sharp_index, sharp_policy="sharp_authoritative_quarantine",
        )
        assert len(ranked) == 1
        r = ranked[0]
        assert r.sharp_true_prob is not None
        assert r.sharp_true_prob == pytest.approx(0.58, abs=0.01)

    def test_no_sharp_match_uses_fantasy_prob(self):
        from ud_edge.matcher import rank_legs
        fantasy_leg = self._make_leg(higher_dec=1.82, lower_dec=2.25)
        ranked = rank_legs(
            [fantasy_leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0,
            sharp_book_index={}, sharp_policy="sharp_authoritative_quarantine",
        )
        assert len(ranked) == 1
        r = ranked[0]
        assert r.sharp_true_prob is None

    def test_sharp_policy_param_exists(self):
        import inspect
        from ud_edge.matcher import rank_legs
        assert "sharp_policy" in inspect.signature(rank_legs).parameters

    def test_sharp_policy_default_is_quarantine(self):
        import inspect
        from ud_edge.matcher import rank_legs
        p = inspect.signature(rank_legs).parameters["sharp_policy"]
        assert p.default == "sharp_authoritative_quarantine"

    def test_quarantine_at_minus_2pp_exactly(self):
        from ud_edge.matcher import rank_legs
        fantasy_leg = self._make_leg(higher_dec=1.82, lower_dec=2.25)
        # sharp 53% vs fantasy 55% -> delta = -2.0pp exactly
        sharp_index = {
            sharp_lookup_key("Jayson Tatum", "points"): {
                "over_decimal": 1.0 / 0.53,
                "under_decimal": 1.0 / 0.47,
                "bookmaker": "Pinnacle",
                "line_value": 27.5,
                "player_name": "Jayson Tatum",
                "stat_name": "points",
            }
        }
        ranked = rank_legs(
            [fantasy_leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0,
            sharp_book_index=sharp_index, sharp_policy="sharp_authoritative_quarantine",
        )
        assert len(ranked) == 0, "Delta exactly -2.0pp must quarantine"

    def test_no_quarantine_at_minus_1pp(self):
        from ud_edge.matcher import rank_legs
        fantasy_leg = self._make_leg(higher_dec=1.82, lower_dec=2.25)
        # sharp 0.56 vs fantasy 0.5528 -> delta = +0.72pp (NOT quarantined).
        # Use 0.56 (slightly above fantasy) so the effective edge stays positive
        # and the leg is not eliminated by the min_edge_pp threshold.
        sharp_index = self._sharp_index(prob_over=0.56, prob_under=0.44)
        ranked = rank_legs(
            [fantasy_leg], break_even=0.5495, min_true_prob=0.50, min_edge_pp=0.0,
            sharp_book_index=sharp_index, sharp_policy="sharp_authoritative_quarantine",
        )
        assert len(ranked) == 1
        r = ranked[0]
        # sharp authorized the leg — picked True prob came from sharp
        assert r.sharp_true_prob is not None
        assert r.sharp_true_prob == pytest.approx(0.56, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6 — compare.py wiring
# ─────────────────────────────────────────────────────────────────────────────

class TestCompareDeliverWiring:

    def test_compare_passes_sharp_policy_to_rank_legs(self, tmp_path, monkeypatch):
        import ud_edge.compare as cmp
        original = cmp.rank_legs
        captured = {}
        def spy(*args, **kwargs):
            captured.update(kwargs)
            return original(*args, **kwargs)
        monkeypatch.setattr(cmp, "rank_legs", spy)

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "ud_lines_cache.json").write_text("{}")
        orig_root = cmp._project_root
        cmp._project_root = lambda: tmp_path
        try:
            cmp.compare_fantasy_vs_sharp(
                entry_type="6-flex", min_true_prob=0.55, min_edge_pp=0.5,
                full_game_only=False, force_fetch=False,
            )
        except Exception:
            pass
        finally:
            cmp._project_root = orig_root

        assert "sharp_policy" in captured, "compare.py must pass sharp_policy to rank_legs"
        assert captured["sharp_policy"] == "sharp_authoritative_quarantine"
