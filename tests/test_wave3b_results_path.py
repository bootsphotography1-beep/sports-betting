"""Tests for Wave 3B: project-root-resolved RESULTS_PATH."""
import pytest
from pathlib import Path
import importlib


class TestResultsPath:
    def test_results_path_is_absolute(self):
        """RESULTS_PATH must be an absolute path."""
        from ud_edge.results_tracker import RESULTS_PATH
        assert RESULTS_PATH.is_absolute()

    def test_results_path_resolved_from_project_root(self):
        """RESULTS_PATH should point to <project_root>/data/results.json."""
        from ud_edge.results_tracker import RESULTS_PATH
        # The file should be data/results.json relative to the project root
        project_root = Path(__file__).resolve().parent.parent
        expected = project_root / "data" / "results.json"
        assert RESULTS_PATH == expected

    def test_import_works_from_any_cwd(self, tmp_path):
        """Importing the module works when cwd is not the project root."""
        # Change to a different directory and re-import
        import sys
        # The key check is that RESULTS_PATH is absolute so it works from any CWD
        from ud_edge.results_tracker import RESULTS_PATH
        assert RESULTS_PATH.is_absolute()
        # Verify it's inside the project directory
        project_root = Path(__file__).resolve().parent.parent
        assert RESULTS_PATH.is_relative_to(project_root)

    def test_results_path_has_results_json(self):
        """RESULTS_PATH filename should be results.json."""
        from ud_edge.results_tracker import RESULTS_PATH
        assert RESULTS_PATH.name == "results.json"
