import logging

from src.models.exceptions import StateTransitionError

logger = logging.getLogger(__name__)

PROJECT_STATES = {
    "待启动": {"收集中", "运行中"},
    "收集中": {"运行中", "待启动"},
    "运行中": {"归档中"},
    "归档中": {"已归档", "运行中"},
    "已归档": set(),
}

GROUP_CREATION_STEPS = [
    "idle",
    "creating_chat",
    "adding_members",
    "deploying_agent",
    "completed",
]

ARCHIVE_STEPS = [
    "idle",
    "sending_notification",
    "archiving_messages",
    "archiving_records",
    "leaving_chat",
    "completed",
]


class StateEngine:
    """项目状态机 - 管理项目生命周期状态流转"""

    def __init__(self, base_client, config):
        self.base_client = base_client
        self.f01_app_token = config['bitable']['f01_app_token']
        self.table_config = config['bitable']['f01_tables']

    def can_transition(self, current, target):
        allowed = PROJECT_STATES.get(current, set())
        return target in allowed

    def transition(self, project_id, target_state, record_id=None, extra_fields=None):
        """状态流转。若传入 record_id 则跳过读取，直接更新"""
        table_id = self.table_config['project_config']

        if record_id:
            fields = {"status": target_state}
            if extra_fields:
                fields.update(extra_fields)
            self.base_client.update_record(
                self.f01_app_token, table_id, record_id, fields
            )
            logger.info(f"Project {project_id}: -> {target_state}")
            return True

        record = self.base_client.find_record_by_field(
            self.f01_app_token, table_id, "project_id", project_id
        )
        if not record:
            raise StateTransitionError("unknown", target_state, f"Project {project_id} not found")

        fields = record.get("fields", {})
        current_state = fields.get("status", "待启动")

        if not self.can_transition(current_state, target_state):
            raise StateTransitionError(current_state, target_state)

        update_fields = {"status": target_state}
        if extra_fields:
            update_fields.update(extra_fields)
        self.base_client.update_record(
            self.f01_app_token, table_id, record["record_id"], update_fields
        )
        logger.info(f"Project {project_id}: {current_state} -> {target_state}")
        return True

    def update_project_chat_id(self, project_id, chat_id, record_id=None):
        """更新群聊ID。若传入 record_id 则跳过读取"""
        table_id = self.table_config['project_config']
        if record_id:
            self.base_client.update_record(
                self.f01_app_token, table_id, record_id,
                {"group_id": chat_id}
            )
            return

        record = self.base_client.find_record_by_field(
            self.f01_app_token, table_id, "project_id", project_id
        )
        if not record:
            raise ValueError(f"Project {project_id} not found")
        self.base_client.update_record(
            self.f01_app_token, table_id, record["record_id"],
            {"group_id": chat_id}
        )

    def get_project_record_id(self, project_id):
        """获取项目记录ID（用于后续批量更新）"""
        table_id = self.table_config['project_config']
        record = self.base_client.find_record_by_field(
            self.f01_app_token, table_id, "project_id", project_id
        )
        return record["record_id"] if record else None
