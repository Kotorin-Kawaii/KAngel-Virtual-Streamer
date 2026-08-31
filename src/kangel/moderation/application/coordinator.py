"""异步 moderation 协调器；不阻塞原始弹幕广播和普通回复链。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from kangel.persona.application.engine import persona_engine
from kangel.shared.logging import logger
from config import settings
from kangel.infrastructure.bounded_work_gate import ai_reply_work_gate
from kangel.transport.websocket.connection_manager import connection_manager
from kangel.transport.websocket.protocol import WebSocketEventType

from kangel.moderation.application.service import ModerationService, moderation_service
from kangel.memory.application.episodic import episodic_memory_manager


class ModerationCoordinator:
    def __init__(self, service: ModerationService = moderation_service):
        self.service = service
        self._tasks: set[asyncio.Task] = set()

    def schedule(
        self, *, danmaku_id: str, message: str, nickname: str,
        identity, connection_id: str, websocket=None, context: dict[str, Any],
    ) -> None:
        if len(self._tasks) >= settings.moderation.max_pending_tasks:
            self.service.record_analysis_dropped()
            logger.warning(
                "主播管理分析队列已满，跳过旁路分析并放行原始弹幕: pending=%d limit=%d",
                len(self._tasks), settings.moderation.max_pending_tasks,
            )
            return
        task = asyncio.create_task(
            self._run(
                danmaku_id=danmaku_id, message=message, nickname=nickname,
                identity=identity, connection_id=connection_id,
                websocket=websocket, context=context,
            ),
            name=f"moderation:{danmaku_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(
        self, *, danmaku_id: str, message: str, nickname: str,
        identity, connection_id: str, websocket, context: dict[str, Any],
    ) -> None:
        try:
            decision = await self.service.analyze_and_decide(
                danmaku_id=danmaku_id, message=message, nickname=nickname,
                identity=identity, connection_id=connection_id, context=context,
            )
            if not decision or decision.action == "none":
                return

            try:
                reaction = await self._generate_reaction(
                    decision=decision, danmaku_id=danmaku_id, message=message,
                    nickname=nickname, identity=identity,
                )
            except Exception as exc:
                # 主模型失败不能取消安全动作；固定模板仍会广播并继续提交禁言。
                logger.warning("主播管理回复生成异常，使用固定设界模板: %s", exc)
                self.service.record_reply_fallback()
                reaction = self._fallback_reply(decision.action)
            try:
                await self._broadcast_reaction(
                    decision=decision, nickname=nickname, reply_data=reaction,
                )
            except Exception as exc:
                # 广播失败不应回滚本站安全动作；连接状态可由 HTTP/重连恢复。
                logger.warning("主播管理回复广播失败，仍提交安全动作: %s", exc)
            completed = self.service.complete_action(decision.moderation_id, reaction)
            if not completed:
                logger.debug("moderation 动作已被其他任务完成: %s", decision.moderation_id)
            await self._send_status(
                websocket=websocket, subject_key=decision.subject_key,
                decision=decision,
            )
            try:
                episodic_memory_manager.capture_moderation(
                    stream_session_id=context.get("stream_session_id"),
                    moderation_id=decision.moderation_id,
                    account_id=(identity.account_id if identity and identity.is_authenticated else None),
                    identity_type=("authenticated" if identity and identity.is_authenticated else "guest"),
                    action=decision.action,
                )
            except Exception as exc:
                logger.warning("记录 P24 moderation 情景候选失败: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("主播管理异步任务失败 danmaku_id=%s", danmaku_id)

    async def _generate_reaction(
        self, *, decision, danmaku_id: str, message: str,
        nickname: str, identity,
    ) -> dict[str, Any]:
        """复用正式主播回复链，但禁止写入观众长期记忆。"""
        lease = await ai_reply_work_gate.acquire(
            limit=settings.rate_limit.ai_reply_concurrency,
            max_waiters=0,
            wait_timeout=0.1,
        )
        if lease is None:
            self.service.record_reply_fallback()
            return self._fallback_reply(decision.action)
        try:
            try:
                result = await persona_engine.generate_reply({
                    "nickname": nickname,
                    "message": message,
                    "danmakuID": f"moderation:{danmaku_id}",
                    "_viewer_identity": identity,
                    "_requires_boundary": True,
                    "_is_moderation_response": True,
                    "_moderation_action": decision.action,
                })
            except Exception as exc:
                logger.warning("主播管理回复模型失败，降级固定模板: %s", exc)
                self.service.record_reply_fallback()
                return self._fallback_reply(decision.action)
        finally:
            await lease.release()
        reply = result.get("reply_data") if isinstance(result, dict) else None
        if self._is_displayable(reply):
            return reply
        logger.warning("主播管理回复模型不可展示，使用固定设界模板 [%s]", decision.moderation_id)
        self.service.record_reply_fallback()
        return self._fallback_reply(decision.action)

    async def _broadcast_reaction(self, *, decision, nickname: str, reply_data: dict) -> None:
        await connection_manager.broadcast_json({
            "type": WebSocketEventType.AI_REPLY,
            "data": {
                "danmaku_id": decision.danmaku_id,
                "nickname": nickname,
                "source": "moderation",
                "moderation_id": decision.moderation_id,
                "reply": reply_data,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        })

    async def _send_status(self, *, websocket, subject_key: str, decision) -> None:
        if websocket is None:
            return
        status = self.service.status(subject_key)
        action = decision.action
        message = {
            "warning": "请注意直播间的交流方式哦。",
            "timeout": "你暂时不能发送弹幕，请冷静一下再回来聊天。",
            "admin_review": "该账号已暂时限制发言，等待管理员处理。",
        }.get(action, "直播间管理状态已更新。")
        try:
            await connection_manager.send_json_to(websocket, {
                "type": WebSocketEventType.STREAMER_MODERATION,
                "data": {
                    "action": action,
                    "scope": "self",
                    "muted": bool(status.get("muted")),
                    "mute_until": status.get("mute_until"),
                    "retry_after_seconds": int(status.get("retry_after_seconds", 0)),
                    "message": message,
                    "moderation_id": decision.moderation_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            })
        except Exception as exc:
            logger.debug("发送主播管理状态失败: %s", exc)

    @staticmethod
    def _is_displayable(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        sentences = value.get("sentences")
        return bool(sentences) and all(
            isinstance(item, dict) and isinstance(item.get("text"), str)
            and bool(item["text"].strip()) for item in sentences
        )

    @staticmethod
    def _fallback_reply(action: str) -> dict[str, Any]:
        if action == "warning":
            emotion, text = "认真", "喂喂，这样说会让直播间气氛变差哦，请好好聊天。"
        elif action == "admin_review":
            emotion, text = "认真", "这次的发言已经越过界线了，我会先请管理员来处理。"
        else:
            emotion, text = "无语", "看来我不得不先让你冷静一下啦，等会儿再回来聊天吧。"
        return {"emotions": [emotion], "sentences": [{"emotion": emotion, "text": text}]}

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


moderation_coordinator = ModerationCoordinator()
