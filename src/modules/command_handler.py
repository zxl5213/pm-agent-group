import logging

from src.feishu.card_builder import CardBuilder
from src.feishu.im_client import IMClient
from src.modules.session_manager import SessionManager

logger = logging.getLogger(__name__)


class CommandHandler:
    """指令处理器 - 解析文本指令和自由文本输入，按会话状态路由"""

    def __init__(self, im_client: IMClient, session_manager: SessionManager, group_manager=None):
        self.im = im_client
        self.card_builder = CardBuilder()
        self.sessions = session_manager
        self.group_manager = group_manager

    def handle_message(self, chat_id, user_id, text,
                        chat_type="p2p", is_mentioned=False):
        """处理收到的文本消息

        chat_type: p2p 与机器人私聊（全部功能）；group 群聊（管理操作受限）。
        is_mentioned: 群聊中是否 @ 了机器人（决定静默还是引导）。
        """
        text = text.strip()
        if not text:
            return

        if chat_type == "group":
            return self._handle_group_message(chat_id, text, is_mentioned)

        session = self.sessions.get_or_create(user_id, chat_id)

        if text.startswith("/"):
            return self._handle_command(chat_id, user_id, text)

        if session.state == "waiting_project_info":
            return self._handle_project_info_input(chat_id, user_id, text)
        elif session.state == "waiting_quick_create":
            return self._handle_quick_create_input(chat_id, user_id, text)
        elif session.state == "waiting_dept_leads":
            return self._handle_dept_leads_input(chat_id, user_id, text)
        elif session.state == "waiting_member_reply":
            return self._handle_member_reply(chat_id, user_id, text)
        else:
            return self._handle_idle_message(chat_id, user_id, text)

    def _handle_group_message(self, chat_id, text, is_mentioned):
        """群聊消息：不执行任何管理操作、不改状态。

        - /帮助：群里开放，任何人可查；
        - 未 @机器人：静默（避免刷屏）；
        - @机器人 + 管理意图（建群/解散等）：引导私聊；
        - @机器人 + 其他内容：简短说明。
        """
        cmd = text.split(maxsplit=1)[0].lower()
        if cmd in ("/帮助", "/help", "/?"):
            self.im.send_card(chat_id, self.card_builder.help_card())
            return

        if not is_mentioned:
            return

        if (text.startswith("/")
                or any(k in text for k in ("建群", "创建", "解散", "关闭", "归档"))):
            self.im.send_text(
                chat_id,
                "建群、解散等项目管理操作请私聊我发起～（点击我的头像即可单聊）")
        else:
            self.im.send_text(
                chat_id,
                "我在群里仅提供说明（输入 /帮助 查看）；项目管理操作请私聊我。")

    def _handle_command(self, chat_id, user_id, text):
        """处理斜杠指令 - 单入口 /建群"""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()

        if cmd in ("/建群", "/create", "/创建项目群", "/快速建群", "/quick", "/快速创建"):
            # 状态保护：进行中的业务流程（解散/归档/成员收集）不被 /建群 中断
            # 依据：实测发现解散确认等待期间发 /建群 会覆盖状态导致流程中断、建群卡误弹
            session = self.sessions.get_or_create(user_id, chat_id)
            busy_states = ("waiting_disband_confirm", "collecting_members", "归档中",
                            "waiting_member_reply")
            if session.state in busy_states:
                self.im.send_text(
                    chat_id,
                    "当前有未完成的项目流程（解散/归档/成员收集），请先完成或取消后再发起 /建群。")
                return
            self.sessions.update_state(user_id, chat_id, "waiting_project_info")
            self.im.send_cardkit(chat_id, self.card_builder.project_info_form_card())

        elif cmd in ("/解散", "/disband", "/关闭", "/归档"):
            session = self.sessions.get_or_create(user_id, chat_id)
            if session.state == "waiting_disband_confirm" and session.project_id:
                # 已在确认流程中，重新显示确认卡而非新开流程
                project = self.group_manager._get_project(session.project_id) if self.group_manager else None
                if project:
                    member_count = 0
                    if project.chat_id:
                        try:
                            members = self.group_manager.chat_client.get_all_members(project.chat_id)
                            member_count = len(members)
                        except Exception:
                            pass
                    self.im.send_card(chat_id, self.card_builder.disband_confirm_card(
                        project_name=project.project_name,
                        group_name=project.group_name or project.project_short_name,
                        member_count=member_count,
                    ))
                    return
            # 新开始解散流程
            self.sessions.update_state(user_id, chat_id, "idle")
            projects = []
            if self.group_manager:
                projects = self.group_manager.list_active_projects(pm_open_id=user_id)
            self.im.send_card(chat_id, self.card_builder.disband_project_select_card(projects))

        elif cmd in ("/帮助", "/help", "/?"):
            self.im.send_card(chat_id, self.card_builder.help_card())

        elif cmd in ("/取消", "/cancel"):
            self.sessions.clear(user_id, chat_id)
            self.im.send_text(chat_id, "已取消当前操作。")

        else:
            self.im.send_text(chat_id, f"未知指令：{cmd}\n输入 /帮助 查看可用指令。")

    def _handle_project_info_input(self, chat_id, user_id, text):
        """处理项目信息文本输入（回退方案，卡片表单优先）"""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) < 1:
            self.im.send_text(chat_id, "请输入项目名称（第一行）和项目简称（第二行）。")
            return

        project_name = lines[0]
        project_abbr = lines[1] if len(lines) >= 2 else project_name[:10]

        session = self.sessions.update_state(
            user_id, chat_id, "waiting_dept_leads",
            project_name=project_name,
            project_abbr=project_abbr,
        )
        scope_users = self.im.list_scope_users()
        resp = self.im.send_cardkit(chat_id, self.card_builder.dept_lead_collect_card(
            project_name=project_name,
            project_abbr=project_abbr,
            scope_users=scope_users,
        ))
        session.update(card_id=resp.get("card_id", "") if resp else "", card_seq=0)

    def _handle_quick_create_input(self, chat_id, user_id, text):
        """处理快速建群输入（项目名称+简称+成员列表）"""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) < 2:
            self.im.send_text(chat_id, "请按格式输入：\n第一行：项目名称\n第二行：项目简称\n第三行：成员姓名（逗号分隔）")
            return

        project_name = lines[0]
        project_abbr = lines[1]
        member_names = []
        if len(lines) >= 3:
            member_names = [n.strip() for n in lines[2].split(",") if n.strip()]

        self.sessions.update_state(
            user_id, chat_id, "quick_creating",
            project_name=project_name,
            project_abbr=project_abbr,
        )

        self.im.send_text(chat_id, f"正在创建项目群「{project_abbr}」...")

        if self.group_manager:
            try:
                project, result = self.group_manager.quick_register_and_create_group(
                    project_name=project_name,
                    project_short_name=project_abbr,
                    pm_open_id=user_id,
                )
                chat_id_new = result.get("chat_id", "")
                self.im.send_card(chat_id, self.card_builder.success_card(
                    project_name=project_name,
                    group_name=f"{project_abbr}项目管理Agent",
                    member_count=1,
                    chat_id=chat_id_new,
                ))
                self.sessions.clear(user_id, chat_id)
            except Exception as e:
                logger.error(f"快速建群失败: {e}", exc_info=True)
                self.im.send_card(chat_id, self.card_builder.error_card(
                    error_type="建群失败",
                    message=str(e),
                    suggestion="请检查项目信息是否正确，或联系管理员。",
                ))
                self.sessions.clear(user_id, chat_id)

    def _handle_dept_leads_input(self, chat_id, user_id, text):
        """等待选择部门负责人时收到普通文本：不作为负责人处理。

        选负责人唯一入口是卡片上的 select_static 下拉；早期「每行一个姓名」的
        文本方案已废弃。误发文本（如打字落到聊天框）不得覆盖卡片上已选人员、
        不得跳转状态，仅提示用户回到卡片操作。
        """
        self.im.send_text(
            chat_id,
            "请在上方「部门负责人收集」卡片的搜索框中选择负责人；"
            "如需放弃本次建群，请发送 /取消。")

    def _handle_member_reply(self, chat_id, user_id, text):
        """处理部门负责人的成员回复"""
        self.im.send_text(chat_id, "成员收集功能开发中，请使用 /快速建群 直接建群。")

    def _handle_idle_message(self, chat_id, user_id, text):
        """处理空闲状态消息"""
        if any(kw in text for kw in ["建群", "创建", "创建群"]):
            self.sessions.update_state(user_id, chat_id, "waiting_project_info")
            self.im.send_cardkit(chat_id, self.card_builder.project_info_form_card())
        elif any(kw in text for kw in ["解散", "关闭", "归档"]):
            self.im.send_text(chat_id, "请输入 /解散 来启动关闭项目流程。")
        elif any(kw in text for kw in ["帮助", "help", "指令"]):
            self.im.send_card(chat_id, self.card_builder.help_card())
        else:
            self.im.send_text(chat_id, "我收到了你的消息。输入 /帮助 查看可用指令。")
