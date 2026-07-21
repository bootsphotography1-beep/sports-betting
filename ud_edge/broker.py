"""Multi-account API-call broker for the Edge Board.

PropLine-only (2026-07-20 spec, 2 keys):
  - 1x 5000-call/day account + 1x 1000-call/day account.
  - Begin-of-UTC-day order: 5000 first, 1000 second.
  - route() returns the lowest-indexed account that still has spend room.
  - record() charges whichever account was routed.
  - All accounts reset at UTC midnight (per-account CallBudget handles it).

Backward-compat: if the new env var (e.g. PROPLINE_ACCOUNTS) is absent and
only the legacy single key (PROPLINE_API_KEY) is set, the broker is built
with one 5000-call account, so existing single-key operators do not need to
change anything to keep working.

This module is a thin wrapper over the existing CallBudget (ud_edge/budget.py).
We do NOT replace CallBudget — we compose it. Per-account CallBudget gives us
the day rollover, the reserve pool, the JSON-on-disk state, and the
math.ceil((limit - used) / hours_left) budget-floor that compute_poll_interval_seconds
already consumes for adaptive interval.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ud_edge.budget import (
    CallBudget,
    BudgetSnapshot,
    DEFAULT_DAILY_LIMIT,
    DEFAULT_RESERVE_FRAC,
)


# ── Exceptions ──────────────────────────────────────────────────────────


class BrokerExhausted(RuntimeError):
    """All accounts in the pool are spent for this UTC day."""


# ── Account ─────────────────────────────────────────────────────────────


@dataclass
class Account:
    """One API key + its daily call quota.

    Wraps a per-key CallBudget for persistence + day rollover. The
    per-account key is not in the budget state file; it lives only in
    process memory.
    """
    name: str
    key: str
    daily_limit: int
    state_path: Path

    def __post_init__(self) -> None:
        # Default reserve follows the project-wide constant (10%).
        self._budget = CallBudget(
            path=self.state_path,
            daily_limit=self.daily_limit,
            reserve_frac=DEFAULT_RESERVE_FRAC,
        )

    def snapshot(self) -> BudgetSnapshot:
        return self._budget.snapshot()

    def can_spend(self, n: int = 1, *, use_reserve: bool = False) -> bool:
        return self._budget.can_spend(n, use_reserve=use_reserve)

    def record(self, n: int = 1, *, use_reserve: bool = False) -> BudgetSnapshot:
        return self._budget.record(n, use_reserve=use_reserve)


# ── Env-var parsing ─────────────────────────────────────────────────────


_NAME_LIMIT_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*:\s*(\d+)\s*$")


def parse_accounts_env(
    env_var: str,
    raw: str,
) -> list[AccountSpec]:
    """Parse "name:limit,name:limit" into a list of AccountSpec (no key).

    The key is supplied separately by `broker_from_env` via the
    `key_env_pattern` template.

    Raises:
        ValueError: on malformed entries (missing colon, non-integer limit,
            non-positive limit). The error message references the env var
            name so the operator knows where to fix.
    """
    if not raw or not raw.strip():
        raise ValueError(f"{env_var} is empty — expected 'name:limit,name:limit'")

    out: list[AccountSpec] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        m = _NAME_LIMIT_RE.match(piece)
        if not m:
            # Either no colon at all, or the limit side is non-digit
            if ":" not in piece:
                raise ValueError(
                    f"{env_var}: bad entry {piece!r} — expected 'name:limit' "
                    f"(e.g. primary:5000)"
                )
            raise ValueError(
                f"{env_var}: limit must be a positive integer in {piece!r} "
                f"(e.g. primary:5000)"
            )
        name, limit_s = m.group(1), m.group(2)
        if not name:
            raise ValueError(f"{env_var}: empty account name in {piece!r}")
        try:
            limit = int(limit_s)
        except ValueError:
            # _NAME_LIMIT_RE already enforced \d+; kept for defense-in-depth.
            raise ValueError(
                f"{env_var}: limit must be an integer, got {limit_s!r}"
            ) from None
        if limit <= 0:
            raise ValueError(
                f"{env_var}: limit must be positive, got {limit} for {name!r}"
            )
        out.append(AccountSpec(name=name, daily_limit=limit))
    if not out:
        raise ValueError(f"{env_var}: parsed to zero accounts")
    return out


@dataclass
class AccountSpec:
    """Name + limit from env (key is filled in by broker_from_env)."""
    name: str
    daily_limit: int


# ── Broker ──────────────────────────────────────────────────────────────


class Broker:
    """Routes calls across an ordered pool of API-key accounts.

    Routing rule (2026-07-20 spec, 2-key case):
      Begin-of-UTC-day order is the env-var declaration order.
      At any cycle, pick the lowest-indexed account that has spend room.
      Record against whichever account was picked; never rebalance mid-day.

    Rotation is "fail-soft":
      route() raises BrokerExhausted when no account can_spend(1).
      The poller treats BrokerExhausted as a "sleep until UTC rollover" signal.
    """

    def __init__(self, accounts: list[Account]):
        if not accounts:
            raise ValueError("Broker needs at least one account")
        self.accounts: list[Account] = list(accounts)

    def route(self) -> Account:
        """Return the lowest-indexed account that can spend 1 call."""
        for a in self.accounts:
            if a.can_spend(1):
                return a
        raise BrokerExhausted(
            f"All {len(self.accounts)} account(s) exhausted for UTC day "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        )

    def record(self, n: int, *, account: Account, use_reserve: bool = False) -> None:
        """Charge n calls to the given account.

        We do NOT call route() here — the caller is expected to call
        route() once per cycle and pass the result back. This keeps
        spend attribution deterministic (no surprise rebalance mid-cycle).
        """
        if account not in self.accounts:
            raise ValueError(
                f"record() got an account not in this broker: {account.name!r}"
            )
        account.record(n, use_reserve=use_reserve)

    def can_spend_any(self, n: int = 1) -> bool:
        return any(a.can_spend(n) for a in self.accounts)

    def pool_snapshot(self) -> list[dict]:
        """One dict per account, in declaration order. Used by /api/budget."""
        out: list[dict] = []
        for a in self.accounts:
            s = a.snapshot()
            out.append({
                "name": a.name,
                "key_hint": _key_hint(a.key),
                "limit": s.limit,
                "used": s.used,
                "remaining_scheduled": s.remaining_scheduled,
                "remaining_total": s.remaining_total,
                "exhausted": s.exhausted,
                "day": s.day,
            })
        return out


def _key_hint(key: str) -> str:
    """Return a non-secret display hint of the key (first 4 + '…' + last 2).

    Used by /api/budget so the operator can see WHICH key the broker is
    currently routing to, without leaking the full secret. Full key never
    leaves the process.
    """
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}…{key[-2:]}"


# ── Factory: env var → Broker ──────────────────────────────────────────


def broker_from_env(
    *,
    env_var: str,
    key_env_pattern: str,
    state_dir: Path,
    legacy_single_key_env: str = "",
    legacy_single_key_default_limit: int = DEFAULT_DAILY_LIMIT,
) -> Broker:
    """Build a Broker from env.

    New style (preferred when present):
        ENV_VAR="primary:5000,free1:1000"
        KEY_ENV_PATTERN="<base>_KEY_{name}"  (e.g. PROPLINE_KEY_PRIMARY)
        For each spec, the key is read from <key_env_pattern>.format(name=spec.name).

    Legacy fallback (used when ENV_VAR is unset AND legacy_single_key_env is set):
        One account named "primary" with limit=legacy_single_key_default_limit.
        Keeps single-key operators working without config changes.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    raw = os.environ.get(env_var, "").strip()
    if raw:
        specs = parse_accounts_env(env_var, raw)
        accounts: list[Account] = []
        for spec in specs:
            key_env = key_env_pattern.format(name=spec.name)
            key = os.environ.get(key_env, "").strip()
            if not key:
                raise ValueError(
                    f"{env_var} declared account {spec.name!r} but "
                    f"{key_env} is unset or empty"
                )
            accounts.append(Account(
                name=spec.name,
                key=key,
                daily_limit=spec.daily_limit,
                state_path=state_dir / f"{env_var.lower()}_{spec.name}.json",
            ))
        return Broker(accounts=accounts)

    if legacy_single_key_env:
        legacy_key = os.environ.get(legacy_single_key_env, "").strip()
        if legacy_key:
            return Broker(accounts=[Account(
                name="primary",
                key=legacy_key,
                daily_limit=legacy_single_key_default_limit,
                state_path=state_dir / f"{env_var.lower()}_primary.json",
            )])
        # Caller passed a legacy_single_key_env but the env is empty/missing.
        # Fall through to the generic error.

    raise ValueError(
        f"broker_from_env: neither {env_var} nor "
        f"{legacy_single_key_env or '<no legacy env>'} is set"
    )


# ── Public env-var names (so callers don't have to guess) ──────────────


PROPLINE_BROKER = dict(
    env_var="PROPLINE_ACCOUNTS",
    key_env_pattern="PROPLINE_KEY_{name}",
    legacy_single_key_env="PROPLINE_API_KEY",
    legacy_single_key_default_limit=DEFAULT_DAILY_LIMIT,
)
