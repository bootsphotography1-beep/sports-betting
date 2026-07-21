"""Tests for the multi-account API-call broker (PropLine, 2 keys).

Strategy (Fin's spec, 2026-07-20):
  - One 5000-call/day account + one 1000-call/day account.
  - Begin-of-UTC-day order: 5000 first, 1000 second.
  - At any cycle, pick the LOWEST-INDEXED account that still has budget.
  - Record against whichever account was picked; never rebalance mid-day.
  - All accounts reset at UTC midnight (handled per-account by CallBudget).
  - PropLine-only (2 keys per the 2026-07-20 spec).

These tests are offline (no network, no clock dependency beyond the broker
reading UTC date from `datetime.now()`).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ud_edge.broker import (
    Account,
    Broker,
    BrokerExhausted,
    parse_accounts_env,
    broker_from_env,
)


# ── Account model ────────────────────────────────────────────────────────


def test_account_uses_provided_daily_limit(tmp_path: Path):
    a = Account(name="primary", key="k", daily_limit=5000, state_path=tmp_path / "a.json")
    assert a.snapshot().limit == 5000
    assert a.snapshot().used == 0


def test_account_record_persists_to_disk(tmp_path: Path):
    a = Account(name="primary", key="k", daily_limit=100, state_path=tmp_path / "a.json")
    a.record(30)
    assert a.snapshot().used == 30

    # Reload from disk and confirm the count survives
    a2 = Account(name="primary", key="k", daily_limit=100, state_path=tmp_path / "a.json")
    assert a2.snapshot().used == 30


def test_account_resets_when_day_rolls_over(tmp_path: Path):
    """State file from a prior day must not poison the new day."""
    state = tmp_path / "a.json"
    state.write_text(json.dumps({"day": "2026-01-01", "used": 9999}))
    a = Account(name="primary", key="k", daily_limit=100, state_path=state)
    snap = a.snapshot()
    assert snap.day != "2026-01-01"
    assert snap.used == 0


# ── Broker rotation (PropLine 5000 + 1000) ──────────────────────────────


def _broker(tmp_path: Path, limits: list[int]) -> Broker:
    accounts = [
        Account(
            name=f"acct{i}",
            key=f"key{i}",
            daily_limit=lim,
            state_path=tmp_path / f"acct{i}.json",
        )
        for i, lim in enumerate(limits)
    ]
    return Broker(accounts=accounts)


def test_broker_picks_lowest_index_with_budget(tmp_path: Path):
    """Spec: 5000-key first, then 1000-key. Always pick lowest-indexed account that can_spend(1)."""
    b = _broker(tmp_path, [5000, 1000])
    assert b.route().name == "acct0"


def test_broker_flips_to_secondary_when_primary_exhausted(tmp_path: Path):
    b = _broker(tmp_path, [5000, 1000])
    # Exhaust primary (the 5000-key)
    primary = b.accounts[0]
    for _ in range(5000):
        primary.record(1)
    # Next route() must land on acct1 (the 1000-key)
    assert b.route().name == "acct1"


def test_broker_returns_exhausted_when_both_done(tmp_path: Path):
    b = _broker(tmp_path, [5000, 1000])
    b.accounts[0].record(5000)
    b.accounts[1].record(1000)
    with pytest.raises(BrokerExhausted):
        b.route()


def test_broker_records_against_routed_account(tmp_path: Path):
    """record(n) on the broker charges the account that route() returned."""
    b = _broker(tmp_path, [5000, 1000])
    a = b.route()
    b.record(3, account=a)
    assert a.snapshot().used == 3


def test_broker_can_spend_any_reflects_pool(tmp_path: Path):
    b = _broker(tmp_path, [5000, 1000])
    assert b.can_spend_any(1) is True
    b.accounts[0].record(5000)
    b.accounts[1].record(1000)
    assert b.can_spend_any(1) is False


def test_broker_pool_snapshot_reports_each_account(tmp_path: Path):
    b = _broker(tmp_path, [5000, 1000])
    b.accounts[0].record(100)
    snap = b.pool_snapshot()
    assert len(snap) == 2
    assert snap[0]["name"] == "acct0"
    assert snap[0]["used"] == 100
    assert snap[0]["limit"] == 5000
    assert snap[0]["exhausted"] is False
    assert snap[1]["name"] == "acct1"
    assert snap[1]["used"] == 0
    assert snap[1]["limit"] == 1000


# ── Env-var parsing (PropLine 2-key spec) ───────────────────────────────


def test_parse_accounts_env_handles_two_keys():
    raw = "primary:5000,free1:1000"
    parsed = parse_accounts_env("PROPLINE_ACCOUNTS", raw)
    assert [p.name for p in parsed] == ["primary", "free1"]
    assert [p.daily_limit for p in parsed] == [5000, 1000]


def test_parse_accounts_env_rejects_malformed():
    with pytest.raises(ValueError, match="name:limit"):
        parse_accounts_env("PROPLINE_ACCOUNTS", "primary")
    with pytest.raises(ValueError, match="integer"):
        parse_accounts_env("PROPLINE_ACCOUNTS", "primary:abc")
    with pytest.raises(ValueError, match="positive"):
        parse_accounts_env("PROPLINE_ACCOUNTS", "primary:0")


def test_parse_accounts_env_strips_whitespace():
    parsed = parse_accounts_env("PROPLINE_ACCOUNTS", "  primary : 5000 , free1 : 1000 ")
    assert [p.name for p in parsed] == ["primary", "free1"]


def test_broker_from_env_loads_propline_keys_per_account(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PROPLINE_ACCOUNTS", "primary:5000,free1:1000")
    monkeypatch.setenv("PROPLINE_KEY_PRIMARY", "key-A")
    monkeypatch.setenv("PROPLINE_KEY_FREE1", "key-B")
    state_dir = tmp_path / "state"
    b = broker_from_env(
        env_var="PROPLINE_ACCOUNTS",
        key_env_pattern="PROPLINE_KEY_{name}",
        state_dir=state_dir,
    )
    assert [a.name for a in b.accounts] == ["primary", "free1"]
    assert [a.key for a in b.accounts] == ["key-A", "key-B"]


def test_broker_from_env_falls_back_to_legacy_single_key(tmp_path: Path, monkeypatch):
    """Pre-rotation operators may only have PROPLINE_API_KEY set.

    Backward-compat: when the new env var is absent, treat the legacy single
    key as one account at the 5000-call default. When the new env var IS
    set, ignore the legacy single key (avoid double-charging)."""
    state_dir = tmp_path / "state"
    monkeypatch.delenv("PROPLINE_ACCOUNTS", raising=False)
    monkeypatch.setenv("PROPLINE_API_KEY", "legacy")
    b = broker_from_env(
        env_var="PROPLINE_ACCOUNTS",
        key_env_pattern="PROPLINE_KEY_{name}",
        legacy_single_key_env="PROPLINE_API_KEY",
        state_dir=state_dir,
    )
    assert len(b.accounts) == 1
    assert b.accounts[0].name == "primary"
    assert b.accounts[0].key == "legacy"
    assert b.accounts[0].daily_limit == 5000
