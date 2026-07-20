"""Tests for Wave 3B: CSV diagnostics in pp_clipboard."""
import csv
from pathlib import Path
from ud_edge.pp_clipboard import parse_prizepicks_csv


def write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


class TestCSVDiagnostics:
    def test_valid_row_parsed(self, tmp_path):
        """A single valid row yields one observation."""
        path = tmp_path / "valid.csv"
        write_csv(path, [{
            "player_name": "Jayson Tatum", "league": "NBA", "stat_type": "pts",
            "line": "27.5", "higher_decimal": "1.91", "lower_decimal": "1.91",
            "event_title": "BOS@NYK", "scheduled_at": "2026-01-01",
        }])
        obs, diag = parse_prizepicks_csv(path, strict=False)
        assert len(obs) == 1
        assert obs[0]["player_name"] == "Jayson Tatum"
        assert diag["parsed"] == 1
        assert diag["skipped_invalid"] == 0
        assert diag["skipped_missing_critical"] == 0

    def test_bad_numeric_line_skipped(self, tmp_path):
        """Row with non-numeric line value is skipped with counted in diagnostics."""
        path = tmp_path / "bad_line.csv"
        write_csv(path, [
            {
                "player_name": "Jayson Tatum", "league": "NBA", "stat_type": "pts",
                "line": "N/A", "higher_decimal": "1.91", "lower_decimal": "1.91",
                "event_title": "BOS@NYK", "scheduled_at": "2026-01-01",
            },
            {
                "player_name": "Luka Doncic", "league": "NBA", "stat_type": "pts",
                "line": "33.5", "higher_decimal": "1.91", "lower_decimal": "1.91",
                "event_title": "DAL@LAL", "scheduled_at": "2026-01-01",
            },
        ])
        obs, diag = parse_prizepicks_csv(path, strict=False)
        assert len(obs) == 1
        assert obs[0]["player_name"] == "Luka Doncic"
        assert diag["parsed"] == 1
        assert diag["skipped_invalid"] == 1
        assert diag["skipped_missing_critical"] == 0

    def test_missing_critical_field_skipped(self, tmp_path):
        """Row missing player_name is skipped and counted."""
        path = tmp_path / "missing.csv"
        write_csv(path, [
            {
                "player_name": "Jayson Tatum", "league": "NBA", "stat_type": "pts",
                "line": "27.5", "higher_decimal": "1.91", "lower_decimal": "1.91",
                "event_title": "BOS@NYK", "scheduled_at": "2026-01-01",
            },
            {
                "player_name": "", "league": "NBA", "stat_type": "pts",
                "line": "33.5", "higher_decimal": "1.91", "lower_decimal": "1.91",
                "event_title": "DAL@LAL", "scheduled_at": "2026-01-01",
            },
            {
                "player_name": "Luka Doncic", "league": "NBA", "stat_type": "",
                "line": "28.0", "higher_decimal": "1.91", "lower_decimal": "1.91",
                "event_title": "DAL@LAL", "scheduled_at": "2026-01-01",
            },
        ])
        obs, diag = parse_prizepicks_csv(path, strict=False)
        assert len(obs) == 1  # only the first valid row
        assert diag["parsed"] == 1
        assert diag["skipped_invalid"] == 0
        assert diag["skipped_missing_critical"] == 2

    def test_three_rows_mixed_diagnostics(self, tmp_path):
        """3 rows: 1 valid, 1 bad numeric, 1 missing critical → correct counts."""
        path = tmp_path / "mixed.csv"
        write_csv(path, [
            {
                "player_name": "Jayson Tatum", "league": "NBA", "stat_type": "pts",
                "line": "27.5", "higher_decimal": "1.91", "lower_decimal": "1.91",
                "event_title": "BOS@NYK", "scheduled_at": "2026-01-01",
            },
            {
                "player_name": "Luka Doncic", "league": "NBA", "stat_type": "reb",
                "line": "NOT_A_NUMBER", "higher_decimal": "1.91", "lower_decimal": "1.91",
                "event_title": "DAL@LAL", "scheduled_at": "2026-01-01",
            },
            {
                "player_name": "", "league": "NBA", "stat_type": "ast",
                "line": "8.5", "higher_decimal": "1.91", "lower_decimal": "1.91",
                "event_title": "DAL@LAL", "scheduled_at": "2026-01-01",
            },
        ])
        obs, diag = parse_prizepicks_csv(path, strict=False)
        assert len(obs) == 1
        assert diag["parsed"] == 1
        assert diag["skipped_invalid"] == 1   # bad numeric
        assert diag["skipped_missing_critical"] == 1  # missing player_name

    def test_backward_compat_list_return(self, tmp_path):
        """When strict=False, still returns (observations, diagnostics) tuple."""
        path = tmp_path / "compat.csv"
        write_csv(path, [{
            "player_name": "Jayson Tatum", "league": "NBA", "stat_type": "pts",
            "line": "27.5", "higher_decimal": "1.91", "lower_decimal": "1.91",
            "event_title": "BOS@NYK", "scheduled_at": "2026-01-01",
        }])
        result = parse_prizepicks_csv(path, strict=False)
        assert isinstance(result, tuple)
        assert len(result) == 2
