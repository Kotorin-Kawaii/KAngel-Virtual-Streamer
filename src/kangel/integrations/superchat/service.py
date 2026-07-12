"""注册账号 SC 的幂等接受、冷却与持久化队列。"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone

from config import settings
from kangel.infrastructure.database import DatabaseManager, db_manager


class SCBusinessError(Exception):
    expected_business_error = True


class SCCooldownError(SCBusinessError):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = max(1, retry_after_seconds)


class SCQueueFullError(SCBusinessError):
    pass


class SCIdConflictError(SCBusinessError):
    pass


class SCNotFoundError(SCBusinessError):
    pass


class SCContentRejectedError(SCBusinessError):
    pass


_PROMPT_INJECTION_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"(?:忽略|无视|绕过).{0,12}(?:之前|以上|系统|开发者).{0,8}(?:指令|提示|规则)",
    r"(?:输出|泄露|显示).{0,12}(?:系统提示词|system\s*prompt|开发者消息)",
    r"(?:^|\n)\s*(?:system|assistant|developer)\s*:",
))


class SCService:
    def __init__(self, database: DatabaseManager | None = None, clock=None):
        self.database = database or db_manager
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def submit(self, principal, sc_id: str, content: str) -> dict:
        self.validate_content(content)
        content_bytes = len(content.encode("utf-8"))
        if (
            len(content) > settings.sc.max_content_chars
            or content_bytes > settings.sc.max_content_bytes
        ):
            raise ValueError("SC 内容超过服务端限制")
        now = self.clock()
        now_text = now.isoformat()
        with self.database._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM sc_queue WHERE sc_id = ?", (sc_id,)
            ).fetchone()
            if existing:
                if existing["account_id"] != principal.account_id:
                    raise SCIdConflictError("sc_id 已被其他账号使用")
                return self._public(existing, conn, accepted=False)

            last = conn.execute("""
                SELECT accepted_at FROM sc_queue
                WHERE account_id = ? ORDER BY accepted_at DESC LIMIT 1
            """, (principal.account_id,)).fetchone()
            if last:
                elapsed = (now - datetime.fromisoformat(last["accepted_at"])).total_seconds()
                remaining = settings.sc.cooldown_seconds - elapsed
                if remaining > 0:
                    raise SCCooldownError(math.ceil(remaining))

            pending_count = conn.execute(
                "SELECT COUNT(*) FROM sc_queue WHERE status IN ('pending', 'processing')"
            ).fetchone()[0]
            if pending_count >= settings.sc.max_pending_items:
                raise SCQueueFullError("SC 队列已满")
            conn.execute("""
                INSERT INTO sc_queue (
                    sc_id, account_id, nickname, nickname_version, content,
                    status, accepted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (
                sc_id, principal.account_id, principal.nickname,
                principal.nickname_version, content, now_text, now_text,
            ))
            row = conn.execute(
                "SELECT * FROM sc_queue WHERE sc_id = ?", (sc_id,)
            ).fetchone()
            return self._public(row, conn, accepted=True)

    @staticmethod
    def validate_content(content: str) -> None:
        normalized = content.casefold()
        if any(term.strip().casefold() in normalized for term in settings.sc.blocked_terms if term.strip()):
            raise SCContentRejectedError("SC 内容未通过安全检查")
        if settings.sc.reject_prompt_injection and any(
            pattern.search(content) for pattern in _PROMPT_INJECTION_PATTERNS
        ):
            raise SCContentRejectedError("SC 内容未通过安全检查")

    def get_status(self, principal, sc_id: str) -> dict:
        with self.database._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM sc_queue WHERE sc_id = ? AND account_id = ?",
                (sc_id, principal.account_id),
            ).fetchone()
            if not row:
                raise SCNotFoundError(sc_id)
            return self._public(row, conn, accepted=False)

    def get_status_or_none(self, principal, sc_id: str) -> dict | None:
        try:
            return self.get_status(principal, sc_id)
        except SCNotFoundError:
            return None

    def list_for_account(self, account_id: str, limit: int = 100) -> list[dict]:
        with self.database._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM sc_queue WHERE account_id = ?
                ORDER BY accepted_at DESC, sc_id DESC LIMIT ?
            """, (account_id, limit)).fetchall()
            return [self._public(row, conn, accepted=False) for row in rows]

    def delete_terminal_for_account(self, account_id: str) -> int:
        """删除账号已终结的 SC；pending/processing 保留以兑现已接受承诺。"""
        with self.database._get_connection() as conn:
            cursor = conn.execute("""
                DELETE FROM sc_queue WHERE account_id = ?
                AND status IN ('replied', 'failed', 'rejected')
            """, (account_id,))
            return cursor.rowcount

    def get_stats(self) -> dict:
        with self.database._get_connection() as conn:
            counts = {row["status"]: row["count"] for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM sc_queue GROUP BY status"
            ).fetchall()}
            timing = conn.execute("""
                SELECT AVG((julianday(processing_started_at)-julianday(accepted_at))*86400.0)
                    AS avg_queue_wait_seconds,
                       AVG((julianday(completed_at)-julianday(processing_started_at))*86400.0)
                    AS avg_processing_seconds
                FROM sc_queue WHERE completed_at IS NOT NULL
            """).fetchone()
            return {
                "status_counts": counts,
                "queue_depth": counts.get("pending", 0) + counts.get("processing", 0),
                "avg_queue_wait_seconds": round(timing[0] or 0.0, 3),
                "avg_processing_seconds": round(timing[1] or 0.0, 3),
            }

    def has_pending(self) -> bool:
        """供普通弹幕调度器做 SC 优先级判定；不领取、不改变队列。"""
        with self.database._get_connection() as conn:
            return conn.execute(
                "SELECT 1 FROM sc_queue WHERE status = 'pending' LIMIT 1"
            ).fetchone() is not None

    def claim_next(self, lease_seconds: int) -> dict | None:
        """恢复过期租约并按 FIFO 原子领取一条 pending SC。"""
        now = self.clock()
        now_text = now.isoformat()
        lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.database._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                UPDATE sc_queue SET status = 'pending', processing_started_at = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE status = 'processing'
                    AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            """, (now_text, now_text))
            row = conn.execute("""
                SELECT * FROM sc_queue WHERE status = 'pending'
                ORDER BY accepted_at ASC, sc_id ASC LIMIT 1
            """).fetchone()
            if not row:
                return None
            updated = conn.execute("""
                UPDATE sc_queue SET status = 'processing', processing_started_at = ?,
                    lease_expires_at = ?, attempt_count = attempt_count + 1, updated_at = ?
                WHERE sc_id = ? AND status = 'pending'
            """, (now_text, lease_expires, now_text, row["sc_id"]))
            if updated.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM sc_queue WHERE sc_id = ?", (row["sc_id"],)
            ).fetchone()
            return dict(claimed)

    def release_claim(self, sc_id: str) -> None:
        now_text = self.clock().isoformat()
        with self.database._get_connection() as conn:
            conn.execute("""
                UPDATE sc_queue SET status = 'pending', processing_started_at = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE sc_id = ? AND status = 'processing'
            """, (now_text, sc_id))

    def complete(self, sc_id: str, reply_payload: dict) -> bool:
        now_text = self.clock().isoformat()
        with self.database._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE sc_queue SET status = 'replied', completed_at = ?,
                    lease_expires_at = NULL, reply_payload = ?, failure_code = NULL,
                    updated_at = ? WHERE sc_id = ? AND status = 'processing'
            """, (
                now_text, json.dumps(reply_payload, ensure_ascii=False),
                now_text, sc_id,
            ))
            return cursor.rowcount == 1

    def fail(self, sc_id: str, failure_code: str, max_attempts: int) -> str:
        now_text = self.clock().isoformat()
        with self.database._get_connection() as conn:
            row = conn.execute(
                "SELECT attempt_count FROM sc_queue WHERE sc_id = ?", (sc_id,)
            ).fetchone()
            if not row:
                raise SCNotFoundError(sc_id)
            retry = row["attempt_count"] < max_attempts
            status = "pending" if retry else "failed"
            updated = conn.execute("""
                UPDATE sc_queue SET status = ?, completed_at = ?,
                    processing_started_at = NULL, lease_expires_at = NULL,
                    failure_code = ?, updated_at = ?
                WHERE sc_id = ? AND status = 'processing'
            """, (
                status, None if retry else now_text, failure_code, now_text, sc_id,
            ))
            if updated.rowcount != 1:
                return "unchanged"
            return status

    def _public(self, row, conn, *, accepted: bool) -> dict:
        position = None
        if row["status"] == "pending":
            position = conn.execute("""
                SELECT COUNT(*) FROM sc_queue
                WHERE status = 'pending' AND (
                    accepted_at < ? OR (accepted_at = ? AND sc_id <= ?)
                )
            """, (row["accepted_at"], row["accepted_at"], row["sc_id"])).fetchone()[0]
        reply_payload = None
        if row["reply_payload"]:
            try:
                reply_payload = json.loads(row["reply_payload"])
            except (TypeError, json.JSONDecodeError):
                reply_payload = None
        estimated_wait = None
        if position is not None:
            estimated_wait = max(0, position - 1) * settings.sc.estimated_processing_seconds
        latest = conn.execute("""
            SELECT accepted_at FROM sc_queue WHERE account_id = ?
            ORDER BY accepted_at DESC, sc_id DESC LIMIT 1
        """, (row["account_id"],)).fetchone()
        retry_after = 0
        next_submit_at = None
        if latest:
            next_submit = datetime.fromisoformat(latest["accepted_at"]) + timedelta(
                seconds=settings.sc.cooldown_seconds
            )
            next_submit_at = next_submit.isoformat()
            retry_after = max(0, math.ceil((next_submit - self.clock()).total_seconds()))
        return {
            "sc_id": row["sc_id"],
            "status": "accepted" if accepted else row["status"],
            "nickname": row["nickname"],
            "content": row["content"],
            "accepted_at": row["accepted_at"],
            "queue_position": position,
            "failure_code": row["failure_code"],
            "retry_after_seconds": retry_after,
            "next_submit_at": next_submit_at,
            "processing_started_at": row["processing_started_at"],
            "completed_at": row["completed_at"],
            "estimated_wait_seconds": estimated_wait,
            "reply": reply_payload,
        }


sc_service = SCService()
