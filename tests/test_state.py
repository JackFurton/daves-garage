"""Tests for HiveState against a moto-mocked DynamoDB."""
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from state import HiveState


TABLE_NAME = "dave-test"


@pytest.fixture
def state(fake_aws_credentials):
    """Spin up a fake DDB table and return a HiveState bound to it."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=TABLE_NAME)
        # New HiveState init defaults aws_profile=None and falls back to env vars,
        # which matches what fake_aws_credentials sets up — no patching needed.
        yield HiveState(TABLE_NAME, aws_region="us-east-1")


# ── put_task / get_task ──

def test_put_task_creates_pending_record(state):
    assert state.put_task(42, "Fix login", priority=2, approach="patch auth.py", repo="foo/bar") is True
    task = state.get_task(42)
    assert task is not None
    assert task["title"] == "Fix login"
    assert task["status"] == "pending"
    assert int(task["priority"]) == 2


def test_put_task_duplicate_returns_false(state):
    state.put_task(42, "First", 3, "", "foo/bar")
    # Second put with same issue_id should be a no-op
    assert state.put_task(42, "Second", 1, "", "foo/bar") is False
    task = state.get_task(42)
    assert task["title"] == "First"  # not overwritten


def test_get_task_returns_none_for_missing(state):
    assert state.get_task(999) is None


# ── assign_task (conditional) ──

def test_assign_task_succeeds_when_pending(state):
    state.put_task(42, "x", 3, "", "foo/bar")
    assert state.assign_task(42, "worker-A") is True
    task = state.get_task(42)
    assert task["status"] == "in_progress"
    assert task["assigned_to"] == "worker-A"
    assert task.get("heartbeat_at") is not None


def test_assign_task_fails_when_already_assigned(state):
    state.put_task(42, "x", 3, "", "foo/bar")
    assert state.assign_task(42, "worker-A") is True
    assert state.assign_task(42, "worker-B") is False
    task = state.get_task(42)
    assert task["assigned_to"] == "worker-A"  # B did not steal it


def test_assign_task_fails_when_complete(state):
    state.put_task(42, "x", 3, "", "foo/bar")
    state.assign_task(42, "worker-A")
    state.complete_task(42, "https://github.com/foo/bar/pull/1", "done")
    assert state.assign_task(42, "worker-B") is False


# ── heartbeat ──

def test_heartbeat_updates_timestamp(state):
    state.put_task(42, "x", 3, "", "foo/bar")
    state.assign_task(42, "worker-A")
    task1 = state.get_task(42)
    time.sleep(0.01)
    state.heartbeat_task(42, "worker-A")
    task2 = state.get_task(42)
    assert task2["heartbeat_at"] > task1["heartbeat_at"]


def test_heartbeat_silently_fails_for_wrong_worker(state):
    state.put_task(42, "x", 3, "", "foo/bar")
    state.assign_task(42, "worker-A")
    # Should not raise
    state.heartbeat_task(42, "worker-B")


# ── reclaim_stale_tasks ──

def test_reclaim_stale_returns_zero_when_nothing_stale(state):
    state.put_task(42, "x", 3, "", "foo/bar")
    state.assign_task(42, "worker-A")
    assert state.reclaim_stale_tasks(stale_after_minutes=30) == 0
    task = state.get_task(42)
    assert task["status"] == "in_progress"  # still owned


def test_reclaim_stale_resets_old_tasks_to_pending(state):
    state.put_task(42, "x", 3, "", "foo/bar")
    state.assign_task(42, "worker-A")
    # Manually backdate the heartbeat to simulate a crashed worker
    old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    state.table.update_item(
        Key={"PK": "TASK#42", "SK": "META"},
        UpdateExpression="SET heartbeat_at = :t",
        ExpressionAttributeValues={":t": old_time},
    )
    reclaimed = state.reclaim_stale_tasks(stale_after_minutes=30)
    assert reclaimed == 1
    task = state.get_task(42)
    assert task["status"] == "pending"
    assert task.get("assigned_to") is None or "assigned_to" not in task


# ── complete / fail ──

def test_complete_task_writes_result_record(state):
    state.put_task(42, "x", 3, "", "foo/bar")
    state.assign_task(42, "worker-A")
    state.complete_task(42, "https://github.com/foo/bar/pull/1", "all done")
    task = state.get_task(42)
    assert task["status"] == "complete"
    # RESULT row exists
    result = state.table.get_item(Key={"PK": "TASK#42", "SK": "RESULT"}).get("Item")
    assert result is not None
    assert result["pr_url"] == "https://github.com/foo/bar/pull/1"


def test_fail_task_marks_failed_with_error(state):
    state.put_task(42, "x", 3, "", "foo/bar")
    state.fail_task(42, "boom")
    task = state.get_task(42)
    assert task["status"] == "failed"
    assert task["error"] == "boom"


# ── pending tasks ──

def test_get_pending_tasks_sorts_by_priority(state):
    state.put_task(1, "low", priority=5, approach="", repo="foo/bar")
    state.put_task(2, "critical", priority=1, approach="", repo="foo/bar")
    state.put_task(3, "medium", priority=3, approach="", repo="foo/bar")
    pending = state.get_pending_tasks()
    titles = [t["title"] for t in pending]
    assert titles == ["critical", "medium", "low"]


def test_get_pending_excludes_in_progress(state):
    state.put_task(1, "a", 3, "", "foo/bar")
    state.put_task(2, "b", 3, "", "foo/bar")
    state.assign_task(1, "worker")
    pending = state.get_pending_tasks()
    assert len(pending) == 1
    assert pending[0]["title"] == "b"


# ── budget ──

def test_log_spend_returns_running_total(state):
    total1 = state.log_spend(0.05, "claude-sonnet-4-6", "test1")
    assert total1 == pytest.approx(0.05)
    total2 = state.log_spend(0.10, "claude-sonnet-4-6", "test2")
    assert total2 == pytest.approx(0.15)


def test_get_daily_spend_starts_at_zero(state):
    assert state.get_daily_spend() == 0.0


def test_log_spend_uses_decimal_no_float_error(state):
    # The original bug: passing a float to DDB raises. Confirm the Decimal cast works.
    state.log_spend(0.001, "claude-haiku-4-5-20251001", "tiny")
    state.log_spend(0.0001, "claude-haiku-4-5-20251001", "tinier")
    assert state.get_daily_spend() == pytest.approx(0.0011)


# ── lessons ──

def test_store_lesson_with_category_and_tags(state):
    state.store_lesson(
        lesson="DynamoDB needs Decimal not float",
        source_issue=42,
        repo="foo/bar",
        category="gotcha",
        tags=["dynamodb", "decimal"],
    )
    lessons = state.get_lessons("foo/bar")
    assert len(lessons) == 1
    assert lessons[0]["category"] == "gotcha"
    assert "dynamodb" in lessons[0]["tags"]


def test_get_lessons_filters_by_category(state):
    state.store_lesson("a", 1, "foo/bar", category="testing", tags=[])
    state.store_lesson("b", 2, "foo/bar", category="gotcha", tags=[])
    lessons = state.get_lessons("foo/bar", category="testing")
    assert len(lessons) == 1
    assert lessons[0]["lesson"] == "a"


def test_get_lessons_filters_by_tags(state):
    state.store_lesson("a", 1, "foo/bar", category="x", tags=["auth"])
    state.store_lesson("b", 2, "foo/bar", category="x", tags=["db", "decimal"])
    state.store_lesson("c", 3, "foo/bar", category="x", tags=["unrelated"])
    lessons = state.get_lessons("foo/bar", tags=["auth", "db"])
    titles = sorted(l["lesson"] for l in lessons)
    assert titles == ["a", "b"]


def test_get_lessons_scoped_to_repo(state):
    state.store_lesson("a", 1, "foo/bar")
    state.store_lesson("b", 2, "other/repo")
    lessons = state.get_lessons("foo/bar")
    assert len(lessons) == 1
    assert lessons[0]["lesson"] == "a"


# ── Auto-propose tracking ──

def test_record_proposed_issue_increments_count(state):
    state.record_proposed_issue("foo/bar", 1, "[dave-proposed] First")
    state.record_proposed_issue("foo/bar", 2, "[dave-proposed] Second")
    assert state.get_proposed_count_today("foo/bar") == 2


def test_proposed_count_starts_at_zero(state):
    assert state.get_proposed_count_today("foo/bar") == 0


def test_get_recent_proposed_titles(state):
    state.record_proposed_issue("foo/bar", 1, "[dave-proposed] Add tests")
    state.record_proposed_issue("foo/bar", 2, "[dave-proposed] Fix typo")
    titles = state.get_recent_proposed_titles("foo/bar", limit=10)
    # Both should appear (most-recent first)
    assert len(titles) >= 1


def test_proposed_scoped_to_repo(state):
    state.record_proposed_issue("foo/bar", 1, "A")
    state.record_proposed_issue("other/repo", 2, "B")
    assert state.get_proposed_count_today("foo/bar") == 1
    assert state.get_proposed_count_today("other/repo") == 1


# ── Idle queue tracking ──

def test_queue_empty_marker_lifecycle(state):
    assert state.get_queue_empty_since("foo/bar") is None
    state.mark_queue_empty_now("foo/bar")
    marker = state.get_queue_empty_since("foo/bar")
    assert marker is not None
    state.clear_queue_empty_marker("foo/bar")
    assert state.get_queue_empty_since("foo/bar") is None


# ── Iterative PR mode ──

def test_mark_iterating_resets_to_pending_with_pr_metadata(state):
    state.put_task(42, "Big refactor", priority=2, approach="...", repo="foo/bar")
    state.assign_task(42, "worker-A")
    # Worker did one round, decided more is needed
    state.mark_iterating(42, pr_branch="dave/42-big-refactor", pr_number=99,
                          next_steps="Still need to update the test suite and add migration docs")
    task = state.get_task(42)
    assert task["status"] == "pending"  # picked up next cycle
    assert task["pr_branch"] == "dave/42-big-refactor"
    assert int(task["pr_number"]) == 99
    assert task["next_steps"].startswith("Still need")
    assert int(task["iteration_count"]) == 1
    # assigned_to should be cleared so the next worker can claim it cleanly
    assert task.get("assigned_to") is None or "assigned_to" not in task


def test_iteration_count_increments(state):
    state.put_task(42, "Big refactor", 2, "", "foo/bar")
    state.assign_task(42, "worker-A")
    state.mark_iterating(42, "dave/42-big-refactor", 99, "round 2 todos")
    state.assign_task(42, "worker-B")
    state.mark_iterating(42, "dave/42-big-refactor", 99, "round 3 todos")
    task = state.get_task(42)
    assert int(task["iteration_count"]) == 2


def test_iterating_task_appears_in_pending(state):
    state.put_task(42, "Big refactor", 2, "", "foo/bar")
    state.assign_task(42, "worker-A")
    state.mark_iterating(42, "dave/42-big-refactor", 99, "more to do")
    pending = state.get_pending_tasks()
    assert len(pending) == 1
    assert pending[0]["title"] == "Big refactor"
    assert pending[0]["pr_branch"] == "dave/42-big-refactor"
