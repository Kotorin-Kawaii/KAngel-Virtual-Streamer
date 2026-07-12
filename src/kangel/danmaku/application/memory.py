"""
弹幕记忆与分析模块
具备对历史弹幕信息的记忆与分析能力
"""

import asyncio
from typing import Dict, List, Optional, Set, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict, deque

from .text_analyzer import chinese_text_analyzer
from kangel.shared.logging import logger
from config import settings


@dataclass
class DanmakuMemoryItem:
    """单个弹幕记忆项"""
    danmaku_id: str
    user_id: str  # 可以是用户昵称或唯一标识
    nickname: str
    content: str
    timestamp: datetime
    importance: float = 0.5  # 0-1，重要程度
    topic_keywords: List[str] = field(default_factory=list)
    sentiment: float = 0.0  # -1到1，情感倾向
    decay_factor: float = 1.0  # 衰减因子
    related_danmaku_ids: Set[str] = field(default_factory=set)  # 相关联的弹幕ID


@dataclass
class UserMemory:
    """用户记忆"""
    user_id: str
    nickname: str
    danmaku_history: deque[DanmakuMemoryItem]  # 最新的弹幕在左侧
    topic_map: Dict[str, List[DanmakuMemoryItem]]  # 话题到弹幕的映射
    last_active: datetime
    importance_score: float = 0.5  # 用户重要程度


@dataclass
class TopicNode:
    """话题节点"""
    topic_id: str
    keywords: List[str]
    danmaku_count: int = 0
    user_count: int = 0
    users: Set[str] = field(default_factory=set)
    timestamp: datetime = field(default_factory=datetime.now)
    intensity: float = 0.0  # 话题强度 0-1


class DanmakuMemoryManager:
    """弹幕记忆管理器"""
    
    def __init__(self):
        # 用户记忆：user_id -> UserMemory
        self.user_memories: Dict[str, UserMemory] = {}
        
        # 话题图谱：topic_id -> TopicNode
        self.topic_graph: Dict[str, TopicNode] = {}
        
        # 时间窗口：最近的弹幕
        self.time_window: deque[DanmakuMemoryItem] = deque(maxlen=settings.danmaku.memory_time_window_size)  # 最近N条弹幕
        
        # 话题热度映射：话题 -> 热度值
        self.topic_heat: Dict[str, float] = {}
        
        # 锁
        self._lock = asyncio.Lock()
        
        # 配置参数
        self.max_user_danmaku = settings.danmaku.memory_max_user_danmaku  # 每个用户最多记忆的弹幕数
        self.topic_decay_time = timedelta(minutes=settings.danmaku.memory_topic_decay_time)  # 话题衰减时间
        self.user_inactive_time = timedelta(minutes=settings.danmaku.memory_user_inactive_time)  # 用户不活跃时间
        self.max_topic_keywords = settings.danmaku.memory_max_topic_keywords  # 每个弹幕最多提取的关键词数
        self.max_topic_memories = settings.danmaku.memory_max_topic_memories  # 每个话题最多记忆的弹幕数
        
        logger.info(f"✅ 弹幕记忆管理器初始化成功 - 时间窗口大小: {settings.danmaku.memory_time_window_size}, 用户弹幕上限: {self.max_user_danmaku}")
    
    async def add_danmaku(
        self,
        danmaku_id: str,
        user_id: str,
        nickname: str,
        content: str,
        timestamp: Optional[datetime] = None
    ) -> DanmakuMemoryItem:
        """
        添加一条弹幕到记忆中
        """
        try:
            # 只在info级别记录开始处理的信息
            logger.info(f"[弹幕记忆] 处理弹幕 - 用户: {nickname}, 内容: {content[:50]}...")
            
            if timestamp is None:
                timestamp = datetime.now()
            
            # 提取话题关键词 - 锁外执行
            topic_keywords = self._extract_topic_keywords(content)
            
            # 分析情感倾向 - 锁外执行
            sentiment = self._analyze_sentiment(content)
            
            # 创建记忆项 - 锁外执行
            memory_item = DanmakuMemoryItem(
                danmaku_id=danmaku_id,
                user_id=user_id,
                nickname=nickname,
                content=content,
                timestamp=timestamp,
                topic_keywords=topic_keywords,
                sentiment=sentiment
            )
            
            async with self._lock:
                # 添加到时间窗口
                self.time_window.appendleft(memory_item)
                
                # 处理用户记忆
                await self._update_user_memory(memory_item)
                
                # 处理话题分析
                await self._update_topic_analysis(memory_item)
                
                # 清理过期记忆 - 每10条弹幕清理一次，减少锁持有时间
                if len(self.time_window) % 10 == 0:
                    await self._cleanup_expired_memory()
            
            # 定期输出状态 - 每20条弹幕输出一次
            if len(self.time_window) % 20 == 0:
                stats = await self.get_stats()
                logger.info(f"[弹幕记忆] 状态 - 用户: {stats['total_users']}, 弹幕: {stats['total_danmaku']}, 话题: {stats['total_topics']}")
            
            return memory_item
        except Exception as e:
            logger.error(f"[弹幕记忆] 处理弹幕时出错: {str(e)}", exc_info=True)
            # 创建一个基本的记忆项作为 fallback
            fallback_item = DanmakuMemoryItem(
                danmaku_id=danmaku_id,
                user_id=user_id,
                nickname=nickname,
                content=content,
                timestamp=timestamp or datetime.now(),
                topic_keywords=[],
                sentiment=0.0
            )
            return fallback_item
    
    async def _update_user_memory(self, memory_item: DanmakuMemoryItem):
        """更新用户记忆"""
        user_id = memory_item.user_id
        
        is_new_user = user_id not in self.user_memories
        if is_new_user:
            # 创建新用户记忆
            self.user_memories[user_id] = UserMemory(
                user_id=user_id,
                nickname=memory_item.nickname,
                danmaku_history=deque(maxlen=self.max_user_danmaku),
                topic_map=defaultdict(list),
                last_active=memory_item.timestamp
            )
        
        user_memory = self.user_memories[user_id]
        user_memory.last_active = memory_item.timestamp
        
        # 添加到用户弹幕历史
        user_memory.danmaku_history.appendleft(memory_item)
        
        # 更新用户话题映射
        for keyword in memory_item.topic_keywords:
            user_memory.topic_map[keyword].append(memory_item)
            # 限制每个话题的记忆数量
            if len(user_memory.topic_map[keyword]) > self.max_topic_memories:
                user_memory.topic_map[keyword] = user_memory.topic_map[keyword][:self.max_topic_memories]
    
    async def _update_topic_analysis(self, memory_item: DanmakuMemoryItem):
        """更新话题分析"""
        for keyword in memory_item.topic_keywords:
            topic_id = self._get_topic_id(keyword)
            
            is_new_topic = topic_id not in self.topic_graph
            if is_new_topic:
                self.topic_graph[topic_id] = TopicNode(
                    topic_id=topic_id,
                    keywords=[keyword],
                    timestamp=memory_item.timestamp
                )
                # 只在创建新话题时记录info日志
                logger.info(f"[弹幕记忆] 话题创建 - 话题: {keyword}, 用户: {memory_item.nickname}")
            
            topic = self.topic_graph[topic_id]
            
            topic.danmaku_count += 1
            topic.users.add(memory_item.user_id)
            topic.user_count = len(topic.users)
            topic.timestamp = memory_item.timestamp
            
            # 计算话题强度
            topic.intensity = min(1.0, topic.danmaku_count / 10)
            
            # 更新话题热度
            self.topic_heat[topic_id] = self._calculate_topic_heat(topic)
    
    def _extract_topic_keywords(self, content: str) -> List[str]:
        """提取标签、作品名、英文实体和动态中文话题。"""
        return chinese_text_analyzer.extract_topics(
            content, max_topics=self.max_topic_keywords
        )
    
    def _analyze_sentiment(self, content: str) -> float:
        """识别否定、转折、反讽、网络梗和主播特有表达。"""
        return chinese_text_analyzer.analyze_sentiment(content)[0]
    
    def _get_topic_id(self, keyword: str) -> str:
        """获取话题ID"""
        return keyword.lower()
    
    def _calculate_topic_heat(self, topic: TopicNode) -> float:
        """计算话题热度"""
        # 基于弹幕数量和用户数量计算热度
        base_heat = min(topic.danmaku_count / 20, 1.0)
        user_factor = min(topic.user_count / 10, 1.0)
        time_factor = self._calculate_time_factor(topic.timestamp)
        
        return base_heat * user_factor * time_factor
    
    def _calculate_time_factor(self, timestamp: datetime) -> float:
        """计算时间衰减因子"""
        delta = datetime.now() - timestamp
        minutes = delta.total_seconds() / 60
        
        # 5分钟内热度最高，之后逐渐衰减
        if minutes < 5:
            return 1.0
        elif minutes < 30:
            return max(0.1, 1.0 - (minutes - 5) / 25)
        else:
            return 0.1
    
    async def _cleanup_expired_memory(self):
        """清理过期记忆"""
        now = datetime.now()
        old_user_count = len(self.user_memories)
        old_topic_count = len(self.topic_graph)
        
        # 清理不活跃用户
        inactive_users = [user_id for user_id, user_memory in self.user_memories.items() 
                        if (now - user_memory.last_active) > self.user_inactive_time]
        
        for user_id in inactive_users:
            del self.user_memories[user_id]
        
        # 清理过期话题
        expired_topics = [topic_id for topic_id, topic in self.topic_graph.items() 
                         if (now - topic.timestamp) > self.topic_decay_time]
        
        for topic_id in expired_topics:
            del self.topic_graph[topic_id]
            if topic_id in self.topic_heat:
                del self.topic_heat[topic_id]
        
        # 清理用户话题映射中的过期话题
        for user_id, user_memory in self.user_memories.items():
            expired_topics_in_user = [topic for topic in user_memory.topic_map.keys() 
                                    if topic not in self.topic_graph]
            for topic in expired_topics_in_user:
                del user_memory.topic_map[topic]
        
        # 输出清理日志 - 只在有清理操作时记录
        user_cleanup_count = old_user_count - len(self.user_memories)
        topic_cleanup_count = old_topic_count - len(self.topic_graph)
        if user_cleanup_count > 0 or topic_cleanup_count > 0:
            logger.info(f"[弹幕记忆] 清理过期记忆 - 用户: {user_cleanup_count}, 话题: {topic_cleanup_count}")
    
    async def get_user_memory(self, user_id: str) -> Optional[UserMemory]:
        """获取用户记忆"""
        async with self._lock:
            user_memory = self.user_memories.get(user_id)
            return user_memory
    
    async def get_related_danmaku(self, user_id: str, topic: str) -> List[DanmakuMemoryItem]:
        """获取用户相关话题的弹幕"""
        async with self._lock:
            user_memory = self.user_memories.get(user_id)
            if not user_memory:
                return []
            
            related_danmaku = user_memory.topic_map.get(topic, [])
            return related_danmaku
    
    async def get_hot_topics(self, limit: int = 5) -> List[Tuple[str, float]]:
        """获取热门话题"""
        async with self._lock:
            # 按热度排序
            hot_topics = sorted(
                self.topic_heat.items(),
                key=lambda x: x[1],
                reverse=True
            )[:limit]
            return hot_topics
    
    async def analyze_group_discussion(self, topic: str) -> Dict[str, Any]:
        """分析群体讨论情况"""
        async with self._lock:
            topic_id = self._get_topic_id(topic)
            topic_node = self.topic_graph.get(topic_id)
            
            if not topic_node:
                return {
                    "topic": topic,
                    "exists": False,
                    "danmaku_count": 0,
                    "user_count": 0,
                    "heat": 0.0,
                    "is_hot": False
                }
            
            heat = self.topic_heat.get(topic_id, 0.0)
            is_hot = heat > 0.7
            
            result = {
                "topic": topic,
                "exists": True,
                "danmaku_count": topic_node.danmaku_count,
                "user_count": topic_node.user_count,
                "heat": heat,
                "is_hot": is_hot
            }
            return result
    
    async def calculate_persona_impact(self) -> Dict[str, float]:
        """计算对人格状态的影响"""
        async with self._lock:
            impact = {
                "mood": 0.0,
                "stress": 0.0,
                "darkness": 0.0
            }
            
            # 分析最近的弹幕
            recent_danmaku = list(self.time_window)[:20]  # 最近20条
            
            if not recent_danmaku:
                return impact
            
            # 计算情感倾向
            total_sentiment = sum(item.sentiment for item in recent_danmaku)
            avg_sentiment = total_sentiment / len(recent_danmaku)
            
            # 直接获取热门话题，不再次获取锁
            hot_topics = sorted(
                self.topic_heat.items(),
                key=lambda x: x[1],
                reverse=True
            )[:3]
            total_heat = sum(heat for _, heat in hot_topics)
            avg_heat = total_heat / len(hot_topics) if hot_topics else 0.0
            
            # 计算用户活跃度
            active_users = len([u for u in self.user_memories.values() 
                             if (datetime.now() - u.last_active).total_seconds() < 300])
            
            # 计算影响
            impact["mood"] = min(1.0, max(-1.0, avg_sentiment * 0.8 + (active_users / 10) * 0.2))
            impact["stress"] = min(1.0, max(0.0, avg_heat * 0.6 + (len(recent_danmaku) / 20) * 0.4))
            
            # 阴暗度影响：基于负面内容和高压力
            negative_count = sum(1 for item in recent_danmaku if item.sentiment < -0.3)
            impact["darkness"] = min(1.0, max(0.0, (negative_count / len(recent_danmaku)) * 0.7 + impact["stress"] * 0.3))
            
            return impact
    
    async def get_memory_context(self, limit: int = 10) -> Dict[str, Any]:
        """获取记忆上下文"""
        async with self._lock:
            recent_danmaku = list(self.time_window)[:limit]
            
            # 直接获取热门话题，不再次获取锁
            # 按热度排序
            hot_topics = sorted(
                self.topic_heat.items(),
                key=lambda x: x[1],
                reverse=True
            )[:5]
            
            active_users = len([u for u in self.user_memories.values() 
                             if (datetime.now() - u.last_active).total_seconds() < 300])
            
            return {
                "recent_danmaku": [
                    {
                        "nickname": item.nickname,
                        "content": item.content,
                        "timestamp": item.timestamp.isoformat(),
                        "sentiment": item.sentiment,
                        "topics": item.topic_keywords
                    }
                    for item in recent_danmaku
                ],
                "hot_topics": [
                    {"topic": topic, "heat": heat}
                    for topic, heat in hot_topics
                ],
                "active_users": active_users,
                "total_users": len(self.user_memories),
                "total_danmaku": len(self.time_window)
            }
    
    async def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        async with self._lock:
            return {
                "total_users": len(self.user_memories),
                "total_danmaku": len(self.time_window),
                "total_topics": len(self.topic_graph),
                "hot_topics_count": len([h for h in self.topic_heat.values() if h > 0.5])
            }


# 全局弹幕记忆管理器实例
danmaku_memory_manager = DanmakuMemoryManager()
