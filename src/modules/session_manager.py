import time
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """用户交互会话"""
    user_id: str
    chat_id: str
    chat_kind: str = "p2p"     # 会话类型 p2p（与机器人私聊）/ group（群聊）
    state: str = "idle"
    project_name: str = ""
    project_abbr: str = ""
    project_id: str = ""
    group_chat_id: str = ""
    dept_leads: list = field(default_factory=list)
    selected_members: list = field(default_factory=list)
    collected_members: dict = field(default_factory=dict)
    progress_message_id: str = ""
    welcome_sent: bool = False
    pm_user_id: str = ""
    pm_chat_id: str = ""
    lead_name: str = ""
    card_id: str = ""          # 当前会话绑定的 cardkit 卡片实体ID
    card_seq: int = 0           # batch_update 已用序号（严格递增）
    member_card_sent_at: float = 0.0  # 成员收集卡发送时间戳（用于24h冻结/逾期判定）
    member_task_id: str = ""        # 负责人收集成员对应的飞书任务 GUID（P2 待办联动）
    approved_ids: list = field(default_factory=list)  # 已被 PM 认可的成员 open_id 列表（增量确认）
    confirm_message_id: str = ""    # PM 确认卡 message_id（P3 追加式提交：空=未提交过，非空=已提交过）
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def update(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
        self.updated_at = time.time()


class SessionManager:
    """会话状态管理 - 跟踪每个用户的建群/解散流程进度"""

    SESSION_TIMEOUT = 3600

    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, user_id, chat_id) -> Session:
        key = f"{user_id}:{chat_id}"
        if key not in self._sessions:
            self._sessions[key] = Session(user_id=user_id, chat_id=chat_id)
        return self._sessions[key]

    def get(self, user_id, chat_id) -> Optional[Session]:
        key = f"{user_id}:{chat_id}"
        return self._sessions.get(key)

    def get_by_user(self, user_id) -> Optional[Session]:
        """按 user_id 查找任意一个会话（用于chat_id未知/占位的场景兜底）"""
        for s in self._sessions.values():
            if s.user_id == user_id:
                return s
        return None

    def migrate_context(self, user_id, real_chat_id) -> Session:
        """把占位chat_id会话的上下文迁移到真实chat_id会话，返回真实会话。

        若占位会话比真实会话更新（新一轮重新发卡的场景），以占位会话为准，
        避免真实会话残留上一轮的旧 card_id / 已选状态。
        """
        real_session = self.get_or_create(user_id, real_chat_id)

        source = None
        placeholder = self.get(user_id, user_id)
        if (placeholder is not None and placeholder is not real_session
                and placeholder.updated_at >= real_session.updated_at):
            # 新一轮发卡产生的占位会话更新：必须以它为准
            source = placeholder
        elif not real_session.project_name:
            old = self.get_by_user(user_id)
            if old is not None and old is not real_session:
                source = old

        if source is not None:
            for attr in ("chat_kind", "state", "project_name", "project_abbr", "project_id",
                         "group_chat_id", "dept_leads", "selected_members",
                         "collected_members", "pm_user_id", "pm_chat_id", "lead_name",
                         "card_id", "card_seq", "member_card_sent_at", "member_task_id",
                         "approved_ids", "confirm_message_id"):
                src_val = getattr(source, attr)
                # 保护 PM 会话关键字段：当 source（负责人占位会话）为空时不覆盖
                # 避免 PM 同时也是负责人时，group_chat_id / dept_leads / collected_members 被清空
                if attr in ("group_chat_id", "dept_leads", "project_id", "collected_members"):
                    if not src_val:
                        continue
                setattr(real_session, attr, src_val)

        # 迁移完成后删除占位会话，避免后续再次误命中
        if placeholder is not None and placeholder is not real_session:
            self.clear(user_id, user_id)

        return real_session

    def update_state(self, user_id, chat_id, state, **kwargs):
        session = self.get_or_create(user_id, chat_id)
        session.state = state
        session.update(**kwargs)
        logger.info(f"会话状态更新: {user_id} -> {state}")
        return session

    def clear(self, user_id, chat_id):
        key = f"{user_id}:{chat_id}"
        if key in self._sessions:
            del self._sessions[key]
            logger.info(f"会话已清除: {user_id}")

    def cleanup_expired(self):
        now = time.time()
        expired = [
            k for k, s in self._sessions.items()
            if now - s.updated_at > self.SESSION_TIMEOUT
        ]
        for k in expired:
            del self._sessions[k]
            logger.info(f"过期会话已清除: {k}")

    def find_overdue_leads(self, now_ts=None, overdue_seconds=24 * 3600):
        """查找逾期未提交成员的负责人会话（P4 每日8点推送使用）。

        判定条件（同时满足）：
        - state == "collecting_members"
        - member_card_sent_at > 0（已发送过收集卡）
        - (now - member_card_sent_at) > overdue_seconds（默认24h）
        - selected_members 为空（负责人尚未提交）

        Args:
            now_ts: 当前时间戳，默认 time.time()
            overdue_seconds: 逾期阈值秒数，默认 24*3600

        Returns:
            list[Session]：逾期未提交的负责人会话列表
        """
        now_ts = now_ts if now_ts is not None else time.time()
        overdue = []
        for s in self._sessions.values():
            if (s.state == "collecting_members"
                    and s.member_card_sent_at > 0
                    and (now_ts - s.member_card_sent_at) > overdue_seconds
                    and not (s.selected_members or [])):
                overdue.append(s)
        return overdue
