"""TDD tests for the generic second-source snapshot adapter.

These tests cover:
1. capture_from_observations — no-vig from both-sided decimals
2. capture_from_observations — 0.0 true probs when one side missing
3. capture_from_observations — canonical key dedupe reuse
4. capture_from_observations — rollback on real DB error
5. CSV adapter parse helper
6. CLI --ingest-csv end-to-end with stale detection
7. Full snapshot DB invariants (source count grows; underdog unchanged)
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest


# ─── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    from ud_edge.stale_pricing import SnapshotStore
    db_path = tmp_path / "test_second_source.sqlite3"
    store = SnapshotStore(db_path=db_path)
    store.init()
    return store


@pytest.fixture
def frozen_now():
    """Return a consistent UTC timestamp for all tests in this module."""
    return datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)


# ─── 1. capture_from_observations — no-vig from both-sided decimals ─────────

class TestCaptureFromObservations:
    def test_no_vig_from_both_sided_decimals(self, tmp_db, frozen_now):
        """Both-sided decimals → no-vig true probabilities stored correctly."""
        from ud_edge.stale_pricing import capture_from_observations, utc_now
        from ud_edge import stale_pricing
        import datetime as dt_module

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return frozen_now

        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            observations = [
                dict(
                    player_name="Jayson Tatum",
                    sport_id="NBA",
                    stat_name="points",
                    line_value=27.5,
                    match_title="BOS@NYK",
                    scheduled_at="2026-07-20T18:00:00Z",
                    higher_decimal=1.74,
                    lower_decimal=2.10,
                    source_line_id="pp_line_1",
                ),
            ]
            rows = capture_from_observations(
                observations, tmp_db, source="prizepicks", captured_at=frozen_now
            )
            assert len(rows) == 1

            cur = tmp_db.conn.cursor()
            cur.execute(
                "SELECT higher_true_prob, lower_true_prob FROM snapshots WHERE source='prizepicks'"
            )
            higher_prob, lower_prob = cur.fetchone()
            # Compute expected from no_vig(1.74, 2.10)
            from ud_edge.no_vig import no_vig
            exp_over, exp_under, _ = no_vig(1.74, 2.10)
            assert abs(higher_prob - exp_over) < 0.001
            assert abs(lower_prob - exp_under) < 0.001
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_zero_true_probs_when_one_side_missing(self, tmp_db, frozen_now):
        """Only higher_decimal priced → both true probs stored as 0.0."""
        from ud_edge.stale_pricing import capture_from_observations
        from ud_edge import stale_pricing
        import datetime as dt_module

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return frozen_now

        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            observations = [
                dict(
                    player_name="Luka Doncic",
                    sport_id="NBA",
                    stat_name="points",
                    line_value=33.5,
                    match_title="DAL@LAL",
                    scheduled_at="2026-07-20T20:00:00Z",
                    higher_decimal=1.91,   # only over side known
                    lower_decimal=0.0,     # missing
                    source_line_id="pp_line_2",
                ),
            ]
            rows = capture_from_observations(
                observations, tmp_db, source="prizepicks", captured_at=frozen_now
            )
            assert len(rows) == 1

            cur = tmp_db.conn.cursor()
            cur.execute(
                "SELECT higher_true_prob, lower_true_prob FROM snapshots WHERE source='prizepicks'"
            )
            higher_prob, lower_prob = cur.fetchone()
            assert higher_prob == 0.0
            assert lower_prob == 0.0
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_dedupe_reuses_same_source_key_time(self, tmp_db, frozen_now):
        """Identical observation (same source/key/time) → same row ID (deduped)."""
        from ud_edge.stale_pricing import capture_from_observations
        from ud_edge import stale_pricing
        import datetime as dt_module

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return frozen_now

        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            obs = dict(
                player_name="Jayson Tatum",
                sport_id="NBA",
                stat_name="points",
                line_value=27.5,
                match_title="BOS@NYK",
                scheduled_at="2026-07-20T18:00:00Z",
                higher_decimal=1.74,
                lower_decimal=2.10,
                source_line_id="pp_line_3",
            )
            rows1 = capture_from_observations(
                [obs], tmp_db, source="prizepicks", captured_at=frozen_now
            )
            rows2 = capture_from_observations(
                [obs], tmp_db, source="prizepicks", captured_at=frozen_now
            )
            assert rows1 == rows2

            cur = tmp_db.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM snapshots WHERE source='prizepicks'")
            assert cur.fetchone()[0] == 1
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_rollback_on_real_db_error(self, tmp_path, frozen_now):
        """A SQLite-level IntegrityError on row 2 rolls back the entire batch."""
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_from_observations,
            SnapshotRecord,
        )
        from ud_edge import stale_pricing
        import datetime as dt_module

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return frozen_now

        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "rollback_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            # Add a trigger that aborts inserts for a specific player name
            store.conn.execute("""
                CREATE TRIGGER fail_player_two
                BEFORE INSERT ON snapshots
                WHEN NEW.player_name = 'Player Two'
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic failure on second obs');
                END
            """)
            store.conn.commit()

            observations = [
                dict(
                    player_name="Player One",
                    sport_id="NBA",
                    stat_name="points",
                    line_value=20.5,
                    match_title="TEAM1@TEAM2",
                    scheduled_at="2026-07-20T18:00:00Z",
                    higher_decimal=1.91,
                    lower_decimal=1.91,
                    source_line_id="l1",
                ),
                dict(
                    player_name="Player Two",
                    sport_id="NBA",
                    stat_name="points",
                    line_value=21.5,
                    match_title="TEAM1@TEAM2",
                    scheduled_at="2026-07-20T18:00:00Z",
                    higher_decimal=1.91,
                    lower_decimal=1.91,
                    source_line_id="l2",
                ),
            ]

            with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
                capture_from_observations(
                    observations, store, source="prizepicks", captured_at=frozen_now
                )

            # Nothing should be committed
            count = store.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            assert count == 0
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_dict_and_dataclass_observations_both_work(self, tmp_db, frozen_now):
        """Both plain dicts and dataclass-like objects are accepted."""
        from ud_edge.stale_pricing import capture_from_observations
        from ud_edge import stale_pricing
        from dataclasses import dataclass
        import datetime as dt_module

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return frozen_now

        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)

        @dataclass
        class Obs:
            player_name: str
            sport_id: str
            stat_name: str
            line_value: float
            match_title: str
            scheduled_at: str
            higher_decimal: float
            lower_decimal: float
            source_line_id: str

        try:
            obs = Obs(
                player_name="Jayson Tatum",
                sport_id="NBA",
                stat_name="points",
                line_value=27.5,
                match_title="BOS@NYK",
                scheduled_at="2026-07-20T18:00:00Z",
                higher_decimal=1.74,
                lower_decimal=2.10,
                source_line_id="pp_line_4",
            )
            rows = capture_from_observations(
                [obs], tmp_db, source="prizepicks", captured_at=frozen_now
            )
            assert len(rows) == 1

            cur = tmp_db.conn.cursor()
            cur.execute("SELECT player_name FROM snapshots WHERE source='prizepicks'")
            assert cur.fetchone()[0] == "Jayson Tatum"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_captured_at_honored(self, tmp_db, frozen_now):
        """captured_at parameter overrides utc_now() for the batch timestamp."""
        from ud_edge.stale_pricing import capture_from_observations
        custom_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        observations = [
            dict(
                player_name="Jayson Tatum",
                sport_id="NBA",
                stat_name="points",
                line_value=27.5,
                match_title="BOS@NYK",
                scheduled_at="2026-07-20T18:00:00Z",
                higher_decimal=1.74,
                lower_decimal=2.10,
                source_line_id="pp_line_5",
            ),
        ]
        rows = capture_from_observations(
            observations, tmp_db, source="prizepicks", captured_at=custom_time
        )
        assert len(rows) == 1

        cur = tmp_db.conn.cursor()
        cur.execute("SELECT captured_at FROM snapshots WHERE source='prizepicks'")
        stored_captured = cur.fetchone()[0]
        assert "2025-01-01" in stored_captured


# ─── 2. CSV adapter parse helper ─────────────────────────────────────────────

class TestCSVAdapter:
    def test_csv_parse_returns_observations_list(self, tmp_path):
        """parse_prizepicks_csv returns a list of observation dicts."""
        from ud_edge.pp_clipboard import parse_prizepicks_csv

        csv_content = (
            "player_name,league,stat_type,line,higher_decimal,lower_decimal,"
            "event_title,scheduled_at\n"
            "Jayson Tatum,NBA,Points,27.5,1.74,2.10,BOS@NYK,2026-07-20T18:00:00Z\n"
            "Luka Doncic,NBA,Points,33.5,1.91,0.0,DAL@LAL,2026-07-20T20:00:00Z\n"
        )
        csv_path = tmp_path / "test_board.csv"
        csv_path.write_text(csv_content)

        observations = parse_prizepicks_csv(csv_path)
        assert len(observations) == 2

        obs1 = observations[0]
        assert obs1["player_name"] == "Jayson Tatum"
        assert obs1["stat_name"] == "points"
        assert obs1["line_value"] == 27.5
        assert obs1["higher_decimal"] == 1.74
        assert obs1["lower_decimal"] == 2.10

        obs2 = observations[1]
        assert obs2["player_name"] == "Luka Doncic"
        assert obs2["stat_name"] == "points"
        assert obs2["line_value"] == 33.5
        assert obs2["lower_decimal"] == 0.0

    def test_csv_skips_rows_missing_player_stat_or_line(self, tmp_path):
        """Rows missing player_name/stat_type/line are skipped silently."""
        from ud_edge.pp_clipboard import parse_prizepicks_csv

        csv_content = (
            "player_name,league,stat_type,line,higher_decimal,lower_decimal,"
            "event_title,scheduled_at\n"
            "Jayson Tatum,NBA,Points,27.5,1.74,2.10,BOS@NYK,2026-07-20T18:00:00Z\n"
            ",NBA,Points,27.5,1.74,2.10,BOS@NYK,2026-07-20T18:00:00Z\n"  # missing player
            "Luka Doncic,NBA,,33.5,1.91,0.0,DAL@LAL,2026-07-20T20:00:00Z\n"  # missing stat
            "Stephen Curry,NBA,Points,,1.91,1.91,GSW@PHX,2026-07-20T22:00:00Z\n"  # missing line
        )
        csv_path = tmp_path / "test_skip.csv"
        csv_path.write_text(csv_content)

        observations = parse_prizepicks_csv(csv_path)
        assert len(observations) == 1
        assert observations[0]["player_name"] == "Jayson Tatum"

    def test_csv_column_order_is_canonical(self, tmp_path):
        """Canonical column order (as specified) is accepted; extra columns ignored."""
        from ud_edge.pp_clipboard import parse_prizepicks_csv

        csv_content = (
            "player_name,league,stat_type,line,higher_decimal,lower_decimal,"
            "event_title,scheduled_at,extra_col,bad_col\n"
            "Jayson Tatum,NBA,Points,27.5,1.74,2.10,BOS@NYK,2026-07-20T18:00:00Z,foo,bar\n"
        )
        csv_path = tmp_path / "test_extra_cols.csv"
        csv_path.write_text(csv_content)

        observations = parse_prizepicks_csv(csv_path)
        assert len(observations) == 1
        assert "extra_col" not in observations[0]


# ─── 3. CLI --ingest-csv end-to-end ─────────────────────────────────────────

class TestCLIIngestCSV:
    def test_ingest_csv_flag_produces_prizepicks_rows(self, tmp_path, monkeypatch):
        """--ingest-csv with the test file creates prizepicks source rows in the DB."""
        from ud_edge.__main__ import main
        from ud_edge import stale_pricing
        from ud_edge.ud_client import UDClient

        csv_content = (
            "player_name,league,stat_type,line,higher_decimal,lower_decimal,"
            "event_title,scheduled_at\n"
            "Jayson Tatum,NBA,Points,27.5,1.74,2.10,BOS@NYK,2026-07-20T18:00:00Z\n"
        )
        csv_path = tmp_path / "prizepicks_board.csv"
        csv_path.write_text(csv_content)

        db_path = tmp_path / "cli_ingest.sqlite3"
        captured_at = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        class FrozenDatetime:
            @staticmethod
            def now(tz=None):
                return captured_at

        import datetime as dt_module
        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: captured_at

        # Block the real network call
        monkeypatch.setattr(UDClient, "fetch", lambda self, force=False: {"over_under_lines": [], "players": [], "appearances": [], "games": []})

        try:
            argv = [
                "--snapshot",
                "--snapshot-db", str(db_path),
                "--ingest-csv", str(csv_path),
            ]
            exit_code = main(argv)
            assert exit_code == 0

            # Verify prizepicks rows were stored
            store = stale_pricing.SnapshotStore(db_path=db_path)
            store.init()
            cur = store.conn.cursor()
            cur.execute(
                "SELECT source, player_name, line_value FROM snapshots "
                "WHERE source='prizepicks' ORDER BY source"
            )
            rows = cur.fetchall()
            assert len(rows) == 1, f"Expected exactly 1 prizepicks row, got {len(rows)}"
            assert rows[0][0] == "prizepicks"
            assert rows[0][1] == "Jayson Tatum"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_csv_source_name_override(self, tmp_path, monkeypatch):
        """--csv-source overrides the default 'prizepicks' source name."""
        from ud_edge.__main__ import main
        from ud_edge import stale_pricing
        from ud_edge.ud_client import UDClient

        csv_content = (
            "player_name,league,stat_type,line,higher_decimal,lower_decimal,"
            "event_title,scheduled_at\n"
            "Luka Doncic,NBA,Points,33.5,1.91,0.0,DAL@LAL,2026-07-20T20:00:00Z\n"
        )
        csv_path = tmp_path / "board2.csv"
        csv_path.write_text(csv_content)

        db_path = tmp_path / "cli_source_name.sqlite3"
        captured_at = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        class FrozenDatetime:
            @staticmethod
            def now(tz=None):
                return captured_at

        import datetime as dt_module
        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: captured_at

        # Block real network
        monkeypatch.setattr(UDClient, "fetch", lambda self, force=False: {"over_under_lines": [], "players": [], "appearances": [], "games": []})

        try:
            argv = [
                "--snapshot",
                "--snapshot-db", str(db_path),
                "--ingest-csv", str(csv_path),
                "--csv-source", "draftkings",
            ]
            exit_code = main(argv)
            assert exit_code == 0

            store = stale_pricing.SnapshotStore(db_path=db_path)
            store.init()
            cur = store.conn.cursor()
            cur.execute("SELECT DISTINCT source FROM snapshots")
            sources = [r[0] for r in cur.fetchall()]
            assert "draftkings" in sources
            assert "prizepicks" not in sources
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now


# ─── 4. Full cross-source stale detection end-to-end ────────────────────────

class TestCrossSourceStaleDetection:
    def test_two_source_demo_triggers_stale_opportunity(self, tmp_path, monkeypatch):
        """Tatum points 27.5 at prizepicks (1.74/2.10) vs 28.5 at draftkings
        produces a cross-source stale opportunity end-to-end through the CLI.

        Setup:
        - prizepicks: Tatum 27.5 at t_old and unchanged at t_now → stale
        - draftkings: Tatum 28.5 at t_old, moved to 28.0 at t_now → fresh
        - stale detection should find prizepicks=27.5 < draftkings=28.5 gap=1.0
        """
        from ud_edge.__main__ import main
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_from_observations,
        )

        db_path = tmp_path / "two_source_stale.sqlite3"
        captured_at = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        class FrozenDatetime:
            @staticmethod
            def now(tz=None):
                return captured_at

        import datetime as dt_module
        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: captured_at

        try:
            # Build DB with two-source data using capture_from_observations
            store = SnapshotStore(db_path=db_path)
            store.init()

            # prizepicks: Tatum 27.5 at t_old and t_now (unchanged → stale)
            t_old = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)  # 120 min ago
            t_now = captured_at

            prizepicks_obs_old = dict(
                player_name="Jayson Tatum",
                sport_id="NBA",
                stat_name="points",
                line_value=27.5,
                match_title="BOS@NYK",
                scheduled_at="2026-07-20T18:00:00Z",
                higher_decimal=1.74,
                lower_decimal=2.10,
                source_line_id="pp_old",
            )
            prizepicks_obs_new = dict(
                player_name="Jayson Tatum",
                sport_id="NBA",
                stat_name="points",
                line_value=27.5,
                match_title="BOS@NYK",
                scheduled_at="2026-07-20T18:00:00Z",
                higher_decimal=1.74,
                lower_decimal=2.10,
                source_line_id="pp_new",
            )
            capture_from_observations(
                [prizepicks_obs_old], store, source="prizepicks", captured_at=t_old
            )
            capture_from_observations(
                [prizepicks_obs_new], store, source="prizepicks", captured_at=t_now
            )

            # draftkings: Tatum 28.5 at t_old, moved to 28.0 at t_now (fresh)
            draftkings_obs_old = dict(
                player_name="Jayson Tatum",
                sport_id="NBA",
                stat_name="points",
                line_value=28.5,
                match_title="BOS@NYK",
                scheduled_at="2026-07-20T18:00:00Z",
                higher_decimal=1.80,
                lower_decimal=2.00,
                source_line_id="dk_old",
            )
            draftkings_obs_new = dict(
                player_name="Jayson Tatum",
                sport_id="NBA",
                stat_name="points",
                line_value=28.0,
                match_title="BOS@NYK",
                scheduled_at="2026-07-20T18:00:00Z",
                higher_decimal=1.83,
                lower_decimal=1.97,
                source_line_id="dk_new",
            )
            capture_from_observations(
                [draftkings_obs_old], store, source="draftkings", captured_at=t_old
            )
            capture_from_observations(
                [draftkings_obs_new], store, source="draftkings", captured_at=t_now
            )

            # Run stale detection
            from ud_edge.stale_pricing import detect_stale_opportunities
            stale = detect_stale_opportunities(
                store,
                min_stale_minutes=30,
                fresh_window_minutes=120,
                min_line_gap=0.5,
                min_prob_gap_pp=0.0,
            )

            assert len(stale) >= 1, f"Expected stale opportunity, got: {stale}"
            s = stale[0]
            assert s["stale_source"] == "prizepicks"
            assert s["fresh_source"] == "draftkings"
            assert s["direction"] == "higher"
            assert s["line_gap"] >= 0.5
            assert s["stale_line"] == 27.5
            assert s["fresh_line"] in (28.5, 28.0)

        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_source_distinct_count_grows(self, tmp_db, frozen_now):
        """Adding a new source increases the distinct source count."""
        from ud_edge.stale_pricing import capture_from_observations, capture_underdog
        from ud_edge.models import Leg
        from ud_edge import stale_pricing
        import datetime as dt_module

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return frozen_now

        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            leg = Leg(
                line_id="l1", appearance_id="a1", player_id="p1",
                player_name="Jayson Tatum", sport_id="NBA", match_id=1,
                match_title="BOS@NYK", scheduled_at="2026-07-20T18:00:00Z",
                stat_name="points", line_value=27.5, line_type="balanced",
                higher_american=-136, higher_decimal=1.735, higher_multiplier=0.86,
                lower_american=110, lower_decimal=2.10, lower_multiplier=1.10,
            )

            capture_underdog([leg], tmp_db, source="underdog", captured_at=frozen_now)

            cur = tmp_db.conn.cursor()
            cur.execute("SELECT COUNT(DISTINCT source) FROM snapshots")
            assert cur.fetchone()[0] == 1

            capture_from_observations(
                [dict(
                    player_name="Jayson Tatum", sport_id="NBA", stat_name="points",
                    line_value=27.5, match_title="BOS@NYK",
                    scheduled_at="2026-07-20T18:00:00Z",
                    higher_decimal=1.74, lower_decimal=2.10, source_line_id="pp1",
                )],
                tmp_db, source="prizepicks", captured_at=frozen_now,
            )

            cur.execute("SELECT COUNT(DISTINCT source) FROM snapshots")
            assert cur.fetchone()[0] == 2
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_underdog_source_unchanged_after_prizepicks_ingest(self, tmp_path, frozen_now):
        """Ingesting prizepicks data does not modify underdog rows."""
        from ud_edge.stale_pricing import SnapshotStore, capture_underdog, capture_from_observations
        from ud_edge.models import Leg
        from ud_edge import stale_pricing
        import datetime as dt_module

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return frozen_now

        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "underdog_unchanged.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            leg = Leg(
                line_id="ud_1", appearance_id="a1", player_id="p1",
                player_name="Jayson Tatum", sport_id="NBA", match_id=1,
                match_title="BOS@NYK", scheduled_at="2026-07-20T18:00:00Z",
                stat_name="points", line_value=27.5, line_type="balanced",
                higher_american=-136, higher_decimal=1.735, higher_multiplier=0.86,
                lower_american=110, lower_decimal=2.10, lower_multiplier=1.10,
            )
            capture_underdog([leg], store, source="underdog", captured_at=frozen_now)

            # Ingest prizepicks data
            capture_from_observations(
                [dict(
                    player_name="Jayson Tatum", sport_id="NBA", stat_name="points",
                    line_value=27.5, match_title="BOS@NYK",
                    scheduled_at="2026-07-20T18:00:00Z",
                    higher_decimal=1.74, lower_decimal=2.10, source_line_id="pp1",
                )],
                store, source="prizepicks", captured_at=frozen_now,
            )

            cur = store.conn.cursor()
            cur.execute(
                "SELECT source, player_name, line_value, higher_true_prob "
                "FROM snapshots WHERE source='underdog'"
            )
            ud_rows = cur.fetchall()
            assert len(ud_rows) == 1
            src, player, line, higher_prob = ud_rows[0]
            assert src == "underdog"
            assert player == "Jayson Tatum"
            assert line == 27.5
            # Underdog's true prob must be from no_vig(1.735, 2.10)
            from ud_edge.no_vig import no_vig
            exp_over, _, _ = no_vig(1.735, 2.10)
            assert abs(higher_prob - exp_over) < 0.001
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now


# ─── 5. Demo seed file ──────────────────────────────────────────────────────

class TestDemoSeedFile:
    def test_two_source_demo_csv_exists(self):
        """tests/data/two_source_demo.csv must exist and be parseable."""
        demo_path = Path(__file__).parent / "data" / "two_source_demo.csv"
        assert demo_path.exists(), f"Demo seed file not found: {demo_path}"

        with open(demo_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) >= 2, "two_source_demo.csv must have at least 2 rows (one per source)"

        # Find Tatum row
        tatum_rows = [r for r in rows if "tatum" in r.get("player_name", "").lower()]
        assert tatum_rows, "two_source_demo.csv must contain a Jayson Tatum row"
        tatum = tatum_rows[0]

        # Find draftkings parallel row
        dk_rows = [r for r in rows if r.get("source", "").lower() == "draftkings"
                   and "tatum" in r.get("player_name", "").lower()]
        assert dk_rows, "two_source_demo.csv must contain a draftkings Tatum row"

    def test_two_source_demo_csv_parsed_by_adapter(self, tmp_path):
        """The demo CSV can be loaded and fed through capture_from_observations."""
        from ud_edge.pp_clipboard import parse_prizepicks_csv
        from ud_edge.stale_pricing import SnapshotStore, capture_from_observations
        from ud_edge import stale_pricing
        import datetime as dt_module

        demo_path = Path(__file__).parent / "data" / "two_source_demo.csv"
        if not demo_path.exists():
            pytest.skip("demo CSV not present")

        frozen_now = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return frozen_now

        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "demo_parse.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            observations = parse_prizepicks_csv(demo_path)
            assert len(observations) >= 2, f"Expected >=2 obs from demo CSV, got {len(observations)}"

            sources = set()
            for obs in observations:
                src = obs.get("source", "prizepicks")
                rows = capture_from_observations(
                    [obs], store, source=src, captured_at=frozen_now
                )
                sources.add(src)

            cur = store.conn.cursor()
            cur.execute("SELECT COUNT(DISTINCT source) FROM snapshots")
            assert cur.fetchone()[0] >= 2, "Demo CSV should populate at least 2 distinct sources"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now
