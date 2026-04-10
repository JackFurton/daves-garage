"""DynamoDB state management — tasks, budget, lessons, reclaim."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from logger import get_logger

log = get_logger("state")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_boto3_session(aws_profile=None, aws_region="us-east-1",
                          aws_access_key_id=None, aws_secret_access_key=None):
    """Build a boto3 Session from inline keys, profile, or env-var fallback (in that order)."""
    if aws_access_key_id and aws_secret_access_key:
        return boto3.Session(
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=aws_region,
        )
    if aws_profile:
        return boto3.Session(profile_name=aws_profile, region_name=aws_region)
    # Falls back to env vars / instance role / default profile
    return boto3.Session(region_name=aws_region)


class HiveState:
    def __init__(self, table_name: str,
                 aws_profile: Optional[str] = None,
                 aws_region: str = "us-east-1",
                 aws_access_key_id: Optional[str] = None,
                 aws_secret_access_key: Optional[str] = None):
        session = _build_boto3_session(
            aws_profile=aws_profile,
            aws_region=aws_region,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )
        self.table = session.resource("dynamodb").Table(table_name)
        self.table_name = table_name

    # ── Tasks ──

    def put_task(self, issue_id: int, title: str, priority: int, approach: str, repo: str) -> bool:
        """Insert a new task. Returns False if the task already exists (concurrent triage)."""
        try:
            self.table.put_item(
                Item={
                    "PK": f"TASK#{issue_id}",
                    "SK": "META",
                    "title": title,
                    "priority": priority,
                    "approach": approach,
                    "repo": repo,
                    "status": "pending",
                    "assigned_to": None,
                    "created_at": _utc_now_iso(),
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                log.debug(f"Task #{issue_id} already exists, skipping put")
                return False
            raise

    def get_task(self, issue_id: int) -> Optional[dict]:
        """Fetch a task by issue id, or None if missing."""
        resp = self.table.get_item(Key={"PK": f"TASK#{issue_id}", "SK": "META"})
        return resp.get("Item")

    def assign_task(self, issue_id: int, worker_id: str) -> bool:
        """Atomically claim a pending task. Returns False if another worker got there first."""
        now = _utc_now_iso()
        try:
            self.table.update_item(
                Key={"PK": f"TASK#{issue_id}", "SK": "META"},
                UpdateExpression="SET #s = :new, assigned_to = :w, assigned_at = :t, heartbeat_at = :t",
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":new": "in_progress",
                    ":pending": "pending",
                    ":w": worker_id,
                    ":t": now,
                },
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def heartbeat_task(self, issue_id: int, worker_id: str):
        """Refresh the heartbeat on a task this worker owns. Best-effort, swallows errors."""
        try:
            self.table.update_item(
                Key={"PK": f"TASK#{issue_id}", "SK": "META"},
                UpdateExpression="SET heartbeat_at = :t",
                ConditionExpression="assigned_to = :w AND #s = :s",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":t": _utc_now_iso(),
                    ":w": worker_id,
                    ":s": "in_progress",
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                log.warning(f"Heartbeat failed for #{issue_id}: {e}")

    def reclaim_stale_tasks(self, stale_after_minutes: int = 30) -> int:
        """Find in_progress tasks with stale heartbeats and put them back to pending.

        Called from the controller before each cycle. Returns the number reclaimed.
        Safe under concurrent runs because the update is conditional on (status=in_progress
        AND the same heartbeat we read).
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)).isoformat()
        resp = self.table.scan(
            FilterExpression="#s = :s AND SK = :sk AND (attribute_not_exists(heartbeat_at) OR heartbeat_at < :cutoff)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "in_progress",
                ":sk": "META",
                ":cutoff": cutoff,
            },
        )
        reclaimed = 0
        for item in resp.get("Items", []):
            issue_id = item["PK"].split("#")[1]
            old_heartbeat = item.get("heartbeat_at", "")
            try:
                expr_values = {
                    ":pending": "pending",
                    ":in_prog": "in_progress",
                }
                if old_heartbeat:
                    update_expr = "SET #s = :pending REMOVE assigned_to, assigned_at, heartbeat_at"
                    cond_expr = "#s = :in_prog AND heartbeat_at = :hb"
                    expr_values[":hb"] = old_heartbeat
                else:
                    update_expr = "SET #s = :pending REMOVE assigned_to, assigned_at"
                    cond_expr = "#s = :in_prog AND attribute_not_exists(heartbeat_at)"
                self.table.update_item(
                    Key={"PK": item["PK"], "SK": "META"},
                    UpdateExpression=update_expr,
                    ConditionExpression=cond_expr,
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues=expr_values,
                )
                log.warning(f"Reclaimed stale task #{issue_id} (last heartbeat: {old_heartbeat or 'never'})")
                reclaimed += 1
            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    continue  # Worker came back to life or another reclaim won
                raise
        return reclaimed

    def complete_task(self, issue_id: int, pr_url: str, summary: str):
        self.table.update_item(
            Key={"PK": f"TASK#{issue_id}", "SK": "META"},
            UpdateExpression="SET #s = :s, completed_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "complete",
                ":t": _utc_now_iso(),
            },
        )
        self.table.put_item(Item={
            "PK": f"TASK#{issue_id}",
            "SK": "RESULT",
            "pr_url": pr_url,
            "summary": summary,
            "completed_at": _utc_now_iso(),
        })

    def fail_task(self, issue_id: int, error: str):
        # NOTE: 'error' is a reserved keyword in DynamoDB — must alias via ExpressionAttributeNames.
        self.table.update_item(
            Key={"PK": f"TASK#{issue_id}", "SK": "META"},
            UpdateExpression="SET #s = :s, #err = :e, failed_at = :t",
            ExpressionAttributeNames={"#s": "status", "#err": "error"},
            ExpressionAttributeValues={
                ":s": "failed",
                ":e": error,
                ":t": _utc_now_iso(),
            },
        )

    def get_pending_tasks(self) -> list:
        """Get all pending tasks, sorted by priority."""
        resp = self.table.scan(
            FilterExpression="#s = :s AND SK = :sk",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "pending", ":sk": "META"},
        )
        items = resp.get("Items", [])
        return sorted(items, key=lambda x: int(x.get("priority", 99)))

    def get_in_progress_count(self) -> int:
        resp = self.table.scan(
            FilterExpression="#s = :s AND SK = :sk",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "in_progress", ":sk": "META"},
        )
        return len(resp.get("Items", []))

    # ── Budget ──

    def log_spend(self, amount_usd: float, model: str, purpose: str) -> float:
        """Atomically increment today's spend and return the new total."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # DynamoDB ADD is atomic and creates the attribute if missing.
        # Decimal(str(...)) avoids the float-binary-precision rejection boto3 enforces.
        resp = self.table.update_item(
            Key={"PK": f"BUDGET#{today}", "SK": "SPEND"},
            UpdateExpression="ADD total_usd :amt, calls :one",
            ExpressionAttributeValues={
                ":amt": Decimal(str(amount_usd)),
                ":one": Decimal(1),
            },
            ReturnValues="UPDATED_NEW",
        )
        new_total = resp.get("Attributes", {}).get("total_usd", Decimal(0))
        return float(new_total)

    def get_daily_spend(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resp = self.table.get_item(Key={"PK": f"BUDGET#{today}", "SK": "SPEND"})
        item = resp.get("Item")
        if not item:
            return 0.0
        return float(item.get("total_usd", 0))

    # ── Lessons ──

    def store_lesson(self, lesson: str, source_issue: int, repo: str,
                     category: str = "uncategorized", tags: Optional[list] = None):
        lesson_id = str(uuid.uuid4())[:8]
        item = {
            "PK": f"LESSON#{lesson_id}",
            "SK": "META",
            "lesson": lesson,
            "source_issue": source_issue,
            "repo": repo,
            "category": category,
            "tags": tags or [],
            "created_at": _utc_now_iso(),
        }
        self.table.put_item(Item=item)

    def get_lessons(self, repo: str, limit: int = 20,
                    category: Optional[str] = None,
                    tags: Optional[list] = None) -> list:
        """Fetch lessons for a repo. If category or tags are given, filter to relevant ones."""
        resp = self.table.scan(
            FilterExpression="begins_with(PK, :prefix) AND SK = :sk AND repo = :repo",
            ExpressionAttributeValues={
                ":prefix": "LESSON#",
                ":sk": "META",
                ":repo": repo,
            },
        )
        items = resp.get("Items", [])

        if category:
            items = [i for i in items if i.get("category") == category]
        if tags:
            tag_set = set(tags)
            items = [i for i in items if tag_set & set(i.get("tags", []))]

        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return items[:limit]

    # ── Config (hot-reload) ──

    def get_config_overrides(self) -> dict:
        resp = self.table.get_item(Key={"PK": "CONFIG", "SK": "SETTINGS"})
        return resp.get("Item", {})

    # ── Auto-propose tracking ──

    def record_proposed_issue(self, repo: str, issue_id: int, title: str):
        """Log that Dave proposed (filed) an issue today."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.table.update_item(
            Key={"PK": f"PROPOSED#{repo}#{today}", "SK": "META"},
            UpdateExpression="ADD #c :one SET last_issue_id = :iid, last_title = :t, last_at = :now",
            ExpressionAttributeNames={"#c": "count"},
            ExpressionAttributeValues={
                ":one": Decimal(1),
                ":iid": issue_id,
                ":t": title,
                ":now": _utc_now_iso(),
            },
        )

    def get_proposed_count_today(self, repo: str) -> int:
        """How many issues has Dave proposed for this repo today?"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resp = self.table.get_item(Key={"PK": f"PROPOSED#{repo}#{today}", "SK": "META"})
        item = resp.get("Item")
        if not item:
            return 0
        return int(item.get("count", 0))

    def get_recent_proposed_titles(self, repo: str, limit: int = 10) -> list:
        """Recent proposal titles, used to deduplicate when Dave proposes a new one."""
        # Scan over the last 7 days of proposals for this repo
        resp = self.table.scan(
            FilterExpression="begins_with(PK, :prefix) AND SK = :sk",
            ExpressionAttributeValues={
                ":prefix": f"PROPOSED#{repo}#",
                ":sk": "META",
            },
        )
        items = resp.get("Items", [])
        items.sort(key=lambda x: x.get("last_at", ""), reverse=True)
        titles = []
        for it in items:
            t = it.get("last_title")
            if t:
                titles.append(t)
            if len(titles) >= limit:
                break
        return titles

    # ── Idle-queue tracking (used by auto-propose) ──

    def mark_queue_empty_now(self, repo: str):
        """Stamp the moment the queue went empty so we can measure idle duration."""
        self.table.put_item(Item={
            "PK": f"QUEUE_STATE#{repo}",
            "SK": "META",
            "empty_since": _utc_now_iso(),
        })

    def clear_queue_empty_marker(self, repo: str):
        """Drop the empty-since marker when the queue is no longer empty."""
        try:
            self.table.delete_item(Key={"PK": f"QUEUE_STATE#{repo}", "SK": "META"})
        except ClientError:
            pass

    def get_queue_empty_since(self, repo: str) -> Optional[str]:
        resp = self.table.get_item(Key={"PK": f"QUEUE_STATE#{repo}", "SK": "META"})
        item = resp.get("Item")
        return item.get("empty_since") if item else None
