import json
import logging
import os
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1, P2ImChatAccessEventBotP2pChatEnteredV1,
    P2ImChatMemberUserAddedV1,
)

from src.modules.command_handler import CommandHandler
from src.modules.card_action_handler import CardActionHandler
from src.modules.session_manager import SessionManager

logger = logging.getLogger(__name__)

_WELCOME_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'welcome_sent.json')


class WsClient:
    """飞书WebSocket长连接客户端 - 接收事件和卡片回调"""

    def __init__(self, app_id, app_secret, command_handler: CommandHandler,
                 card_action_handler: CardActionHandler, im_client=None,
                 session_manager: SessionManager = None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.command_handler = command_handler
        self.card_action_handler = card_action_handler
        self.im_client = im_client
        self.session_manager = session_manager
        self._client = None

    def _build_event_handler(self):
        """构建事件处理器"""
        dispatcher = lark.EventDispatcherHandler.builder("", "")

        dispatcher = dispatcher.register_p2_im_message_receive_v1(
            self._on_message_receive
        )

        dispatcher = dispatcher.register_p2_card_action_trigger(
            self._on_card_action
        )

        # 用户从工作台点击应用图标进入单聊时触发
        dispatcher = dispatcher.register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(
            self._on_p2p_chat_entered
        )

        # 群成员入群事件（含手动拉人）→ 同步多维表格
        dispatcher = dispatcher.register_p2_im_chat_member_user_added_v1(
            self._on_chat_member_user_added
        )

        return dispatcher.build()

    def _on_p2p_chat_entered(self, data: P2ImChatAccessEventBotP2pChatEnteredV1) -> None:
        """用户从工作台点击图标进入单聊：自动发送欢迎消息 + 能力卡"""
        try:
            event = data.event
            available_attrs = [a for a in dir(event) if not a.startswith("_")]
            logger.info(f"收到单聊进入事件，可用字段: {available_attrs}")

            chat_id = ""
            user_id = ""
            # 按常见命名路径提取 chat_id / user_id（不同SDK版本字段名略有差异，做兼容性兜底）
            ctx = getattr(event, "context", None)
            if ctx is not None:
                chat_id = getattr(ctx, "open_chat_id", "") or getattr(ctx, "chat_id", "") or ""
            if not chat_id:
                chat_id = getattr(event, "open_chat_id", "") or getattr(event, "chat_id", "") or ""

            operator = getattr(event, "operator", None) or getattr(event, "user", None)
            if operator is not None:
                user_id = getattr(operator, "open_id", "") or getattr(operator, "user_id", "") or ""
            if not user_id:
                user_id = getattr(event, "open_id", "") or getattr(event, "user_id", "") or ""

            logger.info(f"单聊进入: chat_id={chat_id}, user_id={user_id}")

            if not chat_id or not self.im_client:
                logger.warning(f"无法发送欢迎消息: chat_id={chat_id}, im_client={self.im_client is not None}")
                return

            # 首次进入才发欢迎卡，持久化到文件避免重启后重复
            if self._is_welcome_sent(chat_id):
                logger.info(f"欢迎消息已发过，跳过: chat_id={chat_id}")
                return

            # 先发送纯文本问候（与原型顺序一致：短信 then 卡片）
            greet = "你好！我是项目管理Agent，我会帮你管理项目群、跟踪成员进度并自动归档。"
            self.im_client.send_text(chat_id, greet)

            # 再发送能力卡
            card_builder = self.card_action_handler.card_builder
            self.im_client.send_card(chat_id, card_builder.welcome_card())

            # 标记该会话已发送欢迎消息
            self._mark_welcome_sent(chat_id)

            logger.info(f"欢迎消息已发送至: chat_id={chat_id}")
        except Exception as e:
            logger.error(f"处理单聊进入事件失败: {e}", exc_info=True)

    @staticmethod
    def _is_welcome_sent(chat_id):
        """检查是否已发过欢迎消息（持久化文件）"""
        try:
            if os.path.exists(_WELCOME_FILE):
                with open(_WELCOME_FILE, 'r', encoding='utf-8') as f:
                    sent = json.load(f)
                return chat_id in sent
        except Exception:
            pass
        return False

    @staticmethod
    def _mark_welcome_sent(chat_id):
        """标记已发欢迎消息"""
        try:
            os.makedirs(os.path.dirname(_WELCOME_FILE), exist_ok=True)
            sent = {}
            if os.path.exists(_WELCOME_FILE):
                with open(_WELCOME_FILE, 'r', encoding='utf-8') as f:
                    sent = json.load(f)
            sent[chat_id] = True
            with open(_WELCOME_FILE, 'w', encoding='utf-8') as f:
                json.dump(sent, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"写入welcome_sent文件失败: {e}")

    def _on_message_receive(self, data: P2ImMessageReceiveV1) -> None:
        """处理消息接收事件"""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            chat_id = message.chat_id
            user_id = sender.sender_id.open_id
            content_str = message.content

            try:
                content = json.loads(content_str)
                text = content.get("text", "")
            except (json.JSONDecodeError, TypeError):
                text = ""

            if not text:
                return

            msg_type = message.message_type
            if msg_type != "text":
                return

            mentions = message.mentions or []
            is_mentioned = len(mentions) > 0

            if is_mentioned:
                text = " ".join(
                    part for part in text.split("@_user") if part.strip()
                ).strip()

            chat_type = getattr(message, "chat_type", "") or "p2p"
            logger.info(f"收到消息: chat_id={chat_id}, chat_type={chat_type}, user_id={user_id}, text={text[:50]}")

            # 记录会话类型（私聊/群聊），供指令门禁与卡片回调拦截使用
            if self.session_manager:
                self.session_manager.get_or_create(user_id, chat_id).update(
                    chat_kind=chat_type)

            # 会话处于idle状态时，自动补发欢迎消息+能力卡（覆盖旧会话重新进入场景）
            # 群聊中仅当 @机器人 时允许，未@一律静默
            allow_welcome = chat_type != "group" or is_mentioned
            if allow_welcome and self._should_send_welcome(user_id, chat_id, text):
                self._send_welcome(chat_id, user_id)
                return

            self.command_handler.handle_message(
                chat_id, user_id, text,
                chat_type=chat_type, is_mentioned=is_mentioned)

        except Exception as e:
            logger.error(f"处理消息失败: {e}", exc_info=True)

    def _should_send_welcome(self, user_id, chat_id, text):
        """判断是否应该发送欢迎消息：新会话/idle状态 + 未发过欢迎 + 是普通问候"""
        if not self.session_manager:
            return False

        session = self.session_manager.get(user_id, chat_id)
        # 已发过欢迎：跳过（session 不存在则视为新会话，继续判断）
        if session and session.welcome_sent:
            return False

        # 非 idle 状态跳过（session 不存在时视为 idle）
        if session and session.state != "idle":
            return False

        cmd_text = text.strip().lower()
        skip_prefixes = ("/start", "/help", "/hi", "/你好")
        if any(cmd_text.startswith(p) for p in skip_prefixes):
            return False

        greetings = ("你好", "hi", "hello", "在吗", "在不在", "您好")
        if any(g in cmd_text for g in greetings) or not cmd_text:
            logger.info(f"会话idle状态，用户发起问候，补发欢迎: user={user_id}, chat={chat_id}")
            return True
        return False

    def _send_welcome(self, chat_id, user_id=None):
        """发送欢迎文本 + 能力卡，并标记welcome_sent"""
        if not self.im_client:
            return
        try:
            greet = "你好！我是项目管理Agent，我会帮你管理项目群、跟踪成员进度并自动归档。"
            self.im_client.send_text(chat_id, greet)
            card_builder = self.card_action_handler.card_builder
            self.im_client.send_card(chat_id, card_builder.welcome_card())

            # 标记该会话已发送欢迎消息
            if user_id and self.session_manager:
                self.session_manager.update_state(user_id, chat_id, "idle", welcome_sent=True)

            logger.info(f"欢迎消息已发送至: chat_id={chat_id}")
        except Exception as e:
            logger.error(f"发送欢迎消息失败: {e}", exc_info=True)

    def _on_card_action(self, data):
        """处理卡片按钮回调"""
        try:
            event = data.event
            action = event.action
            value = action.value
            operator = event.operator
            context = event.context

            chat_id = context.open_chat_id if context else ""
            user_id = operator.open_id if operator else ""
            message_id = getattr(action, "open_message_id", "") or getattr(context, "open_message_id", "")

            if not value:
                return {"toast": {"type": "info", "content": "操作无效"}}

            form_value = {}
            if hasattr(action, "form_value") and action.form_value:
                form_value = action.form_value
            elif hasattr(data, "raw") and data.raw:
                try:
                    raw = json.loads(data.raw) if isinstance(data.raw, str) else data.raw
                    action_raw = raw.get("event", {}).get("action", {})
                    form_value = action_raw.get("form_value", {})
                    # form外单选下拉：选中open_id在 option 字段，统一用固定key，
                    # 不依赖组件name（cardkit v2回调中name可能为空）
                    if not form_value and action_raw.get("option"):
                        form_value = {"selected": action_raw["option"]}
                    # form外多选下拉：选中列表在 options 字段
                    if not form_value and action_raw.get("options"):
                        form_value = {"selected_multi": action_raw["options"]}
                    # form外输入框（如有）：提交文本在 input_value
                    if not form_value and action_raw.get("input_value") is not None:
                        form_value = {"input_value": action_raw["input_value"]}
                except Exception:
                    pass

            # 兜底：直接从SDK action对象取 option/options/input_value（form外组件回调）
            if not form_value:
                option = getattr(action, "option", None)
                if option:
                    form_value = {"selected": option}
                else:
                    options = getattr(action, "options", None)
                    if options:
                        form_value = {"selected_multi": options}
                    else:
                        input_value = getattr(action, "input_value", None)
                        if input_value is not None:
                            form_value = {"input_value": input_value}

            logger.info(f"卡片回调: chat_id={chat_id}, user_id={user_id}, value={value}, form_value={form_value}, message_id={message_id}")

            # 异步处理卡片动作，避免飞书回调超时（飞书要求3秒内响应）
            threading.Thread(
                target=self.card_action_handler.handle_action,
                args=(chat_id, user_id, value, form_value, message_id),
                daemon=True,
            ).start()

            # 立即返回 toast 响应，避免飞书报"回调超时"
            return {"toast": {"type": "info", "content": "处理中..."}}

        except Exception as e:
            logger.error(f"处理卡片回调失败: {e}", exc_info=True)
            return {"toast": {"type": "error", "content": "处理失败"}}

    def _on_chat_member_user_added(self, data: P2ImChatMemberUserAddedV1) -> None:
        """群成员入群事件 → 异步同步多维表格项目成员表"""
        try:
            event = data.event
            chat_id = getattr(event, "chat_id", "") or ""

            # operator_id 是 UserId 对象
            operator_id = ""
            op = getattr(event, "operator_id", None)
            if op is not None:
                operator_id = getattr(op, "open_id", "") or ""

            # users 是 ChatMemberUser 列表
            users = getattr(event, "users", None) or []
            logger.info(f"群成员入群事件: chat_id={chat_id}, users_count={len(users)}, operator={operator_id}")

            if not chat_id or not users:
                logger.warning("群成员入群事件缺少 chat_id 或 users，跳过")
                return

            for user in users:
                member_id = ""
                user_id_obj = getattr(user, "user_id", None)
                if user_id_obj is not None:
                    member_id = getattr(user_id_obj, "open_id", "") or ""
                if not member_id:
                    continue

                # 异步处理，避免阻塞事件分发
                threading.Thread(
                    target=self._sync_member_to_bitable,
                    args=(chat_id, member_id, operator_id),
                    daemon=True,
                ).start()

        except Exception as e:
            logger.error(f"处理群成员入群事件失败: {e}", exc_info=True)

    def _sync_member_to_bitable(self, chat_id, member_open_id, operator_open_id):
        """通过群聊ID反查项目，将成员写入多维表格"""
        try:
            if not self.session_manager:
                logger.warning("session_manager 未注入，跳过成员同步")
                return

            # 通过 group_chat_id 反查 PM 会话
            pm_session = None
            for s in self.session_manager._sessions.values():
                if s.group_chat_id == chat_id:
                    pm_session = s
                    break

            project_id = ""
            if pm_session and pm_session.project_id:
                project_id = pm_session.project_id
            else:
                # Fallback: 从 project_config 表按 group_id 反查
                logger.info(f"内存会话未命中，从多维表格反查 group_id={chat_id}")
                if self.card_action_handler and self.card_action_handler.group_manager:
                    gm = self.card_action_handler.group_manager
                    cfg_table = gm.f01_tables['project_config']
                    condition = {
                        "conjunction": "and",
                        "conditions": [
                            {"field_name": "group_id", "operator": "is", "value": [chat_id]},
                        ],
                    }
                    records = gm.base_client.list_all_records(
                        gm.f01_app_token, cfg_table, filter_condition=condition)
                    if records:
                        from src.models.project import _extract_text
                        project_id = _extract_text(
                            records[0].get("fields", {}).get("project_id", ""))
                        logger.info(f"反查到 project_id={project_id}")
                    else:
                        logger.warning(f"多维表格中未找到 group_id={chat_id} 对应的项目，跳过")
                        return
                else:
                    logger.warning("group_manager 未注入，跳过")
                    return

            # 获取成员信息
            if not self.im_client:
                return
            info = self.im_client.get_user_by_open_id(member_open_id)

            from src.models.project import Member
            import time as _time
            member = Member(
                open_id=member_open_id,
                name=info.get("name", "未知"),
                department=info.get("department", "") or "未知部门",
                role="成员",
                join_status="已入群",
                collected=False,
                added_time=int(_time.time() * 1000),
                project_id=project_id,
                chat_id=chat_id,
            )

            # 委托 group_manager 写入（含去重）
            if self.card_action_handler and self.card_action_handler.group_manager:
                gm = self.card_action_handler.group_manager
                table_id = gm.f01_tables['project_members']

                # 去重检查
                condition = {
                    "conjunction": "and",
                    "conditions": [
                        {"field_name": "项目ID", "operator": "is", "value": [project_id]},
                        {"field_name": "open_id", "operator": "is", "value": [member_open_id]},
                    ],
                }
                existing = gm.base_client.list_all_records(
                    gm.f01_app_token, table_id, filter_condition=condition)
                if existing:
                    logger.info(f"成员已存在于项目成员表，跳过: {member_open_id}")
                    return

                gm.base_client.create_record(
                    gm.f01_app_token, table_id, member.to_bitable_fields())
                logger.info(f"手动入群成员已写入多维表格: project_id={project_id}, member={member_open_id}")

        except Exception as e:
            logger.error(f"同步手动入群成员到多维表格失败: {e}", exc_info=True)

    def start(self):
        """启动WebSocket长连接"""
        event_handler = self._build_event_handler()

        self._client = lark.ws.Client(
            app_id=self.app_id,
            app_secret=self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )

        logger.info("启动飞书WebSocket长连接客户端...")
        self._client.start()

    def stop(self):
        """停止客户端"""
        if self._client:
            logger.info("停止飞书WebSocket长连接客户端...")
