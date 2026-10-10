"""逾期推送 - P4 每日8点扫描并提醒逾期未提交成员的负责人

飞书任务 v2 API 的 reminders 字段只支持 relative_fire_minute（一次性相对提醒），
不支持「逾期后每日循环提醒」，所以必须由我们的机器人调度器实现：
- 每日 8:00（Asia/Shanghai）扫描所有 collecting_members 状态且
  member_card_sent_at 已逾期 24h 且 selected_members 为空的负责人会话；
- 给每位逾期负责人推送机器人文本提醒；
- 同步给 PM 发一条汇总提醒（同项目合并）。

依赖：
- SessionManager.find_overdue_leads(now_ts, overdue_seconds) 查询逾期会话
- IMClient.send_text_to_user(open_id, text) 给单个用户发文本
- IMClient.get_user_by_open_id(open_id) 获取姓名（可选）
"""
import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)


class OverduePusher:
    """每日逾期未提交成员的负责人推送器"""

    def __init__(self, im_client, session_manager):
        self.im = im_client
        self.sessions = session_manager

    def run_daily_check(self, now_ts=None, overdue_seconds=24 * 3600):
        """扫描逾期会话并推送提醒。每日 8:00 由 scheduler 调用。

        Args:
            now_ts: 当前时间戳，None 时使用 time.time()
            overdue_seconds: 逾期阈值秒数，默认 24*3600
        """
        now_ts = now_ts if now_ts is not None else time.time()
        overdue_leads = self.sessions.find_overdue_leads(
            now_ts=now_ts, overdue_seconds=overdue_seconds)
        if not overdue_leads:
            logger.info("P4 每日扫描：无逾期未提交的负责人")
            return 0

        logger.info(
            f"P4 每日扫描：发现 {len(overdue_leads)} 位逾期未提交的负责人")

        pushed = 0
        # 按 PM 聚合，用于给 PM 发汇总
        # {pm_user_id: [(session, lead_name, hours_overdue)]}
        pm_summary = defaultdict(list)

        for session in overdue_leads:
            lead_open_id = session.user_id
            lead_name = session.lead_name or lead_open_id
            project_name = session.project_name or "未命名项目"
            project_abbr = session.project_abbr or ""
            pm_user_id = session.pm_user_id or ""

            # 计算逾期小时数（向上取整，至少 1）
            hours_overdue = int(
                (now_ts - session.member_card_sent_at) // 3600) or 1

            # 给负责人发提醒
            text = self._lead_remind_text(
                project_name, project_abbr, hours_overdue)
            try:
                self.im.send_text_to_user(lead_open_id, text)
                pushed += 1
                logger.info(
                    f"P4 已推送逾期提醒给负责人 {lead_name}({lead_open_id})，"
                    f"项目={project_name}，逾期{hours_overdue}h")
            except Exception as e:
                logger.error(
                    f"P4 推送逾期提醒给 {lead_name}({lead_open_id}) 失败: {e}",
                    exc_info=True)

            pm_summary[pm_user_id].append(
                (session, lead_name, hours_overdue))

        # 给每位 PM 发汇总提醒
        for pm_user_id, items in pm_summary.items():
            if not pm_user_id:
                continue
            try:
                summary_text = self._pm_summary_text(items)
                self.im.send_text_to_user(pm_user_id, summary_text)
                logger.info(
                    f"P4 已推送逾期汇总给 PM({pm_user_id})，"
                    f"涉及 {len(items)} 位负责人")
            except Exception as e:
                logger.error(
                    f"P4 推送逾期汇总给 PM({pm_user_id}) 失败: {e}",
                    exc_info=True)

        return pushed

    def _lead_remind_text(self, project_name, project_abbr, hours_overdue):
        """给负责人的提醒文案"""
        abbr_part = f"（{project_abbr}）" if project_abbr else ""
        return (
            f"⏰ 提醒：项目「{project_name}{abbr_part}」的成员收集已逾期 "
            f"{hours_overdue} 小时。\n"
            f"请尽快在「成员收集」卡片中提交本部门成员名单。\n"
            f"如已提交请忽略此提醒。"
        )

    def _pm_summary_text(self, items):
        """给 PM 的汇总文案

        Args:
            items: list of (session, lead_name, hours_overdue)
        """
        lines = [f"⏰ 逾期未提交成员汇总（共 {len(items)} 位负责人）："]
        for _session, lead_name, hours_overdue in items:
            project_name = _session.project_name or "未命名项目"
            lines.append(
                f"• {lead_name} - {project_name}（逾期 {hours_overdue}h）")
        lines.append("可私聊我发送 /建群 查看进度，或联系负责人催办。")
        return "\n".join(lines)
