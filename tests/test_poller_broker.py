"""Wire the multi-account broker into the poller.

Strategy (Fin's spec, 2026-07-20):
  - run_poll_loop() uses a Broker (not a single CallBudget) to route each
    cycle's calls against the correct PropLine account.
  - When the active account can_spend(1) but its reserve pool is empty,
    we still poll (urgency wins on the live PropLine call) and then record
    against the SAME account (no auto-flip mid-cycle).
  - The poller sleeps when BrokerExhausted is raised.
  - All other behavior (urgency-banded interval, confirm-burst, alerts) is
    preserved.

Tests here run offline: the PropLine HTTP call is mocked.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ud_edge.broker import Account, Broker, BrokerExhausted


def _acct(tmp_path: Path, name: str, limit: int) -> Account:
    return Account(
        name=name,
        key=f"k-{name}",
        daily_limit=limit,
        state_path=tmp_path / f"{name}.json",
    )


def _broker(tmp_path: Path) -> Broker:
    return Broker(accounts=[
        _acct(tmp_path, "primary", 5000),
        _acct(tmp_path, "free1", 1000),
    ])


def test_run_poll_loop_uses_first_account_by_default(tmp_path, monkeypatch, capsys):
    """The poller must run in broker mode when given a Broker with
    multiple accounts, and use the lowest-indexed account first.

    Asserts:
      - run_poll_loop enters broker mode
      - The first account's key is what compare_fantasy_vs_sharp sees
      - Recording the cycle (propline_calls=0 in our stub) doesn't
        double-charge
    """
    from ud_edge import poller

    b = _broker(tmp_path)

    # Patch compare_fantasy_vs_sharp to return a tiny payload with 0 cycles
    fake_payload = {
        "fetched_at": "2026-07-20T00:00:00Z",
        "entry_type": "6-flex",
        "min_true_prob": 0.55, "min_edge_pp": 0.5,
        "full_game_only": True, "mispriced_only": False,
        "safety_status": {}, "totals": {},
        "fantasy_meta": {}, "sharp_meta": {"propline_calls": 0, "sources": []},
        "sports": [], "lineups": [], "flat": [],
        "copy_all": {}, "methodology": {},
    }
    seen_keys: list[str] = []

    def capture_cfvs(**kw):
        # Read PROPLINE_API_KEY at the moment of the call.
        seen_keys.append(os.environ.get("PROPLINE_API_KEY", ""))
        return (fake_payload, [])

    monkeypatch.setattr(poller, "compare_fantasy_vs_sharp", capture_cfvs)
    monkeypatch.setattr(poller, "configured_channels", lambda: [])
    monkeypatch.setattr(poller, "notify_opportunity", lambda **kw: True)
    monkeypatch.setattr(poller, "should_alert", lambda *a, **k: False)

    # Stop the loop after the FIRST cycle completes. The poller sleeps
    # at the end of the cycle body before the next iteration; raising
    # SystemExit there is harmless and lets the test finish.
    counter = {"n": 0}

    def sleep_then_stop(s):
        counter["n"] += 1
        raise SystemExit(0)

    monkeypatch.setattr(poller.time, "sleep", sleep_then_stop)

    try:
        poller.run_poll_loop(broker=b, once=True, cache_path=tmp_path)
    except SystemExit:
        pass  # expected: our sleep trap raised

    # The broker's first account.key must have been injected into
    # PROPLINE_API_KEY before compare_fantasy_vs_sharp was called.
    assert seen_keys, "compare_fantasy_vs_sharp was never called"
    assert b.accounts[0].key in seen_keys[0], (
        f"expected the primary key {b.accounts[0].key!r} to be passed "
        f"to compare_fantasy_vs_sharp; saw {seen_keys[0]!r}"
    )
    # The broker never flipped on a 0-call cycle
    assert b.accounts[0].snapshot().used == 0
    assert b.accounts[1].snapshot().used == 0


def test_broker_route_returns_primary_when_first_account_has_budget(tmp_path):
    b = _broker(tmp_path)
    assert b.route().name == "primary"
    assert b.accounts[0].can_spend(1)


def test_broker_flips_to_secondary_after_primary_exhausted(tmp_path):
    b = _broker(tmp_path)
    # Spend the entire primary
    for _ in range(5000):
        b.accounts[0].record(1)
    # Now route must return free1
    assert b.route().name == "free1"


def test_broker_raises_exhausted_after_both_done(tmp_path):
    b = _broker(tmp_path)
    for _ in range(5000):
        b.accounts[0].record(1)
    for _ in range(1000):
        b.accounts[1].record(1)
    with pytest.raises(BrokerExhausted):
        b.route()


def test_record_charges_only_the_routed_account(tmp_path):
    b = _broker(tmp_path)
    a = b.route()
    b.record(n=40, account=a)
    # 40 calls charged against primary; free1 untouched
    assert b.accounts[0].snapshot().used == 40
    assert b.accounts[1].snapshot().used == 0


def test_broker_pool_snapshot_for_dashboard(tmp_path):
    b = _broker(tmp_path)
    b.accounts[0].record(150)
    snap = b.pool_snapshot()
    assert len(snap) == 2
    assert snap[0]["name"] == "primary"
    assert snap[0]["used"] == 150
    assert snap[0]["limit"] == 5000
    assert snap[1]["name"] == "free1"
    assert snap[1]["used"] == 0
    assert snap[1]["limit"] == 1000
    # Key hint should NEVER leak the full key
    assert "a2c7e0ed" not in str(snap) and "8f47da97" not in str(snap)
