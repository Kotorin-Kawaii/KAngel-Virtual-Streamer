"""异步有界等待闸门，用于昂贵且不应无限排队的工作。"""

import asyncio
from dataclasses import dataclass


@dataclass
class BoundedWorkLease:
    gate: "BoundedWorkGate"
    released: bool = False

    async def release(self) -> None:
        if not self.released:
            self.released = True
            await self.gate._release()


class BoundedWorkGate:
    def __init__(self):
        self._active = 0
        self._waiting = 0
        self._condition = asyncio.Condition()

    async def acquire(
        self, *, limit: int, max_waiters: int, wait_timeout: float
    ) -> BoundedWorkLease | None:
        if limit < 1 or max_waiters < 0 or wait_timeout <= 0:
            raise ValueError("有界工作闸门参数无效")
        async with self._condition:
            if self._active < limit:
                self._active += 1
                return BoundedWorkLease(self)
            if self._waiting >= max_waiters:
                return None
            self._waiting += 1
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._active < limit),
                    timeout=wait_timeout,
                )
            except asyncio.TimeoutError:
                return None
            finally:
                self._waiting -= 1
            self._active += 1
            return BoundedWorkLease(self)

    async def _release(self) -> None:
        async with self._condition:
            if self._active > 0:
                self._active -= 1
            self._condition.notify(1)

    def snapshot(self) -> dict:
        return {"active": self._active, "waiting": self._waiting}


ai_reply_work_gate = BoundedWorkGate()
