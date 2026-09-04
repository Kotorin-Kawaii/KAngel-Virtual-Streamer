"""
SQLite 数据库实现
使用SQLite记录直播元数据：弹幕列表、回复记录等
"""

import sqlite3
import json
import hashlib
import uuid
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path

from config import settings
from kangel.shared.logging import logger
from .migrations import upgrade_stream_memory_reliability_v1


class DatabaseManager:
    """SQLite数据库管理器"""

    SQLITE_TIMEOUT_SECONDS = 5.0
    SQLITE_BUSY_TIMEOUT_MS = 5000
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            # 默认放在项目根目录下的data文件夹
            data_dir = Path("data")
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "stream_data.db")
        
        self.db_path = db_path
        self._init_database()
        logger.info(f"数据库初始化成功: {db_path}")
    
    @contextmanager
    def _get_connection(self):
        """获取数据库连接上下文管理器"""
        conn = sqlite3.connect(self.db_path, timeout=self.SQLITE_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        # SQLite 默认会在竞争写锁时立即失败。短暂等待可吸收单进程中
        # 经线程池执行的相邻写入，避免把瞬态锁竞争暴露给直播链路。
        conn.execute(f"PRAGMA busy_timeout = {self.SQLITE_BUSY_TIMEOUT_MS}")
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
            # WAL 让读取不再阻塞写入，仍保持本项目的单进程 SQLite 约束。
            # 内存数据库不支持持久 WAL，测试环境保留其默认模式。
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
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
                    stream_session_id TEXT,
                    source_type TEXT NOT NULL DEFAULT 'legacy',
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
            reply_columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(reply_records)")
            }
            if "stream_session_id" not in reply_columns:
                cursor.execute(
                    "ALTER TABLE reply_records ADD COLUMN stream_session_id TEXT"
                )
            if "source_type" not in reply_columns:
                cursor.execute(
                    "ALTER TABLE reply_records ADD COLUMN source_type TEXT "
                    "NOT NULL DEFAULT 'legacy'"
                )

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS danmaku_processing_claims (
                    stream_session_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    danmaku_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    claim_token TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY (stream_session_id, source_type, danmaku_id)
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
                CREATE TABLE IF NOT EXISTS persona_affect_anchors (
                    stream_session_id TEXT PRIMARY KEY,
                    mood REAL NOT NULL,
                    stress REAL NOT NULL,
                    darkness REAL NOT NULL,
                    source_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                CREATE TABLE IF NOT EXISTS persona_source_event_claims (
                    source_event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (source_event_id, event_type)
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
                CREATE TABLE IF NOT EXISTS auth_refresh_sessions (
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
            # Viewer Impression：账号当前展示留言与异步生成任务完全独立于实时回复链。
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_viewer_impressions (
                    account_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL DEFAULT 1,
                    content TEXT NOT NULL,
                    tone TEXT NOT NULL DEFAULT 'warm',
                    generated_at TEXT NOT NULL,
                    next_request_at TEXT NOT NULL,
                    evidence_cutoff_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                )
            """)
            impression_columns = {
                row[1] for row in cursor.execute(
                    "PRAGMA table_info(account_viewer_impressions)"
                ).fetchall()
            }
            for column, definition in (
                ("evidence_refs_json", "TEXT"),
                ("evidence_counts_json", "TEXT"),
                ("snapshot_hash", "TEXT"),
            ):
                if column not in impression_columns:
                    cursor.execute(
                        f"ALTER TABLE account_viewer_impressions ADD COLUMN {column} {definition}"
                    )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_viewer_impression_tasks (
                    task_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    lease_expires_at TEXT,
                    execution_token TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    error_code TEXT,
                    error_detail TEXT,
                    target_revision INTEGER NOT NULL,
                    evidence_snapshot TEXT,
                    evidence_cutoff_at TEXT,
                    provider TEXT,
                    model TEXT,
                    latency_ms INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id),
                    CHECK(status IN ('pending', 'processing', 'completed', 'failed_retryable', 'failed', 'cancelled')),
                    CHECK(target_revision >= 1)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_viewer_impression_tasks_status "
                "ON account_viewer_impression_tasks(status, next_attempt_at, created_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_viewer_impression_tasks_account "
                "ON account_viewer_impression_tasks(account_id, created_at DESC)"
            )
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_viewer_impression_active_task "
                "ON account_viewer_impression_tasks(account_id) "
                "WHERE status IN ('pending', 'processing', 'failed_retryable')"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    migration_id TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('account_viewer_impression_v1')"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('account_viewer_impression_v2_audit')"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_viewer_impression_stages (
                    task_id TEXT NOT NULL,
                    stage_key TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, stage_key),
                    FOREIGN KEY (task_id) REFERENCES account_viewer_impression_tasks(task_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_viewer_impression_epochs (
                    account_id TEXT PRIMARY KEY,
                    epoch INTEGER NOT NULL DEFAULT 0
                )
            """)
            # A monotonic privacy epoch closes the gap between reading a large
            # snapshot and accepting it: disable/clear/delete followed by
            # immediate re-enable must not admit the old in-memory snapshot.
            for trigger_name, table, event, condition, account_expr in (
                ("impression_optout_cleanup", "account_memory_preferences", "UPDATE OF long_term_memory_enabled",
                 "NEW.long_term_memory_enabled = 0", "NEW.account_id"),
                ("impression_nickname_cleanup", "account_nickname_history", "DELETE", "1", "OLD.account_id"),
                ("impression_account_cleanup", "accounts", "DELETE", "1", "OLD.account_id"),
            ):
                cursor.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger_name} AFTER {event} ON {table}
                    WHEN {condition}
                    BEGIN
                        INSERT INTO account_viewer_impression_epochs(account_id, epoch)
                        VALUES ({account_expr}, 1)
                        ON CONFLICT(account_id) DO UPDATE SET epoch = epoch + 1;
                        UPDATE account_viewer_impression_tasks
                        SET status = 'cancelled', execution_token = NULL, lease_expires_at = NULL,
                            evidence_snapshot = NULL, error_code = 'memory_disabled', error_detail = NULL
                        WHERE account_id = {account_expr}
                          AND status IN ('pending', 'processing', 'failed_retryable');
                        DELETE FROM account_viewer_impressions WHERE account_id = {account_expr};
                    END
                """)
            # Privacy cleanup must cover every existing completion, permanent
            # failure, opt-out, purge and account-deletion path. Triggers keep
            # checkpoint lifetime tied to raw snapshot lifetime even where an
            # older caller clears tasks directly. SQLite foreign_keys is not
            # assumed to be enabled by all callers.
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS impression_stages_snapshot_cleanup
                AFTER UPDATE OF evidence_snapshot, status ON account_viewer_impression_tasks
                WHEN NEW.evidence_snapshot IS NULL OR NEW.status IN ('cancelled', 'failed', 'completed')
                BEGIN
                    DELETE FROM account_viewer_impression_stages WHERE task_id = NEW.task_id;
                END
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS impression_stages_task_cleanup
                AFTER DELETE ON account_viewer_impression_tasks
                BEGIN
                    DELETE FROM account_viewer_impression_stages WHERE task_id = OLD.task_id;
                END
            """)
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('viewer_impression_deep_reflection_v2')"
            )
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
                CREATE TABLE IF NOT EXISTS stream_mainline_sessions (
                    stream_session_id TEXT PRIMARY KEY,
                    theme_id TEXT NOT NULL,
                    theme_date TEXT NOT NULL,
                    special_theme_id TEXT,
                    theme_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    plan_profile_id TEXT NOT NULL,
                    plan_snapshot_json TEXT NOT NULL,
                    plan_version INTEGER NOT NULL DEFAULT 1,
                    current_beat_id TEXT NOT NULL,
                    current_beat_kind TEXT NOT NULL,
                    current_beat_label TEXT NOT NULL,
                    beat_started_at TEXT NOT NULL,
                    beat_version INTEGER NOT NULL DEFAULT 1,
                    trigger_source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT,
                    CHECK(plan_version >= 1),
                    CHECK(beat_version >= 1),
                    CHECK(status IN ('active', 'ended'))
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stream_mainline_beat_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_session_id TEXT NOT NULL,
                    beat_version INTEGER NOT NULL,
                    previous_beat_id TEXT,
                    beat_id TEXT NOT NULL,
                    beat_kind TEXT NOT NULL,
                    beat_label TEXT NOT NULL,
                    activity_version INTEGER,
                    trigger_source TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    UNIQUE(stream_session_id, beat_version),
                    FOREIGN KEY (stream_session_id)
                        REFERENCES stream_mainline_sessions(stream_session_id)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_mainline_transition_session "
                "ON stream_mainline_beat_transitions(stream_session_id, beat_version DESC)"
            )
            mainline_columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(stream_mainline_sessions)")
            }
            if "theme_snapshot_json" not in mainline_columns:
                cursor.execute(
                    "ALTER TABLE stream_mainline_sessions "
                    "ADD COLUMN theme_snapshot_json TEXT NOT NULL DEFAULT '{}'"
                )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stream_director_commits (
                    commit_id TEXT PRIMARY KEY,
                    stream_session_id TEXT NOT NULL,
                    decision_source TEXT NOT NULL,
                    base_plan_version INTEGER NOT NULL,
                    base_beat_version INTEGER NOT NULL,
                    base_activity_version INTEGER NOT NULL,
                    reason_code TEXT NOT NULL,
                    fact_mutations_json TEXT NOT NULL,
                    committed_beat_version INTEGER,
                    committed_activity_version INTEGER,
                    committed_at TEXT NOT NULL,
                    FOREIGN KEY (stream_session_id)
                        REFERENCES stream_mainline_sessions(stream_session_id)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_director_commits_session_time "
                "ON stream_director_commits(stream_session_id, committed_at DESC)"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS streamer_beat_events (
                    stream_session_id TEXT NOT NULL,
                    activity_version INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    beat_type TEXT NOT NULL,
                    display_text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY (stream_session_id, version),
                    UNIQUE (stream_session_id, activity_version)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_streamer_beat_events_session_time "
                "ON streamer_beat_events(stream_session_id, occurred_at DESC)"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS streamer_intent_states (
                    stream_session_id TEXT PRIMARY KEY,
                    interaction_mode TEXT NOT NULL,
                    primary_intent TEXT NOT NULL,
                    energy_level REAL NOT NULL,
                    attention_target TEXT NOT NULL,
                    current_beat TEXT NOT NULL,
                    next_beat_hint TEXT NOT NULL DEFAULT '',
                    last_callback TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_streamer_intent_expiry "
                "ON streamer_intent_states(expires_at)"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stream_special_date_bias_applications (
                    stream_session_id TEXT NOT NULL,
                    special_theme_id TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    PRIMARY KEY (stream_session_id, special_theme_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_english_surprise_jokes (
                    account_id TEXT PRIMARY KEY,
                    used_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stream_english_surprise_jokes (
                    stream_session_id TEXT NOT NULL,
                    viewer_scope TEXT NOT NULL,
                    used_at TEXT NOT NULL,
                    PRIMARY KEY (stream_session_id, viewer_scope)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_stream_english_surprise_jokes_session "
                "ON stream_english_surprise_jokes(stream_session_id)"
            )
            # P21：仅保存脱敏、聚合的场次事实，绝不写入原始互动或账号身份。
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stream_session_facts (
                    stream_session_id TEXT PRIMARY KEY,
                    scheduled_start_at TEXT NOT NULL,
                    scheduled_end_at TEXT NOT NULL,
                    schedule_timezone TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active',
                    facts_json TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    frozen_at TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stream_session_summary_tasks (
                    stream_session_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    input_facts_json TEXT NOT NULL,
                    summary_json TEXT,
                    last_error_code TEXT,
                    source_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY (stream_session_id)
                        REFERENCES stream_session_facts(stream_session_id)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_stream_session_summary_tasks_status "
                "ON stream_session_summary_tasks(status, created_at)"
            )
            # P24：主播私有情景记忆。候选与任务只保存来源引用和结构化信号；
            # 原始文本在异步消费时从既有业务表读取，最终记忆不保存完整原文。
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stream_memory_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    stream_session_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    identity_type TEXT NOT NULL,
                    account_id TEXT,
                    event_type TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT '',
                    salience REAL NOT NULL DEFAULT 0.0,
                    valence REAL NOT NULL DEFAULT 0.0,
                    appraisal_json TEXT NOT NULL DEFAULT '{}',
                    occurred_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    UNIQUE(source_type, source_id)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_candidates_session "
                "ON stream_memory_candidates(stream_session_id, status, salience DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_candidates_account "
                "ON stream_memory_candidates(account_id, status, occurred_at DESC)"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stream_memory_tasks (
                    stream_session_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    candidate_ids_json TEXT NOT NULL,
                    reflection_json TEXT,
                    last_error_code TEXT,
                    source_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_tasks_status "
                "ON stream_memory_tasks(status, created_at)"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stream_episodic_memories (
                    memory_id TEXT PRIMARY KEY,
                    stream_session_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    account_id TEXT,
                    event_type TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL,
                    why_notable TEXT NOT NULL DEFAULT '',
                    emotional_mark TEXT NOT NULL DEFAULT '',
                    follow_up_hint TEXT NOT NULL DEFAULT '',
                    salience REAL NOT NULL DEFAULT 0.0,
                    occurred_at TEXT NOT NULL,
                    evidence_candidate_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_episodic_memories_account "
                "ON stream_episodic_memories(account_id, archived, expires_at, occurred_at DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_episodic_memories_room "
                "ON stream_episodic_memories(scope, archived, expires_at, occurred_at DESC)"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stream_reflections (
                    reflection_id TEXT PRIMARY KEY,
                    stream_session_id TEXT NOT NULL UNIQUE,
                    summary TEXT NOT NULL,
                    emotional_residue TEXT NOT NULL DEFAULT '',
                    open_callbacks_json TEXT NOT NULL,
                    notable_event_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
            """)
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('stream_episodic_memory_v1')"
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

            # P25 赞助感谢墙：只保存展示所需的最小字段。
            # 不保存订单号 / out_trade_no / 计划 ID / 收货信息；
            # sum_amount_cents 仅用于 admin 内部统计，绝不出现在公开响应中。
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sponsor_records (
                    platform TEXT NOT NULL,
                    platform_user_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    anonymous INTEGER NOT NULL DEFAULT 0,
                    hidden INTEGER NOT NULL DEFAULT 0,
                    sum_amount_cents INTEGER NOT NULL DEFAULT 0,
                    first_sponsored_at TEXT,
                    last_sponsored_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (platform, platform_user_id)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_sponsor_records_visible "
                "ON sponsor_records(hidden, platform)"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sponsor_sync_state (
                    platform TEXT PRIMARY KEY,
                    last_success_at TEXT,
                    last_attempt_at TEXT,
                    last_error_code TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    synced_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
            """)
            # Sponsor Fund Transparency：收入账本与 sponsor_records 身份账本物理分离。
            # 这里只保存不可逆订单键、金额、可靠付款时间与聚合月份，不保存任何赞助者字段。
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sponsor_orders (
                    order_key TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                    paid_at TEXT NOT NULL,
                    accounting_month TEXT NOT NULL,
                    order_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_sponsor_orders_month "
                "ON sponsor_orders(platform, accounting_month)"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sponsor_finance_sync_state (
                    platform TEXT PRIMARY KEY,
                    last_success_at TEXT,
                    last_attempt_at TEXT,
                    last_error_code TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    synced_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sponsor_fund_entries (
                    entry_id TEXT PRIMARY KEY,
                    month TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    public_note TEXT,
                    status TEXT NOT NULL CHECK (status IN ('active', 'void')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_sponsor_fund_entries_month "
                "ON sponsor_fund_entries(month, status)"
            )

            # P29 AI token 用量审计：只记调用元数据与 token 计数。
            # 没有 prompt/回复正文、message_id、昵称、账号或 IP，因此不含任何 PII；
            # error_kind 只存异常类名，避免异常消息把 prompt 或密钥带进库。
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ai_token_usage_records (
                    record_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    role TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    usage_reported INTEGER NOT NULL DEFAULT 1,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER,
                    reasoning_tokens_reported INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    error_kind TEXT
                )
            """)
            token_record_columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(ai_token_usage_records)")
            }
            if "reasoning_tokens" not in token_record_columns:
                cursor.execute("ALTER TABLE ai_token_usage_records ADD COLUMN reasoning_tokens INTEGER")
            if "reasoning_tokens_reported" not in token_record_columns:
                cursor.execute("ALTER TABLE ai_token_usage_records ADD COLUMN reasoning_tokens_reported INTEGER NOT NULL DEFAULT 0")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_token_usage_records_day "
                "ON ai_token_usage_records(day, role)"
            )
            # 每日聚合永久保留：明细过期清理后仍能看长期花费曲线。
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ai_token_usage_daily (
                    day TEXT NOT NULL,
                    role TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    calls INTEGER NOT NULL DEFAULT 0,
                    failed_calls INTEGER NOT NULL DEFAULT 0,
                    usage_missing_calls INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_missing_calls INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms_sum INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (day, role, provider, model)
                )
            """)
            token_daily_columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(ai_token_usage_daily)")
            }
            if "reasoning_tokens" not in token_daily_columns:
                cursor.execute("ALTER TABLE ai_token_usage_daily ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0")
            if "reasoning_missing_calls" not in token_daily_columns:
                cursor.execute("ALTER TABLE ai_token_usage_daily ADD COLUMN reasoning_missing_calls INTEGER NOT NULL DEFAULT 0")
            # reasoning token detail did not exist before this migration.  A
            # zero introduced by ALTER TABLE therefore means "unknown", not
            # "the provider explicitly reported no reasoning tokens".  Mark
            # historical calls missing once, then subtract detail rows that
            # explicitly reported reasoning usage (relevant to deployments
            # that briefly ran the additive columns before this migration).
            reasoning_migration_id = "ai_token_reasoning_unknown_v1"
            reasoning_migration_applied = cursor.execute(
                "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
                (reasoning_migration_id,),
            ).fetchone()
            if reasoning_migration_applied is None:
                cursor.execute(
                    "UPDATE ai_token_usage_daily "
                    "SET reasoning_missing_calls = calls"
                )
                cursor.execute(
                    """
                    UPDATE ai_token_usage_daily
                    SET reasoning_missing_calls = MAX(
                        0,
                        calls - COALESCE((
                            SELECT COUNT(*)
                            FROM ai_token_usage_records AS records
                            WHERE records.day = ai_token_usage_daily.day
                              AND records.role = ai_token_usage_daily.role
                              AND records.provider = ai_token_usage_daily.provider
                              AND records.model = ai_token_usage_daily.model
                              AND records.reasoning_tokens_reported = 1
                        ), 0)
                    )
                    """
                )
                cursor.execute(
                    "INSERT INTO schema_migrations(migration_id) VALUES (?)",
                    (reasoning_migration_id,),
                )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_token_usage_daily_day "
                "ON ai_token_usage_daily(day)"
            )

            # 主播管理系统：行为状态与动作审计独立于人物记忆。
            # subject_key 使用 account:<id> 或 guest:<connection_id>，禁止使用昵称。
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_behavior_state (
                    subject_key TEXT PRIMARY KEY,
                    identity_type TEXT NOT NULL,
                    account_id TEXT,
                    stream_session_id TEXT,
                    nickname TEXT,
                    toxicity_score REAL NOT NULL DEFAULT 0,
                    warning_count INTEGER NOT NULL DEFAULT 0,
                    violation_count INTEGER NOT NULL DEFAULT 0,
                    last_violation_at TEXT,
                    mute_until TEXT,
                    pending_action_id TEXT,
                    pending_action TEXT,
                    admin_review_required INTEGER NOT NULL DEFAULT 0,
                    last_decay_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS moderation_actions (
                    moderation_id TEXT PRIMARY KEY,
                    danmaku_id TEXT NOT NULL UNIQUE,
                    subject_key TEXT NOT NULL,
                    identity_type TEXT NOT NULL,
                    account_id TEXT,
                    stream_session_id TEXT,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'reserved',
                    severity REAL NOT NULL DEFAULT 0,
                    toxicity REAL NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0,
                    attack_type TEXT NOT NULL DEFAULT 'none',
                    reason_code TEXT NOT NULL DEFAULT 'none',
                    mute_until TEXT,
                    reply_payload TEXT,
                    message_digest TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_behavior_mute_until "
                "ON user_behavior_state(mute_until)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_moderation_subject_time "
                "ON moderation_actions(subject_key, created_at DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_moderation_status_time "
                "ON moderation_actions(status, created_at DESC)"
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
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_reply_normal_session_danmaku
                ON reply_records(stream_session_id, danmaku_id)
                WHERE source_type = 'normal'
                  AND stream_session_id IS NOT NULL
                  AND stream_session_id != ''
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_session_account ON auth_sessions(account_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_session_expiry ON auth_sessions(expires_at)")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_auth_refresh_session_account "
                "ON auth_refresh_sessions(account_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_auth_refresh_session_expiry "
                "ON auth_refresh_sessions(expires_at)"
            )
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
                "VALUES ('auth_refresh_sessions_v1')"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('streamer_activity_v1')"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('streamer_beat_v1')"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('stream_mainline_v1')"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('stream_director_v1')"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('sc_queue_v1')"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('stream_session_summary_v1')"
            )
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id) "
                "VALUES ('sponsor_wall_v1')"
            )

            # P24 reliability metadata is a formal, idempotent migration so
            # this initializer can safely open the old production database.
            upgrade_stream_memory_reliability_v1(conn)
            
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

    def claim_danmaku_processing(
        self,
        *,
        stream_session_id: str,
        source_type: str,
        danmaku_id: str,
        claim_token: str,
        lease_seconds: int,
    ) -> bool:
        """原子取得一次场次级弹幕处理权；过期 processing claim 可被回收。"""
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO danmaku_processing_claims (
                    stream_session_id, source_type, danmaku_id, status,
                    claim_token, claimed_at, lease_expires_at
                ) VALUES (?, ?, ?, 'processing', ?, ?, ?)
            """, (
                stream_session_id, source_type, danmaku_id,
                claim_token, now_text, lease_expires_at,
            ))
            if cursor.rowcount == 1:
                return True
            cursor = conn.execute("""
                UPDATE danmaku_processing_claims
                SET status='processing', claim_token=?, claimed_at=?,
                    lease_expires_at=?, completed_at=NULL
                WHERE stream_session_id=? AND source_type=? AND danmaku_id=?
                  AND status='processing' AND lease_expires_at <= ?
            """, (
                claim_token, now_text, lease_expires_at,
                stream_session_id, source_type, danmaku_id, now_text,
            ))
            return cursor.rowcount == 1

    def complete_danmaku_processing(
        self,
        *,
        stream_session_id: str,
        source_type: str,
        danmaku_id: str,
        claim_token: str,
    ) -> bool:
        """只允许当前 claim 持有者把处理状态标记为 completed。"""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE danmaku_processing_claims
                SET status='completed', completed_at=?, lease_expires_at=?
                WHERE stream_session_id=? AND source_type=? AND danmaku_id=?
                  AND status='processing' AND claim_token=?
            """, (
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                stream_session_id, source_type, danmaku_id, claim_token,
            ))
            return cursor.rowcount == 1

    def claim_persona_source_event(
        self, *, source_event_id: str, event_type: str
    ) -> bool:
        """在任何人格 mutation 前持久占用稳定 source event。"""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO persona_source_event_claims (
                    source_event_id, event_type, claimed_at
                ) VALUES (?, ?, ?)
            """, (
                source_event_id, event_type, datetime.now(timezone.utc).isoformat()
            ))
            return cursor.rowcount == 1
    
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
        danmaku_record_id: Optional[int] = None,
        stream_session_id: Optional[str] = None,
        source_type: str = "legacy",
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
                    danmaku_record_id, stream_session_id, source_type,
                    danmaku_id, danmaku_nickname, danmaku_message,
                    ai_reply, ai_emotions,
                    mood_before, stress_before, darkness_before,
                    mood_impact, stress_impact, darkness_impact,
                    mood_after, stress_after, darkness_after,
                    emotional_tone, content_intensity, context_relevance,
                    analysis_reasoning, key_factors,
                    selected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                danmaku_record_id, stream_session_id, source_type,
                danmaku_id, danmaku_nickname, danmaku_message,
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

    # ==================== 场次总结事实与任务（P21） ====================

    def create_stream_session_facts(
        self, *, stream_session_id: str, scheduled_start_at: str,
        scheduled_end_at: str, schedule_timezone: str, facts: Dict[str, Any],
        source_version: str, created_at: str,
    ) -> bool:
        """幂等创建场次事实；排期起点是稳定 ID。"""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO stream_session_facts (
                    stream_session_id, scheduled_start_at, scheduled_end_at,
                    schedule_timezone, state, facts_json, source_version, created_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
            """, (
                stream_session_id, scheduled_start_at, scheduled_end_at,
                schedule_timezone, json.dumps(facts, ensure_ascii=False, sort_keys=True),
                source_version, created_at,
            ))
            return cursor.rowcount == 1

    def list_active_stream_session_facts(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM stream_session_facts WHERE state = 'active' "
                "ORDER BY scheduled_start_at"
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["facts"] = json.loads(value.pop("facts_json"))
            values.append(value)
        return values

    def get_stream_session_summary_task(
        self, stream_session_id: str
    ) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM stream_session_summary_tasks WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["input_facts"] = json.loads(value.pop("input_facts_json"))
        value["summary"] = (
            json.loads(value.pop("summary_json")) if value.get("summary_json") else None
        )
        return value

    def get_latest_completed_stream_session_summary(
        self, *, exclude_stream_session_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            if exclude_stream_session_id:
                row = conn.execute("""
                    SELECT * FROM stream_session_summary_tasks
                    WHERE status = 'completed' AND stream_session_id != ?
                    ORDER BY completed_at DESC, stream_session_id DESC LIMIT 1
                """, (exclude_stream_session_id,)).fetchone()
            else:
                row = conn.execute("""
                    SELECT * FROM stream_session_summary_tasks WHERE status = 'completed'
                    ORDER BY completed_at DESC, stream_session_id DESC LIMIT 1
                """).fetchone()
        if not row or not row["summary_json"]:
            return None
        return {
            "stream_session_id": row["stream_session_id"],
            "completed_at": row["completed_at"],
            "summary": json.loads(row["summary_json"]),
        }

    def freeze_stream_session_and_enqueue_summary(
        self, *, stream_session_id: str, facts: Dict[str, Any],
        source_version: str, frozen_at: str,
    ) -> bool:
        """原子冻结事实并创建唯一 pending 任务；重启和重复边界均安全。"""
        serialized = json.dumps(facts, ensure_ascii=False, sort_keys=True)
        with self._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE stream_session_facts
                SET state = 'frozen', facts_json = ?, source_version = ?, frozen_at = ?
                WHERE stream_session_id = ? AND state = 'active'
            """, (serialized, source_version, frozen_at, stream_session_id))
            if cursor.rowcount != 1:
                return False
            conn.execute("""
                INSERT OR IGNORE INTO stream_session_summary_tasks (
                    stream_session_id, status, attempts, input_facts_json,
                    source_version, created_at, updated_at
                ) VALUES (?, 'pending', 0, ?, ?, ?, ?)
            """, (
                stream_session_id, serialized, source_version, frozen_at, frozen_at,
            ))
            return True

    def claim_next_stream_session_summary_task(
        self, *, lease_seconds: int, now: str
    ) -> Optional[Dict[str, Any]]:
        """FIFO 领取低优先级任务，并恢复过期租约。"""
        reference = datetime.fromisoformat(now)
        lease_expires_at = (reference + timedelta(seconds=lease_seconds)).isoformat()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                UPDATE stream_session_summary_tasks
                SET status = 'pending', lease_expires_at = NULL, updated_at = ?
                WHERE status = 'processing'
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            """, (now, now))
            row = conn.execute("""
                SELECT * FROM stream_session_summary_tasks WHERE status = 'pending'
                ORDER BY created_at ASC, stream_session_id ASC LIMIT 1
            """).fetchone()
            if not row:
                return None
            updated = conn.execute("""
                UPDATE stream_session_summary_tasks
                SET status = 'processing', attempts = attempts + 1,
                    lease_expires_at = ?, updated_at = ?
                WHERE stream_session_id = ? AND status = 'pending'
            """, (lease_expires_at, now, row["stream_session_id"]))
            if updated.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM stream_session_summary_tasks WHERE stream_session_id = ?",
                (row["stream_session_id"],),
            ).fetchone()
        value = dict(claimed)
        value["input_facts"] = json.loads(value.pop("input_facts_json"))
        return value

    def release_stream_session_summary_task(self, stream_session_id: str, now: str) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE stream_session_summary_tasks
                SET status = 'pending', lease_expires_at = NULL, updated_at = ?
                WHERE stream_session_id = ? AND status = 'processing'
            """, (now, stream_session_id))

    def complete_stream_session_summary_task(
        self, *, stream_session_id: str, summary: Dict[str, Any], now: str
    ) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE stream_session_summary_tasks
                SET status = 'completed', summary_json = ?, last_error_code = NULL,
                    lease_expires_at = NULL, completed_at = ?, updated_at = ?
                WHERE stream_session_id = ? AND status = 'processing'
            """, (
                json.dumps(summary, ensure_ascii=False, sort_keys=True), now, now,
                stream_session_id,
            ))
            return cursor.rowcount == 1

    def fail_stream_session_summary_task(
        self, *, stream_session_id: str, error_code: str, max_attempts: int, now: str
    ) -> str:
        """有限重试；终态只保留稳定错误码。"""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT attempts FROM stream_session_summary_tasks WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()
            if not row:
                return "missing"
            status = "pending" if row["attempts"] < max_attempts else "failed"
            cursor = conn.execute("""
                UPDATE stream_session_summary_tasks
                SET status = ?, lease_expires_at = NULL, last_error_code = ?,
                    completed_at = ?, updated_at = ?
                WHERE stream_session_id = ? AND status = 'processing'
            """, (
                status, error_code, None if status == "pending" else now, now,
                stream_session_id,
            ))
            return status if cursor.rowcount == 1 else "unchanged"

    # ==================== 主播情景记忆与下播反思（P24） ====================

    def insert_stream_memory_candidate(self, candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """以来源引用幂等写入候选；候选不保存完整弹幕或 SC 正文。"""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO stream_memory_candidates (
                    candidate_id, stream_session_id, scope, identity_type,
                    account_id, event_type, source_type, source_id, topic,
                    salience, valence, appraisal_json, occurred_at, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (
                candidate["candidate_id"], candidate["stream_session_id"],
                candidate.get("scope", "room"), candidate.get("identity_type", "guest"),
                candidate.get("account_id"), candidate["event_type"],
                candidate["source_type"], candidate["source_id"],
                str(candidate.get("topic", ""))[:120],
                float(candidate.get("salience", 0.0)),
                float(candidate.get("valence", 0.0)),
                json.dumps(candidate.get("appraisal", {}), ensure_ascii=False, sort_keys=True),
                candidate["occurred_at"], candidate["created_at"],
            ))
            row = conn.execute(
                "SELECT * FROM stream_memory_candidates WHERE source_type = ? AND source_id = ?",
                (candidate["source_type"], candidate["source_id"]),
            ).fetchone()
        return dict(row) if row else None

    def stream_memory_candidate_exists(self, source_type: str, source_id: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM stream_memory_candidates WHERE source_type = ? AND source_id = ? LIMIT 1",
                (source_type, source_id),
            ).fetchone()
        return row is not None

    def list_stream_memory_candidates(
        self, stream_session_id: str, *, limit: int = 48, pending_only: bool = True
    ) -> List[Dict[str, Any]]:
        conditions = ["stream_session_id = ?"]
        params: List[Any] = [stream_session_id]
        if pending_only:
            conditions.append("status = 'pending'")
        params.append(max(1, limit))
        with self._get_connection() as conn:
            rows = conn.execute(f"""
                SELECT * FROM stream_memory_candidates
                WHERE {' AND '.join(conditions)}
                ORDER BY salience DESC, occurred_at DESC, candidate_id ASC
                LIMIT ?
            """, params).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            try:
                value["appraisal"] = json.loads(value.pop("appraisal_json") or "{}")
            except json.JSONDecodeError:
                value["appraisal"] = {}
            values.append(value)
        return values

    def list_stream_memory_candidate_sessions(
        self, *, exclude_stream_session_id: Optional[str] = None
    ) -> List[str]:
        """返回仍有待冻结候选的场次；用于 P24 脱离 P21 的独立下播收口。"""
        conditions = ["status = 'pending'"]
        params: List[Any] = []
        if exclude_stream_session_id:
            conditions.append("stream_session_id != ?")
            params.append(exclude_stream_session_id)
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT stream_session_id FROM stream_memory_candidates "
                f"WHERE {' AND '.join(conditions)} ORDER BY stream_session_id",
                params,
            ).fetchall()
        return [str(row["stream_session_id"]) for row in rows]

    def get_stream_memory_candidate_inputs(self, candidate_ids: List[str]) -> List[Dict[str, Any]]:
        """消费时按来源读取必要证据；任务表和候选表不复制原始文本。"""
        if not candidate_ids:
            return []
        placeholders = ",".join("?" for _ in candidate_ids)
        with self._get_connection() as conn:
            candidates = conn.execute(
                f"SELECT * FROM stream_memory_candidates WHERE candidate_id IN ({placeholders})",
                candidate_ids,
            ).fetchall()
            result = []
            for row in candidates:
                value = dict(row)
                try:
                    value["appraisal"] = json.loads(value.pop("appraisal_json") or "{}")
                except json.JSONDecodeError:
                    value["appraisal"] = {}
                source_type = value["source_type"]
                source_id = value["source_id"]
                source = None
                if source_type == "account_fragment":
                    source = conn.execute(
                        "SELECT id, viewer_message, streamer_reply, topic_label, sentiment, importance, created_at "
                        "FROM account_conversation_fragments WHERE id = ?", (source_id,)
                    ).fetchone()
                elif source_type == "reply_record":
                    source = conn.execute(
                        "SELECT danmaku_message, ai_reply, emotional_tone, content_intensity, "
                        "context_relevance, selected_at FROM reply_records "
                        "WHERE danmaku_id = ? ORDER BY id DESC LIMIT 1", (source_id,)
                    ).fetchone()
                elif source_type == "sc":
                    source = conn.execute(
                        "SELECT sc_id, content, nickname, accepted_at, completed_at "
                        "FROM sc_queue WHERE sc_id = ?", (source_id,)
                    ).fetchone()
                elif source_type == "moderation":
                    source = conn.execute(
                        "SELECT moderation_id, action, severity, attack_type, reason_code, "
                        "created_at, message_digest FROM moderation_actions WHERE moderation_id = ?",
                        (source_id,),
                    ).fetchone()
                elif source_type == "activity":
                    source = conn.execute(
                        "SELECT stream_session_id, version, activity_id, display_name, object_name, "
                        "trigger_source, changed_at FROM streamer_activity_transitions WHERE id = ?",
                        (source_id,),
                    ).fetchone()
                value["source"] = dict(source) if source else None
                value["source_missing"] = source is None
                result.append(value)
        return result

    def create_stream_memory_task(
        self, *, stream_session_id: str, candidate_ids: List[str], created_at: str,
        source_version: str = "stream_episodic_memory_v1"
    ) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO stream_memory_tasks (
                    stream_session_id, status, attempts, candidate_ids_json,
                    source_version, created_at, updated_at
                ) VALUES (?, 'pending', 0, ?, ?, ?, ?)
            """, (
                stream_session_id,
                json.dumps(
                    list(dict.fromkeys(candidate_ids))[:settings.episodic_memory.max_candidates_per_session],
                    ensure_ascii=False,
                ),
                source_version, created_at, created_at,
            ))
            return cursor.rowcount == 1

    def get_stream_memory_task(self, stream_session_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM stream_memory_tasks WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["candidate_ids"] = json.loads(value.pop("candidate_ids_json") or "[]")
        value["reflection"] = json.loads(value.pop("reflection_json") or "null")
        return value

    def claim_next_stream_memory_task(self, *, lease_seconds: int, now: str) -> Optional[Dict[str, Any]]:
        reference = datetime.fromisoformat(now)
        lease_expires = (reference + timedelta(seconds=lease_seconds)).isoformat()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                UPDATE stream_memory_tasks SET status = 'pending', lease_expires_at = NULL, updated_at = ?
                WHERE status = 'processing' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            """, (now, now))
            row = conn.execute(
                "SELECT * FROM stream_memory_tasks WHERE status = 'pending' "
                "ORDER BY created_at ASC, stream_session_id ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            updated = conn.execute("""
                UPDATE stream_memory_tasks SET status = 'processing', attempts = attempts + 1,
                    lease_expires_at = ?, updated_at = ?
                WHERE stream_session_id = ? AND status = 'pending'
            """, (lease_expires, now, row["stream_session_id"]))
            if updated.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM stream_memory_tasks WHERE stream_session_id = ?",
                (row["stream_session_id"],),
            ).fetchone()
        value = dict(row)
        value["candidate_ids"] = json.loads(value.pop("candidate_ids_json") or "[]")
        return value

    def release_stream_memory_task(self, stream_session_id: str, now: str) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE stream_memory_tasks SET status = 'pending', lease_expires_at = NULL, updated_at = ?
                WHERE stream_session_id = ? AND status = 'processing'
            """, (now, stream_session_id))

    def complete_stream_memory_task(
        self, *, stream_session_id: str, memories: List[Dict[str, Any]],
        reflection: Optional[Dict[str, Any]], now: str
    ) -> bool:
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT candidate_ids_json, status FROM stream_memory_tasks WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()
            if not row or row["status"] != "processing":
                return False
            candidate_ids = json.loads(row["candidate_ids_json"] or "[]")
            active_candidates: dict[str, sqlite3.Row] = {}
            if candidate_ids:
                placeholders = ",".join("?" for _ in candidate_ids)
                active_candidates = {
                    candidate["candidate_id"]: candidate
                    for candidate in conn.execute(
                        f"SELECT * FROM stream_memory_candidates WHERE candidate_id IN ({placeholders})",
                        candidate_ids,
                    ).fetchall()
                }

            # 记忆删除/退出可能与低优先级模型并发发生。提交时再次核验候选
            # 和账号开关，避免一个已在途的总结在删除之后把个人记忆写回来。
            inserted_memory_ids: set[str] = set()
            account_inserted_counts: dict[str, int] = {}
            for memory in memories[:settings.episodic_memory.max_memories_per_session]:
                evidence_ids = [
                    candidate_id for candidate_id in memory.get("evidence_candidate_ids", [])
                    if candidate_id in active_candidates
                ]
                if not evidence_ids:
                    continue
                evidence = [active_candidates[candidate_id] for candidate_id in evidence_ids]
                primary = max(evidence, key=lambda item: float(item["salience"] or 0.0))
                scope = "account" if primary["scope"] == "account" and primary["account_id"] else "room"
                account_id = primary["account_id"] if scope == "account" else None
                if account_id:
                    preference = conn.execute(
                        "SELECT long_term_memory_enabled FROM account_memory_preferences WHERE account_id = ?",
                        (account_id,),
                    ).fetchone()
                    if not preference or not bool(preference["long_term_memory_enabled"]):
                        continue
                    if account_inserted_counts.get(account_id, 0) >= settings.episodic_memory.max_memories_per_account:
                        continue
                # 不允许一个最终条目跨越账号与匿名房间边界，防止模型把
                # 不同身份的候选拼成带身份的叙述。
                evidence_ids = [
                    candidate_id for candidate_id in evidence_ids
                    if (
                        active_candidates[candidate_id]["scope"] == primary["scope"]
                        and active_candidates[candidate_id]["account_id"] == primary["account_id"]
                    )
                ]
                if not evidence_ids:
                    continue
                conn.execute("""
                    INSERT INTO stream_episodic_memories (
                        memory_id, stream_session_id, scope, account_id, event_type, topic,
                        summary, why_notable, emotional_mark, follow_up_hint, salience,
                        occurred_at, evidence_candidate_ids_json, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    memory["memory_id"], stream_session_id, scope,
                    account_id, memory["event_type"], str(memory.get("topic", ""))[:120],
                    str(memory.get("summary", ""))[:240], str(memory.get("why_notable", ""))[:240],
                    str(memory.get("emotional_mark", ""))[:120], str(memory.get("follow_up_hint", ""))[:240],
                    max(0.0, min(1.0, float(memory.get("salience", 0.0)))),
                    memory["occurred_at"], json.dumps(evidence_ids, ensure_ascii=False),
                    now, memory["expires_at"],
                ))
                inserted_memory_ids.add(memory["memory_id"])
                if account_id:
                    account_inserted_counts[account_id] = account_inserted_counts.get(account_id, 0) + 1
            if reflection and inserted_memory_ids:
                reflection = dict(reflection)
                reflection["notable_event_ids"] = [
                    event_id for event_id in reflection.get("notable_event_ids", [])
                    if event_id in inserted_memory_ids
                ]
                conn.execute("""
                    INSERT INTO stream_reflections (
                        reflection_id, stream_session_id, summary, emotional_residue,
                        open_callbacks_json, notable_event_ids_json, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stream_session_id) DO UPDATE SET
                        summary=excluded.summary, emotional_residue=excluded.emotional_residue,
                        open_callbacks_json=excluded.open_callbacks_json,
                        notable_event_ids_json=excluded.notable_event_ids_json,
                        created_at=excluded.created_at, expires_at=excluded.expires_at
                """, (
                    reflection["reflection_id"], stream_session_id,
                    str(reflection.get("summary", ""))[:480],
                    str(reflection.get("emotional_residue", ""))[:180],
                    json.dumps(reflection.get("open_callbacks", []), ensure_ascii=False),
                    json.dumps(reflection.get("notable_event_ids", []), ensure_ascii=False),
                    now, reflection["expires_at"],
                ))
            if candidate_ids:
                placeholders = ",".join("?" for _ in candidate_ids)
                conn.execute(
                    f"UPDATE stream_memory_candidates SET status = 'summarized' "
                    f"WHERE candidate_id IN ({placeholders})", candidate_ids,
                )
            # 任务表只保留候选 ID 与运行状态；私人反思只进入独立的
            # stream_reflections，避免在任务重试/运维查询里复制敏感叙述。
            updated = conn.execute("""
                UPDATE stream_memory_tasks SET status = 'completed', reflection_json = NULL,
                    lease_expires_at = NULL, last_error_code = NULL,
                    completed_at = ?, updated_at = ?
                WHERE stream_session_id = ? AND status = 'processing'
            """, (now, now, stream_session_id))
            return updated.rowcount == 1

    def fail_stream_memory_task(
        self, *, stream_session_id: str, error_code: str, max_attempts: int, now: str
    ) -> str:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT attempts FROM stream_memory_tasks WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()
            if not row:
                return "missing"
            status = "pending" if row["attempts"] < max_attempts else "failed"
            updated = conn.execute("""
                UPDATE stream_memory_tasks SET status = ?, last_error_code = ?,
                    lease_expires_at = NULL, completed_at = ?, updated_at = ?
                WHERE stream_session_id = ? AND status = 'processing'
            """, (status, error_code, None if status == "pending" else now, now, stream_session_id))
            return status if updated.rowcount == 1 else "unchanged"

    # ------------------------------------------------------------------
    # P24 reliability v1 overrides the original one-shot implementation
    # above.  Keeping the old methods in the source eases review of the
    # migration; these definitions are the active lifecycle contract.
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_memory_candidate(row: sqlite3.Row) -> Dict[str, Any]:
        value = dict(row)
        try:
            value["appraisal"] = json.loads(value.pop("appraisal_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            value["appraisal"] = {}
        return value

    @staticmethod
    def _decode_memory_task(row: sqlite3.Row) -> Dict[str, Any]:
        value = dict(row)
        for field, fallback in (
            ("candidate_ids_json", []),
            ("current_batch_candidate_ids_json", []),
            ("reflection_fragments_json", []),
            ("reflection_json", None),
        ):
            raw = value.pop(field, None)
            try:
                value[field.removesuffix("_json")] = json.loads(raw) if raw else fallback
            except (TypeError, json.JSONDecodeError):
                value[field.removesuffix("_json")] = fallback
        # Preserve the historic public key used by callers.
        value["candidate_ids"] = value.pop("candidate_ids", [])
        value["current_batch_candidate_ids"] = value.pop("current_batch_candidate_ids", [])
        value["reflection_fragments"] = value.pop("reflection_fragments", [])
        value["reflection"] = value.pop("reflection", None)
        return value

    @staticmethod
    def _p24_token(stream_session_id: str, now: str) -> str:
        return hashlib.sha256(
            f"p24:{stream_session_id}:{now}:{id(object())}".encode("utf-8")
        ).hexdigest()[:32]

    @staticmethod
    def _p24_memory_id(stream_session_id: str, event_type: str, evidence_ids: list[str]) -> str:
        evidence = ",".join(sorted(set(str(item) for item in evidence_ids)))
        return hashlib.sha256(
            f"episodic:{stream_session_id}:{event_type}:{evidence}".encode("utf-8")
        ).hexdigest()[:32]

    @staticmethod
    def _p24_reflection_id(stream_session_id: str) -> str:
        return hashlib.sha256(
            f"reflection:{stream_session_id}".encode("utf-8")
        ).hexdigest()[:32]

    @staticmethod
    def _p24_error_detail(detail: Any, limit: int = 320) -> str:
        text = " ".join(str(detail or "").split())
        for secret in ("authorization", "api_key", "bearer", "sk-"):
            if secret in text.casefold():
                return "provider error (sensitive detail redacted)"
        return text[:limit]

    def _p24_session_active(self, conn: sqlite3.Connection, stream_session_id: str) -> bool:
        row = conn.execute(
            "SELECT state FROM stream_session_facts WHERE stream_session_id = ?",
            (stream_session_id,),
        ).fetchone()
        return bool(row and str(row["state"]).casefold() == "active")

    def _p24_reset_expired_claims(
        self, conn: sqlite3.Connection, *, now: str, only_session: Optional[str] = None
    ) -> int:
        """Recover crashed executions atomically and make their batch retryable."""
        where = (
            "status = 'processing' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)"
        )
        params: list[Any] = [now]
        if only_session:
            where += " AND stream_session_id = ?"
            params.append(only_session)
        rows = conn.execute(
            "SELECT stream_session_id, claim_token FROM stream_memory_tasks WHERE " + where,
            params,
        ).fetchall()
        for row in rows:
            session_id = str(row["stream_session_id"])
            token = row["claim_token"]
            if token:
                conn.execute(
                    "UPDATE stream_memory_candidates SET status='pending', claim_token=NULL "
                    "WHERE stream_session_id=? AND status='claimed' AND claim_token=?",
                    (session_id, token),
                )
            conn.execute(
                "UPDATE stream_memory_tasks SET status='failed_retryable', "
                "lease_expires_at=NULL, claim_token=NULL, current_batch_candidate_ids_json='[]', "
                "next_attempt_at=?, last_error_code='lease_expired', "
                "last_error_detail='worker lease expired; execution recovered', "
                "last_error_retryable=1, recovery_count=recovery_count+1, updated_at=? "
                "WHERE stream_session_id=? AND status='processing'",
                (now, now, session_id),
            )
        return len(rows)

    def reconcile_stream_memory_lifecycle(
        self, *, now: Optional[str] = None, include_orphans: bool = True,
        batch_limit: Optional[int] = None,
    ) -> Dict[str, int]:
        """Reconcile stale tasks, completed-task stragglers and frozen orphans.

        Active sessions are intentionally excluded.  The operation is bounded
        and safe to run at startup, on every worker wake, or concurrently with
        a scheduler because it holds SQLite's write lock for one transaction.
        """
        now = now or datetime.now(timezone.utc).isoformat()
        limit = max(1, int(batch_limit or settings.episodic_memory.reconciliation_batch_size))
        stats = {"expired_claims": 0, "reopened_tasks": 0, "created_tasks": 0,
                 "discarded": 0, "appended_orphans": 0, "active_skipped": 0}
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stats["expired_claims"] = self._p24_reset_expired_claims(conn, now=now)
            sessions = conn.execute(
                "SELECT DISTINCT stream_session_id FROM stream_memory_candidates "
                "WHERE status IN ('pending','claimed') ORDER BY stream_session_id LIMIT ?",
                (limit,),
            ).fetchall()
            for session_row in sessions:
                session_id = str(session_row["stream_session_id"])
                if self._p24_session_active(conn, session_id):
                    stats["active_skipped"] += 1
                    continue
                task = conn.execute(
                    "SELECT * FROM stream_memory_tasks WHERE stream_session_id=?",
                    (session_id,),
                ).fetchone()
                candidates = conn.execute(
                    "SELECT candidate_id,status FROM stream_memory_candidates "
                    "WHERE stream_session_id=? AND status IN ('pending','claimed') "
                    "ORDER BY salience DESC, occurred_at DESC, candidate_id ASC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
                if not candidates:
                    continue
                candidate_ids = [str(row["candidate_id"]) for row in candidates]
                if task:
                    task_ids = json.loads(task["candidate_ids_json"] or "[]")
                    task_ids = list(dict.fromkeys(str(item) for item in task_ids))
                    missing = [item for item in candidate_ids if item not in task_ids]
                    if str(task["status"]) == "completed" and include_orphans:
                        # A crash in an older implementation could leave an
                        # input candidate pending after the task was marked
                        # completed. Resolve it explicitly; only candidates
                        # that were never part of the snapshot are reopened.
                        stale_input = [item for item in candidate_ids if item in task_ids]
                        for stale_id in stale_input:
                            evidence_exists = conn.execute(
                                "SELECT 1 FROM stream_episodic_memories "
                                "WHERE stream_session_id=? AND evidence_candidate_ids_json LIKE ? LIMIT 1",
                                (session_id, f"%{stale_id}%"),
                            ).fetchone()
                            code = "task_completed_existing_memory" if evidence_exists else "task_completed_unselected"
                            status = "summarized" if evidence_exists else "discarded"
                            conn.execute(
                                "UPDATE stream_memory_candidates SET status=?, claim_token=NULL, "
                                "resolved_at=COALESCE(resolved_at,?), resolution_code=COALESCE(resolution_code,?) "
                                "WHERE candidate_id=? AND status IN ('pending','claimed')",
                                (status, now, code, stale_id),
                            )
                            if status == "discarded":
                                stats["discarded"] += 1
                        # A completed task can have candidates created after its
                        # input snapshot. Re-open it only for those candidates.
                        if missing:
                            task_ids.extend(missing[: max(0, settings.episodic_memory.max_candidates_per_session - len(task_ids))])
                            if len(task_ids) > len(json.loads(task["candidate_ids_json"] or "[]")):
                                conn.execute(
                                    "UPDATE stream_memory_tasks SET status='pending', "
                                    "candidate_ids_json=?, completed_at=NULL, next_attempt_at=?, "
                                    "last_error_code='reconciled_orphan', last_error_retryable=1, updated_at=? "
                                    "WHERE stream_session_id=? AND status='completed'",
                                    (json.dumps(task_ids, ensure_ascii=False), now, now, session_id),
                                )
                                stats["reopened_tasks"] += 1
                                stats["appended_orphans"] += len(missing)
                    elif str(task["status"]) in {"failed", "failed_retryable", "pending"} and missing:
                        capacity = max(0, settings.episodic_memory.max_candidates_per_session - len(task_ids))
                        if capacity:
                            append = missing[:capacity]
                            task_ids.extend(append)
                            conn.execute(
                                "UPDATE stream_memory_tasks SET candidate_ids_json=?, "
                                "status=CASE WHEN status='failed' THEN 'failed_retryable' ELSE status END, "
                                "completed_at=NULL, next_attempt_at=COALESCE(next_attempt_at, ?), "
                                "updated_at=? WHERE stream_session_id=?",
                                (json.dumps(task_ids, ensure_ascii=False), now, now, session_id),
                            )
                            stats["appended_orphans"] += len(append)
                    continue
                # A frozen candidate stream without a task is an orphan task
                # creation case. Active sessions were skipped above.
                if len(candidate_ids) > settings.episodic_memory.max_candidates_per_session:
                    candidate_ids = candidate_ids[:settings.episodic_memory.max_candidates_per_session]
                conn.execute(
                    "INSERT OR IGNORE INTO stream_memory_tasks "
                    "(stream_session_id,status,attempts,candidate_ids_json,source_version,created_at,updated_at,next_attempt_at,batch_size) "
                    "VALUES (?, 'pending', 0, ?, ?, ?, ?, ?, ?)",
                    (session_id, json.dumps(candidate_ids, ensure_ascii=False),
                     "stream_episodic_memory_v1", now, now, now,
                     settings.episodic_memory.batch_size),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    stats["created_tasks"] += 1
        return stats

    def create_stream_memory_task(
        self, *, stream_session_id: str, candidate_ids: List[str], created_at: str,
        source_version: str = "stream_episodic_memory_v1"
    ) -> bool:
        unique_ids = list(dict.fromkeys(str(item) for item in candidate_ids))[
            : settings.episodic_memory.max_candidates_per_session
        ]
        if not unique_ids:
            return False
        with self._get_connection() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO stream_memory_tasks "
                "(stream_session_id,status,attempts,candidate_ids_json,source_version,created_at,updated_at,next_attempt_at,batch_size) "
                "VALUES (?, 'pending', 0, ?, ?, ?, ?, NULL, ?)",
                (stream_session_id, json.dumps(unique_ids, ensure_ascii=False), source_version,
                 created_at, created_at, settings.episodic_memory.batch_size),
            )
            return cursor.rowcount == 1

    def get_stream_memory_task(self, stream_session_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM stream_memory_tasks WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()
        return self._decode_memory_task(row) if row else None

    def claim_next_stream_memory_task(self, *, lease_seconds: int, now: str) -> Optional[Dict[str, Any]]:
        reference = datetime.fromisoformat(now)
        lease_expires = (reference + timedelta(seconds=lease_seconds)).isoformat()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._p24_reset_expired_claims(conn, now=now)
            rows = conn.execute(
                "SELECT * FROM stream_memory_tasks WHERE status IN ('pending','failed_retryable') "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "ORDER BY created_at ASC, stream_session_id ASC",
                (now,),
            ).fetchall()
            chosen = None
            batch_ids: list[str] = []
            for row in rows:
                if self._p24_session_active(conn, str(row["stream_session_id"])):
                    continue
                ids = json.loads(row["candidate_ids_json"] or "[]")
                if not ids:
                    conn.execute(
                        "UPDATE stream_memory_tasks SET status='completed', completed_at=COALESCE(completed_at,?), updated_at=? WHERE stream_session_id=?",
                        (now, now, row["stream_session_id"]),
                    )
                    continue
                if int(row["batch_index"] or 0) >= settings.episodic_memory.max_batches_per_task:
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(
                        f"UPDATE stream_memory_candidates SET status='discarded', resolved_at=?, "
                        f"resolution_code='batch_limit_exceeded' WHERE candidate_id IN ({placeholders}) "
                        "AND status IN ('pending','claimed')",
                        [now, *ids],
                    )
                    conn.execute(
                        "UPDATE stream_memory_tasks SET status='failed', completed_at=?, "
                        "last_error_code='batch_limit_exceeded', last_error_detail=?, "
                        "last_error_retryable=0, updated_at=? WHERE stream_session_id=?",
                        (now, "configured batch limit reached; candidates explicitly discarded", now, row["stream_session_id"]),
                    )
                    continue
                placeholders = ",".join("?" for _ in ids)
                pending_rows = conn.execute(
                    f"SELECT candidate_id FROM stream_memory_candidates WHERE candidate_id IN ({placeholders}) AND status='pending' "
                    "ORDER BY salience DESC, occurred_at DESC, candidate_id ASC",
                    ids,
                ).fetchall()
                batch_size = max(1, min(int(row["batch_size"] or settings.episodic_memory.batch_size), settings.episodic_memory.batch_size))
                batch_ids = [str(item["candidate_id"]) for item in pending_rows[:batch_size]]
                if batch_ids:
                    chosen = row
                    break
                # All inputs are already resolved; this can happen after a
                # commit/crash window. Complete without another model call.
                conn.execute(
                    "UPDATE stream_memory_tasks SET status='completed', completed_at=COALESCE(completed_at,?), "
                    "next_attempt_at=NULL, updated_at=? WHERE stream_session_id=?",
                    (now, now, row["stream_session_id"]),
                )
            if chosen is None:
                return None
            session_id = str(chosen["stream_session_id"])
            token = self._p24_token(session_id, now)
            placeholders = ",".join("?" for _ in batch_ids)
            conn.execute(
                f"UPDATE stream_memory_candidates SET status='claimed', claim_token=? "
                f"WHERE candidate_id IN ({placeholders}) AND status='pending'",
                [token, *batch_ids],
            )
            changed = conn.execute(
                "UPDATE stream_memory_tasks SET status='processing', attempts=attempts+1, "
                "lease_expires_at=?, claim_token=?, current_batch_candidate_ids_json=?, "
                "next_attempt_at=NULL, batch_index=batch_index+1, updated_at=? "
                "WHERE stream_session_id=? AND status IN ('pending','failed_retryable')",
                (lease_expires, token, json.dumps(batch_ids, ensure_ascii=False), now, session_id),
            )
            if changed.rowcount != 1:
                return None
            fresh = conn.execute(
                "SELECT * FROM stream_memory_tasks WHERE stream_session_id=?", (session_id,)
            ).fetchone()
        value = self._decode_memory_task(fresh)
        # The manager consumes exactly this bounded batch. Keep all task IDs
        # separately for diagnostics and reconciliation.
        value["candidate_ids"] = value["current_batch_candidate_ids"]
        value["task_candidate_ids"] = json.loads(fresh["candidate_ids_json"] or "[]")
        value["claim_token"] = fresh["claim_token"]
        return value

    def release_stream_memory_task(
        self, stream_session_id: str, now: str, claim_token: Optional[str] = None
    ) -> None:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT claim_token FROM stream_memory_tasks WHERE stream_session_id=? AND status='processing'",
                (stream_session_id,),
            ).fetchone()
            if not row or (claim_token and row["claim_token"] != claim_token):
                return
            token = row["claim_token"]
            conn.execute(
                "UPDATE stream_memory_candidates SET status='pending', claim_token=NULL "
                "WHERE stream_session_id=? AND status='claimed' AND claim_token=?",
                (stream_session_id, token),
            )
            conn.execute(
                "UPDATE stream_memory_tasks SET status='pending', lease_expires_at=NULL, claim_token=NULL, "
                "current_batch_candidate_ids_json='[]', next_attempt_at=?, updated_at=? "
                "WHERE stream_session_id=? AND status='processing'",
                (now, now, stream_session_id),
            )

    def complete_stream_memory_batch(
        self, *, stream_session_id: str, claim_token: Optional[str],
        memories: List[Dict[str, Any]], reflection: Optional[Dict[str, Any]],
        now: str, final_batch: bool = False,
        discarded_reasons: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Commit one bounded batch and all its state transitions atomically."""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT * FROM stream_memory_tasks WHERE stream_session_id=?", (stream_session_id,)
            ).fetchone()
            if not task:
                return False
            if task["status"] == "completed":
                return True  # idempotent replay after a crash
            if task["status"] != "processing":
                return False
            if claim_token and task["claim_token"] != claim_token:
                return False
            batch_ids = json.loads(task["current_batch_candidate_ids_json"] or "[]")
            if not batch_ids:
                return False
            placeholders = ",".join("?" for _ in batch_ids)
            rows = conn.execute(
                f"SELECT * FROM stream_memory_candidates WHERE candidate_id IN ({placeholders})",
                batch_ids,
            ).fetchall()
            active = {str(row["candidate_id"]): row for row in rows}
            selected: set[str] = set()
            inserted_memory_ids: list[str] = []
            session_memory_count = conn.execute(
                "SELECT COUNT(*) FROM stream_episodic_memories WHERE stream_session_id=?",
                (stream_session_id,),
            ).fetchone()[0]
            account_counts = {
                str(row["account_id"]): int(row["count"])
                for row in conn.execute(
                    "SELECT account_id, COUNT(*) AS count FROM stream_episodic_memories "
                    "WHERE stream_session_id=? AND account_id IS NOT NULL GROUP BY account_id",
                    (stream_session_id,),
                ).fetchall()
            }
            for memory in memories[:settings.episodic_memory.max_memories_per_session]:
                if session_memory_count >= settings.episodic_memory.max_memories_per_session:
                    break
                evidence_ids = [
                    str(item) for item in memory.get("evidence_candidate_ids", [])
                    if str(item) in active
                ]
                if not evidence_ids:
                    continue
                primary = max((active[item] for item in evidence_ids), key=lambda item: float(item["salience"] or 0.0))
                scope = "account" if primary["scope"] == "account" and primary["account_id"] else "room"
                account_id = primary["account_id"] if scope == "account" else None
                if account_id:
                    preference = conn.execute(
                        "SELECT long_term_memory_enabled FROM account_memory_preferences WHERE account_id=?",
                        (account_id,),
                    ).fetchone()
                    if not preference or not bool(preference["long_term_memory_enabled"]):
                        continue
                    if account_counts.get(account_id, 0) >= settings.episodic_memory.max_memories_per_account:
                        continue
                evidence_ids = [
                    item for item in evidence_ids
                    if active[item]["scope"] == primary["scope"]
                    and active[item]["account_id"] == primary["account_id"]
                ]
                if not evidence_ids:
                    continue
                event_type = str(memory.get("event_type") or primary["event_type"])
                memory_id = self._p24_memory_id(stream_session_id, event_type, evidence_ids)
                evidence_json = json.dumps(sorted(set(evidence_ids)), ensure_ascii=False)
                legacy_duplicate = conn.execute(
                    "SELECT memory_id FROM stream_episodic_memories WHERE stream_session_id=? "
                    "AND event_type=? AND evidence_candidate_ids_json=? LIMIT 1",
                    (stream_session_id, event_type, evidence_json),
                ).fetchone()
                if legacy_duplicate:
                    memory_id = str(legacy_duplicate["memory_id"])
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO stream_episodic_memories "
                        "(memory_id,stream_session_id,scope,account_id,event_type,topic,summary,why_notable,emotional_mark,follow_up_hint,salience,occurred_at,evidence_candidate_ids_json,created_at,expires_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (memory_id, stream_session_id, scope, account_id, event_type,
                         str(memory.get("topic", ""))[:120], str(memory.get("summary", ""))[:240],
                         str(memory.get("why_notable", ""))[:240], str(memory.get("emotional_mark", ""))[:120],
                         str(memory.get("follow_up_hint", ""))[:240],
                         max(0.0, min(1.0, float(memory.get("salience", 0.0)))),
                         str(memory.get("occurred_at") or active[evidence_ids[0]]["occurred_at"]),
                         evidence_json, now,
                         str(memory.get("expires_at") or (datetime.now(timezone.utc) + timedelta(days=settings.episodic_memory.room_retention_days)).isoformat())),
                    )
                selected.update(evidence_ids)
                inserted_memory_ids.append(memory_id)
                session_memory_count += 1
                if account_id:
                    account_counts[account_id] = account_counts.get(account_id, 0) + 1

            # Every candidate in a committed batch receives an explicit result.
            discarded_reasons = discarded_reasons or {}
            for candidate_id in batch_ids:
                if candidate_id in selected:
                    status, code = "summarized", "selected"
                else:
                    status, code = "discarded", discarded_reasons.get(candidate_id, "not_selected")
                conn.execute(
                    "UPDATE stream_memory_candidates SET status=?, claim_token=NULL, resolved_at=?, resolution_code=? "
                    "WHERE candidate_id=? AND status='claimed'",
                    (status, now, code, candidate_id),
                )

            fragments = json.loads(task["reflection_fragments_json"] or "[]")
            if reflection:
                fragment = {
                    "summary": str(reflection.get("summary", ""))[:480],
                    "emotional_residue": str(reflection.get("emotional_residue", ""))[:180],
                    "open_callbacks": [str(item)[:160] for item in reflection.get("open_callbacks", [])[:3]],
                    "notable_event_ids": [item for item in reflection.get("notable_event_ids", []) if item in inserted_memory_ids],
                }
                fragments.append(fragment)
            remaining = conn.execute(
                "SELECT COUNT(*) FROM stream_memory_candidates WHERE stream_session_id=? AND status IN ('pending','claimed')",
                (stream_session_id,),
            ).fetchone()[0]
            # `final_batch` is only an observation made before the commit. A
            # late candidate can be appended after the worker claims its
            # batch, so it must never override the authoritative transaction
            # check.  Keep the task pending whenever *any* candidate in this
            # session is still pending/claimed; reconciliation can then append
            # late candidates to the next execution instead of leaving a
            # completed task with an orphan.
            should_finish = remaining == 0
            if should_finish:
                # Keep the semantic distinction: this is one overall reflection
                # assembled from batch-level reflection observations, while the
                # episodic rows above remain concrete evidence records.
                summaries = [str(item.get("summary", "")) for item in fragments if item.get("summary")]
                residues = [str(item.get("emotional_residue", "")) for item in fragments if item.get("emotional_residue")]
                callbacks: list[str] = []
                notable: list[str] = []
                for item in fragments:
                    callbacks.extend(str(value) for value in item.get("open_callbacks", []) if value)
                    notable.extend(str(value) for value in item.get("notable_event_ids", []) if value)
                if summaries or residues or callbacks:
                    reflection_id = self._p24_reflection_id(stream_session_id)
                    conn.execute(
                        "INSERT INTO stream_reflections "
                        "(reflection_id,stream_session_id,summary,emotional_residue,open_callbacks_json,notable_event_ids_json,created_at,expires_at) "
                        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(stream_session_id) DO UPDATE SET "
                        "summary=excluded.summary, emotional_residue=excluded.emotional_residue, "
                        "open_callbacks_json=excluded.open_callbacks_json, notable_event_ids_json=excluded.notable_event_ids_json, "
                        "created_at=excluded.created_at, expires_at=excluded.expires_at",
                        (reflection_id, stream_session_id, "；".join(dict.fromkeys(summaries))[:480],
                         "；".join(dict.fromkeys(residues))[:180],
                         json.dumps(list(dict.fromkeys(callbacks))[:6], ensure_ascii=False),
                         json.dumps(list(dict.fromkeys(notable))[:12], ensure_ascii=False), now,
                         (datetime.now(timezone.utc) + timedelta(days=settings.episodic_memory.reflection_retention_days)).isoformat()),
                    )
                conn.execute(
                    "UPDATE stream_memory_tasks SET status='completed', lease_expires_at=NULL, claim_token=NULL, "
                    "current_batch_candidate_ids_json='[]', reflection_fragments_json=?, next_attempt_at=NULL, "
                    "last_error_code=NULL, last_error_detail=NULL, last_error_retryable=0, completed_at=?, updated_at=? "
                    "WHERE stream_session_id=? AND status='processing'",
                    (json.dumps(fragments, ensure_ascii=False), now, now, stream_session_id),
                )
            else:
                conn.execute(
                    "UPDATE stream_memory_tasks SET status='pending', lease_expires_at=NULL, claim_token=NULL, "
                    "current_batch_candidate_ids_json='[]', reflection_fragments_json=?, next_attempt_at=?, updated_at=? "
                    "WHERE stream_session_id=? AND status='processing'",
                    (json.dumps(fragments, ensure_ascii=False), now, now, stream_session_id),
                )
            return True

    def complete_stream_memory_task(
        self, *, stream_session_id: str, memories: List[Dict[str, Any]],
        reflection: Optional[Dict[str, Any]], now: str
    ) -> bool:
        """Compatibility wrapper for callers that still submit one whole task."""
        task = self.get_stream_memory_task(stream_session_id)
        token = task.get("claim_token") if task else None
        return self.complete_stream_memory_batch(
            stream_session_id=stream_session_id, claim_token=token,
            memories=memories, reflection=reflection, now=now, final_batch=True,
        )

    def fail_stream_memory_task(
        self, *, stream_session_id: str, error_code: str, max_attempts: int = 0,
        now: str, error_detail: Optional[str] = None, retryable: bool = True,
        claim_token: Optional[str] = None,
    ) -> str:
        """Record an auditable failure; retryable errors never exhaust the day."""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM stream_memory_tasks WHERE stream_session_id=?", (stream_session_id,)
            ).fetchone()
            if not row:
                return "missing"
            if row["status"] != "processing":
                return "unchanged"
            if claim_token and row["claim_token"] != claim_token:
                return "unchanged"
            token = row["claim_token"]
            if token:
                if retryable:
                    conn.execute(
                        "UPDATE stream_memory_candidates SET status='pending', claim_token=NULL, last_error_code=? "
                        "WHERE stream_session_id=? AND status='claimed' AND claim_token=?",
                        (error_code, stream_session_id, token),
                    )
                else:
                    conn.execute(
                        "UPDATE stream_memory_candidates SET status='discarded', claim_token=NULL, resolved_at=?, "
                        "resolution_code=?, last_error_code=? WHERE stream_session_id=? AND status='claimed' AND claim_token=?",
                        (now, error_code, error_code, stream_session_id, token),
                    )
            if not retryable:
                task_ids = json.loads(row["candidate_ids_json"] or "[]")
                if task_ids:
                    placeholders = ",".join("?" for _ in task_ids)
                    conn.execute(
                        f"UPDATE stream_memory_candidates SET status='discarded', claim_token=NULL, "
                        f"resolved_at=COALESCE(resolved_at,?), resolution_code=COALESCE(resolution_code,?), "
                        f"last_error_code=COALESCE(last_error_code,?) WHERE candidate_id IN ({placeholders}) "
                        "AND status IN ('pending','claimed')",
                        [now, error_code, error_code, *task_ids],
                    )
            if retryable:
                attempt = max(1, int(row["attempts"] or 1))
                delay = min(
                    settings.episodic_memory.retry_backoff_max_seconds,
                    settings.episodic_memory.retry_backoff_seconds * (2 ** min(attempt - 1, 8)),
                )
                next_at = (datetime.fromisoformat(now) + timedelta(seconds=delay)).isoformat()
                status = "failed_retryable"
                completed_at = None
                retry_flag = 1
            else:
                next_at = None
                status = "failed"
                completed_at = now
                retry_flag = 0
            conn.execute(
                "UPDATE stream_memory_tasks SET status=?, lease_expires_at=NULL, claim_token=NULL, "
                "current_batch_candidate_ids_json='[]', next_attempt_at=?, last_error_code=?, "
                "last_error_detail=?, last_error_retryable=?, completed_at=?, updated_at=? "
                "WHERE stream_session_id=? AND status='processing'",
                (status, next_at, error_code, self._p24_error_detail(error_detail, settings.episodic_memory.error_detail_max_chars),
                 retry_flag, completed_at, now, stream_session_id),
            )
            return status

    def stream_memory_task_has_pending_candidates(self, stream_session_id: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT candidate_ids_json FROM stream_memory_tasks WHERE stream_session_id=?",
                (stream_session_id,),
            ).fetchone()
            if not row:
                return False
            ids = json.loads(row["candidate_ids_json"] or "[]")
            if not ids:
                return False
            placeholders = ",".join("?" for _ in ids)
            return conn.execute(
                f"SELECT 1 FROM stream_memory_candidates WHERE candidate_id IN ({placeholders}) AND status='pending' LIMIT 1",
                ids,
            ).fetchone() is not None

    def stream_session_is_active(self, stream_session_id: str) -> bool:
        with self._get_connection() as conn:
            return self._p24_session_active(conn, stream_session_id)

    def list_account_episodic_memories(self, account_id: str, *, limit: int = 2) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM stream_episodic_memories
                WHERE scope = 'account' AND account_id = ? AND archived = 0 AND expires_at > ?
                ORDER BY salience DESC, occurred_at DESC LIMIT ?
            """, (account_id, now, max(0, limit))).fetchall()
        return [self._decode_episodic_memory(row) for row in rows]

    def list_room_episodic_memories(self, *, topic: str = "", limit: int = 1) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        topic_key = str(topic or "").casefold()
        scan_limit = min(500, max(100, max(0, limit)))
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM stream_episodic_memories
                WHERE scope = 'room' AND archived = 0 AND expires_at > ?
                ORDER BY salience DESC, occurred_at DESC LIMIT ?
            """, (now, scan_limit)).fetchall()
        values = [self._decode_episodic_memory(row) for row in rows]
        if topic_key:
            related = [item for item in values if topic_key in str(item.get("topic", "")).casefold()]
            return related[:max(0, limit)]
        return values

    def get_latest_stream_reflection(self, *, exclude_stream_session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            if exclude_stream_session_id:
                row = conn.execute("""
                    SELECT * FROM stream_reflections WHERE expires_at > ? AND stream_session_id != ?
                    ORDER BY created_at DESC LIMIT 1
                """, (now, exclude_stream_session_id)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM stream_reflections WHERE expires_at > ? ORDER BY created_at DESC LIMIT 1",
                    (now,),
                ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["open_callbacks"] = json.loads(value.pop("open_callbacks_json") or "[]")
        value["notable_event_ids"] = json.loads(value.pop("notable_event_ids_json") or "[]")
        return value

    def delete_account_episodic_memory(self, account_id: str) -> None:
        with self._get_connection() as conn:
            self._delete_account_episodic_memory(conn, account_id)

    @staticmethod
    def _delete_account_episodic_memory(conn, account_id: str) -> None:
        """Join the caller's privacy transaction; never commit a partial clear."""
        now = datetime.now(timezone.utc).isoformat()
        candidate_rows = conn.execute(
            "SELECT candidate_id FROM stream_memory_candidates WHERE account_id = ?", (account_id,)
        ).fetchall()
        candidate_ids = [row["candidate_id"] for row in candidate_rows]
        memory_rows = conn.execute(
            "SELECT memory_id FROM stream_episodic_memories WHERE scope = 'account' AND account_id = ?",
            (account_id,),
        ).fetchall()
        memory_ids = [row["memory_id"] for row in memory_rows]
        conn.execute("DELETE FROM stream_episodic_memories WHERE scope = 'account' AND account_id = ?", (account_id,))
        conn.execute("DELETE FROM stream_memory_candidates WHERE account_id = ?", (account_id,))
        if candidate_ids:
            candidate_id_set = set(candidate_ids)
            task_rows = conn.execute(
                "SELECT stream_session_id, status, candidate_ids_json FROM stream_memory_tasks "
                "WHERE status IN ('pending', 'processing')"
            ).fetchall()
            for task in task_rows:
                task_ids = json.loads(task["candidate_ids_json"] or "[]")
                remaining = [item for item in task_ids if item not in candidate_id_set]
                if remaining == task_ids:
                    continue
                if not remaining and task["status"] == "pending":
                    conn.execute(
                        "UPDATE stream_memory_tasks SET status = 'completed', candidate_ids_json = '[]', "
                        "completed_at = ?, updated_at = ? WHERE stream_session_id = ?",
                        (now, now, task["stream_session_id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE stream_memory_tasks SET candidate_ids_json = ?, updated_at = ? "
                        "WHERE stream_session_id = ?",
                        (json.dumps(remaining, ensure_ascii=False), now, task["stream_session_id"]),
                    )
        if memory_ids:
            patterns = [f"%{memory_id}%" for memory_id in memory_ids]
            conn.execute(
                "DELETE FROM stream_reflections WHERE " + " OR ".join("notable_event_ids_json LIKE ?" for _ in patterns),
                patterns,
            )

    def get_stream_episodic_memory_stats(self) -> Dict[str, Any]:
        """仅返回低基数运行指标，不包含账号、昵称、原文或候选 ID。"""
        with self._get_connection() as conn:
            task_counts = {
                row["status"]: row["count"] for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM stream_memory_tasks GROUP BY status"
                ).fetchall()
            }
            candidate_counts = {
                row["status"]: row["count"] for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM stream_memory_candidates GROUP BY status"
                ).fetchall()
            }
            memory_counts = {
                row["scope"]: row["count"] for row in conn.execute(
                    "SELECT scope, COUNT(*) AS count FROM stream_episodic_memories "
                    "WHERE archived = 0 GROUP BY scope"
                ).fetchall()
            }
            reflection_count = conn.execute(
                "SELECT COUNT(*) FROM stream_reflections WHERE expires_at > ?",
                (datetime.now(timezone.utc).isoformat(),),
            ).fetchone()[0]
            historical_orphans = conn.execute(
                "SELECT COUNT(*) FROM stream_memory_candidates candidate "
                "LEFT JOIN stream_memory_tasks task ON task.stream_session_id = candidate.stream_session_id "
                "LEFT JOIN stream_session_facts fact ON fact.stream_session_id = candidate.stream_session_id "
                "WHERE candidate.status='pending' AND COALESCE(fact.state, 'frozen') != 'active' "
                "AND (task.stream_session_id IS NULL OR task.status IN ('completed','failed'))"
            ).fetchone()[0]
        return {
            "task_status_counts": task_counts,
            "candidate_status_counts": candidate_counts,
            "memory_scope_counts": memory_counts,
            "active_reflections": reflection_count,
            "historical_orphan_pending_candidates": historical_orphans,
        }

    def purge_expired_episodic_memory(self, now: str) -> Dict[str, int]:
        with self._get_connection() as conn:
            memories = conn.execute(
                "DELETE FROM stream_episodic_memories WHERE expires_at <= ?", (now,)
            ).rowcount
            reflections = conn.execute(
                "DELETE FROM stream_reflections WHERE expires_at <= ?", (now,)
            ).rowcount
            # 候选不含原文，但要在治理保留期内留作来源审计、删除竞态和
            # 失败重试诊断；不能把“当前时间”误当成清理截止点。
            retention_days = max(
                settings.episodic_memory.account_retention_days,
                settings.episodic_memory.room_retention_days,
                settings.episodic_memory.reflection_retention_days,
            )
            candidate_cutoff = (
                datetime.fromisoformat(now) - timedelta(days=retention_days)
            ).isoformat()
            candidates = conn.execute("""
                DELETE FROM stream_memory_candidates
                WHERE status IN ('summarized', 'discarded', 'rejected') AND created_at < ?
            """, (candidate_cutoff,)).rowcount
        return {"memories": memories, "reflections": reflections, "candidates": candidates}

    @staticmethod
    def _decode_episodic_memory(row) -> Dict[str, Any]:
        value = dict(row)
        value["evidence_candidate_ids"] = json.loads(value.pop("evidence_candidate_ids_json") or "[]")
        value["archived"] = bool(value.get("archived"))
        return value

    def purge_expired_stream_session_summaries(self, cutoff_at: str) -> Dict[str, int]:
        """仅清理超过保留期的终态任务与冻结事实，绝不取消待处理任务。"""
        with self._get_connection() as conn:
            tasks = conn.execute("""
                DELETE FROM stream_session_summary_tasks
                WHERE status IN ('completed', 'failed')
                  AND completed_at IS NOT NULL AND completed_at < ?
            """, (cutoff_at,)).rowcount
            facts = conn.execute("""
                DELETE FROM stream_session_facts
                WHERE state = 'frozen' AND frozen_at IS NOT NULL AND frozen_at < ?
                  AND NOT EXISTS (
                    SELECT 1 FROM stream_session_summary_tasks task
                    WHERE task.stream_session_id = stream_session_facts.stream_session_id
                  )
            """, (cutoff_at,)).rowcount
        return {"tasks": tasks, "facts": facts}

    def get_stream_session_summary_stats(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            task_counts = {
                row["status"]: row["count"] for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM stream_session_summary_tasks "
                    "GROUP BY status"
                ).fetchall()
            }
            active = conn.execute(
                "SELECT COUNT(*) FROM stream_session_facts WHERE state = 'active'"
            ).fetchone()[0]
        return {"task_status_counts": task_counts, "active_sessions": active}

    # ==================== AI token 用量审计（P29） ====================

    def record_ai_token_usage_batch(
        self, rows: List[Dict[str, Any]], *, detail_enabled: bool = True
    ) -> Dict[str, int]:
        """在单个事务里写明细并累加每日聚合；聚合永久保留，明细可过期清理。"""
        if not rows:
            return {"records": 0, "daily": 0}
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            records = 0
            if detail_enabled:
                cursor = conn.executemany("""
                    INSERT OR IGNORE INTO ai_token_usage_records (
                        record_id, day, created_at, role, provider, model, status,
                        usage_reported, input_tokens, output_tokens,
                        cached_input_tokens, reasoning_tokens, reasoning_tokens_reported,
                        total_tokens, latency_ms, error_kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    (
                        row["record_id"], row["day"], row["created_at"], row["role"],
                        row["provider"], row["model"], row["status"],
                        1 if row.get("usage_reported") else 0,
                        int(row.get("input_tokens") or 0),
                        int(row.get("output_tokens") or 0),
                        int(row.get("cached_input_tokens") or 0),
                        row.get("reasoning_tokens"),
                        1 if row.get("reasoning_tokens_reported") else 0,
                        int(row.get("total_tokens") or 0),
                        int(row.get("latency_ms") or 0),
                        row.get("error_kind"),
                    )
                    for row in rows
                ])
                records = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            daily = conn.executemany("""
                INSERT INTO ai_token_usage_daily (
                    day, role, provider, model, calls, failed_calls,
                    usage_missing_calls, input_tokens, output_tokens,
                    cached_input_tokens, reasoning_tokens, reasoning_missing_calls,
                    total_tokens, latency_ms_sum,
                    first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day, role, provider, model) DO UPDATE SET
                    calls = calls + 1,
                    failed_calls = failed_calls + excluded.failed_calls,
                    usage_missing_calls = usage_missing_calls + excluded.usage_missing_calls,
                    input_tokens = input_tokens + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    cached_input_tokens = cached_input_tokens + excluded.cached_input_tokens,
                    reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
                    reasoning_missing_calls = reasoning_missing_calls + excluded.reasoning_missing_calls,
                    total_tokens = total_tokens + excluded.total_tokens,
                    latency_ms_sum = latency_ms_sum + excluded.latency_ms_sum,
                    updated_at = excluded.updated_at
            """, [
                (
                    row["day"], row["role"], row["provider"], row["model"],
                    1 if row["status"] != "success" else 0,
                    0 if row.get("usage_reported") else 1,
                    int(row.get("input_tokens") or 0),
                    int(row.get("output_tokens") or 0),
                    int(row.get("cached_input_tokens") or 0),
                    int(row.get("reasoning_tokens") or 0),
                    0 if row.get("reasoning_tokens_reported") else 1,
                    int(row.get("total_tokens") or 0),
                    int(row.get("latency_ms") or 0),
                    row["created_at"], row["created_at"],
                )
                for row in rows
            ]).rowcount
        return {"records": records, "daily": daily if daily and daily > 0 else len(rows)}

    def get_ai_token_daily_totals(
        self, start_day: str, end_day: str
    ) -> List[Dict[str, Any]]:
        """按自然日汇总；不含分组维度，用于后台曲线。"""
        with self._get_connection() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT day,
                       SUM(calls) AS calls,
                       SUM(failed_calls) AS failed_calls,
                       SUM(usage_missing_calls) AS usage_missing_calls,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cached_input_tokens) AS cached_input_tokens,
                       SUM(reasoning_tokens) AS reasoning_tokens,
                       SUM(reasoning_missing_calls) AS reasoning_missing_calls,
                       SUM(total_tokens) AS total_tokens,
                       SUM(latency_ms_sum) AS latency_ms_sum
                FROM ai_token_usage_daily
                WHERE day >= ? AND day <= ?
                GROUP BY day ORDER BY day
            """, (start_day, end_day)).fetchall()]

    def get_ai_token_daily_breakdown(
        self, start_day: str, end_day: str
    ) -> List[Dict[str, Any]]:
        """返回区间内的分组明细行，由服务层聚成 role/provider/model 三种视图。"""
        with self._get_connection() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT role, provider, model,
                       SUM(calls) AS calls,
                       SUM(failed_calls) AS failed_calls,
                       SUM(usage_missing_calls) AS usage_missing_calls,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cached_input_tokens) AS cached_input_tokens,
                       SUM(reasoning_tokens) AS reasoning_tokens,
                       SUM(reasoning_missing_calls) AS reasoning_missing_calls,
                       SUM(total_tokens) AS total_tokens,
                       SUM(latency_ms_sum) AS latency_ms_sum
                FROM ai_token_usage_daily
                WHERE day >= ? AND day <= ?
                GROUP BY role, provider, model
                ORDER BY total_tokens DESC, role, provider, model
            """, (start_day, end_day)).fetchall()]

    def get_ai_token_model_days(
        self, start_day: str, end_day: str
    ) -> List[Dict[str, Any]]:
        """按 (day, model) 汇总：金额必须按模型单价折算后再按天求和。"""
        with self._get_connection() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT day, model,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cached_input_tokens) AS cached_input_tokens,
                       SUM(total_tokens) AS total_tokens
                FROM ai_token_usage_daily
                WHERE day >= ? AND day <= ?
                GROUP BY day, model ORDER BY day, model
            """, (start_day, end_day)).fetchall()]

    def list_ai_token_usage_records(
        self, *, day: Optional[str] = None, role: Optional[str] = None,
        status: Optional[str] = None, limit: int = 100, offset: int = 0,
    ) -> Dict[str, Any]:
        """逐次调用明细；字段固定，不含正文与身份信息。"""
        clauses: List[str] = []
        params: List[Any] = []
        if day:
            clauses.append("day = ?")
            params.append(day)
        if role:
            clauses.append("role = ?")
            params.append(role)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        capped = max(1, min(int(limit), 500))
        with self._get_connection() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM ai_token_usage_records{where}", tuple(params)
            ).fetchone()[0]
            records = [dict(row) for row in conn.execute(
                f"SELECT record_id, day, created_at, role, provider, model, status,"
                f" usage_reported, input_tokens, output_tokens, cached_input_tokens,"
                f" reasoning_tokens, reasoning_tokens_reported,"
                f" total_tokens, latency_ms, error_kind"
                f" FROM ai_token_usage_records{where}"
                f" ORDER BY created_at DESC, record_id DESC LIMIT ? OFFSET ?",
                (*params, capped, max(0, int(offset))),
            ).fetchall()]
        return {"records": records, "total": total, "limit": capped, "offset": offset}

    def get_ai_token_usage_summary(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            detail = conn.execute(
                "SELECT COUNT(*) AS rows, MIN(day) AS first_day, MAX(day) AS last_day "
                "FROM ai_token_usage_records"
            ).fetchone()
            daily = conn.execute(
                "SELECT COUNT(*) AS rows, MIN(day) AS first_day, MAX(day) AS last_day "
                "FROM ai_token_usage_daily"
            ).fetchone()
        return {
            "detail_rows": detail["rows"], "detail_first_day": detail["first_day"],
            "detail_last_day": detail["last_day"],
            "daily_rows": daily["rows"], "daily_first_day": daily["first_day"],
            "daily_last_day": daily["last_day"],
        }

    def purge_expired_ai_token_usage_records(self, cutoff_day: str) -> Dict[str, int]:
        """只清理过期明细；每日聚合永久保留，绝不在这里删除。"""
        with self._get_connection() as conn:
            deleted = conn.execute(
                "DELETE FROM ai_token_usage_records WHERE day < ?", (cutoff_day,)
            ).rowcount
        return {"records": max(0, deleted)}

    
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

    def claim_stream_special_date_bias(
        self, stream_session_id: str, special_theme_id: str, applied_at: str
    ) -> bool:
        """原子领取一次特殊日期 bias，重启后也不会再次累计。"""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO stream_special_date_bias_applications (
                    stream_session_id, special_theme_id, applied_at
                ) VALUES (?, ?, ?)
            """, (stream_session_id, special_theme_id, applied_at))
            return cursor.rowcount == 1

    def get_or_create_persona_affect_anchor(
        self,
        *,
        stream_session_id: str,
        mood: float,
        stress: float,
        darkness: float,
        sources: Dict[str, Any],
        updated_at: str,
    ) -> Dict[str, Any]:
        """原子创建或恢复场次级情绪锚点，不让重启重复叠加主题 bias。"""
        safe_sources = {
            str(key)[:48]: str(value)[:96]
            for key, value in sources.items()
            if value is not None and str(key).strip()
        }
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT OR IGNORE INTO persona_affect_anchors (
                    stream_session_id, mood, stress, darkness, source_json,
                    version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """, (
                stream_session_id, mood, stress, darkness,
                json.dumps(safe_sources, ensure_ascii=False, sort_keys=True),
                updated_at, updated_at,
            ))
            row = conn.execute("""
                SELECT stream_session_id, mood, stress, darkness, source_json,
                       version, created_at, updated_at
                FROM persona_affect_anchors WHERE stream_session_id = ?
            """, (stream_session_id,)).fetchone()
        result = dict(row)
        try:
            result["sources"] = json.loads(result.pop("source_json"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result["sources"] = {}
            result.pop("source_json", None)
        return result

    def update_persona_affect_anchor(
        self,
        *,
        stream_session_id: str,
        expected_version: int,
        mood: float,
        stress: float,
        darkness: float,
        sources: Dict[str, Any],
        updated_at: str,
    ) -> Optional[Dict[str, Any]]:
        """以乐观版本更新场次锚点；冲突时不覆盖其他已验证聚合结果。"""
        safe_sources = {
            str(key)[:48]: str(value)[:96]
            for key, value in sources.items()
            if value is not None and str(key).strip()
        }
        with self._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE persona_affect_anchors
                SET mood = ?, stress = ?, darkness = ?, source_json = ?,
                    version = version + 1, updated_at = ?
                WHERE stream_session_id = ? AND version = ?
            """, (
                mood, stress, darkness,
                json.dumps(safe_sources, ensure_ascii=False, sort_keys=True),
                updated_at, stream_session_id, expected_version,
            ))
            if cursor.rowcount != 1:
                return None
            row = conn.execute("""
                SELECT stream_session_id, mood, stress, darkness, source_json,
                       version, created_at, updated_at
                FROM persona_affect_anchors WHERE stream_session_id = ?
            """, (stream_session_id,)).fetchone()
        result = dict(row)
        try:
            result["sources"] = json.loads(result.pop("source_json"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result["sources"] = {}
            result.pop("source_json", None)
        return result

    def claim_english_surprise_joke(
        self,
        *,
        stream_session_id: str,
        viewer_scope: str,
        used_at: str,
        max_per_stream: int,
        account_id: Optional[str] = None,
    ) -> bool:
        """原子领取一次英文首次互动梗。

        登录账号在所有场次最多一次；游客只由调用方保留连接内状态，数据库
        仅用于同场全局配额与去重，避免昵称成为任何持久主键。
        """
        if max_per_stream <= 0:
            return False
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if account_id:
                already_used = conn.execute(
                    "SELECT 1 FROM account_english_surprise_jokes WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
                if already_used:
                    return False
            duplicate = conn.execute(
                "SELECT 1 FROM stream_english_surprise_jokes "
                "WHERE stream_session_id = ? AND viewer_scope = ?",
                (stream_session_id, viewer_scope),
            ).fetchone()
            if duplicate:
                return False
            used_count = conn.execute(
                "SELECT COUNT(*) FROM stream_english_surprise_jokes "
                "WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()[0]
            if used_count >= max_per_stream:
                return False
            conn.execute(
                "INSERT INTO stream_english_surprise_jokes "
                "(stream_session_id, viewer_scope, used_at) VALUES (?, ?, ?)",
                (stream_session_id, viewer_scope, used_at),
            )
            if account_id:
                conn.execute(
                    "INSERT INTO account_english_surprise_jokes (account_id, used_at) "
                    "VALUES (?, ?)",
                    (account_id, used_at),
                )
            return True
    
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
            conn.execute("""
                INSERT INTO account_viewer_impression_epochs(account_id, epoch) VALUES (?, 1)
                ON CONFLICT(account_id) DO UPDATE SET epoch = epoch + 1
            """, (account_id,))
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
                "account_english_surprise_jokes",
            ):
                if table in tables:
                    conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
            if "stream_english_surprise_jokes" in tables:
                conn.execute(
                    "DELETE FROM stream_english_surprise_jokes WHERE viewer_scope = ?",
                    (f"account:{account_id}",),
                )
            if "account_viewer_impression_tasks" in tables:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute("""
                    UPDATE account_viewer_impression_tasks
                    SET status = 'cancelled', execution_token = NULL,
                        lease_expires_at = NULL, evidence_snapshot = NULL,
                        error_code = 'memory_disabled', error_detail = NULL,
                        updated_at = ?
                    WHERE account_id = ?
                      AND status IN ('pending', 'processing', 'failed_retryable')
                """, (now, account_id))
            if "account_viewer_impressions" in tables:
                conn.execute(
                    "DELETE FROM account_viewer_impressions WHERE account_id = ?",
                    (account_id,),
                )
            # One commit must publish the new privacy epoch and removal of ALL
            # evidence together. A second transaction could admit a snapshot
            # containing old episodes under the already advanced epoch.
            self._delete_account_episodic_memory(conn, account_id)

    # ==================== 主播管理系统 ====================

    def get_user_behavior_state(self, subject_key: str) -> Optional[Dict[str, Any]]:
        """读取行为状态；处罚状态与人物记忆治理完全分离。"""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM user_behavior_state WHERE subject_key = ?",
                (subject_key,),
            ).fetchone()
            return dict(row) if row else None

    def decay_user_behavior_state(
        self, subject_key: str, *, now: str, decay_per_minute: float
    ) -> Optional[Dict[str, Any]]:
        """按服务端时钟衰减积分，调用方无需后台定时任务。"""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM user_behavior_state WHERE subject_key = ?",
                (subject_key,),
            ).fetchone()
            if not row:
                return None
            try:
                current = datetime.fromisoformat(row["last_decay_at"])
                reference = datetime.fromisoformat(now)
                minutes = max(0.0, (reference - current).total_seconds() / 60.0)
            except (TypeError, ValueError):
                minutes = 0.0
            score = max(0.0, float(row["toxicity_score"]) - minutes * decay_per_minute / 100.0)
            conn.execute("""
                UPDATE user_behavior_state
                SET toxicity_score = ?, last_decay_at = ?, updated_at = ?
                WHERE subject_key = ?
            """, (score, now, now, subject_key))
            updated = conn.execute(
                "SELECT * FROM user_behavior_state WHERE subject_key = ?",
                (subject_key,),
            ).fetchone()
            return dict(updated) if updated else None

    def get_recent_moderation_actions(
        self, subject_key: str, cutoff: str, limit: int = 8
    ) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT moderation_id, danmaku_id, action, status, severity,
                       toxicity, confidence, attack_type, reason_code,
                       created_at, completed_at
                FROM moderation_actions
                WHERE subject_key = ? AND created_at >= ?
                ORDER BY created_at DESC LIMIT ?
            """, (subject_key, cutoff, max(1, min(int(limit), 30)))).fetchall()
            return [dict(row) for row in rows]

    def get_active_moderation_action(self, subject_key: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM moderation_actions
                WHERE subject_key = ? AND status = 'reserved'
                ORDER BY created_at DESC LIMIT 1
            """, (subject_key,)).fetchone()
            return dict(row) if row else None

    def get_moderation_action(self, moderation_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM moderation_actions WHERE moderation_id = ?",
                (moderation_id,),
            ).fetchone()
            return dict(row) if row else None

    def recover_stale_moderation_actions(
        self, *, now: str, stale_after_seconds: int
    ) -> Dict[str, int]:
        """恢复进程崩溃留下的 reservation。

        timeout/admin_review 已经由后端确定了安全动作，重启后直接提交既定
        mute_until；warning 没有禁言则释放 reservation。不会重新调用 LLM，
        也不会凭空生成第二次主播回复。
        """
        try:
            cutoff = (datetime.fromisoformat(now) - timedelta(seconds=max(1, stale_after_seconds))).isoformat()
        except ValueError:
            cutoff = now
        recovered = {"muted": 0, "released": 0}
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("""
                SELECT moderation_id, subject_key, action, mute_until
                FROM moderation_actions
                WHERE status = 'reserved' AND created_at < ?
            """, (cutoff,)).fetchall()
            for row in rows:
                if row["action"] in {"timeout", "admin_review"}:
                    conn.execute("""
                        UPDATE moderation_actions
                        SET status = 'completed', completed_at = ?
                        WHERE moderation_id = ? AND status = 'reserved'
                    """, (now, row["moderation_id"]))
                    conn.execute("""
                        UPDATE user_behavior_state
                        SET mute_until = COALESCE(?, mute_until),
                            pending_action_id = NULL, pending_action = NULL,
                            updated_at = ?
                        WHERE subject_key = ? AND pending_action_id = ?
                    """, (row["mute_until"], now, row["subject_key"], row["moderation_id"]))
                    recovered["muted"] += 1
                else:
                    conn.execute("""
                        UPDATE moderation_actions
                        SET status = 'released', completed_at = ?
                        WHERE moderation_id = ? AND status = 'reserved'
                    """, (now, row["moderation_id"]))
                    conn.execute("""
                        UPDATE user_behavior_state
                        SET pending_action_id = NULL, pending_action = NULL, updated_at = ?
                        WHERE subject_key = ? AND pending_action_id = ?
                    """, (now, row["subject_key"], row["moderation_id"]))
                    recovered["released"] += 1
        return recovered

    def purge_moderation_history(self, cutoff: str) -> Dict[str, int]:
        """按保留期清理已结束审计与不再活跃的行为状态。"""
        with self._get_connection() as conn:
            action_cursor = conn.execute("""
                DELETE FROM moderation_actions
                WHERE created_at < ? AND status IN ('completed', 'released')
            """, (cutoff,))
            state_cursor = conn.execute("""
                DELETE FROM user_behavior_state
                WHERE updated_at < ? AND pending_action_id IS NULL
                  AND (mute_until IS NULL OR mute_until <= ?)
            """, (cutoff, cutoff))
            return {
                "actions": action_cursor.rowcount,
                "states": state_cursor.rowcount,
            }

    def upsert_moderation_assessment(
        self, *, moderation_id: str, danmaku_id: str, subject_key: str,
        identity_type: str, account_id: Optional[str], stream_session_id: Optional[str],
        action: str, severity: float, toxicity: float, confidence: float,
        attack_type: str, reason_code: str, mute_until: Optional[str],
        message_digest: str, now: str,
    ) -> Dict[str, Any]:
        """原子写入一次 LLM 评估；同一 danmaku_id 只产生一个决策。"""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM moderation_actions WHERE danmaku_id = ?",
                (danmaku_id,),
            ).fetchone()
            if existing:
                return dict(existing)

            state = conn.execute(
                "SELECT * FROM user_behavior_state WHERE subject_key = ?",
                (subject_key,),
            ).fetchone()
            current_score = float(state["toxicity_score"]) if state else 0.0
            warning_count = int(state["warning_count"]) if state else 0
            violation_count = int(state["violation_count"]) if state else 0
            # 状态分值由调用方在 action 选择后传入的 severity/toxicity 再叠加；
            # none 不会产生新的违规积分。
            incident = max(0.0, min(1.0, float(toxicity))) if action != "none" else 0.0
            new_score = max(0.0, min(1.0, current_score * 0.98 + incident * 0.20))
            if action == "warning":
                warning_count += 1
                violation_count += 1
            elif action in {"timeout", "admin_review"}:
                violation_count += 1

            # 正常弹幕不应为每次 LLM 分析创建一条审计记录，否则直播间
            # 的普通流量会无界增长 moderation_actions。行为状态只在有
            # 违规动作时持久化；调用方仍收到与审计行兼容的幂等结果。
            if action == "none":
                return {
                    "moderation_id": moderation_id,
                    "danmaku_id": danmaku_id,
                    "subject_key": subject_key,
                    "identity_type": identity_type,
                    "account_id": account_id,
                    "stream_session_id": stream_session_id,
                    "action": "none",
                    "status": "completed",
                    "severity": severity,
                    "toxicity": toxicity,
                    "confidence": confidence,
                    "attack_type": attack_type,
                    "reason_code": reason_code,
                    "mute_until": None,
                    "reply_payload": None,
                    "message_digest": message_digest,
                    "created_at": now,
                    "completed_at": now,
                }

            conn.execute("""
                INSERT INTO moderation_actions (
                    moderation_id, danmaku_id, subject_key, identity_type,
                    account_id, stream_session_id, action, status, severity,
                    toxicity, confidence, attack_type, reason_code, mute_until,
                    message_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                moderation_id, danmaku_id, subject_key, identity_type,
                account_id, stream_session_id, action,
                "reserved" if action != "none" else "completed",
                severity, toxicity, confidence, attack_type, reason_code, mute_until,
                message_digest, now,
            ))
            conn.execute("""
                INSERT INTO user_behavior_state (
                    subject_key, identity_type, account_id, stream_session_id,
                    nickname, toxicity_score, warning_count, violation_count,
                    last_violation_at, mute_until, pending_action_id,
                    pending_action, admin_review_required, last_decay_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_key) DO UPDATE SET
                    identity_type=excluded.identity_type,
                    account_id=excluded.account_id,
                    stream_session_id=excluded.stream_session_id,
                    nickname=excluded.nickname,
                    toxicity_score=excluded.toxicity_score,
                    warning_count=excluded.warning_count,
                    violation_count=excluded.violation_count,
                    last_violation_at=excluded.last_violation_at,
                    mute_until=excluded.mute_until,
                    pending_action_id=excluded.pending_action_id,
                    pending_action=excluded.pending_action,
                    admin_review_required=excluded.admin_review_required,
                    last_decay_at=excluded.last_decay_at,
                    updated_at=excluded.updated_at
            """, (
                subject_key, identity_type, account_id, stream_session_id, None,
                new_score, warning_count, violation_count,
                now if action != "none" else (state["last_violation_at"] if state else None),
                state["mute_until"] if state else None,
                moderation_id if action in {"timeout", "admin_review"} else None,
                action if action in {"timeout", "admin_review"} else None,
                int(action == "admin_review" or bool(state and state["admin_review_required"])),
                now, now,
            ))
            row = conn.execute(
                "SELECT * FROM moderation_actions WHERE moderation_id = ?",
                (moderation_id,),
            ).fetchone()
            return dict(row)

    def complete_moderation_action(
        self, moderation_id: str, *, reply_payload: Optional[dict],
        mute_until: Optional[str], completed_at: str,
    ) -> bool:
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT subject_key, action, status FROM moderation_actions "
                "WHERE moderation_id = ?", (moderation_id,)
            ).fetchone()
            if not row or row["status"] != "reserved":
                return False
            conn.execute("""
                UPDATE moderation_actions
                SET status = 'completed', reply_payload = ?, mute_until = ?,
                    completed_at = ?
                WHERE moderation_id = ? AND status = 'reserved'
            """, (
                json.dumps(reply_payload, ensure_ascii=False) if reply_payload else None,
                mute_until, completed_at, moderation_id,
            ))
            conn.execute("""
                UPDATE user_behavior_state
                SET mute_until = CASE WHEN ? IS NOT NULL THEN ? ELSE mute_until END,
                    pending_action_id = NULL, pending_action = NULL,
                    updated_at = ?
                WHERE subject_key = ? AND pending_action_id = ?
            """, (mute_until, mute_until, completed_at, row["subject_key"], moderation_id))
            return True

    def release_moderation_action(self, moderation_id: str, released_at: str) -> bool:
        """仅用于任务取消；不改变已完成处罚。"""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE moderation_actions SET status = 'released', completed_at = ?
                WHERE moderation_id = ? AND status = 'reserved'
            """, (released_at, moderation_id))
            if cursor.rowcount:
                conn.execute("""
                    UPDATE user_behavior_state
                    SET pending_action_id = NULL, pending_action = NULL, updated_at = ?
                    WHERE pending_action_id = ?
                """, (released_at, moderation_id))
            return cursor.rowcount == 1

    def clear_guest_behavior_state(self, subject_key: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM moderation_actions WHERE subject_key = ? AND identity_type = 'guest'",
                (subject_key,),
            )
            conn.execute(
                "DELETE FROM user_behavior_state WHERE subject_key = ? AND identity_type = 'guest'",
                (subject_key,),
            )

    def get_moderation_status(self, subject_key: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT subject_key, identity_type, mute_until, pending_action,
                       admin_review_required, toxicity_score, warning_count,
                       violation_count, updated_at
                FROM user_behavior_state WHERE subject_key = ?
            """, (subject_key,)).fetchone()
            return dict(row) if row else None

    def get_moderation_stats(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT action, status, COUNT(*) AS count FROM moderation_actions "
                "GROUP BY action, status"
            ).fetchall()
            return {
                "actions": {
                    f"{row['action']}:{row['status']}": row["count"]
                    for row in rows
                },
                "active_mutes": conn.execute(
                    "SELECT COUNT(*) FROM user_behavior_state "
                    "WHERE mute_until IS NOT NULL AND mute_until > ?",
                    (datetime.now(timezone.utc).isoformat(),),
                ).fetchone()[0],
                "pending_actions": conn.execute(
                    "SELECT COUNT(*) FROM moderation_actions WHERE status = 'reserved'"
                ).fetchone()[0],
            }

    def purge_expired_account_relationships(self, cutoff: str) -> int:
        with self._get_connection() as conn:
            cursor = conn.execute("""
                DELETE FROM account_audience_relationships
                WHERE last_seen_at < ?
            """, (cutoff,))
            return cursor.rowcount

    # ==================== Viewer Impression（低频账号留言） ====================

    def get_account_viewer_impression(self, account_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM account_viewer_impressions WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_active_account_viewer_impression_task(
        self, account_id: str
    ) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM account_viewer_impression_tasks
                WHERE account_id = ?
                  AND status IN ('pending', 'processing', 'failed_retryable')
                ORDER BY created_at DESC
                LIMIT 1
            """, (account_id,)).fetchone()
            return dict(row) if row else None

    def get_account_viewer_impression_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM account_viewer_impression_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_latest_account_viewer_impression_generation(self, account_id: str) -> Optional[Dict[str, Any]]:
        """Public task projection only; never fetch raw or intermediate evidence."""
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT task_id, status, requested_at, next_attempt_at
                FROM account_viewer_impression_tasks WHERE account_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT 1
            """, (account_id,)).fetchone()
            return dict(row) if row else None

    def create_account_viewer_impression_task(
        self,
        *,
        account_id: str,
        requested_at: str,
        evidence_snapshot: str,
        evidence_cutoff_at: str,
        cooldown_days: int,
        max_pending_tasks: int,
        expected_privacy_epoch: Optional[int] = None,
    ) -> Dict[str, Any]:
        """原子执行 active-task、冷却和容量检查并创建一条冻结快照任务。"""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_privacy_epoch is not None:
                epoch = conn.execute(
                    "SELECT epoch FROM account_viewer_impression_epochs WHERE account_id = ?", (account_id,)
                ).fetchone()
                if int(epoch[0] if epoch else 0) != expected_privacy_epoch:
                    return {"status": "memory_disabled"}
            preference = conn.execute(
                "SELECT long_term_memory_enabled FROM account_memory_preferences WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if preference is not None and not bool(preference[0]):
                return {"status": "memory_disabled"}

            active = conn.execute("""
                SELECT task_id, status, requested_at, next_attempt_at
                FROM account_viewer_impression_tasks
                WHERE account_id = ?
                  AND status IN ('pending', 'processing', 'failed_retryable')
                ORDER BY created_at DESC LIMIT 1
            """, (account_id,)).fetchone()
            if active:
                return {
                    "status": "active",
                    "task_id": active["task_id"],
                    "task_status": active["status"],
                    "requested_at": active["requested_at"],
                    "next_attempt_at": active["next_attempt_at"],
                    "existing_task": True,
                }

            current = conn.execute(
                "SELECT revision, next_request_at FROM account_viewer_impressions WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if current and current["next_request_at"]:
                try:
                    if datetime.fromisoformat(current["next_request_at"]) > datetime.fromisoformat(requested_at):
                        return {
                            "status": "cooldown",
                            "next_request_at": current["next_request_at"],
                        }
                except (TypeError, ValueError):
                    pass

            pending_count = conn.execute("""
                SELECT COUNT(*) FROM account_viewer_impression_tasks
                WHERE status IN ('pending', 'processing', 'failed_retryable')
            """).fetchone()[0]
            if int(pending_count) >= max(1, int(max_pending_tasks)):
                return {"status": "capacity"}

            target_revision = int(current["revision"] if current else 0) + 1
            task_id = str(uuid.uuid4())
            conn.execute("""
                INSERT INTO account_viewer_impression_tasks (
                    task_id, account_id, status, requested_at, attempt_count,
                    next_attempt_at, target_revision, evidence_snapshot,
                    evidence_cutoff_at, created_at, updated_at
                ) VALUES (?, ?, 'pending', ?, 0, ?, ?, ?, ?, ?, ?)
            """, (
                task_id, account_id, requested_at, None, target_revision,
                evidence_snapshot, evidence_cutoff_at, requested_at, requested_at,
            ))
            return {
                "status": "pending",
                "task_id": task_id,
                "task_status": "pending",
                "target_revision": target_revision,
                "existing_task": False,
                "next_request_at": (
                    datetime.fromisoformat(requested_at) + timedelta(days=cooldown_days)
                ).isoformat(),
            }

    def claim_account_viewer_impression_task(
        self, *, now: str, lease_seconds: int, max_attempts: int
    ) -> Optional[Dict[str, Any]]:
        """领取一条任务；过期 processing lease 在同一事务中回收。"""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                UPDATE account_viewer_impression_tasks
                SET status = 'failed_retryable', lease_expires_at = NULL,
                    execution_token = NULL, next_attempt_at = ?,
                    error_code = 'lease_expired', error_detail = 'worker lease expired',
                    updated_at = ?
                WHERE status = 'processing'
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            """, (now, now, now))

            while True:
                row = conn.execute("""
                    SELECT * FROM account_viewer_impression_tasks
                    WHERE status = 'pending'
                       OR (status = 'failed_retryable'
                           AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                    ORDER BY created_at ASC
                    LIMIT 1
                """, (now,)).fetchone()
                if not row:
                    return None
                if int(row["attempt_count"] or 0) >= max(1, int(max_attempts)):
                    conn.execute("""
                        UPDATE account_viewer_impression_tasks
                        SET status = 'failed', error_code = COALESCE(error_code, 'max_attempts'),
                            evidence_snapshot = NULL, updated_at = ?
                        WHERE task_id = ? AND status IN ('pending', 'failed_retryable')
                    """, (now, row["task_id"]))
                    continue
                token = str(uuid.uuid4())
                lease_expires = (
                    datetime.fromisoformat(now) + timedelta(seconds=lease_seconds)
                ).isoformat()
                updated = conn.execute("""
                    UPDATE account_viewer_impression_tasks
                    SET status = 'processing', started_at = COALESCE(started_at, ?),
                        lease_expires_at = ?, execution_token = ?, next_attempt_at = NULL,
                        attempt_count = attempt_count + 1, updated_at = ?
                    WHERE task_id = ? AND status IN ('pending', 'failed_retryable')
                """, (now, lease_expires, token, now, row["task_id"]))
                if updated.rowcount != 1:
                    continue
                claimed = dict(row)
                claimed.update({
                    "status": "processing", "execution_token": token,
                    "lease_expires_at": lease_expires,
                    "attempt_count": int(row["attempt_count"] or 0) + 1,
                })
                return claimed

    def viewer_impression_execution_active(
        self, *, task_id: str, account_id: str, execution_token: str, now: str
    ) -> bool:
        with self._get_connection() as conn:
            return self._impression_execution_active(conn, task_id, account_id, execution_token, now)

    @staticmethod
    def _impression_execution_active(conn, task_id, account_id, execution_token, now) -> bool:
        return conn.execute("""
            SELECT 1 FROM account_viewer_impression_tasks t
            JOIN account_memory_preferences p ON p.account_id = t.account_id
            WHERE t.task_id = ? AND t.account_id = ? AND t.execution_token = ?
              AND t.status = 'processing' AND t.evidence_snapshot IS NOT NULL
              AND p.long_term_memory_enabled = 1
              AND julianday(t.lease_expires_at) > julianday(?)
        """, (task_id, account_id, execution_token, now)).fetchone() is not None

    def load_viewer_impression_stages(
        self, *, task_id: str, account_id: str, execution_token: str, now: str
    ) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            conn.execute("BEGIN")
            if not self._impression_execution_active(conn, task_id, account_id, execution_token, now):
                return None
            rows = conn.execute(
                "SELECT stage_key, result_json FROM account_viewer_impression_stages WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            return {row["stage_key"]: json.loads(row["result_json"]) for row in rows}

    def save_viewer_impression_stage(
        self, *, task_id: str, account_id: str, execution_token: str,
        stage_key: str, result: Dict[str, Any], now: str
    ) -> bool:
        # Internal checkpoint keys include bounded chunk/merge coordinates.
        # Callers validate the stage model before persisting it.
        if (not stage_key or len(stage_key) > 80
                or not all(c.isascii() and (c.isalnum() or c in "_:-") for c in stage_key)):
            raise ValueError("invalid_impression_stage_key")
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded) > 500000:
            raise ValueError("impression_checkpoint_too_large")
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._impression_execution_active(conn, task_id, account_id, execution_token, now):
                return False
            conn.execute("""
                INSERT OR IGNORE INTO account_viewer_impression_stages
                (task_id, stage_key, result_json, created_at) VALUES (?, ?, ?, ?)
            """, (task_id, stage_key, encoded, now))
            # Stage results are immutable for a frozen task. Retrying a
            # completed stage may read it but cannot replace it with new prose.
            existing = conn.execute("""
                SELECT result_json FROM account_viewer_impression_stages
                WHERE task_id = ? AND stage_key = ?
            """, (task_id, stage_key)).fetchone()
            return existing[0] == encoded

    def renew_account_viewer_impression_task_lease(
        self,
        *,
        task_id: str,
        execution_token: str,
        now: str,
        lease_seconds: int,
    ) -> bool:
        lease_expires = (
            datetime.fromisoformat(now) + timedelta(seconds=lease_seconds)
        ).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE account_viewer_impression_tasks
                SET lease_expires_at = ?, updated_at = ?
                WHERE task_id = ? AND status = 'processing'
                  AND execution_token = ?
            """, (lease_expires, now, task_id, execution_token))
            return cursor.rowcount == 1

    def release_account_viewer_impression_task(
        self, *, task_id: str, execution_token: str, now: str
    ) -> bool:
        """因 provider 暂不在营业时间而释放领取，不消耗 attempt。"""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE account_viewer_impression_tasks
                SET status = 'pending', started_at = NULL,
                    lease_expires_at = NULL, execution_token = NULL,
                    attempt_count = MAX(0, attempt_count - 1),
                    next_attempt_at = NULL, updated_at = ?
                WHERE task_id = ? AND status = 'processing'
                  AND execution_token = ?
            """, (now, task_id, execution_token))
            return cursor.rowcount == 1

    def complete_account_viewer_impression_task(
        self,
        *,
        task_id: str,
        account_id: str,
        execution_token: str,
        content: str,
        tone: str,
        generated_at: str,
        next_request_at: str,
        provider: Optional[str],
        model: Optional[str],
        latency_ms: int,
        evidence_refs_json: Optional[str] = None,
        evidence_counts_json: Optional[str] = None,
        snapshot_hash: Optional[str] = None,
    ) -> bool:
        """验证租约后原子替换 current letter，旧留言直到本事务提交都保留。"""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("""
                SELECT target_revision, evidence_cutoff_at, lease_expires_at
                FROM account_viewer_impression_tasks
                WHERE task_id = ? AND account_id = ? AND status = 'processing'
                  AND execution_token = ?
            """, (task_id, account_id, execution_token)).fetchone()
            if not task:
                return False
            if task["lease_expires_at"]:
                try:
                    if datetime.fromisoformat(task["lease_expires_at"]) <= datetime.fromisoformat(generated_at):
                        return False
                except (TypeError, ValueError):
                    return False
            pref = conn.execute(
                "SELECT long_term_memory_enabled FROM account_memory_preferences WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if pref is not None and not bool(pref[0]):
                conn.execute("""
                    UPDATE account_viewer_impression_tasks
                    SET status = 'cancelled', execution_token = NULL,
                        lease_expires_at = NULL, evidence_snapshot = NULL,
                        error_code = 'memory_disabled', updated_at = ?
                    WHERE task_id = ?
                """, (generated_at, task_id))
                conn.execute(
                    "DELETE FROM account_viewer_impressions WHERE account_id = ?",
                    (account_id,),
                )
                return False

            current = conn.execute(
                "SELECT revision FROM account_viewer_impressions WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            expected_previous = int(task["target_revision"]) - 1
            if current and int(current["revision"]) != expected_previous:
                conn.execute("""
                    UPDATE account_viewer_impression_tasks
                    SET status = 'failed', execution_token = NULL,
                        lease_expires_at = NULL, evidence_snapshot = NULL,
                        error_code = 'stale_revision', updated_at = ?
                    WHERE task_id = ?
                """, (generated_at, task_id))
                return False
            conn.execute("""
                INSERT INTO account_viewer_impressions (
                    account_id, revision, content, tone, generated_at,
                    next_request_at, evidence_cutoff_at, created_at, updated_at,
                    evidence_refs_json, evidence_counts_json, snapshot_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    revision=excluded.revision, content=excluded.content,
                    tone=excluded.tone, generated_at=excluded.generated_at,
                    next_request_at=excluded.next_request_at,
                    evidence_cutoff_at=excluded.evidence_cutoff_at,
                    evidence_refs_json=excluded.evidence_refs_json,
                    evidence_counts_json=excluded.evidence_counts_json,
                    snapshot_hash=excluded.snapshot_hash,
                    updated_at=excluded.updated_at
            """, (
                account_id, int(task["target_revision"]), content, tone,
                generated_at, next_request_at, task["evidence_cutoff_at"],
                generated_at, generated_at, evidence_refs_json,
                evidence_counts_json, snapshot_hash,
            ))
            updated = conn.execute("""
                UPDATE account_viewer_impression_tasks
                SET status = 'completed', completed_at = ?,
                    lease_expires_at = NULL, execution_token = NULL,
                    next_attempt_at = NULL, error_code = NULL, error_detail = NULL,
                    evidence_snapshot = NULL, provider = ?, model = ?,
                    latency_ms = ?, updated_at = ?
                WHERE task_id = ? AND status = 'processing'
                  AND execution_token = ?
            """, (
                generated_at, provider, model, latency_ms, generated_at,
                task_id, execution_token,
            ))
            if updated.rowcount != 1:
                # The current-letter upsert and task completion are one atomic
                # commit.  A defensive invariant failure must roll both back,
                # never leave a letter committed without a completed task.
                raise RuntimeError("viewer impression task completion invariant failed")
            return True

    def fail_account_viewer_impression_task(
        self,
        *,
        task_id: str,
        execution_token: str,
        now: str,
        error_code: str,
        error_detail: str,
        retryable: bool,
        next_attempt_at: Optional[str],
    ) -> bool:
        with self._get_connection() as conn:
            status = "failed_retryable" if retryable else "failed"
            cursor = conn.execute("""
                UPDATE account_viewer_impression_tasks
                SET status = ?, lease_expires_at = NULL, execution_token = NULL,
                    next_attempt_at = ?, error_code = ?, error_detail = ?,
                    evidence_snapshot = CASE WHEN ? = 'failed_retryable' THEN evidence_snapshot ELSE NULL END,
                    updated_at = ?
                WHERE task_id = ? AND status = 'processing' AND execution_token = ?
            """, (
                status, next_attempt_at, error_code, error_detail, status,
                now, task_id, execution_token,
            ))
            return cursor.rowcount == 1

    def cancel_account_viewer_impression_tasks(self, account_id: str, *, now: str) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE account_viewer_impression_tasks
                SET status = 'cancelled', lease_expires_at = NULL,
                    execution_token = NULL, evidence_snapshot = NULL,
                    error_code = 'memory_disabled', error_detail = NULL, updated_at = ?
                WHERE account_id = ?
                  AND status IN ('pending', 'processing', 'failed_retryable')
            """, (now, account_id))
            conn.execute(
                "DELETE FROM account_viewer_impressions WHERE account_id = ?",
                (account_id,),
            )

    def list_account_viewer_impression_export(self, account_id: str) -> Optional[Dict[str, Any]]:
        row = self.get_account_viewer_impression(account_id)
        if not row:
            return None
        return {
            "revision": int(row["revision"]),
            "content": row["content"],
            "generated_at": row["generated_at"],
            "next_request_at": row["next_request_at"],
        }

    def get_viewer_impression_stats(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            counts = {
                row["status"]: int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM account_viewer_impression_tasks GROUP BY status"
                ).fetchall()
            }
            row = conn.execute("""
                SELECT COUNT(*) AS total, AVG(latency_ms) AS average_latency_ms,
                       MAX(completed_at) AS last_success_at
                FROM account_viewer_impression_tasks WHERE status = 'completed'
            """).fetchone()
            return {
                "task_status_counts": counts,
                "completed_total": int(row["total"] or 0),
                "average_latency_ms": round(float(row["average_latency_ms"] or 0.0), 2),
                "last_success_at": row["last_success_at"],
            }

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

    def get_account_conversation_fragment_by_danmaku(
        self, account_id: str, danmaku_id: str
    ) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM account_conversation_fragments
                WHERE account_id = ? AND danmaku_id = ?
                LIMIT 1
            """, (account_id, danmaku_id)).fetchone()
        return self._decode_conversation_fragment(row)

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

    def create_auth_refresh_session(self, session: Dict[str, Any]) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO auth_refresh_sessions (
                    token_hash, account_id, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, NULL)
            """, (
                session["token_hash"], session["account_id"],
                session["created_at"], session["expires_at"],
            ))

    def rotate_auth_refresh_session(
        self, *, current_token_hash: str, new_refresh_session: Dict[str, Any],
        new_access_session: Dict[str, Any], now: str,
    ) -> Optional[Dict[str, Any]]:
        """一次性消费 refresh token，并原子签发新的 access/refresh 会话。"""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = conn.execute("""
                SELECT a.account_id, a.username, a.nickname, a.created_at,
                       COALESCE(h.version, 1) AS nickname_version
                FROM auth_refresh_sessions AS s
                JOIN accounts AS a ON a.account_id = s.account_id
                LEFT JOIN account_nickname_history AS h
                  ON h.account_id = a.account_id AND h.is_current = 1
                WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?
                  AND a.login_enabled = 1
            """, (current_token_hash, now)).fetchone()
            if not account:
                return None
            consumed = conn.execute("""
                UPDATE auth_refresh_sessions SET revoked_at = ?
                WHERE token_hash = ? AND revoked_at IS NULL
            """, (now, current_token_hash))
            if consumed.rowcount != 1:
                return None
            account_id = account["account_id"]
            conn.execute("""
                INSERT INTO auth_sessions (
                    token_hash, account_id, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, NULL)
            """, (
                new_access_session["token_hash"], account_id,
                new_access_session["created_at"], new_access_session["expires_at"],
            ))
            conn.execute("""
                INSERT INTO auth_refresh_sessions (
                    token_hash, account_id, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, NULL)
            """, (
                new_refresh_session["token_hash"], account_id,
                new_refresh_session["created_at"], new_refresh_session["expires_at"],
            ))
            return dict(account)


# 全局数据库管理器实例
db_manager = DatabaseManager()
