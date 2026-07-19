"""Phase 1: Snapshot storage, movement detection, and evidence-based stale-opportunity
detection for Underdog Fantasy player props.

Architecture
────────────
• Append-only SQLite snapshot store with schema versioning.
• Source-agnostic canonical market key: normalized player + sport + normalized stat +
  event identity (where available).
• Movement detector: per-source, comparing latest vs prior observation.
• Cross-source stale detector: evidence-backed only — requires a source to be
  stale (unchanged ≥ min_stale_minutes) AND another source to have moved recently
  (within fresh_window_minutes). Rejects static disagreement with no movement history.
• No automated wagering; all signals are evidence-backed opinions.

Verified source limitations (as of 2026-07-18)
─────────────────────────────────────────────
• PrizePicks:  Its `/projections?...` endpoint returns 403 with X-DataDome
  bot protection from this host. `/beta/v5/over_under_lines` is Underdog.
• Sleeper:     Public API has no Picks/props endpoint. NOT accessible.
• Pinnacle:    Public access closed 2025-07-23 and geo-blocked. NOT accessible.
• Southpaw:    Old FanDuel DFS contest wrapper, not a sportsbook props feed.
  NOT suitable as a live book cross-reference.

Phase 1 feeds: Underdog (live, authenticated-free) + manual-csv / sharp-csv
for cross-source stale detection. Adapter interface ready for PP/Sleeper/Pinnacle
when/if they become accessible.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ud_edge.models import Leg
from ud_edge.no_vig import no_vig


# ─── UTC clock (swap for testing) ────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


_original_utc_now = utc_now  # stored for test restoration

_STAT_SYNONYMS = {
    "pts": "points",
    "pt": "points",
    "reb": "rebounds",
    "rebs": "rebounds",
    "ast": "assists",
    "a": "assists",
    "to": "turnovers",
    "tos": "turnovers",
    "stl": "steals",
    "blk": "blocks",
    "3pt": "threes",
    "3pts": "threes",
    "three": "threes",
    "hr": "home runs",
    "runs": "runs",
    "rbi": "runs_batted_in",
    "hits": "hits",
    "h": "hits",
    "ks": "strikeouts",
    "so": "strikeouts",
    "walks": "walks",
    "bb": "walks",
}


def _normalize_stat(stat: str) -> str:
    """Collapse common stat aliases to a canonical form."""
    s = stat.strip().lower()
    return _STAT_SYNONYMS.get(s, s)


def _normalize_player(name: str) -> str:
    """Lowercase, strip punctuation except apostrophes (for O'Neill),
    collapse dots in initials (P.J. -> PJ), and collapse whitespace."""
    name = name.strip().lower()
    # D'Angelo → Dangelo, O'Neill → ONeill, P.J. → PJ
    name = name.replace("'", "")
    # Collapse consecutive periods (initials): P.J. → PJ, J.J. → JJ
    name = re.sub(r"\.+", "", name)
    # Collapse hyphens within words: O'Brien → Obrien (team names may use hyphens,
    # but for player matching we treat them as removed for canonical identity)
    # Keep team abbreviations like BOS@NYK separate via event normalization
    name = re.sub(r"-+", "", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def _normalize_event_title(title: str) -> str:
    """Normalize event title for cross-source matching.

    - Strip and lowercase
    - Normalize whitespace
    - Normalize separators: '@' and ' vs ' and ' AT ' all map to '@'
    - Examples: 'BOS@NYK' = 'bos at nyk' = 'BOS vs NYK' = 'BOS AT NYK'
    """
    title = title.strip().lower()
    title = re.sub(r"\s+", " ", title)
    # Normalize 'vs' and 'at' to '@'
    title = re.sub(r"\s+at\s+", " @ ", title)
    title = re.sub(r"\s+vs\.?\s+", " @ ", title)
    title = re.sub(r"\s*@\s*", "@", title)
    return title


# ─── Snapshot record ─────────────────────────────────────────────────────────

@dataclass
class SnapshotRecord:
    """One market observation stored in the snapshot DB."""
    id: Optional[int] = None
    source: str = ""            # e.g. "underdog", "draftkings", "pinnacle", "manual-csv"
    source_line_id: str = ""    # book-specific line identifier
    player_name: str = ""       # display name
    sport: str = ""             # NBA, MLB, NFL, …
    stat: str = ""              # normalized stat name
    line_value: float = 0.0
    higher_decimal: float = 0.0
    lower_decimal: float = 0.0
    higher_true_prob: float = 0.0   # no-vig true probability (over side)
    lower_true_prob: float = 0.0     # no-vig true probability (under side)
    event_title: str = ""           # match title e.g. "BOS@NYK"
    scheduled_at: Optional[str] = None  # ISO-8601 game start time
    captured_at: datetime = field(default_factory=utc_now)


# ─── SnapshotStore ───────────────────────────────────────────────────────────

class SnapshotStore:
    """Append-only SQLite store for market snapshots.

    Schema versioning
    ────────────────
    version 1: initial
        UNIQUE(source, source_line_id, captured_at, canonical_key)  [WRONG — includes line_id]

    version 2: correct dedupe identity
        UNIQUE(source, canonical_key, captured_at)
        canonical_key now includes scheduled_date and uses normalized event title.

    Migration from v1: recreates table with correct UNIQUE constraint and copies
    all existing rows (they remain valid since canonical_key was already stored).
    """

    CURRENT_SCHEMA = 2

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.conn: Optional[sqlite3.Connection] = None

    # ── Connection management ─────────────────────────────────────────────

    def init(self) -> None:
        """Create or migrate schema. Idempotent."""
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")

        # Info table for schema version
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS info (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Detect current schema version
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM info WHERE key='schema_version'")
        row = cur.fetchone()
        current_version = int(row[0]) if row else 0

        if current_version < self.CURRENT_SCHEMA:
            self._migrate_to_v2(current_version)
            self.conn.commit()
        elif current_version == 0:
            # Fresh DB — create v2 schema
            self._create_v2_schema()
            cur.execute(
                "INSERT INTO info (key, value) VALUES ('schema_version', ?)",
                (str(self.CURRENT_SCHEMA),)
            )
            self.conn.commit()

    def _create_v2_schema(self) -> None:
        """Create the v2 snapshots table and indexes."""
        self.conn.execute("""
            CREATE TABLE snapshots (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                source           TEXT NOT NULL,
                source_line_id   TEXT NOT NULL DEFAULT '',
                player_name     TEXT NOT NULL,
                sport           TEXT NOT NULL DEFAULT '',
                stat            TEXT NOT NULL,
                line_value      REAL NOT NULL,
                higher_decimal  REAL NOT NULL DEFAULT 0.0,
                lower_decimal   REAL NOT NULL DEFAULT 0.0,
                higher_true_prob REAL NOT NULL DEFAULT 0.0,
                lower_true_prob  REAL NOT NULL DEFAULT 0.0,
                event_title     TEXT NOT NULL DEFAULT '',
                scheduled_at    TEXT,
                captured_at     TEXT NOT NULL,
                canonical_key   TEXT NOT NULL,
                UNIQUE(source, canonical_key, captured_at)
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_canonical
            ON snapshots(canonical_key, source, captured_at DESC)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_captured
            ON snapshots(captured_at DESC)
        """)

    def _migrate_to_v2(self, from_version: int) -> None:
        """Migrate from v1 (or earlier) to v2.

        v1 had UNIQUE(source, source_line_id, captured_at, canonical_key) which
        incorrectly treated different line_ids as distinct observations. We fix
        this by recreating the table with the correct UNIQUE constraint and
        copying all existing rows (they remain valid — canonical_key was already
        stored and the new constraint is a superset of the old one in practice).
        """
        if from_version >= self.CURRENT_SCHEMA:
            return

        cur = self.conn.cursor()

        # Check if table exists (it should, this is a migration)
        cur.execute("""
            SELECT name FROM sqlite_master WHERE type='table' AND name='snapshots'
        """)
        if not cur.fetchone():
            # No table yet — just create v2 schema
            self._create_v2_schema()
            cur.execute(
                "INSERT OR IGNORE INTO info (key, value) VALUES ('schema_version', ?)",
                (str(self.CURRENT_SCHEMA),)
            )
            return

        # Read the rows before rebuilding so their canonical keys can be
        # recomputed by the exact same Python normalizers used for new data.
        rows = cur.execute("""
            SELECT id, source, source_line_id, player_name, sport, stat,
                   line_value, higher_decimal, lower_decimal,
                   higher_true_prob, lower_true_prob, event_title,
                   scheduled_at, captured_at
            FROM snapshots ORDER BY id
        """).fetchall()

        # Renamed-table indexes retain globally unique names in SQLite. Drop
        # them before creating the v2 table, otherwise IF NOT EXISTS silently
        # leaves the new table without its intended indexes.
        cur.execute("ALTER TABLE snapshots RENAME TO snapshots_old")
        cur.execute("DROP INDEX IF EXISTS idx_snapshots_canonical")
        cur.execute("DROP INDEX IF EXISTS idx_snapshots_captured")
        self._create_v2_schema()

        for row in rows:
            (row_id, source, source_line_id, player_name, sport, stat,
             line_value, higher_decimal, lower_decimal, higher_true_prob,
             lower_true_prob, event_title, scheduled_at, captured_at) = row
            rec = SnapshotRecord(
                source=source,
                source_line_id=source_line_id,
                player_name=player_name,
                sport=sport,
                stat=stat,
                line_value=line_value,
                higher_decimal=higher_decimal,
                lower_decimal=lower_decimal,
                higher_true_prob=higher_true_prob,
                lower_true_prob=lower_true_prob,
                event_title=event_title,
                scheduled_at=scheduled_at,
                captured_at=_parse_iso_utc(captured_at),
            )
            canon = self._canonical_key_for_record(rec)
            cur.execute("""
                INSERT OR IGNORE INTO snapshots (
                    id, source, source_line_id, player_name, sport, stat,
                    line_value, higher_decimal, lower_decimal,
                    higher_true_prob, lower_true_prob, event_title,
                    scheduled_at, captured_at, canonical_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row_id, source, source_line_id, player_name, sport, stat,
                line_value, higher_decimal, lower_decimal,
                higher_true_prob, lower_true_prob, event_title,
                scheduled_at, captured_at, canon,
            ))

        cur.execute("DROP TABLE snapshots_old")

        # Update schema version
        cur.execute(
            "UPDATE info SET value=? WHERE key='schema_version'",
            (str(self.CURRENT_SCHEMA),)
        )

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    # ── Canonical key ────────────────────────────────────────────────────

    @staticmethod
    def _canonical_key(leg: Leg, source: str) -> str:
        """Source-agnostic identity for a market.

        Canonical form: normalized_player|sport|normalized_stat|normalized_event|scheduled_date

        event_title is normalized (lowercase, @/vs/AT all → @) and
        scheduled_at (UTC date) is included so repeat games on different
        dates never share history.
        """
        player = _normalize_player(leg.player_name)
        sport = (leg.sport_id or "").strip().upper()
        stat = _normalize_stat(leg.stat_name)
        event_raw = (leg.match_title or "").strip() or "none"
        event = _normalize_event_title(event_raw)
        # Use date portion of scheduled_at for event-date differentiation
        if leg.scheduled_at:
            # scheduled_at format: "2026-07-20T18:00:00Z"
            scheduled_date = leg.scheduled_at[:10]  # "2026-07-20"
        else:
            scheduled_date = "none"
        return f"{player}|{sport}|{stat}|{event}|{scheduled_date}"

    # ── Insert ───────────────────────────────────────────────────────────

    def insert(self, rec: SnapshotRecord) -> int:
        """Insert one snapshot and commit immediately. Returns row ID or existing ID if deduped.

        Dedupe identity: source + canonical_key + captured_at.
        """
        canon = SnapshotStore._canonical_key_for_record(rec)
        captured_iso = rec.captured_at.isoformat()

        try:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO snapshots (
                    source, source_line_id, player_name, sport, stat,
                    line_value, higher_decimal, lower_decimal,
                    higher_true_prob, lower_true_prob,
                    event_title, scheduled_at, captured_at, canonical_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rec.source,
                rec.source_line_id,
                rec.player_name,
                rec.sport,
                rec.stat,
                rec.line_value,
                rec.higher_decimal,
                rec.lower_decimal,
                rec.higher_true_prob,
                rec.lower_true_prob,
                rec.event_title,
                rec.scheduled_at,
                captured_iso,
                canon,
            ))
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # Dedupe: same source + canonical_key + captured_at → find existing
            cur = self.conn.cursor()
            cur.execute(
                "SELECT id FROM snapshots WHERE source=? AND canonical_key=? AND captured_at=?",
                (rec.source, canon, captured_iso),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            # Not a duplicate (for example, a CHECK/trigger failure). Do not
            # disguise a real database error as a dedupe.
            self.conn.rollback()
            raise

    @staticmethod
    def _canonical_key_for_record(rec: SnapshotRecord) -> str:
        """Build canonical key from a SnapshotRecord — must match _canonical_key(leg)."""
        player = _normalize_player(rec.player_name)
        sport = (rec.sport or "").strip().upper()
        stat = _normalize_stat(rec.stat)
        event_raw = (rec.event_title or "").strip() or "none"
        event = _normalize_event_title(event_raw)
        if rec.scheduled_at:
            scheduled_date = rec.scheduled_at[:10]
        else:
            scheduled_date = "none"
        return f"{player}|{sport}|{stat}|{event}|{scheduled_date}"


# ─── capture_underdog ───────────────────────────────────────────────────────

def capture_underdog(
    legs: list[Leg],
    store: SnapshotStore,
    source: str = "underdog",
    captured_at: Optional[datetime] = None,
) -> list[int]:
    """Convert a list of Leg objects into snapshots and append to the store atomically.

    Computes no-vig true probabilities for each leg and stores them.
    All legs are inserted in a SINGLE transaction (one commit).
    Returns the list of row IDs inserted (or deduped).
    """
    if captured_at is None:
        captured_at = utc_now()

    if store.conn is None:
        store.init()

    captured_iso = captured_at.isoformat()
    records = []
    row_ids = []

    for leg in legs:
        if leg.higher_decimal > 1.0 and leg.lower_decimal > 1.0:
            true_over, true_under, _ = no_vig(leg.higher_decimal, leg.lower_decimal)
        else:
            true_over = 0.0
            true_under = 0.0

        rec = SnapshotRecord(
            source=source,
            source_line_id=leg.line_id,
            player_name=leg.player_name,
            sport=leg.sport_id or "",
            stat=leg.stat_name,
            line_value=leg.line_value,
            higher_decimal=leg.higher_decimal,
            lower_decimal=leg.lower_decimal,
            higher_true_prob=true_over,
            lower_true_prob=true_under,
            event_title=leg.match_title or "",
            scheduled_at=leg.scheduled_at,
            captured_at=captured_at,
        )
        canon = SnapshotStore._canonical_key_for_record(rec)
        records.append((rec, canon))
        row_ids.append(None)  # placeholder

    # Execute all inserts in one transaction
    cur = store.conn.cursor()
    for i, (rec, canon) in enumerate(records):
        try:
            cur.execute("""
                INSERT INTO snapshots (
                    source, source_line_id, player_name, sport, stat,
                    line_value, higher_decimal, lower_decimal,
                    higher_true_prob, lower_true_prob,
                    event_title, scheduled_at, captured_at, canonical_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rec.source,
                rec.source_line_id,
                rec.player_name,
                rec.sport,
                rec.stat,
                rec.line_value,
                rec.higher_decimal,
                rec.lower_decimal,
                rec.higher_true_prob,
                rec.lower_true_prob,
                rec.event_title,
                rec.scheduled_at,
                captured_iso,
                canon,
            ))
            row_ids[i] = cur.lastrowid
        except sqlite3.IntegrityError:
            # Dedupe: find existing row
            cur.execute(
                "SELECT id FROM snapshots WHERE source=? AND canonical_key=? AND captured_at=?",
                (rec.source, canon, captured_iso),
            )
            existing = cur.fetchone()
            if existing:
                row_ids[i] = existing[0]
            else:
                # A non-dedupe constraint/trigger error must abort the entire
                # observation batch so a partial polling tick is never stored.
                store.conn.rollback()
                raise

    # Single commit for the entire batch
    store.conn.commit()
    return row_ids


def capture_from_observations(
    observations,
    store: SnapshotStore,
    source: str,
    captured_at: Optional[datetime] = None,
) -> list[int]:
    """Convert a generic iterable of observation records into snapshots and append to the store atomically.

    Each observation must have these attributes (dataclass or dict):
      - player_name      (str)
      - sport_id         (str)
      - stat_name        (str)
      - line_value      (float)
      - match_title     (str, "" if unknown)
      - scheduled_at    (str, "" if unknown)
      - higher_decimal  (float, 0.0 if unknown)
      - lower_decimal   (float, 0.0 if unknown)
      - source_line_id  (str, "" if unknown)

    No-vig true probabilities are computed when BOTH higher_decimal and
    lower_decimal are > 1.0.  Otherwise both true probs are stored as 0.0.

    All observations are inserted in a SINGLE transaction (one commit).
    Returns the list of row IDs (deduped where applicable).

    A real database-level error (e.g. CHECK constraint or trigger abort)
    rolls back the entire batch and re-raises.
    """
    if captured_at is None:
        captured_at = utc_now()

    if store.conn is None:
        store.init()

    captured_iso = captured_at.isoformat()
    records: list[tuple[SnapshotRecord, str]] = []
    row_ids: list[Optional[int]] = []

    for obs in observations:
        # Accept both dict-style and dataclass-style access
        def _get(field: str, default=""):
            val = getattr(obs, field, None)
            if val is None:
                val = obs.get(field, default) if hasattr(obs, "get") else default
            return val

        player_name = str(_get("player_name", ""))
        sport_id = str(_get("sport_id", ""))
        stat_name = str(_get("stat_name", ""))
        line_value = float(_get("line_value", 0.0))
        # Backwards-compatible: callers may pass `event_title` (our internal
        # vocabulary) or `match_title` (the historical interface).
        match_title = str(
            _get("match_title", None)
            if _get("match_title", None) not in (None, "")
            else _get("event_title", "")
        )
        scheduled_at = str(_get("scheduled_at", "") or "")
        higher_decimal = float(_get("higher_decimal", 0.0))
        lower_decimal = float(_get("lower_decimal", 0.0))
        source_line_id = str(_get("source_line_id", ""))

        # Compute no-vig only when both sides are priced
        if higher_decimal > 1.0 and lower_decimal > 1.0:
            true_over, true_under, _ = no_vig(higher_decimal, lower_decimal)
        else:
            true_over = 0.0
            true_under = 0.0

        rec = SnapshotRecord(
            source=source,
            source_line_id=source_line_id,
            player_name=player_name,
            sport=sport_id,
            stat=stat_name,
            line_value=line_value,
            higher_decimal=higher_decimal,
            lower_decimal=lower_decimal,
            higher_true_prob=true_over,
            lower_true_prob=true_under,
            event_title=match_title,
            scheduled_at=scheduled_at if scheduled_at else None,
            captured_at=captured_at,
        )
        canon = SnapshotStore._canonical_key_for_record(rec)
        records.append((rec, canon))
        row_ids.append(None)  # placeholder

    # Execute all inserts in one transaction
    cur = store.conn.cursor()
    for i, (rec, canon) in enumerate(records):
        try:
            cur.execute("""
                INSERT INTO snapshots (
                    source, source_line_id, player_name, sport, stat,
                    line_value, higher_decimal, lower_decimal,
                    higher_true_prob, lower_true_prob,
                    event_title, scheduled_at, captured_at, canonical_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rec.source,
                rec.source_line_id,
                rec.player_name,
                rec.sport,
                rec.stat,
                rec.line_value,
                rec.higher_decimal,
                rec.lower_decimal,
                rec.higher_true_prob,
                rec.lower_true_prob,
                rec.event_title,
                rec.scheduled_at,
                captured_iso,
                canon,
            ))
            row_ids[i] = cur.lastrowid
        except sqlite3.IntegrityError:
            # Dedupe: find existing row
            cur.execute(
                "SELECT id FROM snapshots WHERE source=? AND canonical_key=? AND captured_at=?",
                (rec.source, canon, captured_iso),
            )
            existing = cur.fetchone()
            if existing:
                row_ids[i] = existing[0]
            else:
                # A non-dedupe constraint/trigger error must abort the entire batch.
                store.conn.rollback()
                raise

    # Single commit for the entire batch
    store.conn.commit()
    return row_ids


# ─── Movement detector ────────────────────────────────────────────────────────

@dataclass
class MovementRecord:
    """A line or same-side fair-probability movement between observations."""
    market_key: str
    source: str
    direction: str           # "up" | "down" | "flat"
    prior_line: float
    current_line: float
    prior_true_prob: float   # true prob of the prior CHOSEN side
    current_true_prob: float
    prob_shift_pp: float     # change in true prob (pp)
    prior_captured_at: datetime
    current_captured_at: datetime
    prior_id: int
    current_id: int


def detect_movements(
    store: SnapshotStore,
    min_line_move: float = 0.5,
    min_prob_move_pp: float = 3.0,
) -> list[dict]:
    """Compare latest vs prior observation per source+market, emit movement records.

    A movement is recorded when EITHER:
    • |current_line - prior_line| >= min_line_move  (absolute line change)
    • |prob_shift_pp| >= min_prob_move_pp           (chosen-side true-prob shift)

    Direction: "up" means the line value increased (e.g. 27.5 → 28.5).
    True-prob shift is measured in percentage points of the chosen side.

    Returns a list of movement dicts.
    """
    if store.conn is None:
        store.init()

    cur = store.conn.cursor()

    # For each (canonical_key, source) pair, get the two most-recent snapshots
    cur.execute("""
        WITH ranked AS (
            SELECT
                id, canonical_key, source, line_value,
                higher_true_prob, lower_true_prob,
                captured_at,
                ROW_NUMBER() OVER (
                    PARTITION BY canonical_key, source
                    ORDER BY captured_at DESC
                ) AS rn
            FROM snapshots
        )
        SELECT
            r1.id, r1.canonical_key, r1.source,
            r1.line_value, r2.line_value,
            r1.higher_true_prob, r2.higher_true_prob,
            r1.lower_true_prob, r2.lower_true_prob,
            r1.captured_at, r2.captured_at,
            r1.id AS curr_id, r2.id AS prior_id
        FROM ranked r1
        JOIN ranked r2
          ON r1.canonical_key = r2.canonical_key
         AND r1.source = r2.source
         AND r1.rn = 1
         AND r2.rn = 2
    """)

    movements = []
    for row in cur.fetchall():
        (curr_id, canon_key, source,
         curr_line, prior_line,
         curr_higher_prob, prior_higher_prob,
         curr_lower_prob, prior_lower_prob,
         curr_captured, prior_captured,
         curr_row_id, prior_row_id) = row

        line_diff = abs(curr_line - prior_line)

        # ── Same-side probability tracking ──────────────────────────────────
        # Determine which side was "chosen" at prior (higher-prob side = market's pick)
        prior_higher = prior_higher_prob >= prior_lower_prob
        # Track that SAME side's probability at current observation
        if prior_higher:
            prior_chosen_prob = prior_higher_prob
            curr_chosen_prob = curr_higher_prob
            prob_side = "higher"
        else:
            prior_chosen_prob = prior_lower_prob
            curr_chosen_prob = curr_lower_prob
            prob_side = "lower"

        prob_shift_pp = (curr_chosen_prob - prior_chosen_prob) * 100.0

        # ── Direction ─────────────────────────────────────────────────────
        if line_diff >= min_line_move:
            direction = "up" if curr_line > prior_line else "down"
        else:
            # Line unchanged but prob shifted — mark as flat
            direction = "flat"

        triggered = (
            line_diff >= min_line_move or
            abs(prob_shift_pp) >= min_prob_move_pp
        )
        if not triggered:
            continue

        movements.append({
            "market_key": canon_key,
            "source": source,
            "direction": direction,
            "prob_side": prob_side,        # which side shifted (higher/lower)
            "prior_line": prior_line,
            "current_line": curr_line,
            "prior_true_prob": prior_chosen_prob,
            "current_true_prob": curr_chosen_prob,
            "prob_shift_pp": prob_shift_pp,
            "prior_captured_at": prior_captured,
            "current_captured_at": curr_captured,
            "prior_id": prior_row_id,
            "current_id": curr_row_id,
        })

    return movements


# ─── Cross-source stale detector ─────────────────────────────────────────────

@dataclass
class StaleOpportunity:
    stale_source: str
    fresh_source: str
    market_key: str
    direction: str           # "higher" or "lower" — which side is mispriced
    stale_line: float
    fresh_line: float
    line_gap: float
    stale_prob: float        # true prob on stale source's side
    fresh_prob: float
    prob_gap_pp: float       # gap in percentage points
    stale_age_minutes: float
    stale_captured_at: str
    fresh_captured_at: str
    confidence: str          # "high" | "medium" | "low"
    evidence: str            # human-readable summary


def detect_stale_opportunities(
    store: SnapshotStore,
    min_stale_minutes: float = 30.0,
    fresh_window_minutes: float = 120.0,
    min_line_gap: float = 0.5,
    min_prob_gap_pp: float = 3.0,
    min_movement_line: float = 0.5,
    min_movement_prob_pp: float = 3.0,
    reject_started: bool = True,
) -> list[dict]:
    """Find cross-source stale opportunities.

    A stale opportunity requires ALL of the following evidence:
    1. At least 2 distinct sources have observed the same canonical market.
    2. Source A (stale) has been UNCHANGED for ≥ min_stale_minutes.
       (i.e., its latest observation is ≥ min_stale_minutes old AND
        it has not moved relative to its prior observation)
    3. Source B (fresh) has a NEW observation within fresh_window_minutes.
       (i.e., its latest observation is < fresh_window_minutes old AND
        it HAS moved relative to its prior observation)
    4. Either |line_gap| >= min_line_gap  OR  prob_gap_pp >= min_prob_gap_pp.
    5. Direction is explicit (which side is higher/lower on each source).

    This does NOT flag static disagreement with no movement history.
    Events with scheduled_at in the past are rejected by default.
    """
    if store.conn is None:
        store.init()

    cur = store.conn.cursor()

    # Get all distinct canonical keys with 2+ sources
    cur.execute("""
        SELECT canonical_key
        FROM snapshots
        GROUP BY canonical_key
        HAVING COUNT(DISTINCT source) >= 2
    """)
    market_keys = [row[0] for row in cur.fetchall()]

    now = utc_now()
    now_ts = now.isoformat()

    opportunities = []

    for market_key in market_keys:
        opp = _find_stale_for_market(
            cur, market_key,
            min_stale_minutes, fresh_window_minutes,
            min_line_gap, min_prob_gap_pp,
            min_movement_line=min_movement_line,
            min_movement_prob_pp=min_movement_prob_pp,
            reject_started=reject_started,
            now_ts=now_ts,
        )
        if opp:
            opportunities.append(opp)

    return opportunities


def _parse_iso_utc(value: str | datetime) -> datetime:
    """Parse ISO-8601 text and normalize it to an aware UTC datetime."""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_event_future(scheduled_at: Optional[str], now: datetime) -> bool:
    """Return True if the event has NOT yet started (scheduled_at > now).

    Handles Z suffix, +00:00 offset, and naive timestamps (treated as UTC).
    Rejects events with scheduled_at in the past or exactly now.
    """
    if not scheduled_at or scheduled_at == "":
        return True  # unknown scheduled time → include (don't exclude)
    try:
        parsed = _parse_iso_utc(scheduled_at)
        normalized_now = _parse_iso_utc(now)
        return parsed > normalized_now
    except (ValueError, TypeError):
        return True  # unparseable → include conservatively


def _find_stale_for_market(
    cur: sqlite3.Cursor,
    market_key: str,
    min_stale_minutes: float,
    fresh_window_minutes: float,
    min_line_gap: float,
    min_prob_gap_pp: float,
    min_movement_line: float = 0.5,
    min_movement_prob_pp: float = 3.0,
    reject_started: bool = True,
    now_ts: Optional[str] = None,
) -> Optional[dict]:
    """Check one canonical market for stale opportunities. Returns first found or None."""
    now = utc_now()

    def parse_ts(ts_str: str) -> datetime:
        if not ts_str:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    def age_minutes(ts_str: str) -> float:
        return (now - parse_ts(ts_str)).total_seconds() / 60.0

    # ── Parse scheduled_at in Python (not SQL) to handle Z / +00:00 / naive ──
    if reject_started and now_ts:
        now_dt = parse_ts(now_ts)
    else:
        now_dt = now

    # Get ALL observations for this market per source (most recent first)
    # Uses bound ? parameter — no string interpolation of market_key
    cur.execute("""
        SELECT id, source, line_value,
               higher_true_prob, lower_true_prob,
               captured_at, scheduled_at
        FROM snapshots
        WHERE canonical_key = ?
        ORDER BY source, captured_at DESC
    """, (market_key,))
    all_rows = cur.fetchall()
    if not all_rows:
        return None

    # Index by source: all observations (newest first)
    by_source: dict[str, list] = {}
    for row in all_rows:
        src = row[1]
        by_source.setdefault(src, []).append(row)

    # Filter out started events in Python
    if reject_started and now_ts:
        for src in list(by_source.keys()):
            filtered = [
                r for r in by_source[src]
                if _is_event_future(r[6], now_dt)
            ]
            if filtered:
                by_source[src] = filtered
            else:
                del by_source[src]

    sources = list(by_source.keys())
    n = len(sources)
    if n < 2:
        return None  # need 2+ sources for cross-source detection

    # Try each ordered pair (stale_candidate, fresh_candidate)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            src_stale = sources[i]
            src_fresh = sources[j]

            stale_obs = by_source[src_stale]
            fresh_obs = by_source[src_fresh]

            # Need at least 2 obs per source to prove unchanged/moved
            if len(stale_obs) < 2 or len(fresh_obs) < 2:
                continue

            opp = _check_stale_pair(
                market_key=market_key,
                src_stale=src_stale, src_fresh=src_fresh,
                stale_obs=stale_obs,
                fresh_obs=fresh_obs,
                now=now,
                min_stale_minutes=min_stale_minutes,
                fresh_window_minutes=fresh_window_minutes,
                min_line_gap=min_line_gap,
                min_prob_gap_pp=min_prob_gap_pp,
                min_movement_line=min_movement_line,
                min_movement_prob_pp=min_movement_prob_pp,
            )
            if opp:
                return opp

    return None


def _check_stale_pair(
    market_key: str,
    src_stale: str, src_fresh: str,
    stale_obs: list,
    fresh_obs: list,
    now: datetime,
    min_stale_minutes: float,
    fresh_window_minutes: float,
    min_line_gap: float,
    min_prob_gap_pp: float,
    min_movement_line: float = 0.5,
    min_movement_prob_pp: float = 3.0,
) -> Optional[dict]:
    """Check if src_stale is stale relative to src_fresh for a given market.

    Stale evidence (A): derive unchanged_since by scanning stale_obs back to the
    latest meaningful change. Require >= 2 observations and stale unchanged
    duration >= min_stale_minutes.

    Fresh evidence (B): require >= 2 observations. Find the most recent
    meaningful movement (line change >= min_movement_line OR same-side prob
    shift >= min_movement_prob_pp). The movement must have occurred within
    fresh_window_minutes of now.

    Direction (C): 'higher' = play Over at stale book (stale line < fresh line).
                   'lower' = play Under at stale book (stale line > fresh line).
    Same line → compare same-side probabilities.

    Gap (D): qualifies if abs line gap >= min_line_gap  OR  abs prob gap >= min_prob_gap_pp.
    """
    def parse_ts(ts_str: str) -> datetime:
        if not ts_str:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    def age_minutes(ts_str: str) -> float:
        return (now - parse_ts(ts_str)).total_seconds() / 60.0

    def line_change(a: list, b: list) -> float:
        return abs(a[2] - b[2])

    def prob_shift_same_side(prior_obs: list, curr_obs: list) -> tuple[float, str]:
        """Return (abs_prob_shift_pp, side) for the same-side probability shift.

        prior_higher = prior higher_prob >= prior lower_prob
        curr_chosen_prob = curr higher_prob if prior_higher else curr lower_prob
        prior_chosen_prob = prior higher_prob if prior_higher else prior lower_prob
        """
        prior_higher = prior_obs[3] >= prior_obs[4]
        if prior_higher:
            return (abs(curr_obs[3] - prior_obs[3]) * 100.0, "higher")
        else:
            return (abs(curr_obs[4] - prior_obs[4]) * 100.0, "lower")

    def find_unchanged_since(obs: list) -> tuple[Optional[list], float]:
        """Scan from newest to oldest, return (first_unchanged_obs, age_minutes).

        'Unchanged' means consecutive pairs have no meaningful movement
        (line change < min_movement_line AND same-side prob shift < min_movement_prob_pp).
        The first observation in the unchanged sequence is returned with its age.
        If no meaningful change found, the oldest observation is used as unchanged_since.
        """
        if len(obs) < 2:
            return None, 0.0
        # Start from newest
        curr = obs[0]
        curr_age = age_minutes(curr[5])
        # Check if there is ANY meaningful change from newest backwards
        for i in range(1, len(obs)):
            prior = obs[i]
            lc = line_change(curr, prior)
            prob_pp, _ = prob_shift_same_side(prior, curr)
            if lc >= min_movement_line or prob_pp >= min_movement_prob_pp:
                # Found a meaningful change at position i-1 -> i
                # The unchanged sequence starts at obs[i-1] (the post-change observation)
                unchanged_obs = obs[i - 1]
                return unchanged_obs, age_minutes(unchanged_obs[5])
        # No meaningful change found — entire history is unchanged
        # unchanged_since = oldest observation
        oldest = obs[-1]
        return oldest, age_minutes(oldest[5])

    def find_last_meaningful_move(obs: list) -> tuple[Optional[list], Optional[list], float]:
        """Find most recent meaningful movement in obs.

        Returns (post_move_obs, pre_move_obs, age_of_post_move) or (None, None, 0).
        Scans from newest to oldest; the first meaningful movement found defines
        the most recent change point.
        """
        if len(obs) < 2:
            return None, None, 0.0
        for i in range(1, len(obs)):
            curr = obs[i - 1]
            prior = obs[i]
            lc = line_change(curr, prior)
            prob_pp, _ = prob_shift_same_side(prior, curr)
            if lc >= min_movement_line or prob_pp >= min_movement_prob_pp:
                # Meaningful movement found: curr (post-move) -> prior (pre-move)
                return curr, prior, age_minutes(curr[5])
        return None, None, 0.0

    # ── Stale evidence: derive unchanged_since ────────────────────────────────
    stale_unchanged_obs, stale_age = find_unchanged_since(stale_obs)
    if stale_unchanged_obs is None:
        return None  # < 2 observations
    if stale_age < min_stale_minutes:
        return None

    # ── Fresh evidence: find most recent meaningful movement ─────────────────
    fresh_move_obs, fresh_prior_obs, fresh_move_age = find_last_meaningful_move(fresh_obs)
    if fresh_move_obs is None:
        return None  # no meaningful movement or < 2 observations
    if fresh_move_age > fresh_window_minutes:
        return None  # movement was outside fresh window

    # Use the most recent observation as the current fresh state
    fresh_latest = fresh_obs[0]
    stale_latest = stale_obs[0]  # most recent stale observation

    # ── Line gap and prob gap ────────────────────────────────────────────
    stale_line = stale_latest[2]
    fresh_line = fresh_latest[2]
    line_gap = abs(stale_line - fresh_line)

    # ── Direction: playable side on stale source ──────────────────────────
    # If stale_line > fresh_line → stale book is too high on over
    # → stale over is overpriced → play Under at stale (direction='lower')
    # Wait, re-read the spec: stale=27.5, fresh=28.5 → play 'higher' on stale
    # stale=27.5 < fresh=28.5 → fresh moved up → stale's under is undervalued
    # → play 'higher' at stale (back Over at stale where stale line < fresh line)
    # stale=29.5, fresh=28.5 → stale over is overpriced → play 'lower' at stale
    # stale > fresh → stale too high → play lower
    if stale_line > fresh_line:
        direction = "lower"
    elif stale_line < fresh_line:
        direction = "higher"
    else:
        # Same line → use probability: higher fair prob = higher side is favorite
        # Fresh higher prob > Stale higher prob → fresh over is more likely
        # → stale under is relatively underpriced → play lower at stale
        stale_higher = stale_latest[3]
        fresh_higher = fresh_latest[3]
        direction = "higher" if fresh_higher > stale_higher else "lower"

    # ── Same-side probabilities ─────────────────────────────────────────
    stale_higher_prob = stale_latest[3]
    fresh_higher_prob = fresh_latest[3]
    stale_lower_prob = stale_latest[4]
    fresh_lower_prob = fresh_latest[4]

    if direction == "higher":
        stale_prob = stale_higher_prob
        fresh_prob = fresh_higher_prob
    else:
        stale_prob = stale_lower_prob
        fresh_prob = fresh_lower_prob

    prob_gap_pp = abs(stale_prob - fresh_prob) * 100.0
    signed_prob_edge_pp = (fresh_prob - stale_prob) * 100.0

    # ── Minimum thresholds (OR semantics) ─────────────────────────────────
    if line_gap < min_line_gap and prob_gap_pp < min_prob_gap_pp:
        return None

    # ── Confidence ───────────────────────────────────────────────────────
    if stale_age >= 120 and (line_gap >= 2.0 or prob_gap_pp >= 10.0):
        confidence = "high"
    elif stale_age >= 60 and (line_gap >= 1.0 or prob_gap_pp >= 5.0):
        confidence = "medium"
    else:
        confidence = "low"

    # ── Evidence: what to play on stale source ──────────────────────────
    side_verb = "Over" if direction == "higher" else "Under"
    evidence = (
        f"play {direction} on {src_stale} (line={stale_line:.1f}, "
        f"stale {stale_age:.0f}min unchanged); "
        f"{src_fresh} moved to line={fresh_line:.1f} within {fresh_move_age:.0f}min; "
        f"gap={line_gap:.1f}pts/{prob_gap_pp:.1f}pp"
    )

    return {
        "stale_source": src_stale,
        "fresh_source": src_fresh,
        "market_key": market_key,
        "direction": direction,
        "opportunity_side": direction,
        "stale_line": stale_line,
        "fresh_line": fresh_line,
        "line_gap": line_gap,
        "stale_prob": stale_prob,
        "fresh_prob": fresh_prob,
        "prob_gap_pp": prob_gap_pp,
        "prob_gap": prob_gap_pp,
        "signed_prob_edge_pp": signed_prob_edge_pp,
        "prob_edge": signed_prob_edge_pp,
        "stale_age_minutes": stale_age,
        "stale_captured_at": stale_latest[5],
        "fresh_captured_at": fresh_latest[5],
        "confidence": confidence,
        "evidence": evidence,
    }


# ─── Report builders ────────────────────────────────────────────────────────

def build_movement_report(movements: list[dict], as_of: datetime) -> str:
    """Build a Markdown + console summary of movement records."""
    lines = [
        "## Line Movement Report",
        f"_Generated: {as_of.isoformat()}_",
        "",
    ]

    if not movements:
        lines.append("*No line movements detected above configured thresholds.*")
        return "\n".join(lines)

    # Group by source
    by_source: dict[str, list[dict]] = {}
    for m in movements:
        by_source.setdefault(m["source"], []).append(m)

    for source, recs in by_source.items():
        lines.append(f"### {source.capitalize()}")
        lines.append("")
        lines.append("| Player · Stat | Prior → Current | Prob Shift |")
        lines.append("|---|---|---|")
        for r in recs:
            key = r["market_key"]
            # Parse canonical key for display
            parts = key.split("|")
            player_stat = f"{parts[0].title()} {parts[2]}" if len(parts) >= 3 else key
            arrow = {"up": "↑", "down": "↓", "flat": "↔"}.get(r["direction"], "?")
            lines.append(
                f"| {player_stat} | {r['prior_line']:.1f} {arrow} {r['current_line']:.1f} "
                f"| {r['prob_side']} {r['prob_shift_pp']:+.1f}pp |"
            )
        lines.append("")

    return "\n".join(lines)


def build_stale_report(
    movements: list[dict],
    stale_opportunities: list[dict],
    as_of: datetime,
) -> str:
    """Build a Markdown report of stale opportunities and movements."""
    lines = [
        "## Stale Pricing Report",
        f"_Generated: {as_of.isoformat()}_",
        "",
    ]

    if not stale_opportunities:
        lines.append(
            "**No confirmed stale opportunities yet.** "
            "Staleness requires evidence: one source unchanged for ≥N minutes "
            "while another source shows recent movement."
        )
        lines.append("")
        lines.append("*This message appears on first run or when insufficient "
                     "cross-source movement history has been captured.*")
    else:
        lines.append(f"### Detected Stale Opportunities ({len(stale_opportunities)})")
        lines.append("")
        lines.append("| Conf | Stale Source | Fresh Source | Market | "
                     "Direction | Line Gap | Prob Gap | Age | Evidence |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for opp in stale_opportunities:
            conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(
                opp["confidence"], "?")
            lines.append(
                f"| {conf_emoji} {opp['confidence']} "
                f"| {opp['stale_source']} ({opp['stale_line']:.1f}) "
                f"| {opp['fresh_source']} ({opp['fresh_line']:.1f}) "
                f"| {opp['market_key']} "
                f"| {opp['direction']} "
                f"| {opp['line_gap']:.1f} "
                f"| {opp['prob_gap_pp']:.1f}pp "
                f"| {opp['stale_age_minutes']:.0f}min "
                f"| {opp['evidence']} |"
            )
        lines.append("")
        lines.append(
            "**Direction is the playable side on the stale source.** "
            "`higher` means play Higher/Over there; `lower` means play Lower/Under there."
        )

    # Movements section
    if movements:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## Recent Movements")
        for m in movements[:20]:  # cap at 20 for readability
            key = m["market_key"]
            parts = key.split("|")
            player_stat = f"{parts[0].title()} {parts[2]}" if len(parts) >= 3 else key
            arrow = {"up": "↑", "down": "↓", "flat": "↔"}.get(m["direction"], "?")
            lines.append(
                f"- **{player_stat}** [{m['source']}]: "
                f"{m['prior_line']:.1f} {arrow} {m['current_line']:.1f} "
                f"({m['prob_shift_pp']:+.1f}pp) — {m['prior_captured_at'][:16]} → {m['current_captured_at'][:16]}"
            )

    return "\n".join(lines)


def print_console_summary(movements: list[dict], stale_opportunities: list[dict]) -> None:
    """Print a compact console summary (for terminal output)."""
    print("\n=== Stale Pricing Summary ===")
    if not stale_opportunities:
        print("  No confirmed stale opportunities yet.")
        print("  (Staleness requires one source unchanged ≥N min + another source moved recently.)")
    else:
        for opp in stale_opportunities:
            print(
                f"  [{opp['confidence'].upper()}] {opp['stale_source']}→{opp['fresh_source']}: "
                f"{opp['market_key']} {opp['direction']} "
                f"gap={opp['line_gap']:.1f}pts/{opp['prob_gap_pp']:.1f}pp "
                f"(stale_age={opp['stale_age_minutes']:.0f}min)"
            )

    if movements:
        print(f"\n  {len(movements)} movement(s) detected above thresholds.")
        for m in movements[:5]:
            key = m["market_key"]
            parts = key.split("|")
            player_stat = f"{parts[0].title()} {parts[2]}" if len(parts) >= 3 else key
            arrow = {"up": "↑", "down": "↓", "flat": "↔"}.get(m["direction"], "?")
            print(
                f"  {player_stat} [{m['source']}]: "
                f"{m['prior_line']:.1f} {arrow} {m['current_line']:.1f} "
                f"({m['prob_shift_pp']:+.1f}pp)"
            )
