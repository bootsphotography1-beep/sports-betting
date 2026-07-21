"""Tests for the cron-only guard on --once (audit follow-up, 2026-07-21).

The user's spec: only the poller (--poll) wakes up on cron. Manual
`--once` runs should require an explicit opt-in via the
UD_EDGE_ALLOW_ONCE=1 env var, so we don't burn the daily PropLine
budget on accidental double-fires.
"""
from __future__ import annotations

import sys
from unittest.mock import patch



def test_once_blocked_when_env_var_unset(monkeypatch):
    """--once must refuse to run without UD_EDGE_ALLOW_ONCE=1."""
    import io
    from contextlib import redirect_stdout

    monkeypatch.delenv("UD_EDGE_ALLOW_ONCE", raising=False)
    monkeypatch.setattr(sys, "argv", ["ud-edge", "--once"])
    buf = io.StringIO()
    with patch("ud_edge.compare.compare_fantasy_vs_sharp") as cfvs, \
         redirect_stdout(buf):
        from ud_edge.__main__ import main
        rc = main()
    out = buf.getvalue()
    assert rc == 2, f"expected return 2 (blocked), got {rc}"
    cfvs.assert_not_called()
    assert "UD_EDGE_ALLOW_ONCE" in out
    assert "blocked" in out.lower() or "refusing" in out.lower() or "burn" in out.lower()


def test_once_allowed_when_env_var_set(monkeypatch):
    monkeypatch.setenv("UD_EDGE_ALLOW_ONCE", "1")
    monkeypatch.setattr(sys, "argv", ["ud-edge", "--once", "--dry-run"])
    # We don't want the real pipeline to run; just confirm we get PAST the guard.
    # Mock the downstream path so the test doesn't touch the network.
    with patch("ud_edge.__main__.run_once", return_value=0) as run_once:
        from ud_edge.__main__ import main
        rc = main()
    # The guard was cleared; run_once was called (or at least attempted to be).
    assert run_once.called or rc in (0, 1), f"guard not cleared; rc={rc}"


def test_poll_unaffected_by_guard(monkeypatch):
    """--poll is the long-running mode and MUST work without the env var."""
    monkeypatch.delenv("UD_EDGE_ALLOW_ONCE", raising=False)
    # Mock the poller so it doesn't actually start
    with patch("ud_edge.poller.run_poll_loop", return_value=0) as poll:
        monkeypatch.setattr(sys, "argv", ["ud-edge", "--poll"])
        from ud_edge.__main__ import main
        rc = main()
    # The poller should have been entered
    assert poll.called, "--poll was not entered even though UD_EDGE_ALLOW_ONCE was unset"
    # And it should not have been blocked
    assert rc == 0


def test_serve_unaffected_by_guard():
    """The dashboard is a separate process; the guard is about API-burning --once only.

    We don't start uvicorn (we don't have to). We assert the guard
    marker appears AFTER the --serve early-exit so the dashboard path
    is never blocked.
    """
    src = open("ud_edge/__main__.py", encoding="utf-8").read()
    assert "UD_EDGE_ALLOW_ONCE" in src
    # Find the byte offsets
    serve_pos = src.index("if args.serve:")
    guard_pos = src.index('os.environ.get("UD_EDGE_ALLOW_ONCE"')
    assert serve_pos < guard_pos, (
        f"--serve early-exit (offset {serve_pos}) should come BEFORE the "
        f"UD_EDGE_ALLOW_ONCE guard (offset {guard_pos})"
    )


def test_self_test_unaffected_by_guard(monkeypatch):
    """--self-test does no I/O; guard must not block it."""
    monkeypatch.delenv("UD_EDGE_ALLOW_ONCE", raising=False)
    monkeypatch.setattr(sys, "argv", ["ud-edge", "--self-test"])
    from ud_edge.__main__ import main
    rc = main()
    # self-test should pass; if the guard blocked it, rc would be 2
    assert rc == 0
