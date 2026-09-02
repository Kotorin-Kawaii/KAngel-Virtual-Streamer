"""爱发电订单财务同步 worker；失败只影响透明账更新时间。"""

import asyncio

from config import settings
from kangel.shared.logging import logger

from .client import AfdianError
from .finance import SponsorFinanceError, SponsorFinanceService, sponsor_finance_service


class SponsorFinanceSyncWorker:
    def __init__(self, service: SponsorFinanceService = sponsor_finance_service, sleep=None):
        self.service = service
        self.sleep = sleep or asyncio.sleep
        self._running = False
        self._task = None

    async def start(self) -> None:
        if not settings.sponsor.finance_sync_enabled:
            logger.info("赞助资金同步未启用，跳过启动")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="sponsor-finance-sync")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_once(self) -> int:
        """管理端手动同步；数据库/网络错误由调用方转换为安全响应。"""
        try:
            return await asyncio.to_thread(self.service.sync_once)
        except (AfdianError, SponsorFinanceError) as exc:
            await asyncio.to_thread(self.service.record_failure, getattr(exc, "code", "sync_error"))
            raise
        except Exception:
            # 手动同步也要留下可观测的失败状态，但不能让 SQLite/网络异常
            # 的原始消息穿透到管理端响应。
            try:
                await asyncio.to_thread(self.service.record_failure, "unexpected_error")
            except Exception:
                pass
            raise

    def _backoff_seconds(self, failures: int) -> float:
        delay = settings.sponsor.sync_backoff_seconds * (2 ** max(0, failures - 1))
        return float(min(delay, settings.sponsor.sync_max_backoff_seconds))

    async def _run(self) -> None:
        while self._running:
            delay = float(settings.sponsor.finance_sync_interval_seconds)
            try:
                count = await asyncio.to_thread(self.service.sync_once)
                logger.info("赞助资金同步成功，订单 %s 条", count)
            except asyncio.CancelledError:
                raise
            except (AfdianError, SponsorFinanceError) as exc:
                try:
                    failures = await asyncio.to_thread(self.service.record_failure, getattr(exc, "code", "sync_error"))
                except Exception:
                    failures = 1
                    logger.exception("写入赞助资金同步失败状态时出错")
                delay = self._backoff_seconds(failures)
                logger.warning("赞助资金同步失败 [%s]（连续 %s 次）", getattr(exc, "code", "sync_error"), failures)
            except Exception:
                logger.exception("赞助资金同步异常")
                try:
                    failures = await asyncio.to_thread(self.service.record_failure, "unexpected_error")
                except Exception:
                    failures = 1
                delay = self._backoff_seconds(failures)
            try:
                await self.sleep(delay)
            except asyncio.CancelledError:
                raise


sponsor_finance_sync_worker = SponsorFinanceSyncWorker()
