"""
心情数值实时推送服务
通过WebSocket定期推送主播心情状态
"""

import asyncio
import json
from datetime import datetime
from typing import Set, Dict, Any
from fastapi import WebSocket

from config import settings
from utils.logger import logger
from core.persona_engine import persona_engine


class MoodPusher:
    """心情数值推送器"""
    
    def __init__(self):
        self._subscribers: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._running = False
        self._push_task: asyncio.Task = None
        self._push_interval_ms = settings.persona.mood_push_interval_ms
        self._enable_push = settings.persona.enable_mood_push
        
        # 推送统计
        self._stats = {
            "total_pushes": 0,
            "start_time": None,
            "last_push_time": None
        }
    
    async def start(self):
        """启动推送服务"""
        if not self._enable_push:
            logger.info("心情推送服务已禁用")
            return
        
        if self._running:
            logger.warning("心情推送服务已在运行")
            return
        
        self._running = True
        self._stats["start_time"] = datetime.now().isoformat()
        self._push_task = asyncio.create_task(self._push_loop())
        
        logger.info(f"🚀 心情推送服务启动，推送间隔: {self._push_interval_ms}ms")
    
    async def stop(self):
        """停止推送服务"""
        if not self._running:
            return
        
        self._running = False
        
        if self._push_task:
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
        
        logger.info("🛑 心情推送服务已停止")
    
    async def subscribe(self, websocket: WebSocket):
        """订阅心情推送"""
        async with self._lock:
            self._subscribers.add(websocket)
        
        # 立即发送一次当前状态
        await self._send_mood_to_client(websocket)
        
        logger.debug(f"新客户端订阅心情推送，当前订阅数: {len(self._subscribers)}")
    
    async def unsubscribe(self, websocket: WebSocket):
        """取消订阅"""
        async with self._lock:
            if websocket in self._subscribers:
                self._subscribers.discard(websocket)
                logger.debug(f"客户端取消订阅心情推送，当前订阅数: {len(self._subscribers)}")
    
    async def _push_loop(self):
        """推送循环"""
        interval_seconds = self._push_interval_ms / 1000.0
        
        while self._running:
            try:
                await asyncio.sleep(interval_seconds)
                
                if self._subscribers:
                    await self._broadcast_mood()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"心情推送循环出错: {e}")
                await asyncio.sleep(1)  # 出错后等待1秒再重试
    
    async def _broadcast_mood(self):
        """广播心情状态到所有订阅者"""
        mood_data = self._build_mood_data()
        message = {
            "type": "mood_update",
            "data": mood_data,
            "timestamp": datetime.now().isoformat()
        }
        
        disconnected = set()
        
        async with self._lock:
            for websocket in self._subscribers:
                try:
                    await websocket.send_text(json.dumps(message, ensure_ascii=False))
                except Exception as e:
                    logger.error(f"发送心情数据失败: {e}")
                    disconnected.add(websocket)
            
            # 移除断开的连接
            for ws in disconnected:
                self._subscribers.discard(ws)
        
        self._stats["total_pushes"] += 1
        self._stats["last_push_time"] = datetime.now().isoformat()
        
        if self._stats["total_pushes"] % 10 == 0:  # 每10次推送记录一次日志
            logger.debug(f"心情推送 #{self._stats['total_pushes']}，订阅数: {len(self._subscribers)}")
    
    async def _send_mood_to_client(self, websocket: WebSocket):
        """发送心情状态给单个客户端"""
        try:
            mood_data = self._build_mood_data()
            message = {
                "type": "mood_update",
                "data": mood_data,
                "timestamp": datetime.now().isoformat()
            }
            await websocket.send_text(json.dumps(message, ensure_ascii=False))
        except Exception as e:
            logger.error(f"发送心情数据给新客户端失败: {e}")
    
    def _build_mood_data(self) -> Dict[str, Any]:
        """构建心情数据"""
        state = persona_engine.state
        behavior = persona_engine.behavior
        
        return {
            "mood": {
                "value": round(state.mood, 3),
                "label": self._get_mood_label(state.mood),
                "description": self._get_mood_description(state.mood)
            },
            "darkness": {
                "value": round(state.darkness, 3),
                "label": self._get_darkness_label(state.darkness),
                "description": self._get_darkness_description(state.darkness)
            },
            "stress": {
                "value": round(state.stress, 3),
                "label": self._get_stress_label(state.stress),
                "description": self._get_stress_description(state.stress)
            },
            "behavior": {
                "reply_aggressiveness": round(behavior.reply_aggressiveness, 3),
                "ignore_probability": round(behavior.ignore_probability, 3)
            },
            "streamer_name": settings.persona.streamer_name
        }
    
    def _get_mood_label(self, mood: float) -> str:
        """获取心情标签"""
        if mood >= 0.8:
            return "非常开心"
        elif mood >= 0.6:
            return "心情不错"
        elif mood >= 0.4:
            return "平静"
        elif mood >= 0.2:
            return "有点低落"
        else:
            return "很不开心"
    
    def _get_mood_description(self, mood: float) -> str:
        """获取心情描述"""
        if mood >= 0.8:
            return "今天心情超级好！想和所有人聊天！"
        elif mood >= 0.6:
            return "感觉还不错，可以继续直播~"
        elif mood >= 0.4:
            return "一般般吧，没什么特别的"
        elif mood >= 0.2:
            return "有点不开心，需要安慰..."
        else:
            return "心情很差，可能需要休息一下"
    
    def _get_darkness_label(self, darkness: float) -> str:
        """获取阴暗度标签"""
        if darkness >= 0.8:
            return "极度阴暗"
        elif darkness >= 0.6:
            return "比较阴暗"
        elif darkness >= 0.4:
            return "有些阴暗"
        elif darkness >= 0.2:
            return "比较阳光"
        else:
            return "非常阳光"
    
    def _get_darkness_description(self, darkness: float) -> str:
        """获取阴暗度描述"""
        if darkness >= 0.8:
            return "这个世界...已经没有什么好留恋的了"
        elif darkness >= 0.6:
            return "有时候会觉得，一切都毫无意义"
        elif darkness >= 0.4:
            return "偶尔会想一些深沉的问题"
        elif darkness >= 0.2:
            return "总体来说还是个阳光的人"
        else:
            return "今天也是元气满满的一天！"
    
    def _get_stress_label(self, stress: float) -> str:
        """获取压力标签"""
        if stress >= 0.8:
            return "压力爆棚"
        elif stress >= 0.6:
            return "压力很大"
        elif stress >= 0.4:
            return "有些压力"
        elif stress >= 0.2:
            return "压力不大"
        else:
            return "毫无压力"
    
    def _get_stress_description(self, stress: float) -> str:
        """获取压力描述"""
        if stress >= 0.8:
            return "快要崩溃了，需要休息！"
        elif stress >= 0.6:
            return "压力有点大，需要放松一下"
        elif stress >= 0.4:
            return "有点紧张，但还能坚持"
        elif stress >= 0.2:
            return "感觉很轻松"
        else:
            return "完全没有压力，超放松的~"
    
    def get_stats(self) -> Dict[str, Any]:
        """获取推送统计信息"""
        return {
            **self._stats,
            "subscriber_count": len(self._subscribers),
            "is_running": self._running,
            "push_interval_ms": self._push_interval_ms,
            "enable_push": self._enable_push
        }


# 全局心情推送器实例
mood_pusher = MoodPusher()
