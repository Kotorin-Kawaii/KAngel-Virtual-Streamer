"""赞助者名单的后台同步任务。

骨架与 integrations/superchat/consumer.py 一致：可 start/stop、循环永不因单次
异常退出、所有 SQLite 调用走 asyncio.to_thread。

这条链路是纯旁路：失败只会让感谢墙停留在上一次成功的数据，
绝不影响弹幕、SC、AI 回复或鉴权。
"""

import asyncio

from config import settings
from kangel.shared.logging import logger

from .client import AfdianError
from .service import SponsorService, sponsor_service


class SponsorSyncWorker:
    def __init__(self, service: SponsorService = sponsor_service, sleep=None):
        self.service = service
        # 注入点：测试用来把退避与轮询间隔压成一次事件循环让渡。
        self.sleep = sleep or asyncio.sleep
        self._running = False
        self._task = None

    async def start(self) -> None:
        config = settings.sponsor
        if not (config.enabled and config.sync_enabled):
            logger.info("赞助名单同步未启用，跳过启动")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="sponsor-sync")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _backoff_seconds(self, failures: int) -> float:
        """连续失败时指数退避，上限由配置约束。"""
        config = settings.sponsor
        delay = config.sync_backoff_seconds * (2 ** max(0, failures - 1))
        return float(min(delay, config.sync_max_backoff_seconds))

    async def _run(self) -> None:
        while self._running:
            delay = float(settings.sponsor.sync_interval_seconds)
            try:
                count = await asyncio.to_thread(self.service.sync_once)
                logger.info("赞助名单同步成功，共 %s 位赞助者", count)
            except asyncio.CancelledError:
                raise
            except AfdianError as exc:
                delay = await self._handle_failure(exc.code, exc)
            except Exception as exc:
                logger.exception("赞助名单同步异常")
                delay = await self._handle_failure("unexpected_error", exc)
            try:
                await self.sleep(delay)
            except asyncio.CancelledError:
                raise

    async def _handle_failure(self, code: str, exc: Exception) -> float:
        """记录失败并返回下次尝试前的等待秒数；记录本身失败也不能中断循环。"""
        try:
            failures = await asyncio.to_thread(self.service.record_failure, code)
        except Exception:
            logger.exception("写入赞助同步失败状态时出错")
            failures = 1
        logger.warning(
            "赞助名单同步失败 [%s]（连续 %s 次）: %s", code, failures, exc
        )
        return self._backoff_seconds(failures)


sponsor_sync_worker = SponsorSyncWorker()
