"""Session-local speech continuity. No viewer text, memory writes or model calls."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

from config import settings
from kangel.stream.domain.live_presence import SpeechThread, SpeechThreadStatus
from .stage_speech_journal import public_stage_text


@dataclass(frozen=True)
class ThreadSeed:
    seed_id: str
    category: str
    topic: str
    points: tuple[str, str, str]
    opener: str


# Reviewed public-persona material. These are opinions, never invented game events.
SEEDS = {
    "game": ThreadSeed("game-presence-v1", "game", "游戏直播的节目感", (
        "聊聊游戏直播里认真和节目效果的关系", "认真玩也可以保留一点嘴硬和自嘲", "回到当前游戏节奏"),
        "说起来，认真玩游戏和做节目效果，这两件事我还挺想聊聊的。"),
    "chat": ThreadSeed("chat-presence-v1", "chat", "直播里的安静时刻", (
        "聊聊直播里安静时刻的感受", "陪伴不一定要求每个人一直说话", "自然收束到当下的聊天"),
        "说起来，直播里安静下来的时候，也不一定非得急着填满吧……这事我还挺想聊聊的。"),
}


# Keep v1 seeds only for already-persisted threads, never select them for new talks.
CONTENT_SEEDS = (
    ThreadSeed("game-retry-v2", "game", "再试一把还是先换个思路", (
        "表达自己对再试一次与换思路的取舍，不声称刚刚失败",
        "接住上段观点，区分不服输与一直重复同一种办法，给一个具体判断理由",
        "收束自己的选择，留一个可接话的观点，不声称游戏取得进展"),
        "玩游戏最烦的就是明知道该换个思路，手又想再试一次。才不是输不起，我只是想赢得漂亮一点。"),
    ThreadSeed("game-style-v2", "game", "漂亮通关和稳稳过关", (
        "表达稳稳过关与漂亮发挥之间自己的偏好，不编造当前关卡",
        "接上段，谈选择冒险或稳妥的具体理由，不讲抽象节目效果",
        "自然收束到自己的游戏态度，不宣布开始或切换游戏"),
        "稳稳过关当然很好啦，可是赢得太没气势又有点不甘心。本天使连赢都想赢得好看一点，要求很高吧。"),
    ThreadSeed("chat-choice-v2", "chat", "新作品值得继续看的那个瞬间", (
        "从开场、角色或细节里挑一个自己在意的点，表达具体偏好",
        "沿上段选定的点展开一个理由或对比，不凭空说今天看了某部作品",
        "用一个有个人态度的结论收束，不升华到陪伴与直播意义"),
        "看新作品的时候，我对那种一出场就很会装酷的角色特别挑。要装就装得漂亮一点嘛，不然本天使可不买账。"),
    ThreadSeed("chat-hobby-v2", "chat", "喜欢的东西要不要跟风", (
        "表达自己对热门与冷门喜好的具体态度",
        "接上段区分真喜欢与为了合群假装喜欢，保留嘴硬和个人趣味",
        "收束成自己的选择，不替观众宣布他们的喜好"),
        "有些东西大家都说好，我偏偏没感觉，这种时候我才不会硬装喜欢。连喜欢什么都要跟风，本天使多没面子啊。"),
    ThreadSeed("chat-music-v2", "chat", "一首歌让人愿意再听的部分", (
        "从旋律、声音或节奏中选择一个自己在意的点，不声称正在播放某首歌",
        "接上段说明为什么某个具体特点值得重听，不编造曲名与今日经历",
        "把观点收束到个人音乐趣味，不宣布换歌或播放"),
        "有些歌就是一个小地方特别抓人，听完还惦记着。整首都要讨好所有人，反而没那个劲儿了，我就喜欢有点脾气的。"),
)


def select_thread_seed(context, thread=None, recent_seed_ids=()):
    """Reviewed concrete content, based on actual activity and session direction."""
    if context is None:
        return None
    if thread is not None:
        # Continuing a thread must never silently replace its original topic.
        return next((seed for seed in (*SEEDS.values(), *CONTENT_SEEDS)
                     if seed.seed_id == thread.seed_id), None)
    category = context.activity["category"]
    if category not in {"game", "chat"}:
        return None
    direction = " ".join(str(v) for v in (
        context.theme.get("id"), context.theme.get("name"), context.plan.get("direction"),
        context.beat.get("objective"))).casefold()
    if category == "game" or any(word in direction for word in ("game", "游戏", "ゲーム")):
        # A game-themed chat is about games, not a claim that gameplay is happening.
        candidates = [replace(s, category=category) for s in CONTENT_SEEDS if s.category == "game"]
    elif any(word in direction for word in ("music", "音乐", "音楽")):
        candidates = [s for s in CONTENT_SEEDS if s.seed_id == "chat-music-v2"]
    else:
        candidates = [s for s in CONTENT_SEEDS if s.category == "chat" and s.seed_id != "chat-music-v2"]
    candidates = [s for s in candidates if s.seed_id not in recent_seed_ids]
    if not candidates:
        return None
    key = f"{context.stream_session_id}:{direction}:{context.beat['id']}:{context.activity['activity_id']}"
    seed = candidates[int.from_bytes(sha256(key.encode()).digest()[:4], "big") % len(candidates)]
    name = str(context.theme.get("name") or context.theme["id"])
    if context.beat["kind"] == "detour":
        label = str(context.beat["label"])
        topic = f"{name}·{label}·{seed.topic}"[:120]
    else:
        topic = f"{name}·{seed.topic}"[:120]
    return replace(seed, topic=topic)


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(value):
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


class SpeechThreadService:
    """All writes use Presence CAS; snapshots are the only persistence surface."""
    def __init__(self, presence):
        self.presence = presence
        self.metrics = presence.metrics

    @staticmethod
    def current(state):
        return state.active_thread or next(iter(state.suspended_threads), None)

    def metric(self, name):
        self.metrics.increment("thread_" + name)

    def write(self, state, thread, **kwargs):
        changed = self.presence.update(
            state,
            active_thread=thread if thread and thread.status == SpeechThreadStatus.ACTIVE else None,
            suspended_threads=(thread,) if thread and thread.status == SpeechThreadStatus.SUSPENDED else (),
            **kwargs,
        )
        if changed is None:
            self.metric("version_conflicts")
        return changed

    def abandon(self, state, reason, now=None):
        changed = self.write(state, None, pending_speech_attempt=None, now=now)
        if changed:
            self.metric("abandoned")
            self.metric("abandoned_" + reason)
        return changed

    @staticmethod
    def incompatibility(thread, facts):
        activity, mainline = facts["activity"], facts["mainline"]
        if mainline.current_beat_kind in {"closing", "opening"} or mainline.current_beat_id == mainline.plan.closing_beat_id:
            return "mainline_change"
        if not (thread.activity_id == activity.activity_id or (
            thread.allow_same_category and thread.activity_category == activity.category
        )):
            return "activity_change"
        if not (thread.beat_id == mainline.current_beat_id or (
            thread.plan_profile_id == mainline.plan.profile_id
            and thread.beat_kind == mainline.current_beat_kind == "mainline"
        )):
            return "mainline_change"
        return None

    def maintain(self, state, facts, *, restart=False, now=None):
        now = now or utcnow()
        thread = self.current(state)
        if state.pending_speech_attempt:
            return self.abandon(state, "uncertain_delivery", now=now)
        if thread is None:
            return state
        if len(state.suspended_threads) > 1 or (state.active_thread and state.suspended_threads):
            normalized = self.write(state, thread, now=now)
            if normalized is None:
                return None
            state = normalized
        expires = parse_time(thread.expires_at) if thread.expires_at else parse_time(thread.started_at) + timedelta(seconds=300)
        if now >= expires:
            self.metric("restore_expired" if restart else "expired")
            return self.abandon(state, "ttl", now=now)
        if restart and thread.status == SpeechThreadStatus.SUSPENDED and not thread.interruption_visible:
            return self.abandon(state, "interruption_delivery_unknown", now=now)
        reason = self.incompatibility(thread, facts)
        if reason:
            return self.abandon(state, reason, now=now)
        if thread.interrupt_count > 3:
            return self.abandon(state, "interrupt_limit", now=now)
        if restart:
            self.metric("restore_success")
        return state

    def suspend(self, session_id, event_id, source, now=None):
        state = self.presence.get(session_id)
        if not state or not state.active_thread:
            return None
        thread = state.active_thread
        if thread.interrupt_count >= 3:
            self.abandon(state, "interrupt_limit", now=now)
            return None
        now = now or utcnow()
        changed = self.write(state, replace(
            thread, status=SpeechThreadStatus.SUSPENDED,
            version=thread.version + 1, interrupt_count=thread.interrupt_count + 1,
            interruption_id=event_id, interruption_source=source,
            interruption_visible=False, last_touched_at=now.isoformat(),
        ), now=now)
        if changed:
            self.metric("suspended")
            return (session_id, thread.thread_id, event_id)
        return None

    def finish_interrupt(self, token, visible, now=None):
        if not token:
            return None
        session_id, thread_id, event_id = token
        state = self.presence.get(session_id)
        thread = self.current(state) if state else None
        if not thread or thread.thread_id != thread_id or thread.interruption_id != event_id:
            return None
        now = now or utcnow()
        if thread.status != SpeechThreadStatus.SUSPENDED or thread.interruption_visible:
            return state
        changed = replace(
            thread, version=thread.version + 1,
            status=SpeechThreadStatus.SUSPENDED if visible else SpeechThreadStatus.ACTIVE,
            interrupt_count=thread.interrupt_count if visible else max(0, thread.interrupt_count - 1),
            interruption_visible=visible,
            interruption_id=event_id if visible else None,
            interruption_source=thread.interruption_source if visible else None,
            last_touched_at=now.isoformat(),
        )
        return self.write(state, changed, now=now,
            **({"last_speech_at": now.isoformat()} if visible else {}))

    def recover_interrupt(self, state, now=None):
        thread = self.current(state)
        if not thread or thread.status != SpeechThreadStatus.SUSPENDED or thread.interruption_visible:
            return state
        # REPLIED is committed before broadcast and cannot establish visibility.
        # Only finish_interrupt's persisted delivery result permits continuation.
        if thread.interruption_source == "sc":
            with self.presence.database._get_connection() as conn:
                row = conn.execute("SELECT status FROM sc_queue WHERE sc_id = ?", (thread.interruption_id,)).fetchone()
            finished = row is None or row[0] not in {"pending", "processing"}
        elif thread.interruption_source == "moderation":
            finished = True  # No durable delivery receipt; never guess.
        else:
            row = self.presence.database.get_danmaku_by_id(thread.interruption_id)
            finished = row is None or bool(row.get("consumption_reason"))
        if finished:
            return self.abandon(state, "interruption_delivery_unknown", now=now) or state
        return state

    def reserve(self, state, operation, now=None):
        if state.pending_speech_attempt or operation not in {"open", "advance", "resume"}:
            return None
        thread = self.current(state)
        if operation != "open" and not thread:
            return None
        if operation == "resume" and (thread.status != SpeechThreadStatus.SUSPENDED or not thread.interruption_visible):
            return None
        if operation == "advance" and thread.status != SpeechThreadStatus.ACTIVE:
            return None
        attempt = {
            "attempt_id": str(uuid4()), "operation": operation,
            "thread_id": thread.thread_id if thread else None,
            "thread_version": thread.version if thread else 0,
            "started_at": (now or utcnow()).isoformat(),
        }
        return self.presence.update(state, pending_speech_attempt=attempt, now=now)

    def finish_speech(self, reserved, facts, seed, *, delivered, hint=None, hint_invalid=False,
                      public_reply=None, now=None):
        now = now or utcnow()
        attempt = reserved.pending_speech_attempt
        current = self.presence.get(reserved.stream_session_id)
        if not current or current.version != reserved.version or current.pending_speech_attempt != attempt:
            self.metric("version_conflicts")
            return None
        if not delivered:
            return self.presence.update(current, pending_speech_attempt=None, now=now)
        last_public_text = public_stage_text(public_reply) if public_reply is not None else None
        thread = self.current(current)
        operation = attempt["operation"]
        if hint_invalid:
            # Delivery is a stage fact even when semantic tracking is rejected.
            # Commit the receipt time and thread cleanup together, without guessing
            # a replacement hint or overwriting a newer Presence version.
            changed = self.write(current, None, pending_speech_attempt=None,
                                 last_speech_at=now.isoformat(), now=now)
            if changed:
                if thread:
                    self.metric("abandoned")
                    self.metric("abandoned_invalid_hint")
                else:
                    self.metric("invalid_hint_atomic")
            return changed
        if operation == "open":
            activity, mainline = facts["activity"], facts["mainline"]
            updated = SpeechThread(
                thread_id=str(uuid4()), topic=(hint or {}).get("topic", seed.topic),
                unfinished_point=(hint or {}).get("unfinished_point", seed.points[1]),
                status=SpeechThreadStatus.ACTIVE, source="self_talk", importance=0.5,
                started_at=now.isoformat(), last_touched_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=settings.stream.live_presence_thread_ttl_seconds)).isoformat(),
                opened_by_speech_id=attempt["attempt_id"], last_speech_id=attempt["attempt_id"],
                activity_id=activity.activity_id, activity_category=activity.category,
                activity_version=activity.version, mainline_beat_version=mainline.beat_version,
                beat_id=mainline.current_beat_id, beat_kind=mainline.current_beat_kind,
                plan_profile_id=mainline.plan.profile_id, allow_same_category=True, seed_id=seed.seed_id,
                last_public_text=last_public_text,
            )
        else:
            complete = thread.step_count >= 2 or (hint or {}).get("action") == "complete"
            updated = None if complete else replace(
                thread, status=SpeechThreadStatus.ACTIVE, version=thread.version + 1,
                step_count=thread.step_count + 1,
                resume_count=thread.resume_count + (operation == "resume"),
                last_speech_id=attempt["attempt_id"], last_touched_at=now.isoformat(),
                unfinished_point=(hint or {}).get("unfinished_point", seed.points[2]),
                last_public_text=last_public_text,
                interruption_id=None, interruption_source=None, interruption_visible=False,
            )
        changed = self.write(current, updated, pending_speech_attempt=None,
            last_speech_at=now.isoformat(), now=now)
        if changed:
            self.metric("opened" if operation == "open" else "resumed" if operation == "resume" else "advance")
            if operation == "open" and thread:
                self.metric("abandoned")
                self.metric("abandoned_replaced")
            if updated is None:
                self.metric("ended")
                if (hint or {}).get("action") == "complete":
                    self.metric("completed")
                    if thread.resume_count or operation == "resume":
                        self.metric("resumed_completed")
                else:
                    # A safety cap cannot prove that the model finished its thought.
                    self.metric("ended_step_limit")
            if settings.stream.live_presence_thread_semantic_hint_enabled:
                if updated is None and (hint or {}).get("action") != "complete":
                    self.metrics.record_semantic_hint(operation, "committed_step_limit")
                elif hint is None:
                    self.metrics.record_semantic_hint(operation, "committed_seed_fallback")
                else:
                    committed = {
                        "open": "committed_open",
                        "advance": "committed_advance",
                        "complete": "committed_complete",
                    }.get(hint.get("action"))
                    if committed:
                        self.metrics.record_semantic_hint(operation, committed)
        return changed

    def diagnostics(self, state):
        thread = self.current(state) if state else None
        if thread is None:
            return {"current": None, "pending_attempt": state.pending_speech_attempt if state else None}
        return {
            "current": thread.to_dict(),
            "age_seconds": max(0, (utcnow() - parse_time(thread.started_at)).total_seconds()),
            "ttl_remaining_seconds": max(0, (parse_time(thread.expires_at) - utcnow()).total_seconds()) if thread.expires_at else None,
            "pending_attempt": state.pending_speech_attempt,
        }
