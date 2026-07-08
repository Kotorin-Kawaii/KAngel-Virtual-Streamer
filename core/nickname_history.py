"""登录观众改名感知上下文；旧昵称原文不进入公开回复提示。"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from core.database_manager import db_manager
from models.viewer import ViewerIdentity


class NicknameHistoryContextManager:
    def __init__(self, database=None, awareness_days: int = 14):
        self._database = database
        self.awareness_days = awareness_days

    @property
    def database(self):
        return self._database or db_manager

    def build_for_reply(
        self, identity: Optional[ViewerIdentity]
    ) -> Optional[dict]:
        if not identity or not identity.is_authenticated:
            return None

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.awareness_days)
        previous = self.database.claim_recent_nickname_change(
            identity.account_id, cutoff.isoformat(), now.isoformat()
        )
        return {
            "current_nickname": identity.current_nickname,
            "nickname_version": identity.nickname_version or 1,
            "recently_renamed": previous is not None,
            # 有意不返回旧昵称原文。直播回复只需要知道发生过改名。
            "old_nickname_may_be_spoken": False,
        }


nickname_history_context_manager = NicknameHistoryContextManager()
