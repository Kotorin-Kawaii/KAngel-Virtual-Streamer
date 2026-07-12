"""
SQLite 数据库实现
使用SQLite记录直播元数据：弹幕列表、回复记录等
"""

import sqlite3
import json
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path

from config import settings
from kangel.shared.logging import logger


class DatabaseManager:
    """SQLite数据库管理器"""
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            # 默认放在项目根目录下的data文件夹
            data_dir = Path("data")
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "stream_data.db")
        
        self.db_path = db_path
        self._init_database()
        logger.info(f"✅ 数据库初始化成功: {db_path}")
    
    @contextmanager
    def _get_connection(self):
        """获取数据库连接上下文管理器"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            if not getattr(e, "expected_business_error", False):
                logger.error(f"数据库操作失败: {e}")
            raise
        finally:
            conn.close()
    
    def _init_database(self):
        """初始化数据库表结构"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 弹幕记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS danmaku_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    danmaku_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    message TEXT NOT NULL,
                    client_ip TEXT,
                    sender_level INTEGER DEFAULT 1,
                    timestamp TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 弹幕回复记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reply_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    danmaku_record_id INTEGER,
                    danmaku_id TEXT NOT NULL,
                    danmaku_nickname TEXT NOT NULL,
                    danmaku_message TEXT NOT NULL,
                    
                    ai_reply TEXT NOT NULL,
                    ai_emotions TEXT,
                    
                    mood_before REAL,
                    stress_before REAL,
                    darkness_before REAL,
                    
                    mood_impact REAL DEFAULT 0,
                    stress_impact REAL DEFAULT 0,
                    darkness_impact REAL DEFAULT 0,
                    
                    mood_after REAL,
                    stress_after REAL,
                    darkness_after REAL,
                    
                    emotional_tone TEXT,
                    content_intensity REAL,
                    context_relevance REAL,
                    analysis_reasoning TEXT,
                    key_factors TEXT,
                    
                    selected_at TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    
                    FOREIGN KEY (danmaku_record_id) REFERENCES danmaku_records(id)
                )
            """)
            
            # 人格状态表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS persona_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mood REAL NOT NULL,
                    stress REAL NOT NULL,
                    darkness REAL NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS persona_internal_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    arousal REAL NOT NULL,
                    fatigue REAL NOT NULL,
                    attachment REAL NOT NULL,
                    confidence REAL NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS persona_event_log (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    mutation_json TEXT NOT NULL,
                    state_before_json TEXT NOT NULL,
                    state_after_json TEXT NOT NULL,
                    pipeline_version TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS audience_relationships (
                    viewer_key TEXT PRIMARY KEY,
                    identity_type TEXT NOT NULL DEFAULT 'legacy_nickname',
                    nickname TEXT NOT NULL,
                    familiarity REAL NOT NULL DEFAULT 0.05,
                    affinity REAL NOT NULL DEFAULT 0.5,
                    trust REAL NOT NULL DEFAULT 0.5,
                    boundary_strikes INTEGER NOT NULL DEFAULT 0,
                    interaction_count INTEGER NOT NULL DEFAULT 0,
                    reply_count INTEGER NOT NULL DEFAULT 0,
                    recent_topics TEXT,
                    last_message TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            legacy_columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(audience_relationships)")
            }
            if "identity_type" not in legacy_columns:
                cursor.execute(
                    "ALTER TABLE audience_relationships "
                    "ADD COLUMN identity_type TEXT NOT NULL DEFAULT 'legacy_nickname'"
                )

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_audience_relationships (
                    account_id TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    familiarity REAL NOT NULL DEFAULT 0.05,
                    affinity REAL NOT NULL DEFAULT 0.5,
                    trust REAL NOT NULL DEFAULT 0.5,
                    boundary_strikes INTEGER NOT NULL DEFAULT 0,
                    interaction_count INTEGER NOT NULL DEFAULT 0,
                    reply_count INTEGER NOT NULL DEFAULT 0,
                    recent_topics TEXT,
                    last_message TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    username_key TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    account_type TEXT NOT NULL DEFAULT 'regular',
                    login_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            account_columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(accounts)")
            }
            if "account_type" not in account_columns:
                cursor.execute(
                    "ALTER TABLE accounts ADD COLUMN account_type "
                    "TEXT NOT NULL DEFAULT 'regular'"
                )
            if "login_enabled" not in account_columns:
                cursor.execute(
                    "ALTER TABLE accounts ADD COLUMN login_enabled "
                    "INTEGER NOT NULL DEFAULT 1"
                )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_nickname_history (
                    account_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    nickname TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    is_current INTEGER NOT NULL DEFAULT 0,
                    mention_presented_at TEXT,
                    PRIMARY KEY (account_id, version),
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_memory_preferences (
                    account_id TEXT PRIMARY KEY,
                    long_term_memory_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_conversation_fragments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    session_scope_id TEXT NOT NULL,
                    danmaku_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    nickname_version INTEGER NOT NULL DEFAULT 1,
                    viewer_message TEXT NOT NULL,
                    streamer_reply TEXT NOT NULL,
                    reply_payload TEXT,
                    topic_key TEXT NOT NULL,
                    topic_label TEXT NOT NULL,
                    transition TEXT NOT NULL,
                    resolved_reference TEXT,
                    sentiment REAL NOT NULL DEFAULT 0.0,
                    importance REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL,
                    UNIQUE(account_id, danmaku_id),
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_topic_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    topic_key TEXT NOT NULL,
                    topic_label TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source_count INTEGER NOT NULL DEFAULT 0,
                    importance REAL NOT NULL DEFAULT 0.5,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL,
                    UNIQUE(account_id, topic_key),
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    migration_id TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS streamer_activity_sessions (
                    stream_session_id TEXT PRIMARY KEY,
                    activity_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    object_name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    min_duration_minutes INTEGER NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    trigger_source TEXT NOT NULL,
                    public_performance INTEGER NOT NULL DEFAULT 0,
                    ended_at TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS streamer_activity_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_session_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    previous_activity_id TEXT,
                    previous_display_name TEXT,
                    previous_object_name TEXT,
                    activity_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    object_name TEXT NOT NULL,
                    trigger_source TEXT NOT NULL,
                    public_performance INTEGER NOT NULL DEFAULT 0,
                    changed_at TEXT NOT NULL,
                    UNIQUE(stream_session_id, version)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_transition_session "
                "ON streamer_activity_transitions(stream_session_id, version DESC)"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sc_queue (
                    sc_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    nickname_version INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    processing_started_at TEXT,
                    lease_expires_at TEXT,
                    completed_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    reply_payload TEXT,
                    failure_code TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                )
            """)
            sc_columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(sc_queue)")
            }
            if "lease_expires_at" not in sc_columns:
                cursor.execute("ALTER TABLE sc_queue ADD COLUMN lease_expires_at TEXT")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_sc_queue_status_time "
                "ON sc_queue(status, accepted_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_sc_queue_account_time "
                "ON sc_queue(account_id, accepted_at DESC)"
            )

            # 为升级前已存在的账号补齐初始昵称版本。
            cursor.execute("""
                INSERT OR IGNORE INTO account_nickname_history (
                    account_id, version, nickname, started_at, is_current
                )
                SELECT account_id, 1, nickname, created_at, 1 FROM accounts
            """)
            cursor.execute("""
                INSERT OR IGNORE INTO account_memory_preferences (
                    account_id, long_term_memory_enabled, updated_at
                )
                SELECT account_id, ?, CURRENT_TIMESTAMP FROM accounts
            """, (1 if settings.memory.enabled_by_default else 0,))
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('account_identity_v1')"
            )
            
            # 创建索引
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_danmaku_timestamp ON danmaku_records(timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_reply_selected_at ON reply_records(selected_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_danmaku_id ON danmaku_records(danmaku_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_reply_danmaku_id ON reply_records(danmaku_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_session_account ON auth_sessions(account_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_session_expiry ON auth_sessions(expires_at)")
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_current_account_nickname "
                "ON account_nickname_history(account_id) WHERE is_current = 1"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_nickname_history_recent "
                "ON account_nickname_history(account_id, ended_at DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_account_fragments_recent "
                "ON account_conversation_fragments(account_id, archived, created_at DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_account_fragments_topic "
                "ON account_conversation_fragments(account_id, topic_key, created_at DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_account_fragments_expiry "
                "ON account_conversation_fragments(expires_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_account_topics_recent "
                "ON account_topic_memories(account_id, last_seen_at DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_account_topics_expiry "
                "ON account_topic_memories(expires_at)"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('account_long_term_memory_v1')"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('account_login_policy_v1')"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('streamer_activity_v1')"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('sc_queue_v1')"
            )
            
            logger.debug("数据库表结构初始化完成")
    
    # ==================== 弹幕记录操作 ====================
    
    def record_danmaku(
        self,
        danmaku_id: str,
        nickname: str,
        message: str,
        client_ip: Optional[str] = None,
        sender_level: int = 1,
        timestamp: Optional[str] = None
    ) -> int:
        """
        记录一条弹幕
        返回: 记录ID
        """
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO danmaku_records 
                (danmaku_id, nickname, message, client_ip, sender_level, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (danmaku_id, nickname, message, client_ip, sender_level, timestamp))
            
            record_id = cursor.lastrowid
            logger.debug(f"弹幕已记录 [ID: {record_id}]: {nickname} - {message[:30]}")
            return record_id
    
    def get_danmaku_records(
        self,
        limit: int = 100,
        offset: int = 0,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """获取弹幕记录列表"""
        query = "SELECT * FROM danmaku_records WHERE 1=1"
        params = []
        
        if start_time:
            query += " AND timestamp >= ?"
            params.append(start_time)
        if end_time:
            query += " AND timestamp <= ?"
            params.append(end_time)
        
        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_danmaku_by_id(self, danmaku_id: str) -> Optional[Dict[str, Any]]:
        """根据danmaku_id获取弹幕记录"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM danmaku_records WHERE danmaku_id = ?", (danmaku_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_danmaku_count(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None
    ) -> int:
        """获取弹幕数量"""
        query = "SELECT COUNT(*) as count FROM danmaku_records WHERE 1=1"
        params = []
        
        if start_time:
            query += " AND timestamp >= ?"
            params.append(start_time)
        if end_time:
            query += " AND timestamp <= ?"
            params.append(end_time)
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchone()["count"]
    
    # ==================== 回复记录操作 ====================
    
    def record_reply(
        self,
        danmaku_id: str,
        danmaku_nickname: str,
        danmaku_message: str,
        ai_reply: Dict[str, Any],
        mood_before: float,
        stress_before: float,
        darkness_before: float,
        mood_impact: float = 0,
        stress_impact: float = 0,
        darkness_impact: float = 0,
        mood_after: Optional[float] = None,
        stress_after: Optional[float] = None,
        darkness_after: Optional[float] = None,
        emotional_tone: Optional[str] = None,
        content_intensity: Optional[float] = None,
        context_relevance: Optional[float] = None,
        analysis_reasoning: Optional[str] = None,
        key_factors: Optional[List[str]] = None,
        selected_at: Optional[str] = None,
        danmaku_record_id: Optional[int] = None
    ) -> int:
        """
        记录一条AI回复
        返回: 记录ID
        """
        if selected_at is None:
            selected_at = datetime.now().isoformat()
        
        if mood_after is None:
            mood_after = mood_before + mood_impact
        if stress_after is None:
            stress_after = stress_before + stress_impact
        if darkness_after is None:
            darkness_after = darkness_before + darkness_impact
        
        ai_reply_json = json.dumps(ai_reply, ensure_ascii=False)
        ai_emotions_json = json.dumps(ai_reply.get("emotions", []), ensure_ascii=False) if ai_reply else None
        key_factors_json = json.dumps(key_factors, ensure_ascii=False) if key_factors else None
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO reply_records (
                    danmaku_record_id, danmaku_id, danmaku_nickname, danmaku_message,
                    ai_reply, ai_emotions,
                    mood_before, stress_before, darkness_before,
                    mood_impact, stress_impact, darkness_impact,
                    mood_after, stress_after, darkness_after,
                    emotional_tone, content_intensity, context_relevance,
                    analysis_reasoning, key_factors,
                    selected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                danmaku_record_id, danmaku_id, danmaku_nickname, danmaku_message,
                ai_reply_json, ai_emotions_json,
                mood_before, stress_before, darkness_before,
                mood_impact, stress_impact, darkness_impact,
                mood_after, stress_after, darkness_after,
                emotional_tone, content_intensity, context_relevance,
                analysis_reasoning, key_factors_json,
                selected_at
            ))
            
            record_id = cursor.lastrowid
            logger.debug(f"回复已记录 [ID: {record_id}]: {danmaku_nickname} - {danmaku_message[:30]}")
            return record_id
    
    def get_reply_records(
        self,
        limit: int = 100,
        offset: int = 0,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """获取回复记录列表"""
        query = "SELECT * FROM reply_records WHERE 1=1"
        params = []
        
        if start_time:
            query += " AND selected_at >= ?"
            params.append(start_time)
        if end_time:
            query += " AND selected_at <= ?"
            params.append(end_time)
        
        query += " ORDER BY selected_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            
            records = []
            for row in cursor.fetchall():
                record = dict(row)
                # 解析JSON字段
                if record.get("ai_reply"):
                    record["ai_reply"] = json.loads(record["ai_reply"])
                if record.get("ai_emotions"):
                    record["ai_emotions"] = json.loads(record["ai_emotions"])
                if record.get("key_factors"):
                    record["key_factors"] = json.loads(record["key_factors"])
                records.append(record)
            
            return records
    
    def get_reply_count(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None
    ) -> int:
        """获取回复数量"""
        query = "SELECT COUNT(*) as count FROM reply_records WHERE 1=1"
        params = []
        
        if start_time:
            query += " AND selected_at >= ?"
            params.append(start_time)
        if end_time:
            query += " AND selected_at <= ?"
            params.append(end_time)
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchone()["count"]

    def get_recent_reply_emotions(self, limit: int = 24) -> List[str]:
        """按实际发送顺序读取最近回复使用的情绪标签。"""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT ai_emotions, ai_reply
                FROM reply_records
                ORDER BY id DESC
                LIMIT ?
            """, (max(1, limit),)).fetchall()

        emotions: List[str] = []
        for row in reversed(rows):
            parsed = []
            if row["ai_emotions"]:
                try:
                    parsed = json.loads(row["ai_emotions"])
                except json.JSONDecodeError:
                    parsed = []
            if not parsed and row["ai_reply"]:
                try:
                    parsed = json.loads(row["ai_reply"]).get("emotions", [])
                except (json.JSONDecodeError, AttributeError):
                    parsed = []
            emotions.extend(str(item) for item in parsed)
        return emotions[-limit:]
    
    # ==================== 数据导出 ====================
    
    def export_danmaku_data(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        导出结构化弹幕数据
        返回包含弹幕和回复的完整数据集
        """
        danmaku_records = self.get_danmaku_records(
            limit=10000,
            start_time=start_time,
            end_time=end_time
        )
        
        reply_records = self.get_reply_records(
            limit=10000,
            start_time=start_time,
            end_time=end_time
        )
        
        # 建立弹幕ID到回复的映射
        reply_map = {}
        for reply in reply_records:
            danmaku_id = reply["danmaku_id"]
            if danmaku_id not in reply_map:
                reply_map[danmaku_id] = []
            reply_map[danmaku_id].append(reply)
        
        # 构建完整数据集
        export_data = {
            "export_time": datetime.now().isoformat(),
            "summary": {
                "total_danmaku": self.get_danmaku_count(start_time, end_time),
                "total_replies": self.get_reply_count(start_time, end_time),
                "time_range": {
                    "start": start_time,
                    "end": end_time
                }
            },
            "danmaku_records": danmaku_records,
            "reply_records": reply_records,
            "danmaku_with_replies": []
        }
        
        # 为每条弹幕添加对应的回复
        for danmaku in danmaku_records:
            danmaku_data = danmaku.copy()
            danmaku_data["replies"] = reply_map.get(danmaku["danmaku_id"], [])
            export_data["danmaku_with_replies"].append(danmaku_data)
        
        return export_data
    
    def get_stats(self) -> Dict[str, Any]:
        """获取数据库统计信息"""
        return {
            "database_path": self.db_path,
            "total_danmaku": self.get_danmaku_count(),
            "total_replies": self.get_reply_count()
        }
    
    def clear_old_data(self, days_to_keep: int = 30):
        """清理旧数据（默认保留30天）"""
        cutoff_time = (datetime.now() - timedelta(days=days_to_keep)).isoformat()
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 删除旧的回复记录
            cursor.execute("DELETE FROM reply_records WHERE selected_at < ?", (cutoff_time,))
            reply_deleted = cursor.rowcount
            
            # 删除旧的弹幕记录
            cursor.execute("DELETE FROM danmaku_records WHERE timestamp < ?", (cutoff_time,))
            danmaku_deleted = cursor.rowcount
            
            # 只保留最新的10条人格状态记录
            cursor.execute("""
                DELETE FROM persona_state 
                WHERE id NOT IN (
                    SELECT id FROM persona_state ORDER BY updated_at DESC LIMIT 10
                )
            """)
            persona_deleted = cursor.rowcount
            
            logger.info(f"清理旧数据完成: 删除了 {danmaku_deleted} 条弹幕, {reply_deleted} 条回复, {persona_deleted} 条人格状态记录")
            return {
                "danmaku_deleted": danmaku_deleted,
                "reply_deleted": reply_deleted,
                "persona_deleted": persona_deleted
            }
    
    # ==================== 人格状态操作 ====================
    
    def save_persona_state(self, mood: float, stress: float, darkness: float) -> int:
        """
        保存人格状态
        返回: 记录ID
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO persona_state (mood, stress, darkness)
                VALUES (?, ?, ?)
            """, (mood, stress, darkness))
            
            record_id = cursor.lastrowid
            logger.debug(f"人格状态已保存 [ID: {record_id}]: mood={mood:.2f}, stress={stress:.2f}, darkness={darkness:.2f}")
            return record_id
    
    def get_latest_persona_state(self) -> Optional[Dict[str, Any]]:
        """
        获取最新的人格状态
        返回: 人格状态字典，或None如果没有记录
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT mood, stress, darkness, updated_at 
                FROM persona_state 
                ORDER BY id DESC 
                LIMIT 1
            """)
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

    def save_internal_persona_state(
        self,
        arousal: float,
        fatigue: float,
        attachment: float,
        confidence: float
    ) -> int:
        """保存仅供后端使用的细粒度人格状态。"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO persona_internal_state
                (arousal, fatigue, attachment, confidence)
                VALUES (?, ?, ?, ?)
            """, (arousal, fatigue, attachment, confidence))
            return cursor.lastrowid

    def get_latest_internal_persona_state(self) -> Optional[Dict[str, Any]]:
        """读取最近一次内部人格状态。"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT arousal, fatigue, attachment, confidence, updated_at
                FROM persona_internal_state
                ORDER BY id DESC
                LIMIT 1
            """)
            row = cursor.fetchone()
            return dict(row) if row else None

    def append_persona_event_log(self, record: Dict[str, Any]) -> None:
        """幂等保存脱敏后的人格事件回放记录。"""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO persona_event_log (
                    event_id, event_type, occurred_at, source, payload_json,
                    mutation_json, state_before_json, state_after_json,
                    pipeline_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record["event_id"], record["event_type"], record["occurred_at"],
                record["source"], json.dumps(record["payload"], ensure_ascii=False),
                json.dumps(record["mutation"], ensure_ascii=False),
                json.dumps(record["state_before"], ensure_ascii=False),
                json.dumps(record["state_after"], ensure_ascii=False),
                record["pipeline_version"],
            ))

    def get_audience_relationship(self, viewer_key: str) -> Optional[Dict[str, Any]]:
        """读取旧版昵称键关系；该表不自动归属到任何账号。"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM audience_relationships WHERE viewer_key = ?",
                (viewer_key,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            result = dict(row)
            result["recent_topics"] = json.loads(result.get("recent_topics") or "[]")
            return result

    def upsert_audience_relationship(self, relationship: Dict[str, Any]) -> None:
        """创建或覆盖一条旧版昵称键关系。"""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO audience_relationships (
                    viewer_key, nickname, familiarity, affinity, trust,
                    boundary_strikes, interaction_count, reply_count,
                    recent_topics, last_message, first_seen_at, last_seen_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(viewer_key) DO UPDATE SET
                    nickname=excluded.nickname,
                    familiarity=excluded.familiarity,
                    affinity=excluded.affinity,
                    trust=excluded.trust,
                    boundary_strikes=excluded.boundary_strikes,
                    interaction_count=excluded.interaction_count,
                    reply_count=excluded.reply_count,
                    recent_topics=excluded.recent_topics,
                    last_message=excluded.last_message,
                    last_seen_at=excluded.last_seen_at,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                relationship["viewer_key"], relationship["nickname"],
                relationship["familiarity"], relationship["affinity"],
                relationship["trust"], relationship["boundary_strikes"],
                relationship["interaction_count"], relationship["reply_count"],
                json.dumps(relationship.get("recent_topics", []), ensure_ascii=False),
                relationship.get("last_message", ""),
                relationship["first_seen_at"], relationship["last_seen_at"]
            ))

    def get_account_audience_relationship(
        self, account_id: str
    ) -> Optional[Dict[str, Any]]:
        """按不可变账号 ID 读取登录用户关系。"""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM account_audience_relationships WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result["viewer_key"] = f"account:{account_id}"
            result["recent_topics"] = json.loads(result.get("recent_topics") or "[]")
            return result

    def upsert_account_audience_relationship(
        self, relationship: Dict[str, Any]
    ) -> None:
        """以不可变账号 ID 创建或更新登录用户关系。"""
        account_id = relationship["account_id"]
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO account_audience_relationships (
                    account_id, nickname, familiarity, affinity, trust,
                    boundary_strikes, interaction_count, reply_count,
                    recent_topics, last_message, first_seen_at, last_seen_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id) DO UPDATE SET
                    nickname=excluded.nickname,
                    familiarity=excluded.familiarity,
                    affinity=excluded.affinity,
                    trust=excluded.trust,
                    boundary_strikes=excluded.boundary_strikes,
                    interaction_count=excluded.interaction_count,
                    reply_count=excluded.reply_count,
                    recent_topics=excluded.recent_topics,
                    last_message=excluded.last_message,
                    last_seen_at=excluded.last_seen_at,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                account_id, relationship["nickname"],
                relationship["familiarity"], relationship["affinity"],
                relationship["trust"], relationship["boundary_strikes"],
                relationship["interaction_count"], relationship["reply_count"],
                json.dumps(relationship.get("recent_topics", []), ensure_ascii=False),
                relationship.get("last_message", ""),
                relationship["first_seen_at"], relationship["last_seen_at"],
            ))

    # ==================== 账号与认证会话 ====================

    def create_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO accounts (
                    account_id, username_key, username, password_salt,
                    password_hash, nickname, account_type, login_enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                account["account_id"], account["username_key"], account["username"],
                account["password_salt"], account["password_hash"],
                account["nickname"], account.get("account_type", "regular"),
                int(account.get("login_enabled", True)), account["created_at"],
            ))
            conn.execute("""
                INSERT INTO account_nickname_history (
                    account_id, version, nickname, started_at, is_current
                ) VALUES (?, 1, ?, ?, 1)
            """, (
                account["account_id"], account["nickname"], account["created_at"],
            ))
            conn.execute("""
                INSERT INTO account_memory_preferences (
                    account_id, long_term_memory_enabled, updated_at
                ) VALUES (?, ?, ?)
            """, (
                account["account_id"],
                1 if settings.memory.enabled_by_default else 0,
                account["created_at"],
            ))
        return self.get_account_by_id(account["account_id"])

    def get_account_by_username_key(self, username_key: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE username_key = ?", (username_key,)
            ).fetchone()
            return dict(row) if row else None

    def get_account_by_id(self, account_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_account_nickname(
        self, account_id: str, nickname: str, changed_at: str
    ) -> Optional[Dict[str, Any]]:
        """原子结束旧昵称版本并创建新版本；账号主键及关系数据不变。"""
        with self._get_connection() as conn:
            account = conn.execute(
                "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            if not account:
                return None
            if account["nickname"] == nickname:
                return dict(account)

            next_version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM account_nickname_history "
                "WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0]
            conn.execute("""
                UPDATE account_nickname_history
                SET ended_at = ?, is_current = 0
                WHERE account_id = ? AND is_current = 1
            """, (changed_at, account_id))
            conn.execute("""
                INSERT INTO account_nickname_history (
                    account_id, version, nickname, started_at, is_current
                ) VALUES (?, ?, ?, ?, 1)
            """, (account_id, next_version, nickname, changed_at))
            conn.execute("""
                UPDATE accounts
                SET nickname = ?, updated_at = ?
                WHERE account_id = ?
            """, (nickname, changed_at, account_id))
            row = conn.execute(
                "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            return dict(row)

    def list_account_nickname_history(self, account_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT account_id, version, nickname, started_at, ended_at,
                       is_current, mention_presented_at
                FROM account_nickname_history
                WHERE account_id = ?
                ORDER BY version DESC
            """, (account_id,)).fetchall()
            return [dict(row) for row in rows]

    def delete_account_nickname_history_version(
        self, account_id: str, version: int
    ) -> bool:
        """只允许物理删除旧版本，确保其不再进入任何提示词或导出。"""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                DELETE FROM account_nickname_history
                WHERE account_id = ? AND version = ? AND is_current = 0
            """, (account_id, version))
            return cursor.rowcount > 0

    def claim_recent_nickname_change(
        self, account_id: str, cutoff: str, presented_at: str
    ) -> Optional[Dict[str, Any]]:
        """最多让一个回复领取一次近期改名提示，避免并发重复提及。"""
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT account_id, version, nickname, started_at, ended_at
                FROM account_nickname_history
                WHERE account_id = ? AND is_current = 0
                  AND ended_at >= ? AND mention_presented_at IS NULL
                ORDER BY ended_at DESC
                LIMIT 1
            """, (account_id, cutoff)).fetchone()
            if not row:
                return None
            cursor = conn.execute("""
                UPDATE account_nickname_history
                SET mention_presented_at = ?
                WHERE account_id = ? AND version = ?
                  AND mention_presented_at IS NULL
            """, (presented_at, account_id, row["version"]))
            return dict(row) if cursor.rowcount == 1 else None

    # ==================== 账号人物记忆治理 ====================

    def get_account_memory_preference(self, account_id: str) -> Dict[str, Any]:
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT account_id, long_term_memory_enabled, updated_at
                FROM account_memory_preferences WHERE account_id = ?
            """, (account_id,)).fetchone()
            if row:
                result = dict(row)
                result["long_term_memory_enabled"] = bool(
                    result["long_term_memory_enabled"]
                )
                return result
        return {
            "account_id": account_id,
            "long_term_memory_enabled": settings.memory.enabled_by_default,
            "updated_at": None,
        }

    def set_account_memory_preference(
        self, account_id: str, enabled: bool, updated_at: str
    ) -> Dict[str, Any]:
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO account_memory_preferences (
                    account_id, long_term_memory_enabled, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    long_term_memory_enabled=excluded.long_term_memory_enabled,
                    updated_at=excluded.updated_at
            """, (account_id, int(enabled), updated_at))
        return self.get_account_memory_preference(account_id)

    def delete_account_persona_memory(self, account_id: str) -> None:
        """删除人物记忆但保留账号、认证会话和昵称身份历史。"""
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM account_audience_relationships WHERE account_id = ?",
                (account_id,),
            )
            # P10 表启用后也由同一治理入口清理，兼容尚未创建的数据库。
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table in (
                "account_conversation_fragments",
                "account_topic_memories",
                "account_memory_summaries",
            ):
                if table in tables:
                    conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))

    def purge_expired_account_relationships(self, cutoff: str) -> int:
        with self._get_connection() as conn:
            cursor = conn.execute("""
                DELETE FROM account_audience_relationships
                WHERE last_seen_at < ?
            """, (cutoff,))
            return cursor.rowcount

    # ==================== 账号长期对话与话题记忆 ====================

    def insert_account_conversation_fragment(
        self, fragment: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO account_conversation_fragments (
                    account_id, session_scope_id, danmaku_id, nickname,
                    nickname_version, viewer_message, streamer_reply,
                    reply_payload, topic_key, topic_label, transition,
                    resolved_reference, sentiment, importance, created_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                fragment["account_id"], fragment["session_scope_id"],
                fragment["danmaku_id"], fragment["nickname"],
                fragment["nickname_version"], fragment["viewer_message"],
                fragment["streamer_reply"],
                json.dumps(fragment.get("reply_payload"), ensure_ascii=False),
                fragment["topic_key"], fragment["topic_label"],
                fragment["transition"], fragment.get("resolved_reference"),
                fragment.get("sentiment", 0.0), fragment["importance"],
                fragment["created_at"], fragment["expires_at"],
            ))
            row = conn.execute("""
                SELECT * FROM account_conversation_fragments
                WHERE account_id = ? AND danmaku_id = ?
            """, (fragment["account_id"], fragment["danmaku_id"])).fetchone()
            return self._decode_conversation_fragment(row)

    def list_account_conversation_fragments(
        self,
        account_id: str,
        *,
        limit: int = 20,
        topic_keys: Optional[List[str]] = None,
        include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        conditions = ["account_id = ?"]
        params: List[Any] = [account_id]
        if not include_archived:
            conditions.append("archived = 0")
        if topic_keys:
            placeholders = ",".join("?" for _ in topic_keys)
            conditions.append(f"topic_key IN ({placeholders})")
            params.extend(topic_keys)
        params.append(max(1, limit))
        with self._get_connection() as conn:
            rows = conn.execute(f"""
                SELECT * FROM account_conversation_fragments
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """, params).fetchall()
            return [self._decode_conversation_fragment(row) for row in rows]

    def mark_account_fragments_accessed(
        self, account_id: str, fragment_ids: List[int], accessed_at: str
    ) -> None:
        if not fragment_ids:
            return
        placeholders = ",".join("?" for _ in fragment_ids)
        with self._get_connection() as conn:
            conn.execute(f"""
                UPDATE account_conversation_fragments
                SET last_accessed_at = ?, access_count = access_count + 1
                WHERE account_id = ? AND id IN ({placeholders})
            """, [accessed_at, account_id, *fragment_ids])

    def archive_account_fragments(
        self, account_id: str, fragment_ids: List[int]
    ) -> None:
        if not fragment_ids:
            return
        placeholders = ",".join("?" for _ in fragment_ids)
        with self._get_connection() as conn:
            conn.execute(f"""
                UPDATE account_conversation_fragments SET archived = 1
                WHERE account_id = ? AND id IN ({placeholders})
            """, [account_id, *fragment_ids])

    def upsert_account_topic_memory(self, memory: Dict[str, Any]) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO account_topic_memories (
                    account_id, topic_key, topic_label, summary, source_count,
                    importance, first_seen_at, last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, topic_key) DO UPDATE SET
                    topic_label=excluded.topic_label,
                    summary=excluded.summary,
                    source_count=excluded.source_count,
                    importance=excluded.importance,
                    first_seen_at=MIN(first_seen_at, excluded.first_seen_at),
                    last_seen_at=MAX(last_seen_at, excluded.last_seen_at),
                    expires_at=MAX(expires_at, excluded.expires_at)
            """, (
                memory["account_id"], memory["topic_key"], memory["topic_label"],
                memory["summary"], memory["source_count"], memory["importance"],
                memory["first_seen_at"], memory["last_seen_at"], memory["expires_at"],
            ))

    def get_account_topic_memory(
        self, account_id: str, topic_key: str
    ) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM account_topic_memories
                WHERE account_id = ? AND topic_key = ?
            """, (account_id, topic_key)).fetchone()
            return dict(row) if row else None

    def list_account_topic_memories(
        self, account_id: str, *, limit: int = 10
    ) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM account_topic_memories
                WHERE account_id = ?
                ORDER BY last_seen_at DESC, id DESC
                LIMIT ?
            """, (account_id, max(1, limit))).fetchall()
            return [dict(row) for row in rows]

    def mark_account_topics_accessed(
        self, account_id: str, topic_ids: List[int], accessed_at: str
    ) -> None:
        if not topic_ids:
            return
        placeholders = ",".join("?" for _ in topic_ids)
        with self._get_connection() as conn:
            conn.execute(f"""
                UPDATE account_topic_memories
                SET last_accessed_at = ?, access_count = access_count + 1
                WHERE account_id = ? AND id IN ({placeholders})
            """, [accessed_at, account_id, *topic_ids])

    def purge_expired_account_long_term_memory(
        self, now: str, max_archived_per_account: int
    ) -> Dict[str, int]:
        with self._get_connection() as conn:
            fragment_cursor = conn.execute(
                "DELETE FROM account_conversation_fragments WHERE expires_at <= ?", (now,)
            )
            topic_cursor = conn.execute(
                "DELETE FROM account_topic_memories WHERE expires_at <= ?", (now,)
            )
            account_rows = conn.execute("""
                SELECT DISTINCT account_id FROM account_conversation_fragments
                WHERE archived = 1
            """).fetchall()
            trimmed = 0
            for row in account_rows:
                extra_rows = conn.execute("""
                    SELECT id FROM account_conversation_fragments
                    WHERE account_id = ? AND archived = 1
                    ORDER BY importance DESC, created_at DESC
                    LIMIT -1 OFFSET ?
                """, (row["account_id"], max_archived_per_account)).fetchall()
                if extra_rows:
                    ids = [item["id"] for item in extra_rows]
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(
                        f"DELETE FROM account_conversation_fragments "
                        f"WHERE account_id = ? AND id IN ({placeholders})",
                        [row["account_id"], *ids],
                    )
                    trimmed += len(ids)
            return {
                "expired_fragments": fragment_cursor.rowcount,
                "expired_topics": topic_cursor.rowcount,
                "trimmed_fragments": trimmed,
            }

    def _decode_conversation_fragment(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        result = dict(row)
        result["reply_payload"] = json.loads(result.get("reply_payload") or "null")
        result["archived"] = bool(result.get("archived"))
        return result

    def create_auth_session(self, session: Dict[str, Any]) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO auth_sessions (
                    token_hash, account_id, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, NULL)
            """, (
                session["token_hash"], session["account_id"],
                session["created_at"], session["expires_at"],
            ))

    def get_active_auth_session(self, token_hash: str, now: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT s.token_hash, s.account_id, s.created_at AS session_created_at,
                       s.expires_at, a.username, a.nickname, a.created_at,
                       COALESCE(h.version, 1) AS nickname_version
                FROM auth_sessions AS s
                JOIN accounts AS a ON a.account_id = s.account_id
                LEFT JOIN account_nickname_history AS h
                  ON h.account_id = a.account_id AND h.is_current = 1
                WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?
                  AND a.login_enabled = 1
            """, (token_hash, now)).fetchone()
            return dict(row) if row else None


# 全局数据库管理器实例
db_manager = DatabaseManager()
