"""Tests for the notify_in_gateway gate on _kanban_notifier_watcher.

After the dispatch/notify split, the notifier watcher is gated by
``notify_in_gateway`` (which falls back to ``dispatch_in_gateway`` when
absent) rather than ``dispatch_in_gateway`` directly.  These tests verify:

- notify_in_gateway=false (explicit) skips before opening any board DB.
- HERMES_KANBAN_NOTIFY_IN_GATEWAY env var disables without config being true.
- notify_in_gateway=true with dispatch_in_gateway=false (the Sophie-shaped
  split) proceeds past the gate — the core behavior the split enables.
- notify absent + dispatch_in_gateway=true proceeds (backward-compat fallback).
- notify absent + dispatch_in_gateway=false skips (backward-compat fallback).
"""

import asyncio
from unittest.mock import MagicMock, patch

from gateway.config import Platform
from gateway.run import GatewayRunner


def _make_runner(with_adapter=False):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: MagicMock()} if with_adapter else {}
    runner._kanban_sub_fail_counts = {}
    return runner


def _fake_kanban_config(dispatch_in_gateway=True, notify_in_gateway=None):
    """Build a config dict matching the shape resolve_notify_in_gateway reads."""
    kanban = {"dispatch_in_gateway": dispatch_in_gateway}
    if notify_in_gateway is not None:
        kanban["notify_in_gateway"] = notify_in_gateway
    return {"kanban": kanban}


def test_notifier_watcher_skips_when_notify_explicitly_false():
    """notify_in_gateway=false returns before opening any board DB,
    even when dispatch_in_gateway=true."""
    runner = _make_runner()
    config = _fake_kanban_config(
        dispatch_in_gateway=True, notify_in_gateway=False
    )
    with patch("hermes_cli.config.load_config", return_value=config):
        with patch("hermes_cli.kanban_db.connect") as mock_connect:
            asyncio.run(runner._kanban_notifier_watcher())
    mock_connect.assert_not_called()


def test_notifier_watcher_env_override_disables(monkeypatch):
    """HERMES_KANBAN_NOTIFY_IN_GATEWAY=false disables even when config
    has notify_in_gateway=true."""
    runner = _make_runner()
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_IN_GATEWAY", "false")
    # Config has both flags true — env must win.
    config = _fake_kanban_config(
        dispatch_in_gateway=True, notify_in_gateway=True
    )
    with patch("hermes_cli.config.load_config", return_value=config):
        with patch("hermes_cli.kanban_db.connect") as mock_connect:
            asyncio.run(runner._kanban_notifier_watcher())
    mock_connect.assert_not_called()


def test_notifier_watcher_runs_when_notify_true_and_dispatch_false():
    """The Sophie-shaped split: dispatch_in_gateway=false but
    notify_in_gateway=true proceeds past the gate to the board fan-out."""
    runner = _make_runner(with_adapter=True)
    config = _fake_kanban_config(
        dispatch_in_gateway=False, notify_in_gateway=True
    )
    past_gate = []
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        # Stop after the initial delay + first per-interval sleep so the loop
        # body runs exactly once.
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import hermes_cli.kanban_db as _kb

    with patch("hermes_cli.config.load_config", return_value=config):
        with patch.object(
            _kb, "list_boards",
            side_effect=lambda *a, **kw: past_gate.append(True) or [],
        ):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                with patch("asyncio.to_thread", side_effect=fake_to_thread):
                    asyncio.run(runner._kanban_notifier_watcher())

    assert past_gate, (
        "list_boards should be called when notify_in_gateway=true "
        "even if dispatch_in_gateway=false"
    )


def test_notifier_watcher_falls_back_to_dispatch_when_notify_absent_true():
    """notify_in_gateway absent + dispatch_in_gateway=true proceeds
    (backward-compat fallback)."""
    runner = _make_runner(with_adapter=True)
    config = _fake_kanban_config(dispatch_in_gateway=True)
    past_gate = []
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import hermes_cli.kanban_db as _kb

    with patch("hermes_cli.config.load_config", return_value=config):
        with patch.object(
            _kb, "list_boards",
            side_effect=lambda *a, **kw: past_gate.append(True) or [],
        ):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                with patch("asyncio.to_thread", side_effect=fake_to_thread):
                    asyncio.run(runner._kanban_notifier_watcher())

    assert past_gate, (
        "list_boards should be called when notify is absent and "
        "dispatch_in_gateway=true (fallback)"
    )


def test_notifier_watcher_skips_when_both_absent_and_dispatch_false():
    """notify_in_gateway absent + dispatch_in_gateway=false skips
    (backward-compat fallback — the pre-split behavior)."""
    runner = _make_runner()
    config = _fake_kanban_config(dispatch_in_gateway=False)
    with patch("hermes_cli.config.load_config", return_value=config):
        with patch("hermes_cli.kanban_db.connect") as mock_connect:
            asyncio.run(runner._kanban_notifier_watcher())
    mock_connect.assert_not_called()
