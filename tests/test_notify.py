"""Tests for ud_edge.notify."""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock


import ud_edge.notify as notify_module


def _patch_paths(tmp_path):
    notify_module.ALERT_STATE = tmp_path / "alert_state.json"
    notify_module.ALERTS_LOG = tmp_path / "alerts.jsonl"


class TestShouldAlertDedup:
    """test_should_alert_dedupes_within_30_min."""

    def test_should_alert_dedupes_within_30_min(self, tmp_path):
        _patch_paths(tmp_path)
        key = "test_key_123"
        notify_module.mark_alerted(key, delta_pp=5.0, line_value=140.5)
        # Immediate re-alert should be suppressed
        result = notify_module.should_alert(
            key, delta_pp=5.0, line_value=140.5, cooldown_minutes=25.0
        )
        assert result is False

    def test_should_alert_accepts_larger_delta_when_line_moves(self, tmp_path):
        _patch_paths(tmp_path)
        key = "test_key_line_move"
        # First alert
        notify_module.mark_alerted(key, delta_pp=3.0, line_value=140.5)
        # Same line but larger delta should trigger
        result = notify_module.should_alert(
            key, delta_pp=7.0, line_value=140.5, cooldown_minutes=25.0, improve_pp=1.0
        )
        assert result is True


class TestNotifyOpportunity:
    """test_notify_opportunity_calls_*."""

    def test_notify_opportunity_calls_slack_when_configured(self, tmp_path):
        _patch_paths(tmp_path)
        with patch.object(notify_module, "send_slack", return_value=True) as mock_slack:
            fired = notify_module.notify_opportunity(
                player="Aaron Judge",
                pick="Over 2.5 HR",
                match="NYY vs BOS",
                ud_pct=0.45,
                sharp_pct=0.52,
                delta_pp=7.0,
                sharp_book="Pinnacle",
                tips_in_min=45.0,
                alert_key="test_slack_1",
                line_value=140.5,
            )
            assert "slack" in fired
            mock_slack.assert_called_once()

    def test_notify_opportunity_calls_telegram_when_configured(self, tmp_path):
        _patch_paths(tmp_path)
        with patch.object(notify_module, "send_telegram", return_value=True) as mock_tg:
            fired = notify_module.notify_opportunity(
                player="Aaron Judge",
                pick="Over 2.5 HR",
                match="NYY vs BOS",
                ud_pct=0.45,
                sharp_pct=0.52,
                delta_pp=7.0,
                sharp_book="Pinnacle",
                tips_in_min=45.0,
                alert_key="test_tg_1",
                line_value=140.5,
            )
            assert "telegram" in fired
            mock_tg.assert_called_once()

    def test_notify_opportunity_calls_ntfy_when_configured(self, tmp_path):
        _patch_paths(tmp_path)
        with patch.object(notify_module, "send_ntfy", return_value=True) as mock_ntfy:
            fired = notify_module.notify_opportunity(
                player="Aaron Judge",
                pick="Over 2.5 HR",
                match="NYY vs BOS",
                ud_pct=0.45,
                sharp_pct=0.52,
                delta_pp=7.0,
                sharp_book="Pinnacle",
                tips_in_min=45.0,
                alert_key="test_ntfy_1",
                line_value=140.5,
            )
            assert "ntfy" in fired
            mock_ntfy.assert_called_once()

    def test_notify_opportunity_returns_false_when_no_channel(self, tmp_path):
        _patch_paths(tmp_path)
        with patch.object(notify_module, "send_ntfy", return_value=False), \
             patch.object(notify_module, "send_slack", return_value=False), \
             patch.object(notify_module, "send_telegram", return_value=False), \
             patch.object(notify_module, "send_discord", return_value=False), \
             patch.object(notify_module, "send_generic_webhook", return_value=False):
            fired = notify_module.notify_opportunity(
                player="Aaron Judge",
                pick="Over 2.5 HR",
                match="NYY vs BOS",
                ud_pct=0.45,
                sharp_pct=0.52,
                delta_pp=7.0,
                sharp_book="Pinnacle",
                tips_in_min=45.0,
                alert_key="test_no_channel",
                line_value=140.5,
            )
            assert fired == []

    def test_notify_opportunity_logs_alert(self, tmp_path):
        _patch_paths(tmp_path)
        with patch.object(notify_module, "send_ntfy", return_value=False):
            notify_module.notify_opportunity(
                player="Aaron Judge",
                pick="Over 2.5 HR",
                match="NYY vs BOS",
                ud_pct=0.45,
                sharp_pct=0.52,
                delta_pp=7.0,
                sharp_book="Pinnacle",
                tips_in_min=45.0,
                alert_key="test_log_1",
                line_value=140.5,
            )
        log_path = tmp_path / "alerts.jsonl"
        assert log_path.exists()
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["player"] == "Aaron Judge"
        assert entry["delta_pp"] == 7.0


class TestSendTelegram:
    """Telegram channel implementation."""

    def test_send_telegram_posts_to_correct_url(self, tmp_path):
        _patch_paths(tmp_path)
        token = "123456:ABC-DEF"
        chat_id = "-1001234567890"
        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            result = notify_module.send_telegram(
                "Test Title",
                "Test body",
                bot_token=token,
                chat_id=chat_id,
            )
            assert result is True
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            expected_url = f"https://api.telegram.org/bot{token}/sendMessage"
            assert call_args[0][0] == expected_url
            payload = call_args.kwargs["json"]
            assert payload["chat_id"] == chat_id
            assert "Test Title" in payload["text"]

    def test_send_telegram_returns_false_when_no_token(self, tmp_path):
        _patch_paths(tmp_path)
        result = notify_module.send_telegram("title", "body", bot_token="", chat_id="123")
        assert result is False

    def test_send_telegram_returns_false_when_no_chat_id(self, tmp_path):
        _patch_paths(tmp_path)
        result = notify_module.send_telegram("title", "body", bot_token="tok", chat_id="")
        assert result is False
