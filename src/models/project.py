from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


def _extract_text(value):
    """从多维表格字段值中提取纯文本（处理富文本数组格式）"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in value)
    return str(value) if value else ""


@dataclass
class Project:
    project_id: str
    project_name: str
    project_short_name: str
    pm_open_id: str
    status: str = "待启动"
    chat_id: str = ""
    group_name: str = ""
    description: str = ""
    created_at: str = ""
    updated_at: str = ""
    record_id: str = ""  # 多维表格记录ID（内存传递用，不写入表格字段）

    def to_bitable_fields(self):
        fields = {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "project_abbr": self.project_short_name,
            "status": self.status,
        }
        if self.pm_open_id:
            fields["PM"] = [{"id": self.pm_open_id}]
        if self.chat_id:
            fields["group_id"] = self.chat_id
        if self.group_name:
            fields["group_name"] = self.group_name
        if self.description:
            fields["项目描述"] = self.description
        return fields

    @classmethod
    def from_bitable_record(cls, record):
        f = record.get("fields", {})
        pm_list = f.get("PM", []) or []
        pm_open_id = pm_list[0].get("id", "") if pm_list and isinstance(pm_list, list) else ""
        return cls(
            project_id=_extract_text(f.get("project_id", "")),
            project_name=_extract_text(f.get("project_name", "")),
            project_short_name=_extract_text(f.get("project_abbr", "")),
            pm_open_id=pm_open_id,
            status=_extract_text(f.get("status", "待启动")),
            chat_id=_extract_text(f.get("group_id", "")),
            group_name=_extract_text(f.get("group_name", "")),
            description=_extract_text(f.get("项目描述", "")),
            created_at=str(f.get("created_at", "")),
            updated_at=str(f.get("updated_at", "")),
        )


@dataclass
class Member:
    open_id: str
    name: str = ""
    department: str = ""
    department_lead: str = ""  # 部门负责人 open_id
    department_lead_name: str = ""
    role: str = "成员"
    join_status: str = "未入群"
    collected: bool = False
    added_time: int = 0  # 毫秒时间戳
    remark: str = ""
    project_id: str = ""
    chat_id: str = ""

    def to_bitable_fields(self):
        fields = {
            "项目ID": self.project_id,
            "群聊ID": self.chat_id,
            "部门": self.department,
            "成员姓名": self.name,
            "open_id": self.open_id,
            "角色": self.role,
            "入群状态": self.join_status,
            "入群方式": "收集流程" if self.collected else "手动拉入",
            "添加时间": self.added_time,
            "备注": self.remark,
        }
        # 部门负责人是 User 类型，需传 [{"id": open_id}]
        if self.department_lead:
            fields["部门负责人"] = [{"id": self.department_lead}]
        return fields


@dataclass
class ArchiveLog:
    project_id: str
    chat_id: str
    archive_reason: str = "群解散归档"
    group_name_before: str = ""
    group_name_after: str = ""
    archived_docs: str = ""
    kb_link: str = ""
    agent_exit_status: str = "进行中"
    error_message: str = ""
    operator: str = ""

    def to_bitable_fields(self):
        import time as _time
        fields = {
            "project_id": self.project_id,
            "archive_reason": self.archive_reason,
            "agent_exit_status": self.agent_exit_status,
            "error_message": self.error_message,
            "archived_at": int(_time.time() * 1000),
        }
        if self.group_name_before:
            fields["group_name_before"] = self.group_name_before
        if self.group_name_after:
            fields["group_name_after"] = self.group_name_after
        if self.archived_docs:
            fields["archived_docs"] = self.archived_docs
        if self.kb_link:
            fields["kb_link"] = {"text": "知识库链接", "link": self.kb_link}
        if self.operator:
            fields["操作人"] = [{"id": self.operator}]
        return fields
