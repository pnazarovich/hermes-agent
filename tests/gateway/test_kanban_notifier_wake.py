"""Tests for kanban.wake_agent_on_terminal — waking a real agent turn on
terminal kanban events (in addition to the one-way notifier ping).

Mirrors test_kanban_notifier.py's harness: a bare GatewayRunner runs one
notifier tick against an in-memory-isolated kanban DB.
"""

import asyncio

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb


class WakeRecordingAdapter:
    """RecordingAdapter + the two surfaces _kanban_wake_agent touches:
    build_source (to forge the synthetic event's source) and handle_message
    (the wake entrypoint — same method the real synthetic-wake sites in
    run.py call on the adapter).
    """

    platform = Platform.TELEGRAM

    def __init__(self):
        self.sent = []
        self.handled = []  # MessageEvents passed to handle_message

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    def build_source(
        self,
        chat_id,
        chat_name=None,
        chat_type="dm",
        user_id=None,
        user_name=None,
        thread_id=None,
        message_id=None,
        **kwargs,
    ):
        return SessionSource(
            platform=self.platform,
            chat_id=str(chat_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(user_id) if user_id else None,
            user_name=user_name,
            thread_id=str(thread_id) if thread_id else None,
            message_id=str(message_id) if message_id else None,
        )

    async def handle_message(self, event: MessageEvent):
        self.handled.append(event)


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)
    # The wake is dispatched via asyncio.create_task; let it run to completion
    # so handle_message has fired before we assert.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _make_runner(adapter, wake=False):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._background_tasks = set()
    runner._kanban_notifier_profile = "main"
    return runner


def _set_wake_config(monkeypatch, *, wake):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"wake_agent_on_terminal": wake}},
    )


def _create_completed_subscription(summary="done once", *, title="notify once", assignee="worker", metadata=None):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee=assignee)
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary, metadata=metadata)
        return tid
    finally:
        conn.close()


def _create_blocked_subscription(reason="needs a secret", *, title="blocked task"):
    conn = kb.connect()
    try:
        # create_task defaults to initial_status="running", so block_task
        # (which accepts running|ready) transitions it directly.
        tid = kb.create_task(conn, title=title, assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.block_task(conn, tid, reason=reason)
        return tid
    finally:
        conn.close()


def test_wake_on_completed_when_enabled(tmp_path, monkeypatch):
    """flag True + completed event → one-way send AND a handle_message wake."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake-completed.db"))
    kb.init_db()
    _set_wake_config(monkeypatch, wake=True)

    tid = _create_completed_subscription(
        summary="shipped the fix",
        metadata={"pr_url": "https://github.com/acme/demo/pull/9"},
    )

    adapter = WakeRecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    # One-way ping still sent.
    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]

    # And the agent was woken exactly once with a well-formed synthetic event.
    assert len(adapter.handled) == 1
    ev = adapter.handled[0]
    assert isinstance(ev, MessageEvent)
    assert ev.internal is True
    assert ev.source.chat_id == "chat-1"  # delivers to the subscribed chat
    assert ev.message_id.startswith(f"kanban:{tid}:")
    assert tid in ev.text
    assert "завершена" in ev.text
    assert "https://github.com/acme/demo/pull/9" in ev.text
    assert "intake-routing" in ev.text


def test_wake_on_blocked_when_enabled(tmp_path, monkeypatch):
    """flag True + blocked event → one-way send AND a handle_message wake."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wake-blocked.db"))
    kb.init_db()
    _set_wake_config(monkeypatch, wake=True)

    tid = _create_blocked_subscription(reason="missing API key")

    adapter = WakeRecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    ev = adapter.handled[0]
    assert ev.internal is True
    assert ev.source.chat_id == "chat-1"
    assert tid in ev.text
    assert "заблокирована" in ev.text
    assert "missing API key" in ev.text


def test_no_wake_when_flag_default_false(tmp_path, monkeypatch):
    """Default (flag absent/False) → ping sent, but NO handle_message wake."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "no-wake.db"))
    kb.init_db()
    # No load_config override → DEFAULT_CONFIG's wake_agent_on_terminal=False.
    tid = _create_completed_subscription()

    adapter = WakeRecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert adapter.handled == [], "agent must not be woken when flag is off"


def test_no_wake_on_non_terminal_commented_event(tmp_path, monkeypatch):
    """A ``commented`` event (agent-authored, non-terminal) must never wake.

    commented is not in TERMINAL_KINDS so the notifier never even claims it —
    this pins that contract: no ping, no wake, even with the flag on.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "commented.db"))
    kb.init_db()
    _set_wake_config(monkeypatch, wake=True)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="just a comment", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(conn, tid, kind="commented", payload={"author": "agent", "len": 3})
    finally:
        conn.close()

    adapter = WakeRecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == [], "commented is not a terminal kind — no ping"
    assert adapter.handled == [], "commented must never wake the agent"


def test_wake_is_idempotent_across_ticks(tmp_path, monkeypatch):
    """The same completed event must not double-wake on a second tick.

    The notifier cursor advances past the event after the first tick, so a
    second tick claims nothing — exactly one wake per (task, event).
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "idempotent.db"))
    kb.init_db()
    _set_wake_config(monkeypatch, wake=True)

    _create_completed_subscription()

    adapter = WakeRecordingAdapter()
    # First tick: wake fires.
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert len(adapter.handled) == 1

    # Second tick on a fresh runner against the SAME db: cursor already past
    # the event, so nothing is claimed → no second wake, no second ping.
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert len(adapter.handled) == 1, "same event must not double-wake"
    assert len(adapter.sent) == 1
