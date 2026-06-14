"""Tests for the 3-feature kanban-core infra queue (manual Albus infra fix).

Covers three independent, config-gated dispatcher features, all default-OFF
so the board behaves identically until explicitly enabled:

  Feature 3 (П.3): auto-archive of stale Done cards in the dispatcher tick.
  Feature 2 (П.2 layer 2): stuck impl->review chain detector (alarm only).
  Feature 1: auto-rework level 2 — auto-respawn a rework card on review-block.

Each feature is exercised both as a pure function and through ``dispatch_once``
(default-disabled => no behavior change; enabled => the documented effect).
"""
from __future__ import annotations

import sys
import tempfile
import time

import pytest


@pytest.fixture()
def kb_home(monkeypatch):
    """Fresh HERMES_HOME with a clean kanban DB, kanban_db reimported."""
    test_home = tempfile.mkdtemp(prefix="kanban_infra_queue_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    with kanban_db.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
    yield kanban_db, test_home


def _fake_spawn(*args, **kwargs):
    return 12345


# ---------------------------------------------------------------------------
# Feature 3: auto-archive stale Done
# ---------------------------------------------------------------------------

def test_auto_archive_disabled_by_default_is_noop(kb_home):
    """archive_after_days<=0 archives nothing, even with ancient Done cards."""
    kb, _ = kb_home
    now = int(time.time())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="old done", initial_status="running")
        kb.complete_task(conn, tid)
        conn.execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            (now - 30 * 86400, tid),
        )
        conn.commit()
        archived = kb.auto_archive_old_done(conn, archive_after_days=0, now=now)
    assert archived == []
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "done"


def test_auto_archive_old_done_archives_past_threshold(kb_home):
    """Done cards older than the threshold are archived; fresh ones are not."""
    kb, _ = kb_home
    now = int(time.time())
    with kb.connect_closing() as conn:
        old = kb.create_task(conn, title="old", initial_status="running")
        kb.complete_task(conn, old)
        fresh = kb.create_task(conn, title="fresh", initial_status="running")
        kb.complete_task(conn, fresh)
        conn.execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            (now - 10 * 86400, old),
        )
        conn.execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            (now - 1 * 86400, fresh),
        )
        conn.commit()
        archived = kb.auto_archive_old_done(conn, archive_after_days=7, now=now)
    assert archived == [old]
    with kb.connect_closing() as conn:
        old_status = conn.execute("SELECT status FROM tasks WHERE id = ?", (old,)).fetchone()["status"]
        fresh_status = conn.execute("SELECT status FROM tasks WHERE id = ?", (fresh,)).fetchone()["status"]
    assert old_status == "archived"
    assert fresh_status == "done"


def test_auto_archive_skips_done_with_unfinished_children(kb_home):
    """A done parent with a not-yet-finished child must NOT be archived."""
    kb, _ = kb_home
    now = int(time.time())
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent done", initial_status="running")
        child = kb.create_task(conn, title="child running", initial_status="running", parents=[parent])
        kb.complete_task(conn, parent)
        conn.execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            (now - 30 * 86400, parent),
        )
        conn.commit()
        archived = kb.auto_archive_old_done(conn, archive_after_days=7, now=now)
    assert parent not in archived
    with kb.connect_closing() as conn:
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (parent,)).fetchone()["status"]
    assert status == "done"


def test_auto_archive_is_idempotent(kb_home):
    """Running the archive twice archives once and then no-ops."""
    kb, _ = kb_home
    now = int(time.time())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="old", initial_status="running")
        kb.complete_task(conn, tid)
        conn.execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            (now - 30 * 86400, tid),
        )
        conn.commit()
        first = kb.auto_archive_old_done(conn, archive_after_days=7, now=now)
        second = kb.auto_archive_old_done(conn, archive_after_days=7, now=now)
    assert first == [tid]
    assert second == []


def test_dispatch_once_auto_archive_disabled_default(kb_home):
    """dispatch_once with no archive kwarg leaves old Done untouched."""
    kb, _ = kb_home
    now = int(time.time())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="old", initial_status="running")
        kb.complete_task(conn, tid)
        conn.execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            (now - 30 * 86400, tid),
        )
        conn.commit()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)
    assert getattr(res, "archived", []) == []
    with kb.connect_closing() as conn:
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]
    assert status == "done"


def test_dispatch_once_auto_archive_enabled(kb_home):
    """dispatch_once with done_archive_days set archives stale Done cards."""
    kb, _ = kb_home
    now = int(time.time())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="old", initial_status="running")
        kb.complete_task(conn, tid)
        conn.execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            (now - 30 * 86400, tid),
        )
        conn.commit()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, done_archive_days=7)
    assert tid in getattr(res, "archived", [])
    with kb.connect_closing() as conn:
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]
    assert status == "archived"


# ---------------------------------------------------------------------------
# Feature 2: stuck impl->review chain detector (alarm only)
# ---------------------------------------------------------------------------

def test_stuck_chain_detector_disabled_by_default(kb_home):
    """threshold<=0 detects nothing."""
    kb, _ = kb_home
    now = int(time.time())
    with kb.connect_closing() as conn:
        impl = kb.create_task(conn, title="impl", initial_status="running")
        rev = kb.create_task(conn, title="reviewer", initial_status="running", parents=[impl])
        kb.complete_task(conn, impl)
        kb.block_task(conn, rev, reason="review-required: needs human")
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'blocked'",
            (now - 99999, rev),
        )
        conn.commit()
        alarms = kb.detect_stuck_chains(conn, threshold_seconds=0, now=now)
    assert alarms == []


def test_stuck_chain_detector_fires_on_done_parent_blocked_reviewer(kb_home):
    """impl done + reviewer sticky-blocked past threshold => alarm + event."""
    kb, _ = kb_home
    now = int(time.time())
    with kb.connect_closing() as conn:
        impl = kb.create_task(conn, title="impl", initial_status="running")
        rev = kb.create_task(conn, title="reviewer", initial_status="running", parents=[impl])
        kb.complete_task(conn, impl)
        kb.block_task(conn, rev, reason="review-required: needs human")
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'blocked'",
            (now - 7200, rev),
        )
        conn.commit()
        alarms = kb.detect_stuck_chains(conn, threshold_seconds=3600, now=now)
    ids = [a[0] for a in alarms]
    assert rev in ids
    with kb.connect_closing() as conn:
        ev = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND kind = 'chain_stuck_alarm'",
            (rev,),
        ).fetchone()
    assert ev["n"] == 1


def test_stuck_chain_detector_not_fired_below_threshold(kb_home):
    """A recently-blocked reviewer (under threshold) does not alarm."""
    kb, _ = kb_home
    now = int(time.time())
    with kb.connect_closing() as conn:
        impl = kb.create_task(conn, title="impl", initial_status="running")
        rev = kb.create_task(conn, title="reviewer", initial_status="running", parents=[impl])
        kb.complete_task(conn, impl)
        kb.block_task(conn, rev, reason="review-required: needs human")
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'blocked'",
            (now - 60, rev),
        )
        conn.commit()
        alarms = kb.detect_stuck_chains(conn, threshold_seconds=3600, now=now)
    assert [a[0] for a in alarms if a[0] == rev] == []


def test_stuck_chain_detector_idempotent(kb_home):
    """Two detector passes emit only one alarm event for the same stuck state."""
    kb, _ = kb_home
    now = int(time.time())
    with kb.connect_closing() as conn:
        impl = kb.create_task(conn, title="impl", initial_status="running")
        rev = kb.create_task(conn, title="reviewer", initial_status="running", parents=[impl])
        kb.complete_task(conn, impl)
        kb.block_task(conn, rev, reason="review-required: needs human")
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'blocked'",
            (now - 7200, rev),
        )
        conn.commit()
        first = kb.detect_stuck_chains(conn, threshold_seconds=3600, now=now)
        second = kb.detect_stuck_chains(conn, threshold_seconds=3600, now=now)
    assert rev in [a[0] for a in first]
    assert rev not in [a[0] for a in second]
    with kb.connect_closing() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND kind = 'chain_stuck_alarm'",
            (rev,),
        ).fetchone()["n"]
    assert n == 1


def test_dispatch_once_stuck_chain_disabled_default(kb_home):
    """dispatch_once with no stuck_chain kwarg raises no alarm."""
    kb, _ = kb_home
    now = int(time.time())
    with kb.connect_closing() as conn:
        impl = kb.create_task(conn, title="impl", initial_status="running")
        rev = kb.create_task(conn, title="reviewer", initial_status="running", parents=[impl])
        kb.complete_task(conn, impl)
        kb.block_task(conn, rev, reason="review-required: needs human")
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'blocked'",
            (now - 99999, rev),
        )
        conn.commit()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)
    assert getattr(res, "stuck_chains", []) == []


# ---------------------------------------------------------------------------
# Feature 1: auto-rework level 2
# ---------------------------------------------------------------------------

def _draft_pr_ok(_pr_url):
    """Stub PR check: Draft + do-not-merge + open => reworkable."""
    return {"isDraft": True, "labels": ["do-not-merge"], "state": "OPEN"}


def _pr_not_draft(_pr_url):
    return {"isDraft": False, "labels": ["do-not-merge"], "state": "OPEN"}


def _setup_review_block(kb, conn, *, impl_body="implement feature", rev_reason="review-blocking: AC-3 fails"):
    """Create impl(done) + reviewer(blocked) chain with a Draft PR comment."""
    impl = kb.create_task(
        conn, title="impl card", body=impl_body, assignee="ultracode",
        branch_name="task/feature-x", workspace_kind="worktree",
        initial_status="running",
    )
    kb.complete_task(conn, impl)
    rev = kb.create_task(
        conn, title="reviewer card", assignee="reviewer",
        initial_status="running", parents=[impl],
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'spawned', NULL, ?)",
        (impl, int(time.time()) - 100),
    )
    kb.add_comment(conn, impl, "ultracode",
                   "PR: https://github.com/acme/repo/pull/461")
    kb.block_task(conn, rev, reason=rev_reason)
    conn.commit()
    return impl, rev


def test_auto_rework_disabled_by_default(kb_home):
    """enabled=False => no rework card created."""
    kb, _ = kb_home
    with kb.connect_closing() as conn:
        impl, rev = _setup_review_block(kb, conn)
        reworked = kb.maybe_auto_rework(
            conn, enabled=False, pr_check_fn=_draft_pr_ok,
        )
    assert reworked == []


def test_auto_rework_happy_path_creates_rework_card(kb_home):
    """review-block + Draft PR + count<max => new rework card on same branch."""
    kb, _ = kb_home
    with kb.connect_closing() as conn:
        impl, rev = _setup_review_block(kb, conn)
        reworked = kb.maybe_auto_rework(
            conn, enabled=True, max_attempts=2, pr_check_fn=_draft_pr_ok,
        )
    assert len(reworked) == 1
    rework_id = reworked[0]
    with kb.connect_closing() as conn:
        rw = conn.execute(
            "SELECT assignee, branch_name, created_by FROM tasks WHERE id = ?",
            (rework_id,),
        ).fetchone()
    assert rw["assignee"] == "ultracode"
    assert rw["branch_name"] == "task/feature-x"
    assert rw["created_by"] == "auto-rework"


def test_auto_rework_stops_at_max_attempts(kb_home):
    """At the attempt limit, no rework is created and an exhausted event fires."""
    kb, _ = kb_home
    with kb.connect_closing() as conn:
        impl, rev = _setup_review_block(kb, conn)
        kb._set_auto_rework_count(conn, impl, 2)
        reworked = kb.maybe_auto_rework(
            conn, enabled=True, max_attempts=2, pr_check_fn=_draft_pr_ok,
        )
    assert reworked == []
    with kb.connect_closing() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE kind = 'auto_rework_exhausted'",
        ).fetchone()["n"]
    assert n >= 1


def test_auto_rework_stop_keyword_escalates(kb_home):
    """A stop-list keyword in the impl body blocks rework (escalation)."""
    kb, _ = kb_home
    with kb.connect_closing() as conn:
        impl, rev = _setup_review_block(
            kb, conn, impl_body="touch prod-db migration for billing",
        )
        reworked = kb.maybe_auto_rework(
            conn, enabled=True, max_attempts=2,
            stop_keywords=["prod-db", "migration", "billing"],
            pr_check_fn=_draft_pr_ok,
        )
    assert reworked == []


def test_auto_rework_non_draft_pr_escalates(kb_home):
    """A non-Draft PR blocks rework (escalation, not silent rework)."""
    kb, _ = kb_home
    with kb.connect_closing() as conn:
        impl, rev = _setup_review_block(kb, conn)
        reworked = kb.maybe_auto_rework(
            conn, enabled=True, max_attempts=2, require_draft_pr=True,
            pr_check_fn=_pr_not_draft,
        )
    assert reworked == []


def test_auto_rework_idempotent(kb_home):
    """Two passes create exactly one rework card for the same block."""
    kb, _ = kb_home
    with kb.connect_closing() as conn:
        impl, rev = _setup_review_block(kb, conn)
        first = kb.maybe_auto_rework(
            conn, enabled=True, max_attempts=2, pr_check_fn=_draft_pr_ok,
        )
        second = kb.maybe_auto_rework(
            conn, enabled=True, max_attempts=2, pr_check_fn=_draft_pr_ok,
        )
    assert len(first) == 1
    assert second == []


def test_auto_rework_ignores_infra_block(kb_home):
    """An auth/quota block (not a content review-block) is not reworked."""
    kb, _ = kb_home
    with kb.connect_closing() as conn:
        impl, rev = _setup_review_block(
            kb, conn, rev_reason="429 rate limit / quota exceeded",
        )
        reworked = kb.maybe_auto_rework(
            conn, enabled=True, max_attempts=2, pr_check_fn=_draft_pr_ok,
        )
    assert reworked == []
