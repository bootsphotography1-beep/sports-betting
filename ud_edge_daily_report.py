"""Module shim: lets you run `python -m ud_edge_daily_report` while the
real implementation lives in scripts/ud_edge_daily_report.py.

Keeps the entrypoint discoverable (`python -m ...`) without putting
all the daily-report logic at the repo root.
"""
from scripts.ud_edge_daily_report import main

if __name__ == "__main__":
    import sys
    sys.exit(main())
