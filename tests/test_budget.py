"""Budget math + alert dedup unit tests (no network)."""
from datetime import datetime, timezone

from ud_edge.budget import CallBudget, compute_poll_interval_seconds, seconds_left_in_utc_day
from ud_edge.notify import should_alert, mark_alerted, ALERT_STATE


def test_budget_resets_and_reserve(tmp_path):
    path = tmp_path / "budget.json"
    b = CallBudget(path=path, daily_limit=100, reserve_frac=0.10)
    snap = b.snapshot()
    assert snap.limit == 100
    assert snap.reserve == 10
    assert snap.remaining_scheduled == 90
    assert b.can_spend(90)
    assert not b.can_spend(91)
    b.record(90)
    assert b.can_spend(1, use_reserve=True)
    assert not b.can_spend(1, use_reserve=False)


def test_interval_densifies_near_tip():
    # Plenty of budget → urgency wins
    near = compute_poll_interval_seconds(
        nearest_tip_minutes=30,
        remaining_scheduled=4000,
        seconds_left_in_utc_day=80_000,
    )
    assert near == 45.0

    mid = compute_poll_interval_seconds(
        nearest_tip_minutes=120,
        remaining_scheduled=4000,
        seconds_left_in_utc_day=80_000,
    )
    assert mid == 180.0

    quiet = compute_poll_interval_seconds(
        nearest_tip_minutes=1000,
        remaining_scheduled=4000,
        seconds_left_in_utc_day=80_000,
    )
    assert quiet == 900.0


def test_interval_stretches_when_budget_tight():
    # 10 calls left, 10 hours left → floor 3600s, clamped to max 1200
    iv = compute_poll_interval_seconds(
        nearest_tip_minutes=30,  # wants 45s
        remaining_scheduled=10,
        seconds_left_in_utc_day=36_000,
        max_interval=1200.0,
    )
    assert iv == 1200.0


def test_should_alert_dedup(tmp_path, monkeypatch):
    state = tmp_path / "alert_state.json"
    monkeypatch.setattr("ud_edge.notify.ALERT_STATE", state)
    key = "p1|hits|higher|0.5"
    assert should_alert(key, delta_pp=2.0, line_value=0.5)
    mark_alerted(key, delta_pp=2.0, line_value=0.5)
    # Immediate re-alert blocked
    assert not should_alert(key, delta_pp=2.0, line_value=0.5, cooldown_minutes=25)
    # Line move triggers
    assert should_alert(key, delta_pp=2.0, line_value=1.5, cooldown_minutes=25)


def test_seconds_left_positive():
    assert seconds_left_in_utc_day(datetime(2026, 7, 19, 22, 0, tzinfo=timezone.utc)) > 0
