class PMAgentError(Exception):
    """项目管理Agent基础异常"""


class FeishuAPIError(PMAgentError):
    def __init__(self, code, msg, context=""):
        self.code = code
        self.msg = msg
        self.context = context
        super().__init__(f"[{code}] {msg}" + (f" | {context}" if context else ""))


class StateTransitionError(PMAgentError):
    def __init__(self, current_state, target_state, reason=""):
        self.current_state = current_state
        self.target_state = target_state
        super().__init__(f"Cannot transition from '{current_state}' to '{target_state}'" + (f": {reason}" if reason else ""))


class GroupCreationError(PMAgentError):
    def __init__(self, step, reason, failed_members=None):
        self.step = step
        self.reason = reason
        self.failed_members = failed_members or []
        super().__init__(f"Group creation failed at step '{step}': {reason}")


class DuplicateProjectError(PMAgentError):
    """活跃项目中已存在相同项目名称或简称"""

    def __init__(self, field, value, existing_project_id=""):
        self.field = field                    # 冲突字段：项目名称 / 项目简称
        self.value = value
        self.existing_project_id = existing_project_id
        msg = f"{field}「{value}」已被项目 {existing_project_id} 占用"
        super().__init__(msg.strip())


class ArchiveError(PMAgentError):
    def __init__(self, step, reason, partial_results=None):
        self.step = step
        self.reason = reason
        self.partial_results = partial_results or {}
        super().__init__(f"Archive failed at step '{step}': {reason}")


class AIEngineError(PMAgentError):
    def __init__(self, reason, fallback_available=False):
        self.fallback_available = fallback_available
        super().__init__(f"AI engine error: {reason}")
