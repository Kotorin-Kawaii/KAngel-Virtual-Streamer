"""AI token 用量记账器。

回复路径只做一次内存 append：记录函数同步、非阻塞、绝不抛异常。落库由后台任务
批量完成，写库失败也只影响审计自身，不会传导到弹幕、SC 或 AI 回复链路。
"""

import asyncio
import logging
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import settings
from kangel.infrastructure.database import db_manager

logger = logging.getLogger(__name__)


class TokenAuditRecorder:
    """收集每次模型调用的 token 用量，按批写入 SQLite。"""

    def __init__(self, database=None, clock=None):
        self.database = database or db_manager
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._queue: Deque[Dict[str, Any]] = deque(
            maxlen=settings.token_audit.queue_capacity
        )
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_purge_at = 0.0
        self._stats: Dict[str, Any] = {
            "recorded": 0, "flushed": 0, "dropped": 0, "errors": 0,
            "purged": 0, "retries": 0,
            "last_flush_at": None, "last_error_kind": None,
        }

    # ------------------------------------------------------------------
    #  记录（回复路径唯一接触的入口）
    # ------------------------------------------------------------------

    def record(
        self, *, role: str, provider: str, model: str, status: str,
        usage: Optional[Dict[str, int]] = None, latency_ms: int = 0,
        error_kind: Optional[str] = None,
    ) -> None:
        """把一次调用放进内存队列；任何异常都在这里被吞掉。"""
        if not settings.token_audit.enabled:
            return
        try:
            now = self.clock()
            usage = usage or {}
            before = len(self._queue)
            self._queue.append({
                "record_id": uuid.uuid4().hex,
                "day": self._day_for(now),
                "created_at": now.isoformat(),
                "role": str(role or "unknown"),
                "provider": str(provider or "unknown"),
                "model": str(model or "unknown"),
                "status": "success" if status == "success" else "failed",
                "usage_reported": bool(usage),
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
                "reasoning_tokens": (
                    int(usage["reasoning_tokens"])
                    if usage.get("reasoning_tokens") is not None else None
                ),
                "reasoning_tokens_reported": usage.get("reasoning_tokens") is not None,
                "total_tokens": int(usage.get("total_tokens") or 0),
                "latency_ms": max(0, int(latency_ms or 0)),
                # 只保留异常类名：异常消息可能带上 prompt 片段或 URL 中的凭据。
                "error_kind": str(error_kind) if error_kind else None,
            })
            self._stats["recorded"] += 1
            if len(self._queue) == before == self._queue.maxlen:
                self._stats["dropped"] += 1
        except Exception:
            self._stats["errors"] += 1

    def _day_for(self, moment: datetime) -> str:
        """按配置时区算自然日，让「每天」与主播作息一致。"""
        try:
            zone = ZoneInfo(settings.stream.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = timezone.utc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(zone).strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    #  后台落库
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not settings.token_audit.enabled:
            logger.info("P29 token 审计已关闭，不启动落库任务")
            return
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="ai-token-audit-flush")
        logger.info(
            "P29 token 审计已启动（明细保留 %s 天，间隔 %ss）",
            settings.token_audit.detail_retention_days,
            settings.token_audit.flush_interval_seconds,
        )

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # 关服前把队列写完，避免最后几次调用的账丢掉。
        try:
            await self.flush_once()
        except Exception as exc:
            logger.warning("P29 token 审计收尾落库失败: %s", type(exc).__name__)

    async def _run(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(settings.token_audit.flush_interval_seconds)
                await self.flush_once()
                await self._maybe_purge()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._stats["errors"] += 1
                logger.exception("P29 token 审计落库循环异常，继续运行")

    async def flush_once(self) -> int:
        """取出一批写库；失败时放回队首重试一次，再失败就丢弃并计数。"""
        batch: List[Dict[str, Any]] = []
        limit = settings.token_audit.flush_batch_size
        while self._queue and len(batch) < limit:
            batch.append(self._queue.popleft())
        if not batch:
            return 0
        try:
            await asyncio.to_thread(
                self.database.record_ai_token_usage_batch, batch,
                detail_enabled=settings.token_audit.detail_enabled,
            )
        except Exception as exc:
            self._stats["last_error_kind"] = type(exc).__name__
            if not batch[0].get("_retried"):
                for row in reversed(batch):
                    row["_retried"] = True
                    self._queue.appendleft(row)
                self._stats["retries"] += 1
            else:
                self._stats["dropped"] += len(batch)
            self._stats["errors"] += 1
            logger.warning("P29 token 审计写库失败: %s", type(exc).__name__)
            return 0
        self._stats["flushed"] += len(batch)
        self._stats["last_flush_at"] = self.clock().isoformat()
        return len(batch)

    async def _maybe_purge(self) -> None:
        """机会式清理过期明细；每日聚合永久保留。"""
        now = time.monotonic()
        if now - self._last_purge_at < settings.token_audit.purge_interval_seconds:
            return
        self._last_purge_at = now
        cutoff = (
            self.clock() - timedelta(days=settings.token_audit.detail_retention_days)
        )
        try:
            result = await asyncio.to_thread(
                self.database.purge_expired_ai_token_usage_records,
                self._day_for(cutoff),
            )
            self._stats["purged"] += result.get("records", 0)
        except Exception as exc:
            self._stats["last_error_kind"] = type(exc).__name__
            logger.debug("P29 过期 token 明细清理失败: %s", type(exc).__name__)

    # ------------------------------------------------------------------
    #  可观测性
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        return {
            **self._stats,
            "queued": len(self._queue),
            "queue_capacity": self._queue.maxlen,
            "running": bool(self._task is not None and not self._task.done()),
            "enabled": settings.token_audit.enabled,
            "detail_enabled": settings.token_audit.detail_enabled,
            "detail_retention_days": settings.token_audit.detail_retention_days,
            "flush_interval_seconds": settings.token_audit.flush_interval_seconds,
        }


token_audit_recorder = TokenAuditRecorder()

__all__ = ["TokenAuditRecorder", "token_audit_recorder"]
