"""
弹幕池模块
管理弹幕的接收、存储、过滤和选择
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Set
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from config import settings
from kangel.shared.logging import logger
from kangel.audience import ViewerIdentity


class DanmakuStatus(Enum):
    """弹幕状态枚举"""
    UNREAD = "unread"      # 未读
    READ = "read"          # 已读
    SELECTED = "selected"  # 已选中（准备回复）
    REPLIED = "replied"    # 已回复
    IGNORED_AFTER_READ = "ignored_after_read"  # 已读后主动让行
    EXPIRED = "expired"    # 已过期


@dataclass
class DanmakuItem:
    """弹幕条目"""
    id: str
    nickname: str
    message: str
    timestamp: datetime
    status: DanmakuStatus = DanmakuStatus.UNREAD
    
    # 额外元数据
    sender_level: int = 1           # 发送者等级
    priority: float = 1.0           # 优先级（用于排序）
    reply_count: int = 0            # 回复次数（防止重复回复）
    
    # 评分相关
    content_score: float = 0.0      # 内容评分
    emotional_match_score: float = 0.0  # 情感匹配度
    
    # 回复相关
    reply_content: str = ""  # 回复内容
    read_at: Optional[str] = None
    consumed_at: Optional[str] = None
    consumption_reason: Optional[str] = None
    viewer_identity: Optional[ViewerIdentity] = None  # 仅供后端身份路由使用
    client_ip: Optional[str] = None  # 仅供后端容量与滥用防护使用
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "nickname": self.nickname,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "status": self.status.value,
            "sender_level": self.sender_level,
            "priority": self.priority,
            "reply_count": self.reply_count,
            "content_score": self.content_score,
            "emotional_match_score": self.emotional_match_score,
            "reply_content": self.reply_content,
            "read_at": self.read_at,
            "consumed_at": self.consumed_at,
            "consumption_reason": self.consumption_reason,
        }
    
    def is_expired(self, time_window_minutes: int = None) -> bool:
        """检查弹幕是否过期"""
        if time_window_minutes is None:
            time_window_minutes = settings.danmaku.time_window_minutes
        
        time_limit = datetime.now() - timedelta(minutes=time_window_minutes)
        return self.timestamp < time_limit
    
    def is_available_for_reply(self) -> bool:
        """检查弹幕是否可用于回复"""
        return (
            self.status in [DanmakuStatus.UNREAD, DanmakuStatus.READ]
            and self.reply_count == 0
            and not self.is_expired()
        )


class DanmakuPool:
    """弹幕池管理器"""
    
    def __init__(self):
        self._unread_pool: deque = deque(maxlen=settings.danmaku.max_unread_pool_size)
        self._read_pool: deque = deque(maxlen=settings.danmaku.max_unread_pool_size)
        self._replied_ids: Set[str] = set()  # 已回复的弹幕ID集合
        self._ignored_ids: Set[str] = set()  # 已读后主动忽略的弹幕ID集合
        # 仅用于进程重启后恢复「已提交 READ、尚未终态」的条目。普通的
        # mark_as_read 不会加入这里，避免同一条正在处理的弹幕被并发重选。
        self._recovered_read_ids: Set[str] = set()
        self._reply_timestamps: deque[datetime] = deque(maxlen=256)
        self._lock = asyncio.Lock()
        
        # 统计信息
        self._stats = {
            "total_received": 0,
            "total_replied": 0,
            "total_expired": 0
        }
    
    async def add_danmaku(
        self,
        danmaku_id: str,
        nickname: str,
        message: str,
        sender_level: int = 1,
        viewer_identity: Optional[ViewerIdentity] = None,
        client_ip: Optional[str] = None,
    ) -> DanmakuItem:
        """添加新弹幕到未读池"""
        item = DanmakuItem(
            id=danmaku_id,
            nickname=nickname,
            message=message,
            timestamp=datetime.now(),
            status=DanmakuStatus.UNREAD,
            sender_level=sender_level,
            viewer_identity=viewer_identity,
            client_ip=client_ip,
        )
        
        async with self._lock:
            self._unread_pool.append(item)
            self._stats["total_received"] += 1
        
        logger.debug(f"弹幕池：添加新弹幕 [ID: {danmaku_id}] 来自 {nickname}")
        return item

    async def restore_read_danmaku(
        self,
        *,
        danmaku_id: str,
        nickname: str,
        message: str,
        timestamp: datetime,
        read_at: str,
    ) -> bool:
        """恢复崩溃前已 READ、但尚未提交终态的弹幕。

        这是一个仅供启动恢复使用的内部入口。恢复项带有独立标记，只有
        它们会进入 selector 的 READ 候选；正常处理中的 READ 项不会因此
        被第二个 worker 抢走。
        """
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone().replace(tzinfo=None)
        item = DanmakuItem(
            id=str(danmaku_id),
            nickname=str(nickname or ""),
            message=str(message or ""),
            timestamp=timestamp,
            status=DanmakuStatus.READ,
            read_at=str(read_at),
        )
        async with self._lock:
            if any(
                existing.id == item.id
                for pool in (self._unread_pool, self._read_pool)
                for existing in pool
            ):
                return False
            self._read_pool.append(item)
            self._recovered_read_ids.add(item.id)
            self._recovered_read_ids.intersection_update(
                {existing.id for existing in self._read_pool}
            )
        logger.info("恢复 P32-B read_pending 弹幕 [ID: %s]", item.id)
        return True
    
    async def mark_as_read(self, danmaku_id: str, *, read_at: Optional[str] = None) -> bool:
        """标记弹幕为已读；SELECTED/UNREAD 均可安全转入 READ。"""
        read_at = read_at or datetime.now(timezone.utc).isoformat()
        async with self._lock:
            for item in list(self._unread_pool):
                if item.id != danmaku_id:
                    continue
                if item.status in (DanmakuStatus.REPLIED, DanmakuStatus.IGNORED_AFTER_READ,
                                   DanmakuStatus.EXPIRED):
                    return False
                item.status = DanmakuStatus.READ
                item.read_at = item.read_at or read_at
                self._unread_pool.remove(item)
                self._read_pool.append(item)
                logger.debug(f"弹幕池：标记为已读 [ID: {danmaku_id}]")
                return True
            for item in self._read_pool:
                if item.id == danmaku_id and item.status in (
                    DanmakuStatus.READ, DanmakuStatus.SELECTED,
                ):
                    item.status = DanmakuStatus.READ
                    item.read_at = item.read_at or read_at
                    return True
        return False

    async def mark_as_ignored_after_read(
        self, danmaku_id: str, *, consumed_at: Optional[str] = None
    ) -> bool:
        """将已读/已选弹幕原子地标为终态 IGNORE_AFTER_READ。"""
        consumed_at = consumed_at or datetime.now(timezone.utc).isoformat()
        async with self._lock:
            for pool in (self._unread_pool, self._read_pool):
                for item in pool:
                    if item.id != danmaku_id:
                        continue
                    if item.status == DanmakuStatus.IGNORED_AFTER_READ:
                        return False
                    if item.status == DanmakuStatus.REPLIED:
                        return False
                    if item.status not in (DanmakuStatus.READ, DanmakuStatus.SELECTED):
                        return False
                    item.status = DanmakuStatus.IGNORED_AFTER_READ
                    item.read_at = item.read_at or consumed_at
                    item.consumed_at = item.consumed_at or consumed_at
                    item.consumption_reason = "ignored_after_read"
                    self._ignored_ids.add(danmaku_id)
                    self._recovered_read_ids.discard(danmaku_id)
                    logger.info(f"弹幕池：标记为已读后忽略 [ID: {danmaku_id}]")
                    return True
        return False
    
    async def mark_as_replied(self, danmaku_id: str, reply_content: str = "") -> bool:
        """标记弹幕为已回复"""
        async with self._lock:
            for pool in (self._unread_pool, self._read_pool):
                for item in pool:
                    if item.id != danmaku_id:
                        continue
                    if item.status in (DanmakuStatus.REPLIED, DanmakuStatus.IGNORED_AFTER_READ,
                                       DanmakuStatus.EXPIRED):
                        return False
                    item.status = DanmakuStatus.REPLIED
                    item.reply_count += 1
                    item.reply_content = reply_content
                    now_text = datetime.now(timezone.utc).isoformat()
                    item.read_at = item.read_at or now_text
                    item.consumed_at = item.consumed_at or now_text
                    item.consumption_reason = "replied"
                    self._replied_ids.add(danmaku_id)
                    self._recovered_read_ids.discard(danmaku_id)
                    self._stats["total_replied"] += 1
                    logger.info(f"弹幕池：标记为已回复 [ID: {danmaku_id}]")
                    return True
        
        return False

    async def record_successful_reply(self) -> None:
        """Record only a terminally committed and successfully broadcast reply."""
        async with self._lock:
            self._reply_timestamps.append(datetime.now(timezone.utc))

    async def release_selection(self, danmaku_id: str) -> bool:
        """AI 容量不足时把尚未处理的选中项安全放回候选池。"""
        async with self._lock:
            for pool in (self._unread_pool, self._read_pool):
                for item in pool:
                    if item.id == danmaku_id and item.status == DanmakuStatus.SELECTED:
                        item.status = (
                            DanmakuStatus.UNREAD
                            if pool is self._unread_pool
                            else DanmakuStatus.READ
                        )
                        if pool is self._read_pool:
                            self._recovered_read_ids.add(danmaku_id)
                        return True
        return False

    async def claim_for_reply(self, danmaku_id: str) -> bool:
        """在池锁内把仍可回复的候选原子标为 SELECTED。"""
        async with self._lock:
            for pool in (self._unread_pool, self._read_pool):
                for item in pool:
                    if item.id == danmaku_id and item.is_available_for_reply():
                        if pool is self._read_pool and item.id not in self._recovered_read_ids:
                            continue
                        item.status = DanmakuStatus.SELECTED
                        self._recovered_read_ids.discard(danmaku_id)
                        return True
        return False
    
    async def get_unread_danmaku(self, limit: int = None) -> List[DanmakuItem]:
        """获取未读弹幕列表（自动过滤过期弹幕）"""
        await self._cleanup_expired()
        
        async with self._lock:
            available = [
                item for item in self._unread_pool 
                if item.is_available_for_reply()
            ]
            
            if limit:
                available = available[:limit]
            
            return available
    
    async def get_available_danmaku_for_selection(self) -> List[DanmakuItem]:
        """获取可用于选择的弹幕（未过期、未回复）"""
        await self._cleanup_expired()
        
        async with self._lock:
            unread = [
                item for item in self._unread_pool 
                if item.is_available_for_reply()
            ]
            recovered = [
                item for item in self._read_pool
                if item.id in self._recovered_read_ids
                and item.is_available_for_reply()
            ]
            return unread + recovered
    
    async def _cleanup_expired(self):
        """清理过期弹幕"""
        async with self._lock:
            expired_count = 0
            
            # 清理未读池
            for item in list(self._unread_pool):
                if item.is_expired():
                    item.status = DanmakuStatus.EXPIRED
                    self._unread_pool.remove(item)
                    expired_count += 1
            
            # 清理已读池
            for item in list(self._read_pool):
                if item.is_expired():
                    item.status = DanmakuStatus.EXPIRED
                    self._read_pool.remove(item)
                    self._recovered_read_ids.discard(item.id)
                    expired_count += 1
            
            if expired_count > 0:
                self._stats["total_expired"] += expired_count
                logger.debug(f"弹幕池：清理了 {expired_count} 条过期弹幕")
    
    def is_already_replied(self, danmaku_id: str) -> bool:
        """检查弹幕是否已被回复过"""
        return danmaku_id in self._replied_ids
    
    def get_pool_stats(self) -> dict:
        """获取弹幕池统计信息"""
        return {
            "unread_count": len(self._unread_pool),
            "read_count": len(self._read_pool),
            "replied_count": len(self._replied_ids),
            "ignored_after_read_count": len(self._ignored_ids),
            "total_received": self._stats["total_received"],
            "total_replied": self._stats["total_replied"],
            "total_expired": self._stats["total_expired"]
        }

    async def get_recent_reply_signal(self, window_seconds: float = 60.0) -> dict:
        """Return a process-local aggregate; never includes reply text or identity."""
        window_seconds = max(1.0, float(window_seconds))
        now = datetime.now(timezone.utc)
        async with self._lock:
            recent = [
                timestamp for timestamp in self._reply_timestamps
                if (now - timestamp).total_seconds() <= window_seconds
            ]
            self._reply_timestamps = deque(recent, maxlen=256)
            last = max(self._reply_timestamps) if self._reply_timestamps else None
        return {
            "count": len(recent),
            "seconds_since_last_reply": (
                max(0.0, (now - last).total_seconds()) if last else None
            ),
        }

    async def has_pending_reply_work(self) -> bool:
        """Return whether a READ/SELECTED item still owns the reply lifecycle."""
        async with self._lock:
            return any(
                item.status in {DanmakuStatus.READ, DanmakuStatus.SELECTED}
                for pool in (self._unread_pool, self._read_pool)
                for item in pool
            )
    
    def get_pool_status(self) -> dict:
        """获取弹幕池详细状态"""
        return {
            "stats": self.get_pool_stats(),
            "unread_items": [item.to_dict() for item in self._unread_pool],
            "read_items": [item.to_dict() for item in self._read_pool],
            "config": {
                "time_window_minutes": settings.danmaku.time_window_minutes,
                "max_pool_size": settings.danmaku.max_unread_pool_size,
                "frequency_threshold": settings.danmaku.frequency_threshold
            }
        }
    
    async def get_replied_danmaku(self, limit: int = 5) -> List[DanmakuItem]:
        """获取已回复的弹幕列表"""
        async with self._lock:
            # 从已读池和未读池中筛选已回复的弹幕
            replied_items = []
            
            # 检查未读池
            for item in self._unread_pool:
                if item.status == DanmakuStatus.REPLIED:
                    replied_items.append(item)
            
            # 检查已读池
            for item in self._read_pool:
                if item.status == DanmakuStatus.REPLIED:
                    replied_items.append(item)
            
            # 按时间戳排序，最近的在前
            replied_items.sort(key=lambda x: x.timestamp, reverse=True)
            
            # 限制数量
            if limit:
                replied_items = replied_items[:limit]
            
            return replied_items


# 全局弹幕池实例
danmaku_pool = DanmakuPool()
