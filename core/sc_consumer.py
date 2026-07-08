"""持久化 SC 队列消费者；绕过普通弹幕筛选并保证有限重试。"""

import asyncio
from datetime import datetime, timezone

from config import settings
from core.bounded_work_gate import ai_reply_work_gate
from core.connection_manager import connection_manager
from core.persona_engine import persona_engine
from core.sc_service import SCService, sc_service
from core.viewer_identity import VerifiedAccountPrincipal, viewer_identity_resolver
from utils.logger import logger


class SCConsumer:
    def __init__(self, service: SCService = sc_service, live_check=None):
        self.service = service
        self.live_check = live_check or self._default_live_check
        self._running = False
        self._task = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="sc-consumer")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                if not self.live_check():
                    await asyncio.sleep(settings.sc.poll_interval_seconds)
                    continue
                processed = await self.process_once()
                if not processed:
                    await asyncio.sleep(settings.sc.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("SC 消费循环异常")
                await asyncio.sleep(settings.sc.poll_interval_seconds)

    async def process_once(self) -> bool:
        item = await asyncio.to_thread(
            self.service.claim_next, settings.sc.processing_lease_seconds
        )
        if not item:
            return False
        lease = await ai_reply_work_gate.acquire(
            limit=settings.rate_limit.ai_reply_concurrency,
            max_waiters=settings.rate_limit.ai_reply_queue_size,
            wait_timeout=settings.rate_limit.ai_reply_queue_wait_seconds,
        )
        if lease is None:
            await asyncio.to_thread(self.service.release_claim, item["sc_id"])
            return False
        try:
            await self._broadcast_status(item, "processing")
            principal = VerifiedAccountPrincipal.from_authentication(
                account_id=item["account_id"],
                issuer="sc_queue",
                nickname=item["nickname"],
                nickname_version=item["nickname_version"],
            )
            identity = viewer_identity_resolver.resolve_for_connection(
                connection_id=f"sc-{item['sc_id']}",
                nickname=item["nickname"],
                principal=principal,
            )
            result = await persona_engine.generate_reply({
                "nickname": item["nickname"],
                "message": item["content"],
                "danmakuID": item["sc_id"],
                "_viewer_identity": identity,
                "_is_sc_danmaku": True,
            })
            if (
                not result
                or result.get("reply_generated") is False
                or not self._is_displayable_reply(result.get("reply_data"))
            ):
                failure_code = (
                    result.get("generation_failure_code", "invalid_ai_reply")
                    if isinstance(result, dict) else "empty_ai_reply"
                )
                raise RuntimeError(failure_code)
            reply_data = result["reply_data"]
            completed = await asyncio.to_thread(
                self.service.complete, item["sc_id"], reply_data
            )
            if not completed:
                logger.warning("SC 完成时租约状态已变化: %s", item["sc_id"])
                return True
            await connection_manager.broadcast_json({
                "type": "ai_reply",
                "data": {
                    "danmaku_id": item["sc_id"],
                    "sc_id": item["sc_id"],
                    "source": "sc",
                    "nickname": item["nickname"],
                    "original_message": item["content"],
                    "reply": reply_data,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            })
            await self._broadcast_status(item, "replied", reply=reply_data)
            logger.info("SC 回复生成并广播成功 [%s]", item["sc_id"])
            return True
        except asyncio.CancelledError:
            await asyncio.to_thread(self.service.release_claim, item["sc_id"])
            raise
        except Exception as exc:
            logger.error("SC 处理失败 [%s]: %s", item["sc_id"], exc)
            failure_code = str(exc)
            if failure_code not in {
                "empty_ai_reply", "invalid_ai_reply", "reply_generation_failed"
            }:
                failure_code = "reply_generation_failed"
            status = await asyncio.to_thread(
                self.service.fail,
                item["sc_id"],
                failure_code,
                settings.sc.max_attempts,
            )
            if status == "failed":
                await self._broadcast_status(item, "failed", failure_code)
            return True
        finally:
            await lease.release()

    @staticmethod
    async def _broadcast_status(
        item: dict, status: str, failure_code=None, reply=None
    ) -> None:
        await connection_manager.broadcast_json({
            "type": "sc_status",
            "data": {
                "sc_id": item["sc_id"],
                "status": status,
                "nickname": item["nickname"],
                "content": item["content"],
                "failure_code": failure_code,
                "reply": reply,
            },
        })

    @staticmethod
    def _is_displayable_reply(reply_data) -> bool:
        if not isinstance(reply_data, dict):
            return False
        sentences = reply_data.get("sentences")
        return bool(sentences) and all(
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and bool(item["text"].strip())
            for item in sentences
        )

    @staticmethod
    def _default_live_check() -> bool:
        from core.stream_metadata import stream_metadata_pusher
        return stream_metadata_pusher.get_metadata().is_live


sc_consumer = SCConsumer()
