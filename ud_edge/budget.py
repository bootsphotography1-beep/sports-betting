"""PropLine daily API budget tracker (default 5,000 calls/day).

Tracks UTC-day usage on disk so the poller can stay under the mathematical
limit while concentrating spend near tip-off.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DAILY_LIMIT = 5000
DEFAULT_RESERVE_FRAC = 0.10  # held back for confirm-burst after alerts


@dataclass
class BudgetSnapshot:
    day: str
    used: int
    limit: int
    reserve: int
    remaining_scheduled: int
    remaining_total: int

    @property
    def exhausted(self) -> bool:
        return self.remaining_total <= 0


class CallBudget:
    """Persistent per-UTC-day counter for PropLine HTTP calls."""

    def __init__(
        self,
        path: Path = Path("data/propline_budget.json"),
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        reserve_frac: float = DEFAULT_RESERVE_FRAC,
    ):
        self.path = path
        self.daily_limit = daily_limit
        self.reserve_frac = reserve_frac
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict:
        if not self.path.exists():
            return {"day": self._today(), "used": 0}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {"day": self._today(), "used": 0}
        if data.get("day") != self._today():
            data = {"day": self._today(), "used": 0}
        return data

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2))

    def snapshot(self) -> BudgetSnapshot:
        data = self._load()
        used = int(data.get("used", 0))
        reserve = int(self.daily_limit * self.reserve_frac)
        scheduled_cap = self.daily_limit - reserve
        remaining_total = max(0, self.daily_limit - used)
        # Scheduled pool can dip into reserve only via record(use_reserve=True)
        remaining_scheduled = max(0, min(scheduled_cap - used, remaining_total))
        if used > scheduled_cap:
            remaining_scheduled = 0
        return BudgetSnapshot(
            day=data["day"],
            used=used,
            limit=self.daily_limit,
            reserve=reserve,
            remaining_scheduled=remaining_scheduled,
            remaining_total=remaining_total,
        )

    def can_spend(self, n: int = 1, *, use_reserve: bool = False) -> bool:
        snap = self.snapshot()
        if use_reserve:
            return snap.remaining_total >= n
        # Prefer scheduled pool; allow reserve only if explicitly requested
        return snap.remaining_scheduled >= n

    def record(self, n: int = 1, *, use_reserve: bool = False) -> BudgetSnapshot:
        data = self._load()
        data["used"] = int(data.get("used", 0)) + n
        data["day"] = self._today()
        data["last_call_at"] = datetime.now(timezone.utc).isoformat()
        if use_reserve:
            data["reserve_used"] = int(data.get("reserve_used", 0)) + n
        self._save(data)
        return self.snapshot()


def compute_poll_interval_seconds(
    *,
    nearest_tip_minutes: float | None,
    remaining_scheduled: int,
    seconds_left_in_utc_day: float,
    min_interval: float = 45.0,
    max_interval: float = 1200.0,
) -> float:
    """Adaptive interval that densifies near tip-off and respects budget.

    Urgency bands (minutes to nearest tip, signed: negative = already started):
      [-20, 90]   → 45s   (last-minute / steam window)
      (90, 240]   → 180s  (pre-game build)
      (240, 720]  → 600s  (slate watch)
      else / none → 900s  (quiet)

    Then floor by budget: cannot poll faster than
      seconds_left / max(remaining_scheduled, 1)
    so we never burn the day early.
    """
    if nearest_tip_minutes is None:
        urgency_interval = 900.0
    else:
        m = nearest_tip_minutes
        if -20 <= m <= 90:
            urgency_interval = 45.0
        elif m <= 240:
            urgency_interval = 180.0
        elif m <= 720:
            urgency_interval = 600.0
        else:
            urgency_interval = 900.0

    budget_floor = seconds_left_in_utc_day / max(remaining_scheduled, 1)
    # If we still have lots of budget, don't idle forever — use urgency.
    # If budget is tight, budget_floor stretches the interval.
    interval = max(urgency_interval, budget_floor)
    return float(min(max_interval, max(min_interval, interval)))


def seconds_left_in_utc_day(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    return max(1.0, (end - now).total_seconds())
