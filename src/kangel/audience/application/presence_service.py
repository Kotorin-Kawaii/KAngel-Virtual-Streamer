"""登录观众的在房状态聚合与短暂重连去抖。"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable


class ViewerPresenceCoordinator:
    """将账号的多个连接聚合为一次进房/离房生命周期。"""

    def __init__(self):
        self._present: set[str] = set()
        self._presence_ids: dict[str, str] = {}
        self._pending_leaves: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def join(self, subject_id: str) -> tuple[bool, str]:
        """标记账号在房，返回是否首次进入及不暴露账号主键的临时 ID。"""
        async with self._lock:
            pending = self._pending_leaves.pop(subject_id, None)
            if pending:
                pending.cancel()
            if subject_id in self._present:
                return False, self._presence_ids[subject_id]
            self._present.add(subject_id)
            presence_id = str(uuid.uuid4())
            self._presence_ids[subject_id] = presence_id
            return True, presence_id

    async def leave(
        self,
        subject_id: str,
        grace_seconds: float,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        """最后一条连接离开后延迟确认；宽限期内重连会取消离房。"""
        async with self._lock:
            if subject_id not in self._present or subject_id in self._pending_leaves:
                return
            task = asyncio.create_task(
                self._confirm_leave(subject_id, grace_seconds, callback)
            )
            self._pending_leaves[subject_id] = task

    async def _confirm_leave(
        self,
        subject_id: str,
        grace_seconds: float,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            if grace_seconds > 0:
                await asyncio.sleep(grace_seconds)
            async with self._lock:
                if self._pending_leaves.get(subject_id) is not asyncio.current_task():
                    return
                self._pending_leaves.pop(subject_id, None)
                self._present.discard(subject_id)
                self._presence_ids.pop(subject_id, None)
            await callback()
        except asyncio.CancelledError:
            return

    async def clear(self) -> None:
        """测试及服务关闭时清理后台任务。"""
        async with self._lock:
            tasks = list(self._pending_leaves.values())
            self._pending_leaves.clear()
            self._present.clear()
            self._presence_ids.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


viewer_presence_coordinator = ViewerPresenceCoordinator()
