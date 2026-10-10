import logging
import threading
import time
import hashlib
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.feishu.card_builder import CardBuilder
from src.feishu.im_client import IMClient
from src.modules.session_manager import SessionManager
from src.models.exceptions import DuplicateProjectError

logger = logging.getLogger(__name__)


def _str(val):
    """安全提取 form_value 中的字符串值（兼容 str/dict/None）"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        # 某些组件可能返回 {"text": "xxx"} 格式
        return val.get("text", "") or str(val)
    return str(val)


class CardActionHandler:
    """卡片动作处理器 - 处理飞书交互卡片按钮回调和表单提交"""

    def __init__(self, im_client: IMClient, session_manager: SessionManager, group_manager=None, scheduler=None):
        self.im = im_client
        self.card_builder = CardBuilder()
        self.sessions = session_manager
        self.group_manager = group_manager
        self.scheduler = scheduler
        # 每张卡片一把锁：快速连续输入时串行化「读状态→匹配→局部更新」，
        # 防止并发回调造成旧候选残留/重复插入
        self._card_locks = {}
        self._locks_guard = threading.Lock()

    def _card_lock(self, card_id):
        """按 card_id 获取（懒创建）该卡片的串行锁。"""
        with self._locks_guard:
            lk = self._card_locks.get(card_id)
            if lk is None:
                lk = threading.Lock()
                self._card_locks[card_id] = lk
            return lk

    def handle_action(self, chat_id, user_id, action_value, form_value=None, message_id=""):
        """处理卡片按钮回调"""
        action = action_value.get("action", "")
        form_value = form_value or {}
        logger.info(f"卡片动作: action={action}, form_value={form_value}, message_id={message_id}")

        # 纵深防御：管理类卡片仅允许在与机器人的私聊中操作。
        # 正常入口已锁私聊，此处拦截旧卡/转发卡在群聊中的回调。
        cb_session = self.sessions.get_or_create(user_id, chat_id)
        if cb_session.chat_kind == "group":
            logger.info(f"群聊中忽略管理卡片回调: action={action}")
            try:
                self.im.send_text(chat_id, "项目管理操作请私聊我进行～")
            except Exception:
                pass
            return

        handler_map = {
            "start_create": self._on_start_create,
            "start_quick_create": self._on_start_create,
            "start_archive": self._on_start_archive,
            "confirm_project_info": self._on_confirm_project_info,
            "quick_create_from_info": self._on_quick_create_from_info,
            "pick_lead": self._on_pick_lead,
            "remove_lead": self._on_remove_lead,
            "pick_member": self._on_pick_member,
            "remove_member": self._on_remove_member,
            "send_dept_leads": self._on_send_dept_leads,
            "skip_collection": self._on_skip_collection,
            "lead_submit_members": self._on_lead_submit_members,
            "approve_members": self._on_approve_members,
            "reject_members": self._on_reject_members,
            "confirm_disband": self._on_confirm_disband,
            "select_disband_project": self._on_select_disband_project,
            "cancel": self._on_cancel,
            "retry": self._on_retry,
            "remind_pending": self._on_remind_pending,
            "decline_collection": self._on_decline_collection,
        }

        handler = handler_map.get(action)
        if handler:
            handler(chat_id, user_id, action_value, form_value, message_id)
        else:
            logger.warning(f"未知卡片动作: {action}")

    def _query_infos(self, open_ids):
        """批量查询用户信息，组装花名册所需结构
        [{"name", "dept", "job_title", "open_id"}]
        """
        infos = []
        for open_id in open_ids:
            info = self.im.get_user_by_open_id(open_id)
            infos.append({
                "name": info.get("name", "未知"),
                "dept": info.get("department", "") or "未知部门",
                "job_title": info.get("job_title", ""),
                "open_id": open_id,
            })
        return infos

    def _remaining_candidates(self, selected_ids):
        """可见范围用户中剔除已选人员后的候选列表（完整信息，供展示部门职务）"""
        try:
            all_users = self.im.list_scope_users()
            selected = set(selected_ids or [])
            return [u for u in all_users if u.get("open_id") not in selected]
        except Exception as e:
            logger.warning(f"加载候选用户失败: {e}")
            return []

    # ---------- cardkit 局部更新辅助 ----------

    def _next_seq(self, session):
        """取本卡片下一个 batch_update 序号（严格递增）"""
        session.card_seq += 1
        return session.card_seq

    def _bind_card(self, session, message_id):
        """按被交互的消息精确定位 cardkit 卡片。

        解决同一用户同时是PM又是负责人时会话重叠、card_id指错卡的问题。
        Returns: True 若当前会话已绑定有效 card_id
        """
        cid = self.im.resolve_card_id(message_id) if message_id else ""
        if cid:
            if cid != session.card_id:
                session.update(card_id=cid)
            return True
        return bool(session.card_id)

    def _act_add_before(self, hr_id, elements):
        return {
            "action": "add_elements",
            "params": {
                "type": "insert_before",
                "target_element_id": hr_id,
                "elements": elements,
            },
        }

    def _act_delete(self, element_ids):
        return {
            "action": "delete_elements",
            "params": {"element_ids": element_ids},
        }

    def _lock_info_card(self, message_id, project_name, project_abbr, status):
        """信息卡提交成功后锁定：先在顶部插入只读摘要，再删除介绍/表单/取消按钮。

        使用已验证的 insert_before + delete，不依赖 form 内部组件的禁用能力。
        """
        if not message_id:
            return
        card_id = self.im.resolve_card_id(message_id)
        if not card_id:
            logger.warning(f"信息卡无法定位 card_id: message_id={message_id}")
            return
        group_name = f"{project_abbr}项目管理Agent"
        elements = self.card_builder.project_info_locked_elements(
            project_name, project_abbr, group_name, status)
        actions = [
            self._act_add_before("info_intro", elements),
            self._act_delete(["info_intro", "info_form", "info_cancel"]),
        ]
        self.im.batch_update_card(card_id, actions)

    def _lock_lead_collect_card(self, message_id, project_name, project_abbr,
                                lead_infos, status):
        """负责人收集卡发送成功后锁定：插入只读摘要，删除所有可交互组件。

        与 _lock_info_card 同一模式：先在 lead_info 前插入只读摘要，
        再删除项目信息、花名册、搜索区、按钮等全部交互组件。
        """
        if not message_id:
            return
        card_id = self.im.resolve_card_id(message_id)
        if not card_id:
            logger.warning(f"负责人收集卡无法定位 card_id: message_id={message_id}")
            return
        elements = self.card_builder.dept_lead_collect_locked_elements(
            project_name, project_abbr, lead_infos, status)
        # 收集所有需删除的交互组件 element_id（含花名册行动态 id）
        delete_ids = [
            "lead_info", "lead_info_hr", "lead_count",
            "lead_hr", "lead_search_title", "lead_picker", "lead_pick_tail",
            "lead_send_btn", "lead_skip_cancel",
        ]
        # lead_empty 仅在花名册为空时存在；非空时删除会触发 300314 错误
        if not lead_infos:
            delete_ids.append("lead_empty")
        for info in lead_infos:
            delete_ids.append(self.card_builder._row_id(info.get("open_id", "")))
        actions = [
            self._act_add_before("lead_info", elements),
            self._act_delete(delete_ids),
        ]
        self.im.batch_update_card(card_id, actions)

    def _freeze_or_overdue_member_card(self, lead_open_id):
        """24h到期：检查成员提交情况，≥1次提交→冻结，0次→标记逾期（不冻结）。

        由 scheduler 在 member_freeze:{lead_open_id} 定时器到期时回调。
        """
        session = self.sessions.get(lead_open_id, lead_open_id)
        if not session:
            logger.warning(f"成员卡冻结：未找到会话 lead_open_id={lead_open_id}")
            return
        card_id = session.card_id
        if not card_id:
            logger.warning(f"成员卡冻结：card_id 为空 lead_open_id={lead_open_id}")
            return

        selected = list(session.selected_members or [])
        if selected:
            # ≥1 次提交 → 冻结：插入只读摘要，删除所有交互组件
            member_infos = []
            for oid in selected:
                info = self.im.get_user_by_open_id(oid)
                member_infos.append({
                    "name": info.get("name", "未知"),
                    "dept": info.get("department", "") or "未知部门",
                    "job_title": info.get("job_title", ""),
                    "open_id": oid,
                })
            locked = self.card_builder.member_collect_locked_elements(
                session.project_name, session.project_abbr, member_infos)
            # mem_empty 仅在花名册为空时存在；非空时删除会触发 300314 错误
            delete_ids = [
                "mem_intro", "mem_info", "mem_deadline", "mem_info_hr",
                "mem_count", "mem_hr", "mem_search_title",
                "mem_picker", "mem_pick_tail", "mem_submit_btn", "mem_skip_cancel",
            ]
            for oid in selected:
                delete_ids.append(self.card_builder._row_id(oid))
            actions = [
                self._act_add_before("mem_intro", locked),
                self._act_delete(delete_ids),
            ]
            self.im.batch_update_card(card_id, actions)
            logger.info(f"成员收集卡已冻结: lead={lead_open_id}, 提交{len(selected)}人")
        else:
            # 0 次提交 → 标记逾期（不冻结，卡片保持可交互）
            actions = [
                self._act_patch("mem_deadline", {
                    "text": {"tag": "lark_md",
                        "content": self.card_builder.member_collect_overdue_text()}
                }),
            ]
            self.im.batch_update_card(card_id, actions)
            logger.info(f"成员收集卡标记逾期: lead={lead_open_id}")

    def _act_patch(self, element_id, partial_element):
        return {
            "action": "partial_update_element",
            "params": {
                "element_id": element_id,
                "partial_element": partial_element,
            },
        }

    def _act_update(self, element_id, element):
        """update_element：用新元素整体替换指定 element_id 的旧元素。"""
        return {
            "action": "update_element",
            "params": {
                "element_id": element_id,
                "element": element,
            },
        }

    def _empty_hint(self, empty_id, hint_pos="above"):
        """空花名册提示。hint_pos: 搜索区相对花名册的方位 above/below。"""
        pos_word = "上方" if hint_pos == "above" else "下方"
        return {
            "tag": "div",
            "element_id": empty_id,
            "text": {"tag": "lark_md",
                     "content": f'<font color="grey">暂未选择，请在{pos_word}搜索姓名添加</font>'},
        }

    def _act_recreate_picker(self, picker_id, tail_id, new_picker):
        """删旧下拉 + 原位（tail 锚点前）插入全新下拉，返回两个动作。

        为什么不用 partial_update：select_static 的选中态是客户端运行时状态，
        patch initial_index/options 清不掉它，选项被删后客户端找不到文本，
        会把原始 open_id 直接显示出来、且下拉无法正常打开。全新元素无选中
        历史，占位符正常、选项完整、可正常点开。动作按顺序先删后插，id复用。
        """
        return [
            self._act_delete([picker_id]),
            self._act_add_before(tail_id, [new_picker]),
        ]

    def _build_picker(self, picker_id, picker_name, pick_action,
                      remaining_users):
        return self.card_builder._person_picker(
            picker_id, picker_name, pick_action, remaining_users)

    def _apply_pick(self, session, ids_attr, new_id, remove_action,
                    count_id, empty_id, hr_id, noun,
                    picker_id, tail_id, picker_name, pick_action):
        """下拉选中某人的统一局部更新（全程持卡锁）：
        空提示删除 → 花名册加行 → 计数更新 → 旧下拉删除、原位插入全新下拉
        （选项剔除已选人员）。选中后下拉自动收起，同批候选随关闭消失。

        Returns: 是否执行了更新
        """
        with self._card_lock(session.card_id):
            ids = list(getattr(session, ids_attr))
            if not new_id or new_id in ids:
                return False
            was_empty = not ids
            ids.append(new_id)
            session.update(**{ids_attr: ids})

            info = self.im.get_user_by_open_id(new_id)
            row_info = {
                "name": info.get("name", "未知"),
                "dept": info.get("department", "") or "未知部门",
                "job_title": info.get("job_title", ""),
                "open_id": new_id,
            }

            actions = []
            if was_empty:
                actions.append(self._act_delete([empty_id]))
            actions.append(self._act_add_before(
                hr_id, [self.card_builder._roster_row(row_info, remove_action)]))
            actions.append(self._act_patch(
                count_id,
                {"text": {"tag": "lark_md",
                          "content": f"**已选{noun}（{len(ids)}人）**"}}))
            actions.extend(self._act_recreate_picker(
                picker_id, tail_id,
                self._build_picker(picker_id, picker_name, pick_action,
                                   self._remaining_candidates(ids))))

            self.im.batch_update_card(session.card_id, actions)
            return True

    def _apply_remove(self, session, ids_attr, target,
                      count_id, empty_id, hr_id, noun,
                      picker_id, tail_id, picker_name, pick_action,
                      hint_pos="above"):
        """点花名册「移除」（持卡锁，与选人串行）：
        删行 → 必要时补空提示 → 计数更新 → 删旧下拉、原位插入全新下拉
        （被移除者重新出现在选项中）。hint_pos 控制空提示文案方位。
        """
        with self._card_lock(session.card_id):
            ids = [oid for oid in getattr(session, ids_attr) if oid != target]
            session.update(**{ids_attr: ids})

            actions = [self._act_delete([self.card_builder._row_id(target)])]
            if not ids:
                actions.append(self._act_add_before(
                    hr_id, [self._empty_hint(empty_id, hint_pos)]))
            actions.append(self._act_patch(
                count_id,
                {"text": {"tag": "lark_md",
                          "content": f"**已选{noun}（{len(ids)}人）**"}}))
            actions.extend(self._act_recreate_picker(
                picker_id, tail_id,
                self._build_picker(picker_id, picker_name, pick_action,
                                   self._remaining_candidates(ids))))

            self.im.batch_update_card(session.card_id, actions)
            return True

    def _on_start_create(self, chat_id, user_id, value, form_value, message_id=""):
        """开始建群流程 → 发送项目信息表单"""
        self.sessions.update_state(user_id, chat_id, "waiting_project_info")
        self.im.send_card(chat_id, self.card_builder.project_info_form_card())

    def _on_start_archive(self, chat_id, user_id, value, form_value, message_id=""):
        """开始关闭并归档流程"""
        self.sessions.update_state(user_id, chat_id, "waiting_disband_confirm")
        self.im.send_card(chat_id, self.card_builder.disband_confirm_card(
            project_name="（请选择项目）",
            group_name="（待选择）",
            member_count=0,
        ))

    def _check_duplicate_project(self, chat_id, project_name, project_abbr):
        """前置查重：活跃项目中是否已存在相同项目名/简称。
        命中则发送友好提示并返回 True；无冲突返回 False。
        不改状态、不换卡。
        """
        if not self.group_manager:
            return False
        try:
            dup_field, dup_proj = self.group_manager.find_duplicate_project(
                project_name, project_abbr)
        except Exception as e:
            logger.warning(f"查重失败，跳过前置校验: {e}")
            return False
        if dup_field:
            dup_value = project_name if dup_field == "项目名称" else project_abbr
            self.im.send_text(
                chat_id,
                f"⚠️ {dup_field}「{dup_value}」已被现有项目「{dup_proj.project_name}」"
                f"占用（状态：{dup_proj.status}）。\n"
                f"请修改后重新提交，或如需复用请先归档已有项目。"
            )
            return True
        return False

    def _on_confirm_project_info(self, chat_id, user_id, value, form_value, message_id=""):
        """收集成员（表单提交）→ 保存项目信息 → 进入部门负责人收集卡"""
        project_name = _str(form_value.get("project_name", "")).strip()
        project_abbr = _str(form_value.get("project_abbr", "")).strip()

        if not project_name:
            self.im.send_text(chat_id, "项目名称不能为空，请重新输入 /建群")
            return

        if not project_abbr:
            project_abbr = project_name[:10]
        project_abbr = project_abbr[:10]  # 限长10，与群名一致

        # 前置查重：命中则不换卡、不改状态
        if self._check_duplicate_project(chat_id, project_name, project_abbr):
            return

        session = self.sessions.update_state(
            user_id, chat_id, "waiting_dept_leads",
            project_name=project_name,
            project_abbr=project_abbr,
            # 重置上一轮残留的 dept_leads/selected_members：
            # _on_send_dept_leads 会将 dept_leads 覆写为 dict 列表（进度追踪用），
            # 若不清空，下一轮 pick_lead 的 _remaining_candidates 做 set(ids)
            # 时会因 dict 不可哈希而报 unhashable type: 'dict'，导致下拉重建失败。
            dept_leads=[],
            selected_members=[],
        )

        scope_users = self.im.list_scope_users()
        resp = self.im.send_cardkit(
            chat_id,
            self.card_builder.dept_lead_collect_card(
                project_name, project_abbr, scope_users=scope_users),
        )
        session.update(card_id=resp.get("card_id", "") if resp else "", card_seq=0)

        self._lock_info_card(
            message_id, project_name, project_abbr,
            "已进入成员收集流程，请在下方卡片中选择部门负责人。")

    def _on_quick_create_from_info(self, chat_id, user_id, value, form_value, message_id=""):
        """快速建群（表单提交）→ 从表单获取项目信息 → 直接建群"""
        project_name = _str(form_value.get("project_name", "")).strip()
        project_abbr = _str(form_value.get("project_abbr", "")).strip()

        if not project_name:
            self.im.send_text(chat_id, "请先填写项目名称，再点击快速建群。")
            return

        if not project_abbr:
            project_abbr = project_name[:10]
        project_abbr = project_abbr[:10]  # 限长10，与群名一致

        # 前置查重：命中则不建群、不锁卡
        if self._check_duplicate_project(chat_id, project_name, project_abbr):
            return

        if self._execute_build(chat_id, user_id, project_name, project_abbr):
            self._lock_info_card(
                message_id, project_name, project_abbr,
                "项目群已创建完成，可直接进入群聊。")


    def _on_pick_lead(self, chat_id, user_id, value, form_value, message_id=""):
        """下拉选中负责人 → 花名册加行、下拉重置（cardkit 局部更新）。
        选中项 open_id 在 form_value["selected"]（select_static 回调字段 option）。"""
        session = self.sessions.get_or_create(user_id, chat_id)
        if not self._bind_card(session, message_id):
            logger.warning("负责人卡缺少 card_id，无法局部更新")
            return
        new_id = _str(form_value.get("selected", ""))
        try:
            self._apply_pick(
                session, "dept_leads", new_id, "remove_lead",
                count_id="lead_count", empty_id="lead_empty",
                hr_id="lead_hr", noun="负责人",
                picker_id="lead_picker", tail_id="lead_pick_tail",
                picker_name="lead_pick", pick_action="pick_lead")
        except Exception as e:
            logger.warning(f"局部更新负责人收集卡失败: {e}", exc_info=True)

    def _on_remove_lead(self, chat_id, user_id, value, form_value, message_id=""):
        """移除负责人 → cardkit 局部更新"""
        session = self.sessions.get_or_create(user_id, chat_id)
        target = value.get("open_id", "")

        if not self._bind_card(session, message_id):
            logger.warning("负责人卡缺少 card_id，无法局部更新")
            return
        try:
            self._apply_remove(
                session, "dept_leads", target,
                count_id="lead_count", empty_id="lead_empty",
                hr_id="lead_hr", noun="负责人",
                picker_id="lead_picker", tail_id="lead_pick_tail",
                picker_name="lead_pick", pick_action="pick_lead",
                hint_pos="below")
        except Exception as e:
            logger.warning(f"局部更新负责人收集卡失败: {e}", exc_info=True)

    def _on_send_dept_leads(self, chat_id, user_id, value, form_value, message_id=""):
        """发送部门负责人收集表
        流程：立即建群（PM+Agent入群）→ 解析负责人 → 发收集卡 → 群内展示进度看板
        """
        project_name = value.get("project_name", "")
        project_abbr = value.get("project_abbr", "")

        if not project_name:
            session = self.sessions.get_or_create(user_id, chat_id)
            project_name = session.project_name
            project_abbr = session.project_abbr

        # 1. 获取负责人 open_id 列表：form外按钮不带form_value，从session读取暂存选择
        lead_ids = form_value.get("leads", [])
        if not isinstance(lead_ids, list):
            lead_ids = [lead_ids] if lead_ids else []

        if not lead_ids:
            session = self.sessions.get_or_create(user_id, chat_id)
            lead_ids = list(session.dept_leads)

        if not lead_ids:
            self.im.send_text(chat_id, "请至少选择一位负责人。")
            return

        # 2. 立即建群（PM+Agent入群）
        if not self.group_manager:
            self.im.send_text(chat_id, "系统未就绪，GroupManager未初始化。")
            return

        _t_total = time.time()
        _t = time.time()
        try:
            project, result = self.group_manager.quick_register_and_create_group(
                project_name=project_name,
                project_short_name=project_abbr,
                pm_open_id=user_id,
            )
            group_chat_id = result.get("chat_id", "")
            logger.info(f"[PERF] _on_send_dept_leads 阶段①建群 耗时 {time.time()-_t:.2f}s")
        except Exception as e:
            logger.error(f"建群失败: {e}", exc_info=True)
            self.im.send_card(chat_id, self.card_builder.error_card(
                error_type="建群失败", message=str(e),
                suggestion="请检查项目信息或联系管理员。",
            ))
            return

        # 3. 查询每位负责人的部门和职务信息
        _t = time.time()
        depts = []
        for open_id in lead_ids:
            info = self.im.get_user_by_open_id(open_id)
            depts.append({
                "dept": info.get("department", "") or "未知部门",
                "lead": info.get("name", "未知"),
                "job_title": info.get("job_title", ""),
                "open_id": open_id,
                "status": "pending",
                "member_count": 0,
                "members": [],
            })
        logger.info(f"[PERF] _on_send_dept_leads 阶段②查负责人({len(lead_ids)}人) 耗时 {time.time()-_t:.2f}s")

        # 保存到会话
        self.sessions.update_state(
            user_id, chat_id, "collecting_members",
            project_name=project_name, project_abbr=project_abbr,
            project_id=project.project_id, group_chat_id=group_chat_id,
            dept_leads=depts,
        )

        # 4. 向每位负责人发送收集卡片
        _t = time.time()
        scope_users = self.im.list_scope_users()
        logger.info(f"[PERF] _on_send_dept_leads 阶段③list_scope_users 耗时 {time.time()-_t:.2f}s")
        _t_loop = time.time()

        def _send_to_one_lead(d):
            """单个负责人的收集卡发送（并发安全：每负责人独立session和API调用）"""
            try:
                sent_at = time.time()
                deadline_dt = datetime.fromtimestamp(sent_at) + timedelta(hours=24)
                deadline_str = deadline_dt.strftime("%m-%d %H:%M")
                # 在负责人的session中保存上下文，供成员添加回调时使用
                # chat_id 先用 open_id 占位，首次回调时 migrate 到真实chat
                lead_session = self.sessions.update_state(
                    d["open_id"], d["open_id"], "collecting_members",
                    project_name=project_name, project_abbr=project_abbr,
                    pm_user_id=user_id, pm_chat_id=chat_id, lead_name=d['lead'],
                    member_card_sent_at=sent_at,
                )
                _t_card = time.time()
                resp = self.im.send_cardkit_to_user(
                    d["open_id"],
                    self.card_builder.lead_member_collect_card(
                        project_name, project_abbr,
                        pm_user_id=user_id, pm_chat_id=chat_id, lead_name=d['lead'],
                        scope_users=scope_users, deadline_str=deadline_str,
                    ),
                )
                logger.info(f"[PERF]   发卡循环-{d['lead']} send_cardkit 耗时 {time.time()-_t_card:.2f}s")
                lead_session.update(
                    card_id=resp.get("card_id", "") if resp else "", card_seq=0)
                # 注册 24h 定时器：到期冻结或标记逾期
                if self.scheduler and resp and resp.get("card_id"):
                    self.scheduler.schedule_confirm(
                        f"member_freeze:{d['open_id']}",
                        callback=self._freeze_or_overdue_member_card,
                        timeout_minutes=1440,
                        lead_open_id=d["open_id"],
                    )
                # P2 飞书待办联动：为负责人创建飞书任务（截止=T+24h，提前1h提醒）
                # 权限不足或失败时降级为机器人消息提醒（B计划）
                _t_task = time.time()
                task_info = self.im.create_task(
                    summary=f"[{project_name}] 收集部门成员",
                    assignee_open_id=d["open_id"],
                    deadline_ts=sent_at + 24 * 3600,
                    description=(
                        f"请为项目「{project_name}（{project_abbr}）」"
                        f"收集本部门成员名单，并在飞书任务中标记完成。"
                    ),
                    reminder_minutes_before=60,
                )
                task_guid = task_info.get("task_guid", "")
                logger.info(f"[PERF]   发卡循环-{d['lead']} create_task 耗时 {time.time()-_t_task:.2f}s")
                if task_guid:
                    lead_session.update(member_task_id=task_guid)
                    logger.info(f"已为 {d['lead']} 创建飞书任务: {task_guid}")
                else:
                    # B 计划：发条机器人消息作为提醒（不阻塞主流程）
                    try:
                        self.im.send_text_to_user(
                            d["open_id"],
                            f"请在24小时内为项目「{project_name}」收集本部门成员。",
                        )
                    except Exception as fallback_err:
                        logger.warning(f"B 计划消息提醒也失败: {fallback_err}")
                return 1
            except Exception as e:
                logger.error(f"向 {d['lead']} 发送收集卡失败: {e}", exc_info=True)
                d["status"] = "send_failed"
                return 0

        # 并发发送：每负责人独立 session 和 API 调用，互不依赖
        max_workers = min(len(depts), 5)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_send_to_one_lead, d): d for d in depts}
            results = [f.result() for f in as_completed(futures)]
        sent_count = sum(results)

        # 5. 在群内发送进度看板卡
        logger.info(f"[PERF] _on_send_dept_leads 阶段④发卡循环({len(depts)}人) 耗时 {time.time()-_t_loop:.2f}s")
        _t = time.time()
        bitable_url = self._get_bitable_url()
        progress_msg = self.im.send_card(
            group_chat_id,
            self.card_builder.progress_dashboard_card(
                depts, project_name, project_abbr, bitable_url=bitable_url,
            ),
        )
        progress_msg_id = progress_msg.get("message_id", "") if progress_msg else ""
        self.sessions.update_state(user_id, chat_id, "collecting_members", progress_message_id=progress_msg_id)

        # 6. 通知PM建群结果
        summary = f"✅ 项目群已创建，已向 {sent_count}/{len(lead_ids)} 位负责人发送收集表。"
        failed = [d["lead"] for d in depts if d["status"] == "send_failed"]
        if failed:
            summary += f"\n❌ 发送失败：{', '.join(failed)}"
        summary += "\n\n群内可实时查看收集进度，负责人提交成员后需您确认拉入群。"
        self.im.send_text(chat_id, summary)

        # 7. 锁定负责人收集卡（防止重复操作）
        lock_status = f"已向 {sent_count}/{len(lead_ids)} 位负责人发送收集表，等待成员提交。"
        lead_infos_for_lock = [
            {"name": d["lead"], "dept": d["dept"],
             "job_title": d["job_title"], "open_id": d["open_id"]}
            for d in depts
        ]
        self._lock_lead_collect_card(
            message_id, project_name, project_abbr,
            lead_infos_for_lock, lock_status)
        logger.info(f"[PERF] _on_send_dept_leads 阶段⑤收尾 耗时 {time.time()-_t:.2f}s")
        logger.info(f"[PERF] _on_send_dept_leads 总耗时 {time.time()-_t_total:.2f}s")

    def _on_skip_collection(self, chat_id, user_id, value, form_value, message_id=""):
        """跳过成员收集 → 直接建群"""
        project_name = value.get("project_name", "")
        project_abbr = value.get("project_abbr", "")

        session = self.sessions.get_or_create(user_id, chat_id)
        project_name = project_name or session.project_name
        project_abbr = project_abbr or session.project_abbr

        self._execute_build(chat_id, user_id, project_name, project_abbr)

    def _get_bitable_url(self):
        """获取项目配置表的多维表格链接"""
        if self.group_manager:
            app_token = getattr(self.group_manager, 'f01_app_token', '')
            if app_token:
                return f"https://feishu.cn/base/{app_token}"
        return ""

    def _on_pick_member(self, chat_id, user_id, value, form_value, message_id=""):
        """下拉选中成员 → 花名册加行、下拉重置（cardkit 局部更新）。
        选中项 open_id 在 form_value["selected"]（select_static 回调字段 option）。"""
        session = self.sessions.migrate_context(user_id, chat_id)
        if not self._bind_card(session, message_id):
            logger.warning("成员卡缺少 card_id，无法局部更新")
            return
        new_id = _str(form_value.get("selected", ""))
        try:
            self._apply_pick(
                session, "selected_members", new_id, "remove_member",
                count_id="mem_count", empty_id="mem_empty",
                hr_id="mem_hr", noun="成员",
                picker_id="mem_picker", tail_id="mem_pick_tail",
                picker_name="member_pick", pick_action="pick_member")
        except Exception as e:
            logger.warning(f"局部更新成员收集卡失败: {e}", exc_info=True)

    def _on_remove_member(self, chat_id, user_id, value, form_value, message_id=""):
        """移除成员 → cardkit 局部更新"""
        session = self.sessions.migrate_context(user_id, chat_id)
        target = value.get("open_id", "")

        if not self._bind_card(session, message_id):
            logger.warning("成员卡缺少 card_id，无法局部更新")
            return
        try:
            self._apply_remove(
                session, "selected_members", target,
                count_id="mem_count", empty_id="mem_empty",
                hr_id="mem_hr", noun="成员",
                picker_id="mem_picker", tail_id="mem_pick_tail",
                picker_name="member_pick", pick_action="pick_member",
                hint_pos="below")
        except Exception as e:
            logger.warning(f"局部更新成员收集卡失败: {e}", exc_info=True)

    def _on_lead_submit_members(self, chat_id, user_id, value, form_value, message_id=""):
        """部门负责人提交成员名单 → 解析成员 → 群内弹确认卡 → 启动超时定时器

        P3 追加式提交：
        - 未冻结前可重复提交，每次以累积后的 selected_members 为准；
        - 第一次提交：发新 PM 确认卡 + 调用 complete_task + 启动定时器；
        - 后续提交：取消旧定时器，重置 30 分钟，发新卡覆盖旧的，
          不再重复 complete_task（任务已完成）。
        - 重复成员由 _on_pick_member 在挑选阶段去重，此处直接读累积值。
        """
        # 优先从 value 获取，兜底从 session 获取（含占位chat_id会话迁移）
        session = self.sessions.migrate_context(user_id, chat_id)
        project_name = value.get("project_name", "") or session.project_name
        project_abbr = value.get("project_abbr", "") or session.project_abbr
        pm_user_id = value.get("pm_user_id", "") or session.pm_user_id
        pm_chat_id = value.get("pm_chat_id", "") or session.pm_chat_id
        lead_name = value.get("lead_name", "") or session.lead_name or "负责人"

        # 获取成员 open_id 列表：form外按钮不带form_value，从session读取暂存选择
        member_ids = form_value.get("members", [])
        if not isinstance(member_ids, list):
            member_ids = [member_ids] if member_ids else []

        if not member_ids:
            member_ids = list(session.selected_members)

        if not member_ids:
            self.im.send_text(chat_id, "请至少选择一位成员。")
            return

        # P3 判断是否首次提交：confirm_message_id 为空 → 首次
        is_first_submit = not session.confirm_message_id

        # 查询每个成员的信息（用累积后的 member_ids）
        members = []
        for open_id in member_ids:
            info = self.im.get_user_by_open_id(open_id)
            members.append({
                "name": info.get("name", "未知"),
                "open_id": open_id,
                "dept": info.get("department", ""),
                "position": info.get("job_title", ""),
                "employee_id": "",
            })

        # 增量确认：只把本次新增（未被 PM 认可过）的成员发给 PM 确认
        approved_ids = set(session.approved_ids or [])
        new_members = [m for m in members if m["open_id"] not in approved_ids]
        if not new_members:
            self.im.send_text(
                chat_id,
                "ℹ️ 当前花名册中的成员已全部被 PM 认可，无新增成员需要确认。"
                "如需追加，请先在搜索框选择新成员加入花名册。",
            )
            return

        # 向负责人确认已提交（区分首次/追加）
        if is_first_submit:
            self.im.send_text(
                chat_id,
                f"✅ 已提交 {len(new_members)} 位成员，等待PM确认后拉入群。",
            )
        else:
            self.im.send_text(
                chat_id,
                f"✅ 已重新提交，本次新增 {len(new_members)} 位成员，"
                f"PM 确认卡已更新，定时器已重置。",
            )

        # P2 飞书待办联动：仅在首次提交时 complete_task
        task_guid = session.member_task_id
        if task_guid and is_first_submit:
            ok = self.im.complete_task(task_guid)
            if ok:
                logger.info(f"已完成飞书任务: {task_guid}")
            else:
                logger.warning(f"完成飞书任务失败: {task_guid}")

        # 定位PM会话，获取群聊ID
        if not (pm_user_id and pm_chat_id):
            return
        pm_session = self.sessions.get(pm_user_id, pm_chat_id)
        if not pm_session:
            return

        group_chat_id = pm_session.group_chat_id
        project_id = pm_session.project_id
        if not group_chat_id:
            return

        # 更新会话中该负责人的状态和成员（每次都用累积后的完整列表覆盖）
        lead_dept = ""
        for d in pm_session.dept_leads:
            if d.get("open_id") == user_id:
                d["status"] = "filled"
                d["member_count"] = len(members)
                d["members"] = members
                lead_dept = d.get("dept", "")
                break

        # 群内成员确认卡：首次提交发新卡，后续提交更新同一卡（避免多张未确认卡造成重复）
        confirm_card_content = self.card_builder.member_confirm_card(
            lead_name=lead_name,
            dept_name=lead_dept,
            members=new_members,
            lead_open_id=user_id,
            project_name=project_name,
        )

        if is_first_submit or not session.confirm_message_id:
            # 首次：发新卡
            confirm_msg = self.im.send_card(group_chat_id, confirm_card_content)
            confirm_msg_id = confirm_msg.get("message_id", "") if confirm_msg else ""
            session.update(confirm_message_id=confirm_msg_id)
        else:
            # 后续：更新已有确认卡，PM 始终只有一张待确认卡，内容为全部未认可成员
            confirm_msg_id = session.confirm_message_id
            try:
                self.im.update_card(confirm_msg_id, confirm_card_content)
            except Exception as e:
                logger.warning(f"更新确认卡失败，改发新卡: {e}")
                confirm_msg = self.im.send_card(group_chat_id, confirm_card_content)
                confirm_msg_id = confirm_msg.get("message_id", "") if confirm_msg else ""
                session.update(confirm_message_id=confirm_msg_id)

        # 启动超时自动认可定时器（schedule_confirm 内部会取消同 key 旧定时器）
        timer_key = f"{project_id}:{user_id}"
        if self.scheduler:
            self.scheduler.schedule_confirm(
                key=timer_key,
                callback=self._auto_approve_members,
                pm_user_id=pm_user_id,
                pm_chat_id=pm_chat_id,
                lead_open_id=user_id,
                lead_name=lead_name,
                members=new_members,
                group_chat_id=group_chat_id,
                confirm_msg_id=confirm_msg_id,
                dept_name=lead_dept,
            )

        # 提交后立即把成员行替换为只读版本（无移除按钮），防止负责人在PM审核期间删除成员
        # 使用 update_elements 替换整行，避免 delete 元素不存在时整体失败
        if session.card_id and new_members:
            try:
                actions = []
                for m in new_members:
                    row_id = self.card_builder._row_id(m.get("open_id", ""))
                    readonly_row = self.card_builder._roster_row_readonly({
                        "name": m.get("name", ""),
                        "job_title": m.get("position", ""),
                        "dept": m.get("dept", ""),
                        "open_id": m.get("open_id", ""),
                    })
                    actions.append(self._act_update(row_id, readonly_row))
                if actions:
                    self.im.batch_update_card(session.card_id, actions)
            except Exception as e:
                logger.warning(f"提交后锁定成员行失败: {e}")

        # 更新群内进度看板
        self._update_progress_dashboard(pm_session)

    def _auto_approve_members(self, pm_user_id, pm_chat_id, lead_open_id, lead_name,
                               members, group_chat_id, confirm_msg_id="", dept_name=""):
        """超时自动认可回调"""
        logger.info(f"超时自动认可: {lead_name} 的 {len(members)} 名成员")
        self._add_members_to_group(
            pm_user_id, pm_chat_id, lead_open_id, lead_name,
            members, group_chat_id, confirm_msg_id, auto=True,
            dept_name=dept_name,
        )

    def _on_approve_members(self, chat_id, user_id, value, form_value, message_id=""):
        """PM认可成员 → 拉入群"""
        import json as _json
        members = _json.loads(value.get("members", "[]"))
        lead_name = value.get("lead_name", "")
        lead_open_id = value.get("lead_open_id", "")
        dept_name = value.get("dept_name", "")

        # 从按钮回调无法直接拿到PM会话，通过chat_id（群聊）反查
        pm_session = self._find_session_by_group_chat(chat_id)
        if not pm_session:
            logger.warning(f"未找到群聊 {chat_id} 对应的PM会话")
            return

        # 权限校验：只有 PM 可以认可成员
        if user_id != pm_session.user_id:
            logger.info(f"非PM用户 {user_id} 尝试认可成员，已拦截")
            self.im.send_text(chat_id, "⚠️ 只有项目经理可以确认成员入群。")
            return

        # 取消定时器
        if self.scheduler:
            self.scheduler.cancel_confirm(f"{pm_session.project_id}:{lead_open_id}")

        # dept_name 已从 value 获取
        self._add_members_to_group(
            pm_session.user_id, pm_session.chat_id, lead_open_id, lead_name,
            members, chat_id, message_id, auto=False,
            dept_name=dept_name,
        )

    def _on_reject_members(self, chat_id, user_id, value, form_value, message_id=""):
        """PM拒绝成员 → 通知负责人重填"""
        import json as _json
        members = _json.loads(value.get("members", "[]"))
        lead_name = value.get("lead_name", "")
        lead_open_id = value.get("lead_open_id", "")

        pm_session = self._find_session_by_group_chat(chat_id)
        if not pm_session:
            return

        # 权限校验：只有 PM 可以拒绝成员
        if user_id != pm_session.user_id:
            logger.info(f"非PM用户 {user_id} 尝试拒绝成员，已拦截")
            self.im.send_text(chat_id, "⚠️ 只有项目经理可以拒绝成员。")
            return

        # 取消定时器
        if self.scheduler:
            self.scheduler.cancel_confirm(f"{pm_session.project_id}:{lead_open_id}")

        # 重置该负责人状态为待填
        for d in pm_session.dept_leads:
            if d.get("open_id") == lead_open_id or d.get("lead") == lead_name:
                d["status"] = "pending"
                d["member_count"] = 0
                d["members"] = []
                break

        # 通知负责人
        if lead_open_id:
            try:
                self.im.send_text_to_user(
                    lead_open_id,
                    f"PM拒绝了您提交的成员名单，请重新填写后提交。",
                )
            except Exception as e:
                logger.error(f"通知负责人失败: {e}")

        self.im.send_text(chat_id, f"已拒绝 {lead_name} 提交的成员名单，已通知其重新填写。")

        # 冻结群内确认卡：替换为灰色只读卡，认可/拒绝按钮消失
        if message_id:
            dept_name = value.get("dept_name", "")
            try:
                self.im.update_card(
                    message_id,
                    self.card_builder.member_confirm_locked_card(
                        lead_name=lead_name, dept_name=dept_name,
                        members=members, status_text="❌ 已拒绝", added_count=0),
                )
            except Exception as e:
                logger.warning(f"更新群内确认卡为拒绝锁定状态失败: {e}")

        # 拒绝后清空负责人 confirm_message_id，下次重新提交时发新卡
        if lead_open_id:
            lead_session = self.sessions.get_by_user(lead_open_id)
            if lead_session:
                lead_session.update(confirm_message_id="")

        # 恢复负责人成员收集卡上被拒绝成员的「移除」按钮（用带移除按钮的行整体替换）
        if lead_open_id and members:
            lead_session = self.sessions.get_by_user(lead_open_id)
            if lead_session and lead_session.card_id:
                restore_actions = []
                for m in members:
                    digest = hashlib.md5(m.get("open_id", "").encode("utf-8")).hexdigest()[:8]
                    row_id = f"row_{digest}"
                    row_info = {
                        "name": m.get("name", "未知"),
                        "dept": m.get("dept", "") or "未知部门",
                        "job_title": m.get("job_title", "") or m.get("position", ""),
                        "open_id": m.get("open_id", ""),
                    }
                    new_row = self.card_builder._roster_row(row_info, "remove_member")
                    restore_actions.append(self._act_update(row_id, new_row))
                if restore_actions:
                    try:
                        self.im.batch_update_card(lead_session.card_id, restore_actions)
                    except Exception as e:
                        logger.warning(f"拒绝后恢复移除按钮失败: {e}")

        self._update_progress_dashboard(pm_session)

    def _add_members_to_group(self, pm_user_id, pm_chat_id, lead_open_id, lead_name,
                              members, group_chat_id, confirm_msg_id="", auto=False,
                              dept_name=""):
        """将成员拉入群聊并更新进度"""
        valid_ids = [m["open_id"] for m in members if m.get("open_id")]
        added = 0
        if valid_ids:
            try:
                from src.feishu.chat_client import ChatClient
                cc = ChatClient(self.group_manager.config)
                _t = time.time()
                cc.add_members(group_chat_id, valid_ids)
                logger.info(f"[PERF] _add_members_to_group add_members({len(valid_ids)}人) 耗时 {time.time()-_t:.2f}s")
                added = len(valid_ids)
            except Exception as e:
                logger.error(f"拉入群失败: {e}")

            # 同步写入多维表格「项目成员表」
            if added > 0 and self.group_manager:
                pm_session = self.sessions.get(pm_user_id, pm_chat_id)
                project_id = pm_session.project_id if pm_session else ""
                if project_id:
                    try:
                        self.group_manager.add_project_members(
                            project_id=project_id,
                            members=members,
                            dept_name=dept_name,
                            lead_name=lead_name,
                            lead_open_id=lead_open_id,
                            chat_id=group_chat_id,
                        )
                    except Exception as e:
                        logger.warning(f"写入项目成员表失败: {e}")

        # 更新群内确认卡状态：用只读锁定卡替换原确认卡，认可/拒绝按钮消失
        if confirm_msg_id:
            status_text = "⏰ 超时自动认可" if auto else "✅ 已认可"
            try:
                self.im.send_text(
                    group_chat_id,
                    f"{status_text}：{lead_name} 提交的 {len(members)} 名成员（{added}人已入群）。",
                )
            except Exception:
                pass
            try:
                self.im.update_card(
                    confirm_msg_id,
                    self.card_builder.member_confirm_locked_card(
                        lead_name=lead_name, dept_name=dept_name,
                        members=members, status_text=status_text,
                        added_count=added),
                )
            except Exception as e:
                logger.warning(f"更新群内确认卡为锁定状态失败: {e}")

        # 更新PM会话中该负责人状态
        pm_session = self.sessions.get(pm_user_id, pm_chat_id)
        if pm_session:
            for d in pm_session.dept_leads:
                if d.get("open_id") == lead_open_id or d.get("lead") == lead_name:
                    d["status"] = "approved"
                    break
            self._update_progress_dashboard(pm_session)

        # 锁定负责人那边的成员收集卡（只删移除+不参与按钮，保留搜索/提交）
        self._lock_member_collect_card_after_approval(lead_open_id, members, auto=auto)

        # 把本次认可的成员 open_id 加到负责人 session.approved_ids，
        # 下次提交时这些成员会被过滤掉，PM 确认卡只显示真正新增的成员
        lead_session = self.sessions.get_by_user(lead_open_id)
        if lead_session:
            new_ids = [m.get("open_id", "") for m in members if m.get("open_id")]
            existing = set(lead_session.approved_ids or [])
            merged = list(existing | set(new_ids))
            # 清空 confirm_message_id：确认卡已锁定，下次追加提交需发新卡
            lead_session.update(approved_ids=merged, confirm_message_id="")

    def _lock_member_collect_card_after_approval(self, lead_open_id, members, auto=False):
        """PM 认可/超时自动认可后，精细化锁定负责人那边的成员收集卡。

        只冻结「移除」按钮和「本部门不参与」按钮，保留搜索/提交/花名册成员显示，
        允许负责人继续追加新成员提交（P3 追加式提交需求）。
        """
        session = self.sessions.get_by_user(lead_open_id)
        if not session or not session.card_id:
            logger.warning(f"认可后锁定成员卡：未找到会话或 card_id lead_open_id={lead_open_id}")
            return

        # members 字段含 position（来自 _on_lead_submit_members），转为 locked 期望的 job_title
        member_infos = []
        for m in members:
            member_infos.append({
                "name": m.get("name", "未知"),
                "dept": m.get("dept", "") or "未知部门",
                "job_title": m.get("job_title", "") or m.get("position", ""),
                "open_id": m.get("open_id", ""),
            })

        # 顶部插入提示：PM 已认可，可继续追加新成员
        status_text = ("⏰ PM 超时未操作，已自动认可" if auto
                       else "✅ PM 已认可，可继续提交新成员追加（已认可成员不可移除）")
        hint = self.card_builder._md(status_text)

        # 先插入提示（容错：重复插入不会报错，飞书会自动 dedupe 相邻同 element_id）
        try:
            self.im.batch_update_card(session.card_id, [
                self._act_add_before("mem_intro", [hint]),
            ])
        except Exception as e:
            logger.warning(f"插入提示失败（可忽略）: {e}")

        # 第一步：删除「本部门不参与」按钮容器（第一次锁定时执行；
        # 后续追加认可时该元素已被删，会触发 300314，单独容错跳过）
        try:
            self.im.batch_update_card(session.card_id, [
                self._act_delete(["mem_skip_cancel"]),
            ])
        except Exception as e:
            if "300314" in str(e) or "not find element" in str(e):
                logger.info("mem_skip_cancel 已被删除（之前锁定过），跳过")
            else:
                logger.warning(f"删除 mem_skip_cancel 失败: {e}")

        # 第二步：删除本次认可成员的「移除」按钮
        # 注意：提交后成员行已被替换为只读版本（无移除按钮），所以此处可能 300314，容错跳过
        rm_ids = []
        for info in member_infos:
            digest = hashlib.md5(info.get("open_id", "").encode("utf-8")).hexdigest()[:8]
            rm_ids.append(f"rm_{digest}")
        if rm_ids:
            try:
                self.im.batch_update_card(session.card_id, [
                    self._act_delete(rm_ids),
                ])
            except Exception as e:
                if "300314" in str(e) or "not find element" in str(e):
                    logger.info("移除按钮已不存在（提交后已锁定为只读行），跳过")
                else:
                    logger.warning(f"删除移除按钮失败: {e}")

        logger.info(f"成员收集卡已精细化锁定（PM认可）: lead={lead_open_id}, 本次 {len(member_infos)} 人")

    def _find_session_by_group_chat(self, group_chat_id):
        """通过群聊ID反查PM会话"""
        for key, session in self.sessions._sessions.items():
            if session.group_chat_id == group_chat_id:
                return session
        return None

    def _update_progress_dashboard(self, pm_session):
        """更新群内进度看板卡"""
        if not (pm_session.group_chat_id and pm_session.progress_message_id):
            return
        try:
            self.im.update_card(
                pm_session.progress_message_id,
                self.card_builder.progress_dashboard_card(
                    pm_session.dept_leads,
                    pm_session.project_name,
                    pm_session.project_abbr,
                    bitable_url=self._get_bitable_url(),
                ),
            )
        except Exception as e:
            logger.warning(f"更新进度看板失败: {e}")

    def _execute_build(self, chat_id, user_id, project_name, project_abbr, member_names="", dept_leads=None):
        """执行建群流程"""
        if not project_name:
            self.im.send_text(chat_id, "项目信息缺失，请重新输入 /建群")
            self.sessions.clear(user_id, chat_id)
            return False

        # 计算未收集部门
        uncollected_depts = []
        if dept_leads:
            for d in dept_leads:
                if d.get("status") not in ("filled", "not_found"):
                    uncollected_depts.append(d.get("dept", d.get("lead", "未知")))

        steps = [
            {"label": "注册项目", "desc": f"项目：{project_name}"},
            {"label": "创建群聊", "desc": f"群名：{project_abbr}项目管理Agent"},
            {"label": "Agent入群", "desc": "自动添加机器人"},
            {"label": "更新配置", "desc": "写入项目配置表"},
        ]

        progress_msg = self.im.send_card(
            chat_id, self.card_builder.build_progress_card(steps, 0)
        )
        message_id = progress_msg.get("message_id", "") if progress_msg else ""

        self.sessions.update_state(
            user_id, chat_id, "building",
            project_name=project_name,
            project_abbr=project_abbr,
            progress_message_id=message_id,
        )

        if not self.group_manager:
            self.im.send_card(chat_id, self.card_builder.error_card(
                error_type="系统未就绪",
                message="GroupManager未初始化",
            ))
            return False

        try:
            # 注册与建群已并行编排，一次等待后直接到完成态
            project, result = self.group_manager.quick_register_and_create_group(
                project_name=project_name,
                project_short_name=project_abbr,
                pm_open_id=user_id,
            )
            new_chat_id = result.get("chat_id", "")

            if message_id:
                self.im.update_card(
                    message_id,
                    self.card_builder.build_progress_card(steps, len(steps)),
                )

            self.im.send_card(chat_id, self.card_builder.success_card(
                project_name=project_name,
                group_name=f"{project_abbr}项目管理Agent",
                member_count=1,
                chat_id=new_chat_id,
                uncollected_depts=uncollected_depts if uncollected_depts else None,
            ))
            self.sessions.clear(user_id, chat_id)
            return True

        except DuplicateProjectError as e:
            logger.warning(f"建群查重拦截（兜底）: {e}")
            if message_id:
                self.im.update_card(
                    message_id,
                    self.card_builder.build_progress_card(steps, 1, error_msg=str(e)),
                )
            self.im.send_text(
                chat_id,
                f"⚠️ {e.field}「{e.value}」已被现有项目占用。\n"
                f"请修改后重新 /建群，或如需复用请先归档已有项目。"
            )
            self.sessions.clear(user_id, chat_id)
            return False
        except Exception as e:
            logger.error(f"建群失败: {e}", exc_info=True)
            if message_id:
                self.im.update_card(
                    message_id,
                    self.card_builder.build_progress_card(steps, 1, error_msg=str(e)),
                )
            self.im.send_card(chat_id, self.card_builder.error_card(
                error_type="建群失败",
                message=str(e),
                suggestion="请检查项目信息或联系管理员。",
            ))
            self.sessions.clear(user_id, chat_id)
            return False

    def _on_select_disband_project(self, chat_id, user_id, value, form_value, message_id=""):
        """选择要解散的项目 → 显示确认卡片"""
        project_id = value.get("project_id", "")
        if not project_id or not self.group_manager:
            self.im.send_text(chat_id, "项目信息缺失，请重试。")
            return

        project = self.group_manager._get_project(project_id)
        if not project:
            self.im.send_text(chat_id, "项目不存在或已归档。")
            return

        member_count = 0
        if project.chat_id:
            try:
                members = self.group_manager.chat_client.get_all_members(project.chat_id)
                member_count = len(members)
            except Exception as e:
                logger.warning(f"获取群成员数失败: {e}")

        self.sessions.update_state(
            user_id, chat_id, "waiting_disband_confirm",
            project_id=project_id, project_name=project.project_name,
        )
        self.im.send_card(chat_id, self.card_builder.disband_confirm_card(
            project_name=project.project_name,
            group_name=project.group_name or project.project_short_name,
            member_count=member_count,
        ))

    def _on_confirm_disband(self, chat_id, user_id, value, form_value, message_id=""):
        """确认解散 → 异步执行归档"""
        dissolve = value.get("dissolve", True)
        session = self.sessions.get_or_create(user_id, chat_id)
        project_id = session.project_id
        if not project_id or not self.group_manager:
            self.im.send_text(chat_id, "项目信息缺失，请重新发送 /解散。")
            return

        mode_text = "解散群聊" if dissolve else "保留群聊"
        # 立即将原确认卡更新为「进行中」状态（按钮消失，避免重复点击）
        if message_id:
            try:
                self.im.update_card(
                    message_id, self.card_builder.disband_processing_card(mode_text))
            except Exception as e:
                logger.warning(f"更新归档进行中卡片失败: {e}")

        def _do_disband():
            try:
                results = self.group_manager.disband_project_group(
                    project_id, operator_open_id=user_id, dissolve=dissolve)
                # 归档完成 → 更新为只读完成卡
                if message_id:
                    try:
                        self.im.update_card(
                            message_id,
                            self.card_builder.disband_completed_card(
                                project_name=session.project_name or "",
                                dissolve=dissolve, results=results))
                    except Exception as e:
                        logger.warning(f"更新归档完成卡片失败: {e}")
                self.sessions.clear(user_id, chat_id)
            except Exception as e:
                logger.error(f"解散归档失败: {e}", exc_info=True)
                self.im.send_text(chat_id, f"❌ 归档失败：{e}\n请稍后重试或联系管理员。")

        threading.Thread(target=_do_disband, daemon=True).start()

    def _on_cancel(self, chat_id, user_id, value, form_value, message_id=""):
        """取消操作"""
        self.sessions.clear(user_id, chat_id)
        self.im.send_text(chat_id, "已取消当前操作。输入 /帮助 查看可用指令。")

    def _on_retry(self, chat_id, user_id, value, form_value, message_id=""):
        """重试"""
        session = self.sessions.get_or_create(user_id, chat_id)
        if session.project_name:
            self._execute_build(
                chat_id, user_id, session.project_name, session.project_abbr,
                dept_leads=session.dept_leads,
            )
        else:
            self.sessions.update_state(user_id, chat_id, "waiting_project_info")
            self.im.send_card(chat_id, self.card_builder.project_info_form_card())

    def _on_remind_pending(self, chat_id, user_id, value, form_value, message_id=""):
        """催填提醒 - 向未填写的负责人发送催填消息"""
        session = self.sessions.get_or_create(user_id, chat_id)
        if not session.dept_leads:
            self.im.send_text(chat_id, "没有进行中的收集任务。")
            return

        reminded = 0
        for d in session.dept_leads:
            if d.get("status") == "pending" and d.get("open_id"):
                try:
                    self.im.send_text_to_user(
                        d["open_id"],
                        f"⏰ 提醒：PM正在等待您为项目「{session.project_name}」提交部门成员名单。",
                    )
                    reminded += 1
                except Exception as e:
                    logger.error(f"催填提醒发送失败 {d['lead']}: {e}")

        self.im.send_text(chat_id, f"已向 {reminded} 位未填写的负责人发送催填提醒。")

    def _on_decline_collection(self, chat_id, user_id, value, form_value, message_id=""):
        """部门负责人拒绝收集 → 更新PM进度"""
        project_name = value.get("project_name", "")
        pm_user_id = value.get("pm_user_id", "")
        pm_chat_id = value.get("pm_chat_id", "")
        lead_name = value.get("lead_name", "负责人")

        self.im.send_text(chat_id, "已通知PM您的部门不参与此项目。")

        if pm_user_id and pm_chat_id:
            pm_session = self.sessions.get(pm_user_id, pm_chat_id)
            if pm_session and pm_session.dept_leads:
                for d in pm_session.dept_leads:
                    if d.get("open_id") == user_id:
                        d["status"] = "declined"
                        break

                self.im.send_text(
                    pm_chat_id,
                    f"📋 {lead_name} 的部门不参与此项目。",
                )
                self.im.send_card(
                    pm_chat_id,
                    self.card_builder.progress_dashboard_card(
                        pm_session.dept_leads, project_name,
                        pm_session.project_abbr,
                        bitable_url=self._get_bitable_url(),
                    ),
                )
