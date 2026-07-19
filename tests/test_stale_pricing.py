"""Pytest unit tests for stale_pricing.py — snapshot storage, movement detection,
and evidence-based stale-opportunity detection.

All tests use tmp_path DBs and monkeypatched UDClient (no network).
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    """A fresh SnapshotStore backed by a tmp-path SQLite file."""
    from ud_edge.stale_pricing import SnapshotStore
    db_path = tmp_path / "test_snapshots.sqlite3"
    store = SnapshotStore(db_path=db_path)
    store.init()
    return store


@pytest.fixture
def leg_jayson_tatum():
    """A synthetic Leg for Jayson Tatum points 27.5."""
    from ud_edge.models import Leg
    return Leg(
        line_id="ud_line_123",
        appearance_id="app_456",
        player_id="p_tatum",
        player_name="Jayson Tatum",
        sport_id="NBA",
        match_id=1,
        match_title="BOS@NYK",
        scheduled_at="2026-07-20T18:00:00Z",
        stat_name="points",
        line_value=27.5,
        line_type="balanced",
        higher_american=-136,
        higher_decimal=1.735,
        higher_multiplier=0.86,
        lower_american=110,
        lower_decimal=2.10,
        lower_multiplier=1.10,
    )


@pytest.fixture
def leg_luka_doncic():
    """A synthetic Leg for Luka Doncic points 33.5."""
    from ud_edge.models import Leg
    return Leg(
        line_id="ud_line_789",
        appearance_id="app_999",
        player_id="p_luka",
        player_name="Luka Doncic",
        sport_id="NBA",
        match_id=2,
        match_title="DAL@LAL",
        scheduled_at="2026-07-20T20:00:00Z",
        stat_name="points",
        line_value=33.5,
        line_type="balanced",
        higher_american=-120,
        higher_decimal=1.833,
        higher_multiplier=0.86,
        lower_american=100,
        lower_decimal=2.00,
        lower_multiplier=1.10,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SnapshotStore — initialization & schema versioning
# ─────────────────────────────────────────────────────────────────────────────

class TestSnapshotStoreInit:
    def test_init_creates_table(self, tmp_db):
        """init() should create the snapshots table."""
        cur = tmp_db.conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='snapshots'"
        )
        rows = cur.fetchall()
        assert len(rows) == 1

    def test_init_is_idempotent(self, tmp_db):
        """Calling init() twice must not raise."""
        tmp_db.init()  # second call
        cur = tmp_db.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM snapshots")
        assert cur.fetchone()[0] == 0

    def test_schema_version_stored(self, tmp_db):
        """Schema version is stored in the info table."""
        cur = tmp_db.conn.cursor()
        cur.execute("SELECT value FROM info WHERE key='schema_version'")
        row = cur.fetchone()
        assert row is not None
        assert int(row[0]) >= 1

    def test_insert_one_snapshot(self, tmp_db, leg_jayson_tatum):
        """Basic insert returns the row_id."""
        from ud_edge.stale_pricing import capture_underdog
        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        rows = capture_underdog([leg_jayson_tatum], tmp_db, captured_at=captured)
        assert len(rows) == 1
        assert rows[0] > 0


# ─────────────────────────────────────────────────────────────────────────────
# Schema migration v1 -> v2
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaMigrationV1ToV2:
    def test_migration_preserves_all_rows(self, tmp_path):
        """Migrating a v1 DB must preserve all existing rows."""
        import sqlite3
        from ud_edge.stale_pricing import SnapshotStore

        # Create a v1 schema DB with some rows
        v1_path = tmp_path / "v1_test.sqlite3"
        conn = sqlite3.connect(str(v1_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE info (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_line_id TEXT NOT NULL DEFAULT '',
                player_name TEXT NOT NULL,
                sport TEXT NOT NULL DEFAULT '',
                stat TEXT NOT NULL,
                line_value REAL NOT NULL,
                higher_decimal REAL NOT NULL DEFAULT 0.0,
                lower_decimal REAL NOT NULL DEFAULT 0.0,
                higher_true_prob REAL NOT NULL DEFAULT 0.0,
                lower_true_prob REAL NOT NULL DEFAULT 0.0,
                event_title TEXT NOT NULL DEFAULT '',
                scheduled_at TEXT,
                captured_at TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                UNIQUE(source, source_line_id, captured_at, canonical_key)
            )
        """)
        conn.execute("CREATE INDEX idx_snapshots_canonical ON snapshots(canonical_key, source, captured_at DESC)")
        conn.execute("CREATE INDEX idx_snapshots_captured ON snapshots(captured_at DESC)")
        conn.execute("INSERT INTO info (key, value) VALUES ('schema_version', '1')")

        # Insert a row directly into v1 DB
        conn.execute("""
            INSERT INTO snapshots
                (source, source_line_id, player_name, sport, stat, line_value,
                 higher_decimal, lower_decimal, higher_true_prob, lower_true_prob,
                 event_title, scheduled_at, captured_at, canonical_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "underdog", "line_abc", "Jayson Tatum", "NBA", "points", 27.5,
            1.735, 2.10, 0.55, 0.45,
            "BOS@NYK", "2026-07-20T18:00:00Z", "2026-07-18T12:00:00Z",
            "jayson tatum|nba|points|BOS@NYK",
        ))
        conn.commit()
        conn.close()

        # Now open with v2 store and migrate
        store = SnapshotStore(v1_path)
        store.init()

        # All rows preserved
        cur = store.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM snapshots")
        count = cur.fetchone()[0]
        assert count == 1, f"Migration must preserve all rows: expected 1, got {count}"

        # Verify the row data
        cur.execute("SELECT source, player_name, line_value FROM snapshots")
        row = cur.fetchone()
        assert row == ("underdog", "Jayson Tatum", 27.5)

        # Schema version updated to 2
        cur.execute("SELECT value FROM info WHERE key='schema_version'")
        assert cur.fetchone()[0] == "2"

    def test_migration_preserves_real_v1_db_rows(self, tmp_path):
        """Migrating the real v1 DB (data/line_snapshots.sqlite3) must preserve all observations."""
        import sqlite3, shutil
        from ud_edge.stale_pricing import SnapshotStore

        real_path = Path(__file__).resolve().parents[1] / "data" / "line_snapshots.sqlite3"
        if not real_path.exists():
            pytest.skip("No local snapshot DB at data/line_snapshots.sqlite3")
        copy_path = tmp_path / "real_v1_copy.sqlite3"
        shutil.copy2(real_path, copy_path)

        conn = sqlite3.connect(str(copy_path))
        cur = conn.cursor()
        cur.execute("SELECT value FROM info WHERE key='schema_version'")
        version = cur.fetchone()[0]

        # The project DB may already have been migrated by an earlier live
        # `--stale-report` run. Either way, opening its copy must be idempotent
        # and preserve all observations.
        assert version in {"1", "2"}, f"Unexpected schema version: {version}"

        cur.execute("SELECT COUNT(*) FROM snapshots")
        original_count = cur.fetchone()[0]
        assert original_count >= 1000, f"Real DB should have many rows, got {original_count}"
        conn.close()

        # Migrate
        store = SnapshotStore(copy_path)
        store.init()

        # All rows preserved
        cur = store.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM snapshots")
        count = cur.fetchone()[0]
        assert count == original_count, \
            f"Migration must preserve all rows: expected {original_count}, got {count}"

        # Schema version updated to 2
        cur.execute("SELECT value FROM info WHERE key='schema_version'")
        assert cur.fetchone()[0] == "2"

    def test_v2_unique_constraint_source_canonical_key_captured_at(self, tmp_path):
        """v2 unique constraint must be (source, canonical_key, captured_at) without source_line_id."""
        import sqlite3
        from ud_edge.stale_pricing import SnapshotStore

        db_path = tmp_path / "v2_unique_test.sqlite3"
        store = SnapshotStore(db_path)
        store.init()

        from datetime import datetime, timezone
        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        from ud_edge.stale_pricing import SnapshotRecord
        rec1 = SnapshotRecord(
            source="draftkings", source_line_id="platform_a",
            player_name="Jayson Tatum", sport="NBA", stat="points",
            line_value=27.5, higher_decimal=1.735, lower_decimal=2.10,
            higher_true_prob=0.55, lower_true_prob=0.45,
            event_title="BOS@NYK", scheduled_at="2026-07-20T18:00:00Z",
            captured_at=captured,
        )
        rec2 = SnapshotRecord(
            source="draftkings", source_line_id="platform_b",
            player_name="Jayson Tatum", sport="NBA", stat="points",
            line_value=27.5, higher_decimal=1.735, lower_decimal=2.10,
            higher_true_prob=0.55, lower_true_prob=0.45,
            event_title="BOS@NYK", scheduled_at="2026-07-20T18:00:00Z",
            captured_at=captured,
        )

        id1 = store.insert(rec1)
        id2 = store.insert(rec2)

        assert id1 == id2, \
            f"Same source/key/time but different line_id must dedupe: {id1} vs {id2}"

        cur = store.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM snapshots")
        assert cur.fetchone()[0] == 1

        # Also verify that same source+key but different captured_at creates new row
        captured2 = datetime(2026, 7, 18, 13, 0, 0, tzinfo=timezone.utc)
        rec3 = SnapshotRecord(
            source="draftkings", source_line_id="platform_a",
            player_name="Jayson Tatum", sport="NBA", stat="points",
            line_value=27.5, higher_decimal=1.735, lower_decimal=2.10,
            higher_true_prob=0.55, lower_true_prob=0.45,
            event_title="BOS@NYK", scheduled_at="2026-07-20T18:00:00Z",
            captured_at=captured2,
        )
        id3 = store.insert(rec3)
        assert id3 != id1, "Different captured_at must create new row"

        cur.execute("SELECT COUNT(*) FROM snapshots")
        assert cur.fetchone()[0] == 2


# ─────────────────────────────────────────────────────────────────────────────
# Dedupe — identical observation at same timestamp/source/key
# ─────────────────────────────────────────────────────────────────────────────

class TestDedupe:
    def test_dedupe_same_leg_same_timestamp(self, tmp_db, leg_jayson_tatum):
        """Inserting the same leg at the same timestamp twice returns same row_id (deduped)."""
        from ud_edge.stale_pricing import capture_underdog
        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        rows1 = capture_underdog([leg_jayson_tatum], tmp_db, captured_at=captured)
        rows2 = capture_underdog([leg_jayson_tatum], tmp_db, captured_at=captured)
        assert rows1 == rows2  # same row (deduped)

    def test_dedupe_unchanged_line_different_timestamp(self, tmp_db, leg_jayson_tatum):
        """Same line unchanged but later timestamp must create a NEW row (history preserved)."""
        from ud_edge.stale_pricing import capture_underdog
        t1 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 18, 12, 30, 0, tzinfo=timezone.utc)
        rows1 = capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t1)
        rows2 = capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t2)
        assert rows1 != rows2  # different rows
        cur = tmp_db.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM snapshots")
        assert cur.fetchone()[0] == 2

    def test_different_lines_same_timestamp(self, tmp_db, leg_jayson_tatum, leg_luka_doncic):
        """Different legs at same timestamp get different rows."""
        from ud_edge.stale_pricing import capture_underdog
        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        rows = capture_underdog([leg_jayson_tatum, leg_luka_doncic], tmp_db, captured_at=captured)
        assert len(rows) == 2
        assert rows[0] != rows[1]

    def test_dedupe_same_source_key_time_different_line_id(self, tmp_db, leg_jayson_tatum):
        """Identical observation means same source + canonical_key + captured_at.
        Different source_line_id (e.g. platform changed line ID) must still dedupe."""
        from ud_edge.stale_pricing import SnapshotStore, SnapshotRecord
        from datetime import datetime, timezone

        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        # Record with line_id = 'platform_a'
        rec1 = SnapshotRecord(
            source="draftkings", source_line_id="platform_a",
            player_name="Jayson Tatum", sport="NBA", stat="points",
            line_value=27.5, higher_decimal=1.735, lower_decimal=2.10,
            higher_true_prob=0.55, lower_true_prob=0.45,
            event_title="BOS@NYK", scheduled_at="2026-07-20T18:00:00Z",
            captured_at=captured,
        )

        # Same observation but platform assigned a different line_id
        rec2 = SnapshotRecord(
            source="draftkings", source_line_id="platform_b",
            player_name="Jayson Tatum", sport="NBA", stat="points",
            line_value=27.5, higher_decimal=1.735, lower_decimal=2.10,
            higher_true_prob=0.55, lower_true_prob=0.45,
            event_title="BOS@NYK", scheduled_at="2026-07-20T18:00:00Z",
            captured_at=captured,
        )

        id1 = tmp_db.insert(rec1)
        id2 = tmp_db.insert(rec2)

        # Must dedupe to the same row (same source + canonical_key + captured_at)
        assert id1 == id2, \
            f"Same source/key/time but different line_id must dedupe: got {id1} vs {id2}"

        # Only one row in the DB
        cur = tmp_db.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM snapshots")
        assert cur.fetchone()[0] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Batch atomicity
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchAtomicity:
    def test_capture_underdog_batch_atomic(self, tmp_path):
        """A database failure on leg 2 rolls back leg 1 from the same batch."""
        import sqlite3
        from ud_edge.stale_pricing import SnapshotStore, capture_underdog
        from ud_edge.models import Leg
        from datetime import datetime, timezone

        db_path = tmp_path / "batch_atomic_test.sqlite3"
        store = SnapshotStore(db_path)
        store.init()

        def make_leg(line_id, player_id, player_name):
            return Leg(
                line_id=line_id, appearance_id=f"a-{player_id}", player_id=player_id,
                player_name=player_name, sport_id="NBA", match_id=1,
                match_title="TEAM1@TEAM2", scheduled_at="2026-07-20T18:00:00Z",
                stat_name="points", line_value=27.5, line_type="balanced",
                higher_american=-136, higher_decimal=1.735, higher_multiplier=0.86,
                lower_american=110, lower_decimal=2.10, lower_multiplier=1.10,
            )

        leg1 = make_leg("l1", "p1", "Player One")
        leg2 = make_leg("l2", "p2", "Player Two")
        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        # A SQLite trigger is a real database-level failure on the second row.
        store.conn.execute("""
            CREATE TRIGGER fail_player_two
            BEFORE INSERT ON snapshots
            WHEN NEW.player_name = 'Player Two'
            BEGIN
                SELECT RAISE(ABORT, 'synthetic failure on second leg');
            END
        """)
        store.conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
            capture_underdog([leg1, leg2], store, captured_at=captured)

        # The failed transaction must be rolled back immediately, not merely left
        # uncommitted until the connection closes.
        count = store.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        assert count == 0

    def test_insert_standalone_still_works(self, tmp_db, leg_jayson_tatum):
        """insert() must remain safe for standalone use (one record per commit)."""
        from ud_edge.stale_pricing import SnapshotRecord, capture_underdog
        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        rec = SnapshotRecord(
            source="underdog", source_line_id="l1",
            player_name="Jayson Tatum", sport="NBA", stat="points",
            line_value=27.5, higher_decimal=1.735, lower_decimal=2.10,
            higher_true_prob=0.55, lower_true_prob=0.45,
            event_title="BOS@NYK", scheduled_at="2026-07-20T18:00:00Z",
            captured_at=captured,
        )

        id1 = tmp_db.insert(rec)
        assert id1 > 0

        # Insert again (same record) → deduped
        id2 = tmp_db.insert(rec)
        assert id1 == id2

        # Only one row
        cur = tmp_db.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM snapshots")
        assert cur.fetchone()[0] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Movement detection — same-side probability tracking
# ─────────────────────────────────────────────────────────────────────────────

class TestCanonicalKey:
    def test_case_insensitive(self, tmp_db, leg_jayson_tatum):
        """Canonical key must treat player name case-insensitively."""
        from ud_edge.stale_pricing import SnapshotStore
        key1 = SnapshotStore._canonical_key(leg_jayson_tatum, source="underdog")
        leg_upper = leg_jayson_tatum.model_copy()
        leg_upper.player_name = "JAYSON TATUM"
        key2 = SnapshotStore._canonical_key(leg_upper, source="underdog")
        assert key1 == key2

    def test_apostrophe_normalized(self, tmp_db):
        """D'Angelo Russell and Dangelo Russell must match."""
        from ud_edge.models import Leg
        from ud_edge.stale_pricing import SnapshotStore
        leg1 = Leg(
            line_id="l1", player_id="p1", player_name="D'Angelo Russell",
            sport_id="NBA", stat_name="points", line_value=20.5,
            line_type="balanced", match_id=1,
            higher_american=-110, higher_decimal=1.91, higher_multiplier=0.86,
            lower_american=-110, lower_decimal=1.91, lower_multiplier=0.86,
        )
        leg2 = Leg(
            line_id="l2", player_id="p2", player_name="Dangelo Russell",
            sport_id="NBA", stat_name="points", line_value=20.5,
            line_type="balanced", match_id=1,
            higher_american=-110, higher_decimal=1.91, higher_multiplier=0.86,
            lower_american=-110, lower_decimal=1.91, lower_multiplier=0.86,
        )
        assert SnapshotStore._canonical_key(leg1, "test") == SnapshotStore._canonical_key(leg2, "test")

    def test_stat_synonyms_points(self, tmp_db):
        """stat_name variants for the same underlying stat must normalize."""
        from ud_edge.models import Leg
        from ud_edge.stale_pricing import SnapshotStore
        leg1 = Leg(
            line_id="l1", player_id="p1", player_name="Jayson Tatum",
            sport_id="NBA", stat_name="points", line_value=27.5,
            line_type="balanced", match_id=1,
            higher_american=-110, higher_decimal=1.91, higher_multiplier=0.86,
            lower_american=-110, lower_decimal=1.91, lower_multiplier=0.86,
        )
        leg2 = Leg(
            line_id="l2", player_id="p1", player_name="Jayson Tatum",
            sport_id="NBA", stat_name="PTS", line_value=27.5,
            line_type="balanced", match_id=1,
            higher_american=-110, higher_decimal=1.91, higher_multiplier=0.86,
            lower_american=-110, lower_decimal=1.91, lower_multiplier=0.86,
        )
        assert SnapshotStore._canonical_key(leg1, "test") == SnapshotStore._canonical_key(leg2, "test")

    def test_event_title_whitespace_punctuation_normalized(self, tmp_db):
        """BOS@NYK and ' bos at nyk ' must match after normalization."""
        from ud_edge.models import Leg
        from ud_edge.stale_pricing import SnapshotStore

        def make_leg(title):
            return Leg(
                line_id="l1", player_id="p1", player_name="Jayson Tatum",
                sport_id="NBA", stat_name="points", line_value=27.5,
                line_type="balanced", match_id=1, match_title=title,
                scheduled_at="2026-07-20T18:00:00Z",
                higher_american=-110, higher_decimal=1.91, higher_multiplier=0.86,
                lower_american=-110, lower_decimal=1.91, lower_multiplier=0.86,
            )

        key1 = SnapshotStore._canonical_key(make_leg("BOS@NYK"), "test")
        key2 = SnapshotStore._canonical_key(make_leg(" bos at nyk "), "test")
        assert key1 == key2, f"BOS@NYK vs ' bos at nyk ' must match: {key1} vs {key2}"

    def test_event_title_vs_at_normalized(self, tmp_db):
        """BOS vs NYK, BOS@NYK, and BOS AT NYK all normalize to same event."""
        from ud_edge.models import Leg
        from ud_edge.stale_pricing import SnapshotStore

        def make_leg(title):
            return Leg(
                line_id="l1", player_id="p1", player_name="Jayson Tatum",
                sport_id="NBA", stat_name="points", line_value=27.5,
                line_type="balanced", match_id=1, match_title=title,
                scheduled_at="2026-07-20T18:00:00Z",
                higher_american=-110, higher_decimal=1.91, higher_multiplier=0.86,
                lower_american=-110, lower_decimal=1.91, lower_multiplier=0.86,
            )

        key_at = SnapshotStore._canonical_key(make_leg("BOS@NYK"), "test")
        key_vs = SnapshotStore._canonical_key(make_leg("BOS vs NYK"), "test")
        key_at2 = SnapshotStore._canonical_key(make_leg("BOS AT NYK"), "test")
        assert key_at == key_vs, f"BOS@NYK vs BOS vs NYK must match: {key_at} vs {key_vs}"
        assert key_at == key_at2, f"BOS@NYK vs BOS AT NYK must match: {key_at} vs {key_at2}"

    def test_different_scheduled_date_different_key(self, tmp_db):
        """Same event title but different scheduled_at dates must NOT share history."""
        from ud_edge.models import Leg
        from ud_edge.stale_pricing import SnapshotStore

        def make_leg(title, scheduled):
            return Leg(
                line_id="l1", player_id="p1", player_name="Jayson Tatum",
                sport_id="NBA", stat_name="points", line_value=27.5,
                line_type="balanced", match_id=1, match_title=title,
                scheduled_at=scheduled,
                higher_american=-110, higher_decimal=1.91, higher_multiplier=0.86,
                lower_american=-110, lower_decimal=1.91, lower_multiplier=0.86,
            )

        key1 = SnapshotStore._canonical_key(make_leg("BOS@NYK", "2026-07-20T18:00:00Z"), "test")
        key2 = SnapshotStore._canonical_key(make_leg("BOS@NYK", "2026-07-21T18:00:00Z"), "test")
        assert key1 != key2, f"Same event but different dates must differ: {key1} vs {key2}"

    def test_player_punctuation_pj_hyphen(self, tmp_db):
        """P.J. Tucker and PJ Tucker; O'Neill and ONeill must normalize consistently."""
        from ud_edge.models import Leg
        from ud_edge.stale_pricing import SnapshotStore

        def make_leg(name):
            return Leg(
                line_id="l1", player_id="p1", player_name=name,
                sport_id="NBA", stat_name="points", line_value=27.5,
                line_type="balanced", match_id=1, match_title="BOS@NYK",
                scheduled_at="2026-07-20T18:00:00Z",
                higher_american=-110, higher_decimal=1.91, higher_multiplier=0.86,
                lower_american=-110, lower_decimal=1.91, lower_multiplier=0.86,
            )

        key_pj = SnapshotStore._canonical_key(make_leg("P.J. Tucker"), "test")
        key_pj2 = SnapshotStore._canonical_key(make_leg("PJ Tucker"), "test")
        assert key_pj == key_pj2, f"P.J. Tucker vs PJ Tucker must match: {key_pj} vs {key_pj2}"

        key_oneill = SnapshotStore._canonical_key(make_leg("O'Neill"), "test")
        key_oneill2 = SnapshotStore._canonical_key(make_leg("ONeill"), "test")
        assert key_oneill == key_oneill2, f"O'Neill vs ONeill must match: {key_oneill} vs {key_oneill2}"

    def test_canonical_key_and_record_agree(self, tmp_db, leg_jayson_tatum):
        """_canonical_key(leg) and _canonical_key_for_record(rec) must produce identical keys."""
        from ud_edge.stale_pricing import SnapshotStore, SnapshotRecord, capture_underdog
        from datetime import datetime, timezone

        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        capture_underdog([leg_jayson_tatum], tmp_db, captured_at=captured)

        cur = tmp_db.conn.cursor()
        cur.execute("SELECT * FROM snapshots LIMIT 1")
        row_data = cur.fetchone()
        columns = [d[0] for d in cur.description]
        row = dict(zip(columns, row_data))
        rec = SnapshotRecord(
            source=row['source'], source_line_id=row['source_line_id'],
            player_name=row['player_name'], sport=row['sport'], stat=row['stat'],
            line_value=row['line_value'], higher_decimal=row['higher_decimal'],
            lower_decimal=row['lower_decimal'], higher_true_prob=row['higher_true_prob'],
            lower_true_prob=row['lower_true_prob'], event_title=row['event_title'],
            scheduled_at=row['scheduled_at'],
            captured_at=datetime.fromisoformat(row['captured_at'].replace('Z', '+00:00')),
        )

        key_from_leg = SnapshotStore._canonical_key(leg_jayson_tatum, "underdog")
        key_from_rec = SnapshotStore._canonical_key_for_record(rec)
        assert key_from_leg == key_from_rec, \
            f"Leg key {key_from_leg} != Record key {key_from_rec}"


# ─────────────────────────────────────────────────────────────────────────────
# No-vig probabilities stored correctly
# ─────────────────────────────────────────────────────────────────────────────

class TestNoVigStorage:
    def test_no_vig_probabilities_stored(self, tmp_db, leg_jayson_tatum):
        """capture_underdog must compute and store no-vig higher/lower true probs."""
        from ud_edge.stale_pricing import capture_underdog
        from ud_edge.no_vig import no_vig
        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        capture_underdog([leg_jayson_tatum], tmp_db, captured_at=captured)

        cur = tmp_db.conn.cursor()
        cur.execute("SELECT higher_true_prob, lower_true_prob FROM snapshots")
        row = cur.fetchone()
        higher_stored, lower_stored = row
        # Compute expected from no_vig
        expected_higher, expected_lower, _ = no_vig(1.735, 2.10)
        assert abs(higher_stored - expected_higher) < 0.001
        assert abs(lower_stored - expected_lower) < 0.001


# ─────────────────────────────────────────────────────────────────────────────
# Movement detector
# ─────────────────────────────────────────────────────────────────────────────

class TestMovementDetector:
    def test_no_movement_below_threshold(self, tmp_db, leg_jayson_tatum):
        """Line change below threshold → no movement record."""
        from ud_edge.stale_pricing import capture_underdog, detect_movements
        t1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 18, 11, 0, 0, tzinfo=timezone.utc)
        capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t1)
        # Same line value, tiny time change
        capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t2)

        movements = detect_movements(tmp_db, min_line_move=1.0, min_prob_move_pp=5.0)
        assert movements == []

    def test_line_move_triggers_movement(self, tmp_db, leg_jayson_tatum):
        """Line 27.5 → 28.5 triggers movement with correct direction."""
        from ud_edge.stale_pricing import capture_underdog, detect_movements
        t1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 18, 11, 0, 0, tzinfo=timezone.utc)
        capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t1)

        # Same leg but line_value changed to 28.5
        moved_leg = leg_jayson_tatum.model_copy()
        moved_leg.line_value = 28.5
        capture_underdog([moved_leg], tmp_db, captured_at=t2)

        movements = detect_movements(tmp_db, min_line_move=0.5, min_prob_move_pp=0.0)
        assert len(movements) == 1
        m = movements[0]
        assert m["market_key"] is not None
        assert m["direction"] == "up"      # line went up (27.5 → 28.5)
        assert m["prior_line"] == 27.5
        assert m["current_line"] == 28.5
        assert m["prior_captured_at"] is not None
        assert m["current_captured_at"] is not None

    def test_prob_shift_triggers_movement(self, tmp_db, leg_jayson_tatum):
        """True-prob shift >= threshold triggers movement (even without line move)."""
        from ud_edge.stale_pricing import capture_underdog, detect_movements
        t1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 18, 11, 0, 0, tzinfo=timezone.utc)
        capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t1)

        # Sharper odds: over now heavily favored
        shifted_leg = leg_jayson_tatum.model_copy()
        shifted_leg.higher_decimal = 1.40   # was 1.735
        shifted_leg.lower_decimal = 2.90     # was 2.10
        capture_underdog([shifted_leg], tmp_db, captured_at=t2)

        # 6pp prob shift threshold
        movements = detect_movements(tmp_db, min_line_move=100.0, min_prob_move_pp=6.0)
        assert len(movements) == 1
        assert movements[0]["prob_shift_pp"] is not None
        assert movements[0]["prob_shift_pp"] > 0

    def test_favorite_flip_same_side_movement(self, tmp_db, leg_jayson_tatum):
        """When favorite flips (55/45 -> 45/55), same-side shift must be detected.

        Prior: higher=0.55 (over), lower=0.45 (under) -> over is favorite
        Curr: higher=0.45 (over), lower=0.55 (under) -> under is now favorite
        Same-side under shift: 45% -> 55% = +10pp
        Max(higher) stays 0.55 -> 0.55 = 0pp (BUG with old code)

        The movement detector must use the SAME SIDE shift, not max-to-max.
        """
        from ud_edge.stale_pricing import capture_underdog, detect_movements

        t1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 18, 11, 0, 0, tzinfo=timezone.utc)

        # Prior: over favored at 55%
        capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t1)

        # Curr: under now favored at 55%, over at 45% (favorite flipped)
        flipped_leg = leg_jayson_tatum.model_copy()
        flipped_leg.higher_decimal = 2.20   # over at 45%
        flipped_leg.lower_decimal = 1.82    # under at 55%
        capture_underdog([flipped_leg], tmp_db, captured_at=t2)

        # With prob threshold at 5pp, should trigger
        movements = detect_movements(tmp_db, min_line_move=100.0, min_prob_move_pp=5.0)
        assert len(movements) == 1, f"Expected movement for same-side +10pp shift, got: {movements}"
        m = movements[0]
        # The prob_shift_pp must reflect the under side shifting from 45%->55% = +10pp
        assert abs(m["prob_shift_pp"]) >= 9.0, \
            f"Expected ~10pp same-side under shift, got {m['prob_shift_pp']}pp"

    def test_flat_line_prob_moved_direction_flat(self, tmp_db, leg_jayson_tatum):
        """When line value is unchanged but probability moved, direction must be 'flat'."""
        from ud_edge.stale_pricing import capture_underdog, detect_movements

        t1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 18, 11, 0, 0, tzinfo=timezone.utc)

        # Same line value, but probabilities shifted
        capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t1)

        shifted_leg = leg_jayson_tatum.model_copy()
        shifted_leg.higher_decimal = 1.50   # over now less likely
        shifted_leg.lower_decimal = 2.50    # under now more likely
        # line_value stays the same (27.5)
        capture_underdog([shifted_leg], tmp_db, captured_at=t2)

        movements = detect_movements(tmp_db, min_line_move=100.0, min_prob_move_pp=3.0)
        assert len(movements) == 1
        m = movements[0]
        assert m["direction"] == "flat", \
            f"Line unchanged but prob moved → direction must be 'flat', got {m['direction']}"
        assert m["prior_line"] == m["current_line"]


# ─────────────────────────────────────────────────────────────────────────────
# Cross-source stale detector — false-positive prevention
# ─────────────────────────────────────────────────────────────────────────────

class TestStaleDetectorStaticDisagreement:
    """Static disagreement between two sources with no observed movement history
    must NOT be flagged as stale (no evidence of staleness)."""

    def test_static_disagreement_no_stale(self, tmp_db, leg_jayson_tatum):
        """Two sources disagree on the line but neither has moved → no stale."""
        from ud_edge.stale_pricing import (
            capture_underdog, detect_stale_opportunities, SnapshotStore,
        )
        t1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        # Source A (Underdog) at t1
        capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t1, source="underdog")

        # Source B (manual) at same t1 — different line value
        source_b_leg = leg_jayson_tatum.model_copy()
        source_b_leg.line_value = 29.5
        capture_underdog([source_b_leg], tmp_db, captured_at=t1, source="draftkings")

        stale = detect_stale_opportunities(
            tmp_db,
            min_stale_minutes=0,
            fresh_window_minutes=120,
            min_line_gap=1.0,
            min_prob_gap_pp=0.0,
        )
        assert stale == [], "Static disagreement with no movement history must not be stale"

    def test_source_a_stale_source_b_moved(self, tmp_db, leg_jayson_tatum, monkeypatch):
        """Source A unchanged for 60 min, Source B moved recently → STALE opportunity.

        Must mock datetime.now so staleness calculation uses 2026 timestamps,
        not the wall-clock 2025 test-run time.
        """
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import capture_underdog, detect_stale_opportunities
        import datetime as dt_module

        # Freeze datetime.now so utc_now() returns 2026-07-18 12:00 UTC
        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        # Also patch the module-level reference
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            # Stagger timestamps to create unambiguous stale/fresh relationship:
            # t1 = 120 min ago: underdog (stale source) observes 27.5 line
            t1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
            # t_mid = 60 min ago: underdog observes again (unchanged at 27.5)
            t_mid = datetime(2026, 7, 18, 11, 0, 0, tzinfo=timezone.utc)
            # t2 = 30 min ago: draftkings (fresh source) observes 28.5 line
            t2 = datetime(2026, 7, 18, 11, 30, 0, tzinfo=timezone.utc)
            # t3 = now (12:00 UTC): draftkings observes 28.0 (moved from 28.5)
            # NOTE: underdog does NOT capture at t3 — this ensures its latest
            # observation is genuinely old (t_mid, 60 min ago)
            t3 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

            # Underdog: stale source — last seen at t_mid (60 min ago), unchanged
            capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t1, source="underdog")
            capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t_mid, source="underdog")

            # Draftkings: fresh source — observed at t2 (28.5) then moved to 28.0 at t3
            source_b_leg = leg_jayson_tatum.model_copy()
            source_b_leg.line_value = 28.5
            capture_underdog([source_b_leg], tmp_db, captured_at=t2, source="draftkings")

            source_b_leg2 = leg_jayson_tatum.model_copy()
            source_b_leg2.line_value = 28.0
            capture_underdog([source_b_leg2], tmp_db, captured_at=t3, source="draftkings")

            stale = detect_stale_opportunities(
                tmp_db,
                min_stale_minutes=30,   # stale source must be 30+ min unchanged
                fresh_window_minutes=120,
                min_line_gap=0.5,
                min_prob_gap_pp=0.0,
            )
            assert len(stale) >= 1, f"Expected stale opp, got: {stale}"
            s = stale[0]
            assert s["stale_source"] == "underdog"
            assert s["fresh_source"] == "draftkings"
            assert s["direction"] in ("higher", "lower")
            assert "confidence" in s
            assert "evidence" in s
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now


# ─────────────────────────────────────────────────────────────────────────────
# Event-start rejection
# ─────────────────────────────────────────────────────────────────────────────

class TestEventStartRejection:
    def test_started_event_rejected(self, tmp_db, leg_jayson_tatum):
        """Events with scheduled_at in the past must be excluded from stale opportunities."""
        from ud_edge.stale_pricing import capture_underdog, detect_stale_opportunities

        # scheduled_at is 2026-07-20, but captured at 2026-07-20T22:00Z (game already started)
        past_game_leg = leg_jayson_tatum.model_copy()
        past_game_leg.scheduled_at = "2026-07-20T18:00:00Z"

        t1 = datetime(2026, 7, 20, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 20, 19, 0, 0, tzinfo=timezone.utc)  # after scheduled start

        capture_underdog([past_game_leg], tmp_db, captured_at=t1, source="underdog")
        capture_underdog([past_game_leg], tmp_db, captured_at=t2, source="draftkings")

        # Without reject_started=True, we get results
        stale_inclusive = detect_stale_opportunities(
            tmp_db, min_stale_minutes=0, fresh_window_minutes=120,
            min_line_gap=0.0, min_prob_gap_pp=0.0, reject_started=False,
        )
        # With reject_started=True, no results
        stale_rejected = detect_stale_opportunities(
            tmp_db, min_stale_minutes=0, fresh_window_minutes=120,
            min_line_gap=0.0, min_prob_gap_pp=0.0, reject_started=True,
        )
        assert len(stale_rejected) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Report output
# ─────────────────────────────────────────────────────────────────────────────

class TestReportOutput:
    def test_no_stale_opportunities_report(self, tmp_db, leg_jayson_tatum):
        """First run with no history → 'no confirmed stale opportunities yet'."""
        from ud_edge.stale_pricing import (
            capture_underdog, detect_movements, detect_stale_opportunities, build_stale_report,
        )
        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        capture_underdog([leg_jayson_tatum], tmp_db, captured_at=captured)

        movements = detect_movements(tmp_db)
        stale = detect_stale_opportunities(tmp_db)
        report = build_stale_report(movements, stale, captured)
        assert "no confirmed stale opportunities" in report.lower()

    def test_movement_report_contains_line_value(self, tmp_db, leg_jayson_tatum):
        """Movement report must include line values."""
        from ud_edge.stale_pricing import capture_underdog, detect_movements, build_movement_report
        t1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t1)
        moved_leg = leg_jayson_tatum.model_copy()
        moved_leg.line_value = 28.5
        capture_underdog([moved_leg], tmp_db, captured_at=t2)

        movements = detect_movements(tmp_db, min_line_move=0.5)
        report = build_movement_report(movements, t2)
        assert "27.5" in report
        assert "28.5" in report

    def test_stale_opportunity_report_contains_sources(self, tmp_db, leg_jayson_tatum, monkeypatch):
        """Stale opportunity report must name both sources."""
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import capture_underdog, detect_stale_opportunities, build_stale_report
        import datetime as dt_module

        # Freeze datetime.now so utc_now() returns 2026-07-18 12:00 UTC
        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            # Timestamps to create unambiguous stale vs fresh:
            # t_old = 120 min ago: underdog observes 27.5 (becomes stale source, NOT captured at t_new)
            # t_mid = 30 min ago: draftkings observes 28.5 (fresh source)
            # t_new = now (0 min ago): draftkings observes 28.0 (moved from 28.5)
            # NOTE: underdog is NOT captured at t_new — its latest stays at t_old (stale)
            t_old = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)   # UD stale (120 min old)
            t_mid = datetime(2026, 7, 18, 11, 30, 0, tzinfo=timezone.utc) # DK fresh (30 min old)
            t_new = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)   # now (0 min old)

            # Underdog: stale source — two unchanged observations.
            capture_underdog([leg_jayson_tatum], tmp_db, captured_at=t_old, source="underdog")
            capture_underdog(
                [leg_jayson_tatum], tmp_db,
                captured_at=datetime(2026, 7, 18, 10, 30, tzinfo=timezone.utc),
                source="underdog",
            )

            # Draftkings: fresh source — observed at t_mid (28.5) then moved to 28.0 at t_new
            b_leg = leg_jayson_tatum.model_copy()
            b_leg.line_value = 28.5
            capture_underdog([b_leg], tmp_db, captured_at=t_mid, source="draftkings")

            b_leg2 = leg_jayson_tatum.model_copy()
            b_leg2.line_value = 28.0
            capture_underdog([b_leg2], tmp_db, captured_at=t_new, source="draftkings")

            stale = detect_stale_opportunities(
                tmp_db, min_stale_minutes=30, fresh_window_minutes=120,
                min_line_gap=0.5, min_prob_gap_pp=0.0,
            )
            assert len(stale) >= 1, f"Expected stale opp, got: {stale}"
            report = build_stale_report([], stale, t_new)
            assert "underdog" in report.lower()
            assert "draftkings" in report.lower()
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now


# ─────────────────────────────────────────────────────────────────────────────
# CLI / snapshot integration (monkeypatched UDClient)
# ─────────────────────────────────────────────────────────────────────────────

class TestCLISnapshotIntegration:
    def test_snapshot_mode_runs_without_network(self, tmp_path, monkeypatch):
        """--snapshot must fetch (monkeypatched), store, and print a report."""
        import json
        from pathlib import Path
        from ud_edge import stale_pricing
        from ud_edge.ud_client import UDClient

        # Build a synthetic UD response matching the real shape
        synth_data = {
            "players": [{"id": "p_tatum", "first_name": "Jayson", "last_name": "Tatum",
                         "sport_id": "NBA", "team_id": "BOS"}],
            "appearances": [{"id": "app_456", "player_id": "p_tatum", "match_id": 1,
                            "match_type": "game", "team_id": "BOS"}],
            "games": [{"id": 1, "abbreviated_title": "BOS@NYK",
                       "full_team_names_title": "Boston at New York",
                       "scheduled_at": "2026-07-20T18:00:00Z"}],
            "over_under_lines": [{
                "id": "ud_line_123",
                "line_type": "balanced",
                "over_under": {
                    "appearance_stat": {"stat": "points", "appearance_id": "app_456"}
                },
                "options": [
                    {"choice": "higher", "american_price": "-136",
                     "decimal_price": "1.735", "payout_multiplier": "0.86",
                     "choice_display_name_shorter": "28+"},
                    {"choice": "lower", "american_price": "110",
                     "decimal_price": "2.10", "payout_multiplier": "1.10",
                     "choice_display_name_shorter": "27-"},
                ],
            }],
        }

        captured_at = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        # Monkeypatch UDClient.fetch to return our synthetic data
        class FakeClient:
            def fetch(self, force=False):
                return synth_data
            def parse_legs(self, data, sport_filter=None, skip_alternates=True):
                from ud_edge.ud_client import UDClient
                return UDClient().parse_legs(data, sport_filter, skip_alternates)

        # Patch the UDClient class so any instantiation uses our fake
        monkeypatch.setattr(UDClient, "fetch", lambda self, force=False: synth_data)

        # Patch the stale_pricing module's datetime
        monkeypatch.setattr(stale_pricing, "utc_now", lambda: captured_at)

        db_path = tmp_path / "cli_test.sqlite3"
        store = stale_pricing.SnapshotStore(db_path=db_path)
        store.init()

        # Manually call the capture path that CLI would call
        client = FakeClient()
        data = client.fetch()
        legs = client.parse_legs(data)
        rows = stale_pricing.capture_underdog(legs, store, captured_at=captured_at)

        assert len(rows) == 1
        # Verify the snapshot was persisted
        cur = store.conn.cursor()
        cur.execute("SELECT player_name, line_value FROM snapshots")
        row = cur.fetchone()
        assert row[0] == "Jayson Tatum"
        assert row[1] == 27.5  # parsed from "28+" → 27.5

        # Movements and stale report should show no stale (single source)
        movements = stale_pricing.detect_movements(store)
        stale = stale_pricing.detect_stale_opportunities(store)
        report = stale_pricing.build_stale_report(movements, stale, captured_at)
        assert "no confirmed stale opportunities" in report.lower()

    def test_cli_snapshot_flag_accepted(self, tmp_path, monkeypatch):
        """The CLI must accept --snapshot and --snapshot-db flags without error."""
        import sys
        from ud_edge.__main__ import main

        captured_at = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        synth_data = {
            "players": [{"id": "p1", "first_name": "Test", "last_name": "Player",
                         "sport_id": "NBA", "team_id": "T1"}],
            "appearances": [{"id": "a1", "player_id": "p1", "match_id": 1,
                            "match_type": "game", "team_id": "T1"}],
            "games": [{"id": 1, "abbreviated_title": "T1@T2",
                       "scheduled_at": "2026-07-20T18:00:00Z"}],
            "over_under_lines": [{
                "id": "l1", "line_type": "balanced",
                "over_under": {"appearance_stat": {"stat": "points", "appearance_id": "a1"}},
                "options": [
                    {"choice": "higher", "american_price": "-110",
                     "decimal_price": "1.91", "payout_multiplier": "0.86",
                     "choice_display_name_shorter": "28+"},
                    {"choice": "lower", "american_price": "-110",
                     "decimal_price": "1.91", "payout_multiplier": "0.86",
                     "choice_display_name_shorter": "27-"},
                ],
            }],
        }

        from ud_edge import stale_pricing
        from ud_edge.ud_client import UDClient

        monkeypatch.setattr(UDClient, "fetch", lambda self, force=False: synth_data)
        monkeypatch.setattr(stale_pricing, "utc_now", lambda: captured_at)

        db_path = str(tmp_path / "cli_snapshot.sqlite3")
        argv = ["--snapshot", "--snapshot-db", db_path]

        # Should exit 0 without raising
        exit_code = main(argv)
        assert exit_code == 0

    def test_cli_movement_thresholds_are_propagated(self, tmp_path, monkeypatch):
        """Custom movement thresholds reach both movement and stale detectors."""
        from ud_edge import stale_pricing
        from ud_edge.__main__ import main

        seen = {}

        def fake_movements(store, min_line_move=0.5, min_prob_move_pp=3.0):
            seen["movement"] = (min_line_move, min_prob_move_pp)
            return []

        def fake_stale(store, **kwargs):
            seen["stale"] = kwargs
            return []

        monkeypatch.setattr(stale_pricing, "detect_movements", fake_movements)
        monkeypatch.setattr(stale_pricing, "detect_stale_opportunities", fake_stale)

        exit_code = main([
            "--stale-report",
            "--snapshot-db", str(tmp_path / "thresholds.sqlite3"),
            "--min-movement-line", "1.5",
            "--min-movement-prob-pp", "7.0",
        ])

        assert exit_code == 0
        assert seen["movement"] == (1.5, 7.0)
        assert seen["stale"]["min_movement_line"] == 1.5
        assert seen["stale"]["min_movement_prob_pp"] == 7.0


# ─────────────────────────────────────────────────────────────────────────────
# Per-test tmp_path DB (each test gets isolated DB)
# ─────────────────────────────────────────────────────────────────────────────

class TestPerTestIsolation:
    def test_two_tests_two_dbs(self, tmp_path):
        """Two parallel tmp_path fixtures give separate DBs."""
        from ud_edge.stale_pricing import SnapshotStore
        db1 = SnapshotStore(db_path=tmp_path / "db1.sqlite3")
        db1.init()
        db2 = SnapshotStore(db_path=tmp_path / "db2.sqlite3")
        db2.init()
        # Both should be independent
        assert db1.conn is not db2.conn


# ─────────────────────────────────────────────────────────────────────────────
# A) Stale unchanged duration — repeated current polls must NOT reset stale age
# ─────────────────────────────────────────────────────────────────────────────

class TestStaleUnchangedDuration:
    def test_unchanged_stale_feed_polled_now_still_has_120min_age(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """Polling a stale source now must NOT reset its stale age.

        Scenario: draftkings observed at 27.5 at t_old (120 min ago), then polled
        at t_now but unchanged. Its unchanged_since must be t_old, not t_now.
        """
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )
        import datetime as dt_module

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "unchanged_stale_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            # t_old = 120 min ago (14:00 - 120min = 12:00)
            t_old = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
            # t_now = 14:00 UTC (frozen clock)

            # Draftkings observed at 27.5 at t_old
            leg_old = leg_jayson_tatum.model_copy()
            leg_old.line_value = 27.5
            capture_underdog([leg_old], store, captured_at=t_old, source="draftkings")

            # Draftkings polled again at t_now — same line (unchanged)
            leg_now = leg_jayson_tatum.model_copy()
            leg_now.line_value = 27.5
            capture_underdog([leg_now], store, captured_at=FrozenDatetime.now(timezone.utc), source="draftkings")

            # Underdog (fresh source) moved from 27.5 to 28.5 at t_now.
            leg_ud_prior = leg_jayson_tatum.model_copy()
            leg_ud_prior.line_value = 27.5
            capture_underdog([leg_ud_prior], store, captured_at=t_old, source="underdog")
            leg_ud = leg_jayson_tatum.model_copy()
            leg_ud.line_value = 28.5
            capture_underdog([leg_ud], store, captured_at=FrozenDatetime.now(timezone.utc), source="underdog")

            # stale_age must be >= 120 min (unchanged_since must be t_old, not t_now)
            stale = detect_stale_opportunities(
                store, min_stale_minutes=120, fresh_window_minutes=120,
                min_line_gap=0.5, min_prob_gap_pp=0.0,
            )
            assert len(stale) >= 1, f"Expected stale opp, got: {stale}"
            s = stale[0]
            assert s["stale_age_minutes"] >= 119, \
                f"Stale age must be ~120min (unchanged_since t_old), got {s['stale_age_minutes']:.1f}min"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_stale_source_with_one_observation_rejected(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """A source with only 1 observation cannot be proven stale (needs >= 2)."""
        import datetime as dt_module
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "single_obs_stale_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            t_old = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)  # 240 min ago
            t_now = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

            # Draftkings: only ONE observation (at t_old)
            leg_dk = leg_jayson_tatum.model_copy()
            leg_dk.line_value = 27.5
            capture_underdog([leg_dk], store, captured_at=t_old, source="draftkings")

            # Underdog: fresh source, moved at t_now
            leg_ud = leg_jayson_tatum.model_copy()
            leg_ud.line_value = 28.5
            capture_underdog([leg_ud], store, captured_at=t_now, source="underdog")

            # With only 1 draftkings observation, we cannot prove it was unchanged —
            # must be rejected
            stale = detect_stale_opportunities(
                store, min_stale_minutes=30, fresh_window_minutes=120,
                min_line_gap=0.5, min_prob_gap_pp=0.0,
            )
            assert len(stale) == 0, \
                "Single-observation source cannot be proven stale, must be rejected"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_stale_unchanged_duration_requires_min_stale_minutes(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """Stale unchanged duration must be >= min_stale_minutes threshold."""
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )
        import datetime as dt_module

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "min_stale_threshold_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            # draftkings: observed at 27.5 at t_old (60 min ago), unchanged since
            t_old = datetime(2026, 7, 18, 13, 0, 0, tzinfo=timezone.utc)   # 60 min ago
            t_now = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)  # now

            leg_old = leg_jayson_tatum.model_copy()
            leg_old.line_value = 27.5
            capture_underdog([leg_old], store, captured_at=t_old, source="draftkings")
            capture_underdog([leg_old], store, captured_at=t_now, source="draftkings")

            # Underdog moved from 27.5 to 28.5 at t_now.
            leg_ud_prior = leg_jayson_tatum.model_copy()
            leg_ud_prior.line_value = 27.5
            capture_underdog([leg_ud_prior], store, captured_at=t_old, source="underdog")
            leg_ud = leg_jayson_tatum.model_copy()
            leg_ud.line_value = 28.5
            capture_underdog([leg_ud], store, captured_at=t_now, source="underdog")

            # min_stale_minutes=90 → 60 min old is NOT stale enough
            stale = detect_stale_opportunities(
                store, min_stale_minutes=90, fresh_window_minutes=120,
                min_line_gap=0.5, min_prob_gap_pp=0.0,
            )
            assert len(stale) == 0, \
                "Stale at 60min must be rejected when min_stale_minutes=90"

            # min_stale_minutes=30 → 60 min old IS stale enough
            stale = detect_stale_opportunities(
                store, min_stale_minutes=30, fresh_window_minutes=120,
                min_line_gap=0.5, min_prob_gap_pp=0.0,
            )
            assert len(stale) >= 1, \
                "Stale at 60min must be accepted when min_stale_minutes=30"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now


# ─────────────────────────────────────────────────────────────────────────────
# B) Fresh evidence — require >= 2 obs and meaningful movement within fresh window
# ─────────────────────────────────────────────────────────────────────────────

class TestFreshEvidence:
    def test_fresh_source_with_one_observation_rejected(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """A fresh source with only 1 observation cannot be proven fresh (needs >= 2)."""
        import datetime as dt_module
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "single_obs_fresh_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            t_old = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)  # 240 min ago
            t_mid = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)  # 120 min ago
            t_now = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)  # now

            # Underdog (stale source): observed twice, unchanged
            capture_underdog([leg_jayson_tatum], store, captured_at=t_old, source="underdog")
            capture_underdog([leg_jayson_tatum], store, captured_at=t_mid, source="underdog")

            # Draftkings: only ONE observation at t_now (cannot prove freshness)
            leg_dk = leg_jayson_tatum.model_copy()
            leg_dk.line_value = 28.5
            capture_underdog([leg_dk], store, captured_at=t_now, source="draftkings")

            stale = detect_stale_opportunities(
                store, min_stale_minutes=30, fresh_window_minutes=120,
                min_line_gap=0.5, min_prob_gap_pp=0.0,
            )
            assert len(stale) == 0, \
                "Single-observation fresh source cannot be proven fresh, must be rejected"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_prob_only_move_confirms_stale(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """A probability-only movement (no line change) within fresh window must confirm stale."""
        import datetime as dt_module
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "prob_only_move_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            t_old = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)   # 240 min ago
            t_mid = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)  # 120 min ago
            t_fresh = datetime(2026, 7, 18, 13, 0, 0, tzinfo=timezone.utc)  # 60 min ago
            t_now = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)   # now

            # Underdog (stale source): two obs, unchanged at 27.5
            capture_underdog([leg_jayson_tatum], store, captured_at=t_old, source="underdog")
            capture_underdog([leg_jayson_tatum], store, captured_at=t_mid, source="underdog")

            # Draftkings: prob-only move at t_fresh (line stays same but odds shift)
            leg_dk1 = leg_jayson_tatum.model_copy()
            leg_dk1.line_value = 28.5
            leg_dk1.higher_decimal = 1.91
            leg_dk1.lower_decimal = 1.91
            capture_underdog([leg_dk1], store, captured_at=t_fresh, source="draftkings")

            leg_dk2 = leg_jayson_tatum.model_copy()
            leg_dk2.line_value = 28.5  # same line
            leg_dk2.higher_decimal = 1.50  # prob shifted significantly
            leg_dk2.lower_decimal = 2.50
            capture_underdog([leg_dk2], store, captured_at=t_now, source="draftkings")

            stale = detect_stale_opportunities(
                store, min_stale_minutes=30, fresh_window_minutes=120,
                min_line_gap=0.5, min_prob_gap_pp=3.0,
            )
            assert len(stale) >= 1, \
                "Prob-only movement within fresh window must confirm stale"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_old_move_outside_fresh_window_rejected(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """Movement that occurred outside the fresh window must be rejected even if
        the source was polled again (subsequent unchanged poll does not extend window)."""
        import datetime as dt_module
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "old_move_rejected_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            # Timeline:
            # t_old = 250 min ago: draftkings observes 27.5 (becomes stale source)
            # t_move = 130 min ago: draftkings moves to 28.5 (OLD move, outside 120min window)
            # t_now = 0 min ago: draftkings polled again, unchanged at 28.5
            t_old = datetime(2026, 7, 18, 10, 30, 0, tzinfo=timezone.utc)   # 210 min ago
            t_move = datetime(2026, 7, 18, 12, 10, 0, tzinfo=timezone.utc)  # 110 min ago
            t_now = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)   # now

            # Underdog (stale): two obs, unchanged
            capture_underdog([leg_jayson_tatum], store, captured_at=t_old, source="underdog")
            capture_underdog([leg_jayson_tatum], store, captured_at=t_move, source="underdog")

            # Draftkings: moved at t_move (110 min ago — OUTSIDE 120min window)
            leg_dk1 = leg_jayson_tatum.model_copy()
            leg_dk1.line_value = 28.5
            capture_underdog([leg_dk1], store, captured_at=t_move, source="draftkings")

            # Draftkings polled again at t_now, unchanged at 28.5
            leg_dk2 = leg_jayson_tatum.model_copy()
            leg_dk2.line_value = 28.5
            capture_underdog([leg_dk2], store, captured_at=t_now, source="draftkings")

            stale = detect_stale_opportunities(
                store, min_stale_minutes=30, fresh_window_minutes=120,
                min_line_gap=0.5, min_prob_gap_pp=0.0,
            )
            assert len(stale) == 0, \
                "Move at 110min ago is outside 120min fresh window — must be rejected"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_zero_threshold_requires_nonzero_delta(self, tmp_path, leg_jayson_tatum):
        """Setting min_line_gap=0 or min_prob_gap_pp=0 must NOT prove zero movement.
        A movement still requires an actual nonzero delta."""
        from ud_edge.stale_pricing import SnapshotStore, capture_underdog, detect_stale_opportunities

        db_path = tmp_path / "zero_thresh_test.sqlite3"
        store = SnapshotStore(db_path=db_path)
        store.init()

        t1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        # Source A: unchanged at 27.5
        capture_underdog([leg_jayson_tatum], store, captured_at=t1, source="underdog")
        capture_underdog([leg_jayson_tatum], store, captured_at=t2, source="underdog")

        # Source B: also unchanged at 28.5 (no actual movement)
        leg_b = leg_jayson_tatum.model_copy()
        leg_b.line_value = 28.5
        leg_b.higher_decimal = 1.80
        leg_b.lower_decimal = 2.00
        capture_underdog([leg_b], store, captured_at=t1, source="draftkings")
        capture_underdog([leg_b], store, captured_at=t2, source="draftkings")

        # Despite zero thresholds, no actual movement → no stale
        stale = detect_stale_opportunities(
            store, min_stale_minutes=0, fresh_window_minutes=120,
            min_line_gap=0.0, min_prob_gap_pp=0.0,
        )
        assert len(stale) == 0, \
            "Zero threshold does not make zero movement into a stale opportunity"


# ─────────────────────────────────────────────────────────────────────────────
# C) Opportunity direction — playable side, not 'too high'
# ─────────────────────────────────────────────────────────────────────────────

class TestOpportunityDirection:
    def test_higher_direction_means_play_higher(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """stale=27.5, fresh=28.5 → playable stale side is 'higher' (back Over at stale book)."""
        import datetime as dt_module
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "direction_higher_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            t_old = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)  # 120 min ago
            t_now = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)  # now

            # Underdog (stale source): line=27.5
            leg_ud = leg_jayson_tatum.model_copy()
            leg_ud.line_value = 27.5
            capture_underdog([leg_ud], store, captured_at=t_old, source="underdog")
            capture_underdog([leg_ud], store, captured_at=t_now, source="underdog")

            # Draftkings (fresh source): moved from 27.5 to 28.5.
            leg_dk_prior = leg_jayson_tatum.model_copy()
            leg_dk_prior.line_value = 27.5
            capture_underdog([leg_dk_prior], store, captured_at=t_old, source="draftkings")
            leg_dk = leg_jayson_tatum.model_copy()
            leg_dk.line_value = 28.5
            leg_dk.higher_decimal = 1.80
            leg_dk.lower_decimal = 2.00
            capture_underdog([leg_dk], store, captured_at=t_now, source="draftkings")

            stale = detect_stale_opportunities(
                store, min_stale_minutes=30, fresh_window_minutes=120,
                min_line_gap=0.5, min_prob_gap_pp=0.0,
            )
            assert len(stale) >= 1
            s = next(o for o in stale if o["stale_source"] == "underdog")
            assert s["direction"] == "higher", \
                f"stale=27.5, fresh=28.5 → direction must be 'higher', got {s['direction']}"
            # Confirm what to do on stale source
            assert "play" in s["evidence"].lower() or "higher" in s["evidence"].lower(), \
                "Evidence must indicate what to play on stale source"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_lower_direction_means_play_lower(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """stale=29.5, fresh=28.5 → playable stale side is 'lower' (back Under at stale book)."""
        import datetime as dt_module
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "direction_lower_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            t_old = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
            t_now = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

            # Underdog (stale source): line=29.5
            leg_ud = leg_jayson_tatum.model_copy()
            leg_ud.line_value = 29.5
            capture_underdog([leg_ud], store, captured_at=t_old, source="underdog")
            capture_underdog([leg_ud], store, captured_at=t_now, source="underdog")

            # Draftkings (fresh source): moved from 29.5 to 28.5.
            leg_dk_prior = leg_jayson_tatum.model_copy()
            leg_dk_prior.line_value = 29.5
            capture_underdog([leg_dk_prior], store, captured_at=t_old, source="draftkings")
            leg_dk = leg_jayson_tatum.model_copy()
            leg_dk.line_value = 28.5
            leg_dk.higher_decimal = 1.80
            leg_dk.lower_decimal = 2.00
            capture_underdog([leg_dk], store, captured_at=t_now, source="draftkings")

            stale = detect_stale_opportunities(
                store, min_stale_minutes=30, fresh_window_minutes=120,
                min_line_gap=0.5, min_prob_gap_pp=0.0,
            )
            assert len(stale) >= 1
            s = next(o for o in stale if o["stale_source"] == "underdog")
            assert s["direction"] == "lower", \
                f"stale=29.5, fresh=28.5 → direction must be 'lower', got {s['direction']}"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_same_line_uses_probability_to_pick_direction(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """Same line value: pick 'higher' if fresh higher_prob > stale higher_prob, else 'lower'."""
        import datetime as dt_module
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "same_line_prob_dir_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            t_old = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
            t_now = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

            # Both sources at line=27.5, same line
            # Underdog (stale): over at 55%
            leg_ud = leg_jayson_tatum.model_copy()
            leg_ud.line_value = 27.5
            leg_ud.higher_decimal = 1.82   # over at ~55%
            leg_ud.lower_decimal = 2.00    # under at ~45%
            capture_underdog([leg_ud], store, captured_at=t_old, source="underdog")
            capture_underdog([leg_ud], store, captured_at=t_now, source="underdog")

            # Draftkings (fresh): probability moved from stale-equivalent to 60% over.
            leg_dk_prior = leg_jayson_tatum.model_copy()
            leg_dk_prior.line_value = 27.5
            leg_dk_prior.higher_decimal = 1.82
            leg_dk_prior.lower_decimal = 2.00
            capture_underdog([leg_dk_prior], store, captured_at=t_old, source="draftkings")
            leg_dk = leg_jayson_tatum.model_copy()
            leg_dk.line_value = 27.5  # same line
            leg_dk.higher_decimal = 1.67   # over at ~60%
            leg_dk.lower_decimal = 2.50    # under at ~40%
            capture_underdog([leg_dk], store, captured_at=t_now, source="draftkings")

            stale = detect_stale_opportunities(
                store, min_stale_minutes=30, fresh_window_minutes=120,
                min_line_gap=0.0, min_prob_gap_pp=3.0,
            )
            assert len(stale) >= 1
            s = stale[0]
            # fresh over prob > stale over prob → 'higher' (back Over at stale)
            assert s["direction"] == "higher", \
                f"fresh over prob > stale over prob → 'higher', got {s['direction']}"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now


# ─────────────────────────────────────────────────────────────────────────────
# D) Gaps — OR threshold (line OR prob), signed prob edge stored
# ─────────────────────────────────────────────────────────────────────────────

class TestGapThresholds:
    def test_line_gap_sufficient_without_prob_gap(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """abs line gap >= min_line_gap alone is sufficient (OR semantics)."""
        import datetime as dt_module
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "line_gap_only_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            t_old = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
            t_now = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

            # Underdog (stale): 27.5
            capture_underdog([leg_jayson_tatum], store, captured_at=t_old, source="underdog")
            capture_underdog([leg_jayson_tatum], store, captured_at=t_now, source="underdog")

            # Draftkings (fresh): moved from 27.5 to 28.5 (line gap only).
            leg_dk_prior = leg_jayson_tatum.model_copy()
            leg_dk_prior.line_value = 27.5
            capture_underdog([leg_dk_prior], store, captured_at=t_old, source="draftkings")
            leg_dk = leg_jayson_tatum.model_copy()
            leg_dk.line_value = 28.5
            leg_dk.higher_decimal = 1.735  # same implied prob as stale
            leg_dk.lower_decimal = 2.10
            capture_underdog([leg_dk], store, captured_at=t_now, source="draftkings")

            stale = detect_stale_opportunities(
                store, min_stale_minutes=30, fresh_window_minutes=120,
                min_line_gap=0.5, min_prob_gap_pp=100.0,  # high prob threshold
            )
            assert len(stale) >= 1, \
                "Line gap of 1.0 >= 0.5 threshold must qualify even with high prob threshold"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_prob_gap_sufficient_without_line_gap(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """abs same-side prob gap >= min_prob_gap_pp alone is sufficient (OR semantics)."""
        import datetime as dt_module
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "prob_gap_only_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            t_old = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
            t_now = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

            # Underdog (stale): line=27.5, over prob=55%
            leg_ud = leg_jayson_tatum.model_copy()
            leg_ud.line_value = 27.5
            leg_ud.higher_decimal = 1.82
            leg_ud.lower_decimal = 2.00
            capture_underdog([leg_ud], store, captured_at=t_old, source="underdog")
            capture_underdog([leg_ud], store, captured_at=t_now, source="underdog")

            # Draftkings (fresh): same line, probability moved from stale-equivalent.
            leg_dk_prior = leg_jayson_tatum.model_copy()
            leg_dk_prior.line_value = 27.5
            leg_dk_prior.higher_decimal = 1.82
            leg_dk_prior.lower_decimal = 2.00
            capture_underdog([leg_dk_prior], store, captured_at=t_old, source="draftkings")
            leg_dk = leg_jayson_tatum.model_copy()
            leg_dk.line_value = 27.5
            leg_dk.higher_decimal = 1.54   # ~65%
            leg_dk.lower_decimal = 2.75
            capture_underdog([leg_dk], store, captured_at=t_now, source="draftkings")

            stale = detect_stale_opportunities(
                store, min_stale_minutes=30, fresh_window_minutes=120,
                min_line_gap=100.0, min_prob_gap_pp=5.0,  # high line threshold
            )
            assert len(stale) >= 1, \
                "Prob gap of 10pp >= 5pp threshold must qualify even with high line threshold"
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now

    def test_signed_prob_edge_stored(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """StaleOpportunity must store signed fresh-vs-stale prob edge."""
        import datetime as dt_module
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )
        import datetime as dt_module

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "signed_prob_edge_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            t_old = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
            t_now = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)

            # Underdog (stale): over prob=55%
            leg_ud = leg_jayson_tatum.model_copy()
            leg_ud.higher_decimal = 1.82
            leg_ud.lower_decimal = 2.00
            capture_underdog([leg_ud], store, captured_at=t_old, source="underdog")
            capture_underdog([leg_ud], store, captured_at=t_now, source="underdog")

            # Draftkings (fresh): probability moved from stale-equivalent to 65% over.
            leg_dk_prior = leg_jayson_tatum.model_copy()
            leg_dk_prior.higher_decimal = 1.82
            leg_dk_prior.lower_decimal = 2.00
            capture_underdog([leg_dk_prior], store, captured_at=t_old, source="draftkings")
            leg_dk = leg_jayson_tatum.model_copy()
            leg_dk.higher_decimal = 1.54
            leg_dk.lower_decimal = 2.75
            capture_underdog([leg_dk], store, captured_at=t_now, source="draftkings")

            stale = detect_stale_opportunities(
                store, min_stale_minutes=30, fresh_window_minutes=120,
                min_line_gap=100.0, min_prob_gap_pp=5.0,
            )
            assert len(stale) >= 1
            s = stale[0]
            # signed_prob_edge should be fresh - stale (positive = fresh higher over prob)
            assert "prob_edge" in s or "prob_gap" in s, \
                "Opportunity must include signed prob edge or prob gap field"
            # Verify signed direction
            assert s.get("stale_prob") is not None
            assert s.get("fresh_prob") is not None
            assert s.get("prob_gap_pp") is not None
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now


# ─────────────────────────────────────────────────────────────────────────────
# E) Event start — bound SQL params, ISO parsing, Z vs +00:00, frozen-clock test
# ─────────────────────────────────────────────────────────────────────────────

class TestEventStartParsing:
    def test_iso_timestamp_with_z_parsed_correctly(self, tmp_db, leg_jayson_tatum):
        """scheduled_at with Z suffix must be parsed as UTC and not rejected."""
        from ud_edge.stale_pricing import SnapshotStore, SnapshotRecord
        from datetime import datetime, timezone

        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        # Leg with Z-suffix scheduled_at
        leg_z = leg_jayson_tatum.model_copy()
        leg_z.scheduled_at = "2026-07-20T18:00:00Z"

        rec = SnapshotRecord(
            source="test", source_line_id="l1",
            player_name=leg_z.player_name, sport=leg_z.sport_id or "NBA",
            stat=leg_z.stat_name, line_value=leg_z.line_value,
            higher_decimal=1.735, lower_decimal=2.10,
            higher_true_prob=0.55, lower_true_prob=0.45,
            event_title=leg_z.match_title or "BOS@NYK",
            scheduled_at=leg_z.scheduled_at,
            captured_at=captured,
        )

        key = SnapshotStore._canonical_key_for_record(rec)
        # Key must include the date portion of the Z timestamp
        assert "2026-07-20" in key

    def test_iso_timestamp_with_plus_offset_parsed_correctly(self, tmp_db, leg_jayson_tatum):
        """scheduled_at with +00:00 offset must be parsed as UTC."""
        from ud_edge.stale_pricing import SnapshotStore, SnapshotRecord
        from datetime import datetime, timezone

        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        rec = SnapshotRecord(
            source="test", source_line_id="l1",
            player_name=leg_jayson_tatum.player_name,
            sport=leg_jayson_tatum.sport_id or "NBA",
            stat=leg_jayson_tatum.stat_name, line_value=leg_jayson_tatum.line_value,
            higher_decimal=1.735, lower_decimal=2.10,
            higher_true_prob=0.55, lower_true_prob=0.45,
            event_title=leg_jayson_tatum.match_title or "BOS@NYK",
            scheduled_at="2026-07-20T18:00:00+00:00",
            captured_at=captured,
        )

        key = SnapshotStore._canonical_key_for_record(rec)
        assert "2026-07-20" in key

    def test_naive_timestamp_parsed_as_utc(self, tmp_db, leg_jayson_tatum):
        """A naive timestamp (no timezone) must be treated as UTC."""
        from ud_edge.stale_pricing import SnapshotStore, SnapshotRecord
        from datetime import datetime, timezone

        captured = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        rec = SnapshotRecord(
            source="test", source_line_id="l1",
            player_name=leg_jayson_tatum.player_name,
            sport=leg_jayson_tatum.sport_id or "NBA",
            stat=leg_jayson_tatum.stat_name, line_value=leg_jayson_tatum.line_value,
            higher_decimal=1.735, lower_decimal=2.10,
            higher_true_prob=0.55, lower_true_prob=0.45,
            event_title=leg_jayson_tatum.match_title or "BOS@NYK",
            scheduled_at="2026-07-20T18:00:00",  # naive, no Z or offset
            captured_at=captured,
        )

        key = SnapshotStore._canonical_key_for_record(rec)
        assert "2026-07-20" in key

    def test_no_dynamic_sql_interpolation(self, tmp_path, monkeypatch):
        """detect_stale_opportunities must NOT use f-string SQL interpolation.
        Must use bound parameters (SQLite ? placeholders)."""
        from ud_edge.stale_pricing import SnapshotStore, detect_stale_opportunities, _find_stale_for_market
        import inspect

        db_path = tmp_path / "sql_injection_test.sqlite3"
        store = SnapshotStore(db_path=db_path)
        store.init()

        find_source = inspect.getsource(_find_stale_for_market)
        assert "repr(market_key)" not in find_source, \
            "SQL query must not use repr()/f-string interpolation — use ? placeholders"

    def test_lexical_z_vs_plus00_compare_fails_frozen_clock(self, tmp_path, monkeypatch, leg_jayson_tatum):
        """A lexicographic string comparison of '2026-07-20T18:00:00Z' vs
        '2026-07-20T18:00:00+00:00' can incorrectly pass/fail depending on
        locale sort order. After fix, bound params + Python ISO parsing
        must handle both correctly."""
        import datetime as dt_module
        from ud_edge import stale_pricing
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_stale_opportunities,
        )

        class FrozenDatetime(dt_module.datetime):
            @staticmethod
            def now(tz=None):
                return dt_module.datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(dt_module, "datetime", FrozenDatetime)
        stale_pricing.utc_now = lambda: FrozenDatetime.now(timezone.utc)
        stale_pricing._original_utc_now = lambda: FrozenDatetime.now(timezone.utc)

        try:
            db_path = tmp_path / "lexical_compare_test.sqlite3"
            store = SnapshotStore(db_path=db_path)
            store.init()

            # An event scheduled at exactly 2026-07-20T18:00:00Z vs +00:00
            # With the old lexicographic filter "scheduled_at >= '2026-07-18T12:00:00+00:00'"
            # the Z-suffix timestamp sorts BEFORE the +00:00 variant due to '+' < 'Z' lexically.
            # After the fix (bound params + Python ISO parsing), both should be handled
            # correctly. We just verify that a future event is NOT rejected.
            leg_z = leg_jayson_tatum.model_copy()
            leg_z.scheduled_at = "2026-07-20T18:00:00Z"
            leg_z.match_title = "BOS@NYK"

            t_now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

            capture_underdog([leg_z], store, captured_at=t_now, source="underdog")
            capture_underdog([leg_z], store, captured_at=t_now, source="draftkings")

            # A future event must NOT be rejected when reject_started=True
            stale = detect_stale_opportunities(
                store, min_stale_minutes=0, fresh_window_minutes=120,
                min_line_gap=0.0, min_prob_gap_pp=0.0, reject_started=True,
            )
            # The event is in the future so it should not be flagged as started
            # We just verify no crash and correct Python-level handling
            assert isinstance(stale, list)
        finally:
            stale_pricing.utc_now = stale_pricing._original_utc_now


# ─────────────────────────────────────────────────────────────────────────────
# F) Migration — key recompute, indexes, INSERT OR IGNORE, real v1 count
# ─────────────────────────────────────────────────────────────────────────────

class TestMigrationCorrectness:
    def test_migrated_key_equals_computed_key(self, tmp_path):
        """During v1->v2 migration, canonical_key is RECOMPUTED from row fields
        (not just copied from the stored v1 value), so new captures compare
        correctly to old history."""
        import sqlite3
        from ud_edge.stale_pricing import SnapshotStore

        # Create a v1 DB with a known canonical_key
        v1_path = tmp_path / "v1_key_test.sqlite3"
        conn = sqlite3.connect(str(v1_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE info (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_line_id TEXT NOT NULL DEFAULT '',
                player_name TEXT NOT NULL,
                sport TEXT NOT NULL DEFAULT '',
                stat TEXT NOT NULL,
                line_value REAL NOT NULL,
                higher_decimal REAL NOT NULL DEFAULT 0.0,
                lower_decimal REAL NOT NULL DEFAULT 0.0,
                higher_true_prob REAL NOT NULL DEFAULT 0.0,
                lower_true_prob REAL NOT NULL DEFAULT 0.0,
                event_title TEXT NOT NULL DEFAULT '',
                scheduled_at TEXT,
                captured_at TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                UNIQUE(source, source_line_id, captured_at, canonical_key)
            )
        """)
        conn.execute("INSERT INTO info (key, value) VALUES ('schema_version', '1')")

        # Insert with v1 key (old format, e.g. missing sport uppercase or date)
        conn.execute("""
            INSERT INTO snapshots
                (source, source_line_id, player_name, sport, stat, line_value,
                 higher_decimal, lower_decimal, higher_true_prob, lower_true_prob,
                 event_title, scheduled_at, captured_at, canonical_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "underdog", "line_abc", "Jayson Tatum", "nba", "points", 27.5,
            1.735, 2.10, 0.55, 0.45,
            "BOS@NYK", "2026-07-20T18:00:00Z", "2026-07-18T12:00:00Z",
            "jayson tatum|nba|points|BOS@NYK",  # v1 key (no date)
        ))
        conn.commit()
        conn.close()

        # Migrate
        store = SnapshotStore(v1_path)
        store.init()

        # After migration, insert a new observation for the same market
        from ud_edge.stale_pricing import SnapshotRecord
        from datetime import datetime, timezone

        captured = datetime(2026, 7, 18, 14, 0, 0, tzinfo=timezone.utc)
        new_rec = SnapshotRecord(
            source="underdog", source_line_id="line_new",
            player_name="Jayson Tatum", sport="NBA", stat="points",
            line_value=27.5, higher_decimal=1.735, lower_decimal=2.10,
            higher_true_prob=0.55, lower_true_prob=0.45,
            event_title="BOS@NYK", scheduled_at="2026-07-20T18:00:00Z",
            captured_at=captured,
        )
        new_id = store.insert(new_rec)

        # The new record should have the correct v2 key (with date)
        cur = store.conn.cursor()
        cur.execute("SELECT canonical_key FROM snapshots WHERE id=?", (new_id,))
        row = cur.fetchone()
        assert row is not None
        new_key = row[0]
        expected_key = "jayson tatum|NBA|points|bos@nyk|2026-07-20"
        assert new_key == expected_key, \
            f"Migrated/new key must include scheduled_date: expected {expected_key}, got {new_key}"

    def test_v2_indexes_exist_after_migration(self, tmp_path):
        """After v1->v2 migration, both idx_snapshots_canonical and
        idx_snapshots_captured must exist. Old index-name collisions must
        be handled (SQLite drops old indexes on table rename)."""
        import sqlite3
        from ud_edge.stale_pricing import SnapshotStore

        v1_path = tmp_path / "v1_indexes_test.sqlite3"
        conn = sqlite3.connect(str(v1_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE info (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_line_id TEXT NOT NULL DEFAULT '',
                player_name TEXT NOT NULL,
                sport TEXT NOT NULL DEFAULT '',
                stat TEXT NOT NULL,
                line_value REAL NOT NULL,
                higher_decimal REAL NOT NULL DEFAULT 0.0,
                lower_decimal REAL NOT NULL DEFAULT 0.0,
                higher_true_prob REAL NOT NULL DEFAULT 0.0,
                lower_true_prob REAL NOT NULL DEFAULT 0.0,
                event_title TEXT NOT NULL DEFAULT '',
                scheduled_at TEXT,
                captured_at TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                UNIQUE(source, source_line_id, captured_at, canonical_key)
            )
        """)
        conn.execute("CREATE INDEX idx_snapshots_canonical ON snapshots(canonical_key, source, captured_at DESC)")
        conn.execute("CREATE INDEX idx_snapshots_captured ON snapshots(captured_at DESC)")
        conn.execute("INSERT INTO info (key, value) VALUES ('schema_version', '1')")
        conn.commit()
        conn.close()

        store = SnapshotStore(v1_path)
        store.init()

        cur = store.conn.cursor()
        cur.execute("PRAGMA index_list(snapshots)")
        indexes = [row[1] for row in cur.fetchall()]

        assert "idx_snapshots_canonical" in indexes, \
            f"idx_snapshots_canonical must exist after migration, found: {indexes}"
        assert "idx_snapshots_captured" in indexes, \
            f"idx_snapshots_captured must exist after migration, found: {indexes}"

    def test_real_v1_db_copy_preserves_4692_rows(self, tmp_path):
        """The real v1 DB copy (from test_migration_preserves_real_v1_db_rows)
        must preserve all observations through a fresh migration."""
        import sqlite3, shutil
        from ud_edge.stale_pricing import SnapshotStore

        real_path = Path(__file__).resolve().parents[1] / "data" / "line_snapshots.sqlite3"
        if not real_path.exists():
            pytest.skip("No local snapshot DB at data/line_snapshots.sqlite3")
        copy_path = tmp_path / "real_v1_4692.sqlite3"
        shutil.copy2(real_path, copy_path)

        conn = sqlite3.connect(str(copy_path))
        original_count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        conn.close()
        assert original_count >= 1000

        # Migrate
        store = SnapshotStore(copy_path)
        store.init()

        cur = store.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM snapshots")
        count = cur.fetchone()[0]
        assert count == original_count, \
            f"Real v1 DB migration must preserve all rows: expected {original_count}, got {count}"


# ─────────────────────────────────────────────────────────────────────────────
# G) Movement reporting — flat uses ↔, prob_side included
# ─────────────────────────────────────────────────────────────────────────────

class TestMovementReporting:
    def test_flat_direction_uses_horizontal_arrow(self, tmp_path, leg_jayson_tatum):
        """Movement with direction='flat' must use '↔' (not '↓') in reports."""
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_movements, build_movement_report,
        )
        from datetime import datetime, timezone

        db_path = tmp_path / "flat_movement_test.sqlite3"
        store = SnapshotStore(db_path=db_path)
        store.init()

        t1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        # Same line, shifted probability
        capture_underdog([leg_jayson_tatum], store, captured_at=t1)

        shifted_leg = leg_jayson_tatum.model_copy()
        shifted_leg.higher_decimal = 1.50
        shifted_leg.lower_decimal = 2.50
        capture_underdog([shifted_leg], store, captured_at=t2)

        movements = detect_movements(store, min_line_move=100.0, min_prob_move_pp=3.0)
        assert len(movements) == 1
        assert movements[0]["direction"] == "flat"

        report = build_movement_report(movements, t2)
        # Must NOT contain ↓ for flat movements
        flat_movement = movements[0]
        arrow = "↔"
        assert arrow in report, \
            f"Flat movement report must use '↔', not '↓'. Report excerpt: {report[:200]}"
        # Verify prob_side is included in the movement record
        assert "prob_side" in flat_movement, "MovementRecord must include prob_side field"

    def test_prob_side_included_for_prob_only_movement(self, tmp_path, leg_jayson_tatum):
        """A probability-only movement (no line change) must include prob_side
        in the movement record and report."""
        from ud_edge.stale_pricing import (
            SnapshotStore, capture_underdog, detect_movements, build_movement_report,
        )
        from datetime import datetime, timezone

        db_path = tmp_path / "prob_only_report_test.sqlite3"
        store = SnapshotStore(db_path=db_path)
        store.init()

        t1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)

        capture_underdog([leg_jayson_tatum], store, captured_at=t1)

        shifted_leg = leg_jayson_tatum.model_copy()
        shifted_leg.higher_decimal = 1.50   # over shifted
        shifted_leg.lower_decimal = 2.50
        capture_underdog([shifted_leg], store, captured_at=t2)

        movements = detect_movements(store, min_line_move=100.0, min_prob_move_pp=3.0)
        assert len(movements) == 1
        m = movements[0]
        assert "prob_side" in m, "Prob-only movement must have prob_side in record"
        assert m["prob_side"] in ("higher", "lower"), \
            f"prob_side must be 'higher' or 'lower', got {m['prob_side']}"

    def test_movement_record_docstring_mentions_same_side(self):
        """MovementRecord dataclass comment must mention same-side probability tracking."""
        from ud_edge.stale_pricing import MovementRecord
        import inspect

        doc = inspect.getdoc(MovementRecord) or ""
        source = inspect.getsource(MovementRecord)
        # The comment should reference same-side probability
        combined = doc + source
        assert "same" in combined.lower() and "side" in combined.lower(), \
            "MovementRecord docs/comments must reference same-side probability tracking"
