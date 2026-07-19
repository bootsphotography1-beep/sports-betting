"""Push notifications when a mispricing opportunity is detected.

Channels (any combination; all configured channels fire):
  1. NTFY_TOPIC          → https://ntfy.sh/<topic>  (phone app, zero signup)
  2. SLACK_WEBHOOK_URL   → Slack incoming webhook
  3. DISCORD_WEBHOOK_URL → Discord webhook
  4. ALERT_WEBHOOK_URL   → generic JSON POST {text, title, ...}

Every alert is also appended to data/alerts.jsonl.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests


ALERTS_LOG = Path("data/alerts.jsonl")
ALERT_STATE = Path("data/alert_state.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_alert_log(payload: dict) -> None:
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERTS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def _load_state() -> dict:
    if not ALERT_STATE.exists():
        return {}
    try:
        return json.loads(ALERT_STATE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
    ALERT_STATE.write_text(json.dumps(state, indent=2))


def should_alert(
    alert_key: str,
    *,
    delta_pp: float,
    line_value: float,
    cooldown_minutes: float = 25.0,
    improve_pp: float = 1.0,
) -> bool:
    """Dedup: alert if new, line moved, or edge improved by ≥ improve_pp after cooldown."""
    state = _load_state()
    prev = state.get(alert_key)
    if prev is None:
        return True
    try:
        prev_at = datetime.fromisoformat(prev["sent_at"])
    except Exception:
        return True
    age_min = (datetime.now(timezone.utc) - prev_at).total_seconds() / 60.0
    if abs(float(prev.get("line_value", line_value)) - line_value) >= 0.5:
        return True
    if age_min < cooldown_minutes:
        return False
    if delta_pp >= float(prev.get("delta_pp", 0)) + improve_pp:
        return True
    # Re-ping after cooldown even if flat — opportunity still live
    return age_min >= cooldown_minutes * 2


def mark_alerted(alert_key: str, *, delta_pp: float, line_value: float) -> None:
    state = _load_state()
    state[alert_key] = {
        "sent_at": _now_iso(),
        "delta_pp": delta_pp,
        "line_value": line_value,
    }
    _save_state(state)


def format_opportunity_message(
    *,
    player: str,
    pick: str,
    match: str,
    ud_pct: float,
    sharp_pct: float,
    delta_pp: float,
    sharp_book: str,
    tips_in_min: Optional[float],
) -> tuple[str, str]:
    """Return (title, body). Title is ASCII-safe for HTTP headers (ntfy)."""
    tips = f"{tips_in_min:.0f}m" if tips_in_min is not None else "?"
    title = f"UD EDGE | {player} | {delta_pp:+.1f}pp"
    body = (
        f"PLACE ON -> Underdog Fantasy\n"
        f"{player} — {pick}\n"
        f"{match} · tips in {tips}\n"
        f"UD {ud_pct:.1%} -> {sharp_book} {sharp_pct:.1%} ({delta_pp:+.1f}pp)\n"
        f"Signal only from {sharp_book}. Open Underdog to enter the slip."
    )
    return title, body


def send_ntfy(title: str, body: str, topic: Optional[str] = None) -> bool:
    topic = (topic or os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic:
        return False
    url = f"https://ntfy.sh/{topic}"
    # ntfy header values must be latin-1; keep title ASCII
    safe_title = title.encode("ascii", "replace").decode("ascii")
    r = requests.post(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": safe_title,
            "Priority": "high",
            "Tags": "chart_with_upwards_trend,baseball",
            "Click": "https://underdogfantasy.com",
        },
        timeout=15,
    )
    r.raise_for_status()
    return True


def send_slack(title: str, body: str, webhook: Optional[str] = None) -> bool:
    webhook = (webhook or os.environ.get("SLACK_WEBHOOK_URL") or "").strip()
    if not webhook:
        return False
    text = f"*{title}*\n```{body}```"
    r = requests.post(webhook, json={"text": text}, timeout=15)
    r.raise_for_status()
    return True


def send_discord(title: str, body: str, webhook: Optional[str] = None) -> bool:
    webhook = (webhook or os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if not webhook:
        return False
    content = f"**{title}**\n```\n{body}\n```"
    r = requests.post(webhook, json={"content": content}, timeout=15)
    r.raise_for_status()
    return True


def send_generic_webhook(title: str, body: str, webhook: Optional[str] = None) -> bool:
    webhook = (webhook or os.environ.get("ALERT_WEBHOOK_URL") or "").strip()
    if not webhook:
        return False
    r = requests.post(
        webhook,
        json={"title": title, "text": body, "app": "Underdog Fantasy"},
        timeout=15,
    )
    r.raise_for_status()
    return True


def notify_opportunity(
    *,
    player: str,
    pick: str,
    match: str,
    ud_pct: float,
    sharp_pct: float,
    delta_pp: float,
    sharp_book: str,
    tips_in_min: Optional[float],
    alert_key: str,
    line_value: float,
) -> list[str]:
    """Send to all configured channels. Returns list of channel names that fired."""
    title, body = format_opportunity_message(
        player=player, pick=pick, match=match,
        ud_pct=ud_pct, sharp_pct=sharp_pct, delta_pp=delta_pp,
        sharp_book=sharp_book, tips_in_min=tips_in_min,
    )
    fired: list[str] = []
    errors: list[str] = []

    for name, fn in (
        ("ntfy", lambda: send_ntfy(title, body)),
        ("slack", lambda: send_slack(title, body)),
        ("discord", lambda: send_discord(title, body)),
        ("webhook", lambda: send_generic_webhook(title, body)),
    ):
        try:
            if fn():
                fired.append(name)
        except Exception as e:
            errors.append(f"{name}: {e}")

    payload = {
        "at": _now_iso(),
        "alert_key": alert_key,
        "title": title,
        "body": body,
        "channels": fired,
        "errors": errors,
        "place_on": "Underdog Fantasy",
        "delta_pp": delta_pp,
        "player": player,
        "pick": pick,
    }
    append_alert_log(payload)
    if fired:
        mark_alerted(alert_key, delta_pp=delta_pp, line_value=line_value)
    print(f"[notify] {title} → {fired or ['log-only']}"
          + (f" errors={errors}" if errors else ""))
    return fired


def configured_channels() -> list[str]:
    ch = []
    if os.environ.get("NTFY_TOPIC", "").strip():
        ch.append(f"ntfy:{os.environ['NTFY_TOPIC'].strip()}")
    if os.environ.get("SLACK_WEBHOOK_URL", "").strip():
        ch.append("slack")
    if os.environ.get("DISCORD_WEBHOOK_URL", "").strip():
        ch.append("discord")
    if os.environ.get("ALERT_WEBHOOK_URL", "").strip():
        ch.append("webhook")
    return ch
