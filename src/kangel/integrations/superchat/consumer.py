"""持久化 SC 队列消费者；绕过普通弹幕筛选并保证有限重试。"""

import asyncio
from time import perf_counter
from datetime import datetime, timezone

from config import settings
from kangel.infrastructure.bounded_work_gate import ai_reply_work_gate
from kangel.infrastructure.reply_timing import reply_timing_metrics
from kangel.transport.websocket.connection_manager import connection_manager
from kangel.transport.websocket.protocol import WebSocketEventType
from kangel.persona.application.engine import persona_engine
from kangel.moderation.application.coordinator import moderation_coordinator
from .service import SCService, sc_service
from kangel.audience.application.identity_service import VerifiedAccountPrincipal, viewer_identity_resolver
from kangel.shared.logging import logger
from kangel.memory.application.episodic import episodic_memory_manager


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
            from kangel.stream.application.metadata import stream_metadata_pusher
            stream_session_id = stream_metadata_pusher.get_current_stream_session_id()
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
            try:
                episodic_memory_manager.capture_reply(
                    stream_session_id=stream_session_id,
                    danmaku_id=item["sc_id"], message=item["content"], identity=identity,
                    analysis=result.get("analysis"), is_sc=True,
                )
            except Exception as exc:
                logger.warning("记录 SC P24 情景记忆候选失败 [%s]: %s", item["sc_id"], exc)
            broadcast_started_at = perf_counter()
            try:
                await connection_manager.broadcast_json({
                    "type": WebSocketEventType.AI_REPLY,
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
            finally:
                reply_timing_metrics.record(
                    "broadcast", (perf_counter() - broadcast_started_at) * 1000,
                    path="sc",
                )
            await self._broadcast_status(item, "replied", reply=reply_data)
            # SC 完成只是一个低频、可合并的 Director 信号；不保证触发 AI，
            # 更不保证产生事实变化或公开演出。
            await stream_metadata_pusher.notify_director_event(
                "sc_completed", priority=3
            )
            # SC 已经完成“必读”承诺后，再异步进入主播管理分析。
            # 这条旁路不会取消或回滚已接受、已读取的 SC；若判断为越界，
            # 只会追加主播自己的设界回应，并让后续普通弹幕受到本站禁言状态影响。
            try:
                from kangel.stream.application.metadata import stream_metadata_pusher

                metadata = stream_metadata_pusher.get_metadata().to_dict()
                moderation_coordinator.schedule(
                    danmaku_id=item["sc_id"],
                    message=item["content"],
                    nickname=item["nickname"],
                    identity=identity,
                    connection_id=f"sc-{item['sc_id']}",
                    websocket=None,
                    context={
                        "stream_session_id": metadata.get("stream_session_id"),
                        "viewer_relationship": {},
                        "direct_context": {"source": "sc", "sc_id": item["sc_id"]},
                        "stream_context": {
                            "is_live": metadata.get("is_live"),
                            "daily_theme_id": metadata.get("daily_theme_id"),
                            "daily_theme_name": metadata.get("daily_theme_name"),
                            "special_date_theme": metadata.get("special_date_theme"),
                            "current_activity": metadata.get("current_activity"),
                            "viewer_count": metadata.get("viewer_count"),
                            "source": "sc",
                        },
                        "persona_state": persona_engine.state.model_dump(),
                        "internal_state": persona_engine.internal_state.model_dump(),
                    },
                )
            except Exception as moderation_error:
                # SC 主流程已经完成，安全旁路失败不能改变 SC 的最终状态。
                logger.warning("SC 主播管理旁路启动失败 [%s]: %s", item["sc_id"], moderation_error)
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
            "type": WebSocketEventType.SC_STATUS,
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
        from kangel.stream.application.metadata import stream_metadata_pusher
        return stream_metadata_pusher.get_metadata().is_live


sc_consumer = SCConsumer()
