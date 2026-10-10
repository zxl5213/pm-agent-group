import logging
import time
import threading

from src.feishu.chat_client import ChatClient
from src.feishu.im_client import IMClient
from src.feishu.base_client import BaseClient
from src.feishu.card_builder import CardBuilder
from src.models.project import Project, Member, ArchiveLog
from src.models.exceptions import GroupCreationError, ArchiveError, FeishuAPIError
from src.modules.state_engine import StateEngine

logger = logging.getLogger(__name__)

MAX_SHORT_NAME_LENGTH = 10


def _make_group_name(project_short_name):
    """按PRD要求生成群名：项目简称+项目管理Agent"""
    return f"{project_short_name[:MAX_SHORT_NAME_LENGTH]}项目管理Agent"


class GroupManager:
    """群聊生命周期管理 - 覆盖F-01建群和F-02解散归档"""

    def __init__(self, config):
        self.config = config
        self._disband_locks = {}  # project_id -> threading.Lock
        self._register_lock = threading.Lock()
        self._pending_registrations = set()  # (项目名, 简称lower) 占位，堵住查重通过→记录可见窗口的并发重复提交
        self.chat_client = ChatClient(config)
        self.im_client = IMClient(config)
        self.base_client = BaseClient(config)
        self.card_builder = CardBuilder()
        self.state_engine = StateEngine(self.base_client, config)
        self.f01_app_token = config['bitable']['f01_app_token']
        self.f01_tables = config['bitable']['f01_tables']

    # ======================== F-01: 建群 ========================

    def create_project_group(self, project_id, quick_mode=False, member_open_ids=None, project=None):
        """建群主入口

        Args:
            project_id: 项目ID
            quick_mode: True=快速建群（跳过成员收集）
            member_open_ids: 快速模式下直接传入的成员open_id列表
            project: 可选，由 register_project 返回的 Project 对象，传入则跳过 _get_project 和 get_project_record_id 两次多维表读取
        """
        _t_load = time.time()
        if project is None:
            project = self._get_project(project_id)
            logger.info(f"[PERF] create_project_group 前置_get_project 耗时 {time.time()-_t_load:.2f}s")
        if not project:
            raise GroupCreationError("load_project", f"项目 {project_id} 不存在")

        if project.chat_id:
            raise GroupCreationError("check_existing", f"项目已有群聊 {project.chat_id}")

        # 缓存 record_id，避免后续多次读取；优先用 project 对象携带的 record_id
        if project.record_id:
            record_id = project.record_id
        else:
            _t = time.time()
            record_id = self.state_engine.get_project_record_id(project_id)
            logger.info(f"[PERF] create_project_group 前置get_project_record_id 耗时 {time.time()-_t:.2f}s")
        if not project.record_id:
            logger.info(f"[PERF] create_project_group 前置加载总耗时 {time.time()-_t_load:.2f}s (含两次多维表读取)")

        try:
            self.state_engine.transition(project_id, "收集中", record_id=record_id)
        except Exception:
            pass

        if not quick_mode:
            return self._start_member_collection(project)

        _t_total = time.time()
        _t = time.time()
        chat_id = self._do_create_chat(project, record_id)
        logger.info(f"[PERF] create_project_group 步①create_chat 耗时 {time.time()-_t:.2f}s")
        # 确保 PM 始终被拉入群聊（即使 quick_mode 未传成员列表）
        all_members = list(member_open_ids or [])
        if project.pm_open_id and project.pm_open_id not in all_members:
            all_members.insert(0, project.pm_open_id)
        if all_members:
            _t = time.time()
            self._do_add_members(project, chat_id, all_members)
            logger.info(f"[PERF] create_project_group 步②add_members({len(all_members)}人) 耗时 {time.time()-_t:.2f}s")
        _t = time.time()
        self._do_deploy_agent(project, chat_id)
        logger.info(f"[PERF] create_project_group 步③deploy_agent 耗时 {time.time()-_t:.2f}s")
        _t = time.time()
        self._send_agent_welcome(project, chat_id)
        logger.info(f"[PERF] create_project_group 步④send_welcome 耗时 {time.time()-_t:.2f}s")
        # PM写入多维表 + 状态流转：后置异步执行（不阻塞建群返回）
        # 依据：实测 sync_pm_bitable 4.51s + transition 2.43s = 6.94s，且后续发收集表不依赖这两个操作完成
        def _async_postprocess(project_id, project, chat_id, record_id):
            try:
                if project.pm_open_id:
                    self._sync_pm_to_bitable(project_id, project.pm_open_id, chat_id=chat_id)
                self.state_engine.transition(project_id, "运行中", record_id=record_id)
            except Exception as e:
                logger.error(f"建群后置处理(sync_pm+transition)失败: {e}")
        threading.Thread(target=_async_postprocess, args=(project_id, project, chat_id, record_id), daemon=True).start()
        logger.info(f"[PERF] create_project_group 总耗时 {time.time()-_t_total:.2f}s (sync_pm+transition已异步)")
        return {"chat_id": chat_id, "status": "completed"}

    def _do_create_chat(self, project, record_id=None):
        """第1步：创建群聊"""
        group_name = _make_group_name(project.project_short_name)
        try:
            data = self.chat_client.create_chat(
                name=group_name,
                description=f"项目管理Agent群 - {project.project_name}",
            )
            chat_id = data.get("chat_id", "")
            if not chat_id:
                raise GroupCreationError("create_chat", "未返回chat_id")
            self.state_engine.update_project_chat_id(project.project_id, chat_id, record_id=record_id)
            logger.info(f"群聊创建成功: {group_name} -> {chat_id}")
            return chat_id
        except FeishuAPIError as e:
            if "已存在" in e.msg or "duplicate" in e.msg.lower():
                raise GroupCreationError("create_chat", f"群名 '{group_name}' 已存在，请修改项目简称")
            raise GroupCreationError("create_chat", str(e))

    def _do_add_members(self, project, chat_id, open_id_list):
        """第2步：批量拉人入群，失败的跳过并记录"""
        if not open_id_list:
            return {"success": [], "failed": []}

        success_ids = []
        failed_ids = []
        batch_size = 50

        for i in range(0, len(open_id_list), batch_size):
            batch = open_id_list[i:i + batch_size]
            try:
                self.chat_client.add_members(chat_id, batch)
                success_ids.extend(batch)
            except FeishuAPIError as e:
                logger.warning(f"批量拉人失败，改为逐个拉入: {e}")
                for oid in batch:
                    try:
                        self.chat_client.add_members(chat_id, [oid])
                        success_ids.append(oid)
                    except FeishuAPIError:
                        failed_ids.append(oid)

        if failed_ids:
            self.im_client.send_text(
                chat_id,
                f"以下成员拉入失败（open_id无效或无权限），请手工拉入：\n"
                + "\n".join(failed_ids)
            )

        logger.info(f"成员拉入完成: 成功{len(success_ids)}人, 失败{len(failed_ids)}人")
        return {"success": success_ids, "failed": failed_ids}

    def _do_deploy_agent(self, project, chat_id, max_retries=3):
        """Agent入群：用PM的user_token + app_id将机器人添加到群聊"""
        for attempt in range(max_retries):
            try:
                self.chat_client.add_bot_to_chat(chat_id)
                logger.info(f"Agent入群成功: {chat_id}")
                return True
            except FeishuAPIError as e:
                logger.warning(f"Agent入群失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    import time
                    time.sleep(3)

        app_id = self.config['feishu']['app_id']
        logger.error(f"Agent入群失败，PM需手动添加机器人 (App ID: {app_id}): {chat_id}")
        return False

    def _send_agent_welcome(self, project, chat_id):
        """Agent入群后在群内发送欢迎消息"""
        group_name = _make_group_name(project.project_short_name)
        try:
            self.im_client.send_card(
                chat_id,
                self.card_builder.agent_welcome_card(project.project_name, group_name),
            )
            logger.info(f"Agent欢迎消息已发送到群: {chat_id}")
        except Exception as e:
            logger.warning(f"欢迎消息发送失败: {e}")

    def _sync_pm_to_bitable(self, project_id, pm_open_id, chat_id=""):
        """建群时将 PM 写入多维表格项目成员表"""
        import time as _time
        try:
            table_id = self.f01_tables['project_members']
            # 去重检查
            condition = {
                "conjunction": "and",
                "conditions": [
                    {"field_name": "项目ID", "operator": "is", "value": [project_id]},
                    {"field_name": "open_id", "operator": "is", "value": [pm_open_id]},
                ],
            }
            existing = self.base_client.list_all_records(
                self.f01_app_token, table_id, filter_condition=condition)
            if existing:
                logger.info(f"PM已存在于项目成员表，跳过: project_id={project_id}")
                return

            info = self.im_client.get_user_by_open_id(pm_open_id)
            member = Member(
                open_id=pm_open_id,
                name=info.get("name", "PM"),
                department=info.get("department", "") or "未知部门",
                role="PM",
                join_status="已入群",
                collected=True,
                added_time=int(_time.time() * 1000),
                project_id=project_id,
                chat_id=chat_id,
            )
            self.base_client.create_record(self.f01_app_token, table_id, member.to_bitable_fields())
            logger.info(f"PM已写入项目成员表: project_id={project_id}, pm={pm_open_id}")
        except Exception as e:
            logger.warning(f"PM写入项目成员表失败: {e}")

    def _start_member_collection(self, project):
        """发送成员收集卡片到PM"""
        card = self._build_collection_card(project)
        pm_chat = self._get_pm_chat_id(project.pm_open_id)
        if pm_chat:
            self.im_client.send_card(pm_chat, card)
        return {"status": "collecting", "project_id": project.project_id}

    def _build_collection_card(self, project):
        """构建成员收集交互卡片"""
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"📋 成员收集 - {project.project_name}"},
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                    "content": f"**项目简称：** {project.project_short_name}\n"
                              f"**PM：** <at email={project.pm_open_id}>请填写项目成员"}},
                {"tag": "div", "text": {"tag": "lark_md",
                    "content": "请在下方输入框中填写成员的姓名和部门，系统将自动解析open_id并拉入群聊。"}},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "快速建群"},
                     "type": "primary", "value": {"action": "quick_create", "project_id": project.project_id}},
                    {"tag": "button", "text": {"tag": "plain_text", "content": "取消"},
                     "type": "default", "value": {"action": "cancel", "project_id": project.project_id}},
                ]},
            ],
        }

    def _get_pm_chat_id(self, pm_open_id):
        """获取与PM的私聊chat_id（通过p2p消息）"""
        try:
            data = self.im_client._send_message(pm_open_id, "text", {"text": " "})
            return pm_open_id
        except Exception:
            return None

    # ======================== F-02: 解散归档 ========================

    def disband_project_group(self, project_id, operator_open_id="", dissolve=True):
        """解散群聊并归档"""
        # 防重入：同一个项目不能同时归档两次
        lock = self._disband_locks.setdefault(project_id, threading.Lock())
        if not lock.acquire(blocking=False):
            logger.warning(f"项目 {project_id} 正在归档中，跳过重复请求")
            return {"status": "already_processing"}
        try:
            return self._do_disband(project_id, operator_open_id, dissolve)
        finally:
            lock.release()

    def _do_disband(self, project_id, operator_open_id, dissolve):
        """解散群聊并归档"""
        project = self._get_project(project_id)
        if not project:
            raise ArchiveError("load_project", f"项目 {project_id} 不存在")
        if not project.chat_id:
            raise ArchiveError("check_chat", "项目无关联群聊")

        chat_id = project.chat_id
        archived_name = f"[已归档] {project.project_short_name[:MAX_SHORT_NAME_LENGTH]}"
        archive_log = ArchiveLog(
            project_id=project_id, chat_id=chat_id, operator=operator_open_id,
            group_name_before=project.group_name or project.project_short_name,
            group_name_after=archived_name,
        )
        log_record = self._create_archive_log(archive_log)

        try:
            self.state_engine.transition(project_id, "归档中")
        except Exception:
            pass

        results = {}
        _t_total = time.time()

        # 第1步：发送归档通知
        _t = time.time()
        self._archive_notify(chat_id, project.project_short_name)
        logger.info(f"[PERF] _do_disband 步①notify 耗时 {time.time()-_t:.2f}s")
        results["notify"] = "done"

        # 第2步：归档结构化数据索引
        _t = time.time()
        self._archive_records_index(project_id, chat_id)
        logger.info(f"[PERF] _do_disband 步②records_index 耗时 {time.time()-_t:.2f}s")
        results["records_indexed"] = "done"

        # 第3步：识别模式 → 解散 or 改群名+Agent退群
        _t = time.time()
        if dissolve:
            self.chat_client.disband_chat(chat_id)
            results["chat_disbanded"] = True
        else:
            self.chat_client.update_chat(chat_id, name=archived_name)
            self._leave_chat(chat_id, operator_open_id)
            results["left_chat"] = True
        logger.info(f"[PERF] _do_disband 步④{'disband' if dissolve else 'rename+leave'} 耗时 {time.time()-_t:.2f}s")

        # 第5步：更新状态
        _t = time.time()
        self.state_engine.transition(project_id, "已归档")
        self._update_archive_log(log_record["record_id"], agent_exit_status="已完成",
                                 error_message="")
        logger.info(f"[PERF] _do_disband 步⑤update_status 耗时 {time.time()-_t:.2f}s")
        logger.info(f"[PERF] _do_disband 总耗时 {time.time()-_t_total:.2f}s")
        results["status"] = "completed"
        return results

    def _archive_notify(self, chat_id, project_name):
        """退群前发送归档通知"""
        self.im_client.send_text(
            chat_id,
            f"📦 项目群 [{project_name}] 即将归档。\n"
            f"聊天记录和结构化数据已备份，群名将修改为 [已归档] {project_name}。"
        )

    def _archive_records_index(self, project_id, chat_id):
        """记录归档元数据到归档日志表"""
        pass

    def _leave_chat(self, chat_id, pm_open_id=""):
        """Agent主动退群（飞书API支持机器人自己移出自己）"""
        try:
            app_id = self.config['feishu']['app_id']
            self.chat_client.remove_members(chat_id, [app_id], id_type="app_id")
            logger.info("Agent已退出群聊")
        except FeishuAPIError as e:
            logger.warning(f"Agent退群失败: {e}")


    # ======================== 工具方法 ========================

    def _get_project(self, project_id):
        table_id = self.f01_tables['project_config']
        record = self.base_client.find_record_by_field(
            self.f01_app_token, table_id, "project_id", project_id
        )
        if not record:
            return None
        return Project.from_bitable_record(record)

    def list_active_projects(self, pm_open_id=None):
        """列出活跃项目（状态 != 已归档），可按PM过滤"""
        table_id = self.f01_tables['project_config']
        records = self.base_client.list_all_records(self.f01_app_token, table_id)
        projects = []
        for r in records:
            p = Project.from_bitable_record(r)
            if p.status == "已归档":
                continue
            if pm_open_id and p.pm_open_id != pm_open_id:
                continue
            projects.append({
                "project_id": p.project_id,
                "project_name": p.project_name,
                "project_abbr": p.project_short_name,
                "chat_id": p.chat_id,
                "status": p.status,
            })
        return projects

    def _create_archive_log(self, archive_log):
        table_id = self.f01_tables['archive_log']
        result = self.base_client.create_record(
            self.f01_app_token, table_id, archive_log.to_bitable_fields()
        )
        # create_record 返回 {"record": {"record_id": "recXXX", ...}}
        record = result.get("record", result) if isinstance(result, dict) else {}
        return {"record_id": record.get("record_id", "")}

    def _update_archive_log(self, record_id, agent_exit_status=None, error_message=None):
        table_id = self.f01_tables['archive_log']
        fields = {}
        if agent_exit_status:
            fields["agent_exit_status"] = agent_exit_status
        if error_message is not None:
            fields["error_message"] = error_message
        if fields:
            self.base_client.update_record(self.f01_app_token, table_id, record_id, fields)

    def find_duplicate_project(self, project_name, project_abbr, exclude_project_id=""):
        """在活跃项目中查重（已归档项目不占位，名称/简称可复用）。

        - 项目名称：去首尾空格后精确匹配；
        - 项目简称：去首尾空格后不区分大小写匹配；
        返回 (冲突字段, 已存在的Project)；无冲突返回 (None, None)。
        """
        table_id = self.f01_tables['project_config']
        records = self.base_client.list_all_records(self.f01_app_token, table_id)

        name_key = project_name.strip()
        abbr_key = project_abbr.strip().lower()
        for r in records:
            p = Project.from_bitable_record(r)
            if exclude_project_id and p.project_id == exclude_project_id:
                continue
            if p.status == "已归档":
                continue
            if p.project_name.strip() == name_key:
                return "项目名称", p
            if p.project_short_name.strip().lower() == abbr_key:
                return "项目简称", p
        return None, None

    def register_project(self, project_name, project_short_name, pm_open_id, description=""):
        """注册新项目到项目配置表"""
        import uuid
        _t_total = time.time()
        # 兜底：建记录前再次查重，防止并发提交漏过前置检查
        _t = time.time()
        dup_field, dup_proj = self.find_duplicate_project(
            project_name, project_short_name or project_name[:MAX_SHORT_NAME_LENGTH])
        logger.info(f"[PERF] register_project 步①find_duplicate 耗时 {time.time()-_t:.2f}s")
        if dup_field:
            from src.models.exceptions import DuplicateProjectError
            raise DuplicateProjectError(
                dup_field,
                project_name if dup_field == "项目名称" else project_short_name,
                dup_proj.project_id)

        project_id = f"P{uuid.uuid4().hex[:8].upper()}"
        project = Project(
            project_id=project_id,
            project_name=project_name,
            project_short_name=project_short_name or project_name[:MAX_SHORT_NAME_LENGTH],
            pm_open_id=pm_open_id,
            description=description,
        )
        table_id = self.f01_tables['project_config']
        fields = project.to_bitable_fields()
        _t = time.time()
        try:
            resp = self.base_client.create_record(self.f01_app_token, table_id, fields)
            # create_record 返回 {"record": {"record_id": "recXXX"}}
            if isinstance(resp, dict):
                project.record_id = resp.get("record", {}).get("record_id", "")
        except FeishuAPIError as e:
            if "UserFieldConvFail" in str(e):
                logger.warning(f"PM人员字段转换失败，移除PM字段后重试: {e}")
                fields.pop("PM", None)
                resp = self.base_client.create_record(self.f01_app_token, table_id, fields)
                if isinstance(resp, dict):
                    project.record_id = resp.get("record", {}).get("record_id", "")
            else:
                raise
        logger.info(f"[PERF] register_project 步②create_record 耗时 {time.time()-_t:.2f}s")
        logger.info(f"[PERF] register_project 总耗时 {time.time()-_t_total:.2f}s (record_id={project.record_id})")
        logger.info(f"项目注册成功: {project_id} - {project_name}")
        return project

    # 建群链路等待上限（秒）：create_chat/add_members/deploy(3轮重试)/welcome 均可能慢
    _CHAIN_WAIT_TIMEOUT = 300

    def quick_register_and_create_group(self, project_name, project_short_name,
                                        pm_open_id, member_open_ids=None, description=""):
        """快速注册+建群并行编排（热路径）。

        依赖分析：create_record 与 create_chat 互不依赖（建群只需项目名/简称，均在内存
        对象中），故分两条 daemon 线程并行；多维表收尾（group_id 回写/PM 成员/状态流转）
        在线程内等待 chat_id 就绪后完成，不阻塞建群返回。查重仍同步执行，保持
        DuplicateProjectError 语义。项目记录初始状态直接写「收集中」，省掉一次 transition。

        Returns:
            (Project, {"chat_id": str, "status": "completed"})
        Raises:
            DuplicateProjectError: 查重命中或同名项目正在注册中
            GroupCreationError: 建群链路失败（此时记录可能已写入，与历史行为一致）
        """
        import uuid as _uuid
        from src.models.exceptions import DuplicateProjectError
        _t_total = time.time()

        # 1. 同步查重（必须先于一切副作用）
        abbr = project_short_name or project_name[:MAX_SHORT_NAME_LENGTH]
        _t = time.time()
        dup_field, dup_proj = self.find_duplicate_project(project_name, abbr)
        logger.info(f"[PERF] parallel_register_create 步①find_duplicate 耗时 {time.time()-_t:.2f}s")
        if dup_field:
            raise DuplicateProjectError(
                dup_field,
                project_name if dup_field == "项目名称" else project_short_name,
                dup_proj.project_id)

        # 2. 进程内占位：堵住「查重通过→多维表记录可见」窗口（约3-4s）的并发重复提交
        dedup_key = (project_name.strip(), abbr.strip().lower())
        with self._register_lock:
            if dedup_key in self._pending_registrations:
                raise DuplicateProjectError("项目名称", project_name, "（正在创建中）")
            self._pending_registrations.add(dedup_key)

        # 3. 构造内存项目（状态直接「收集中」，无需再调一次 transition）
        project = Project(
            project_id=f"P{_uuid.uuid4().hex[:8].upper()}",
            project_name=project_name,
            project_short_name=abbr,
            pm_open_id=pm_open_id,
            description=description,
            status="收集中",
        )

        chat_box = {}          # {"chat_id": ...} 或 {"error": exc}
        chat_ready = threading.Event()

        # ---- T_record：项目记录写入 → 等群就绪 → 多维表收尾 ----
        def _record_worker():
            record_id = ""
            table_id = self.f01_tables['project_config']
            fields = project.to_bitable_fields()
            try:
                for attempt in range(3):
                    try:
                        _t = time.time()
                        resp = self.base_client.create_record(
                            self.f01_app_token, table_id, fields)
                        if isinstance(resp, dict):
                            record_id = resp.get("record", {}).get("record_id", "")
                        project.record_id = record_id
                        logger.info(f"[PERF] parallel_register_create T记录 create_record 耗时 {time.time()-_t:.2f}s")
                        break
                    except FeishuAPIError as e:
                        # UserFieldConvFail 兜底：移除 PM 字段重试
                        if "UserFieldConvFail" in str(e) and "PM" in fields:
                            logger.warning(f"PM人员字段转换失败，移除PM字段后重试: {e}")
                            fields.pop("PM", None)
                            continue
                        if attempt < 2:
                            logger.warning(f"项目记录写入失败(第{attempt+1}次)，2s后重试: {e}")
                            time.sleep(2)
                            continue
                        raise
            except Exception as e:
                logger.error(f"项目记录写入最终失败（群仍将创建，需人工补录）: {e}", exc_info=True)
            finally:
                # 记录写入成功后 find_duplicate 即可见；彻底失败也放行允许用户重试
                with self._register_lock:
                    self._pending_registrations.discard(dedup_key)

            chat_ready.wait(timeout=self._CHAIN_WAIT_TIMEOUT)
            chat_id = chat_box.get("chat_id", "")

            if not record_id:
                # B计划：记录缺失 → PM私聊告警，群照常可用
                logger.error(
                    f"项目 {project.project_id} 多维表记录缺失，chat_id={chat_id}，需人工补录")
                if pm_open_id:
                    try:
                        self.im_client.send_text_to_user(
                            pm_open_id,
                            f"⚠ 项目群已创建，但项目「{project_name}」写入多维表格失败，"
                            f"请联系管理员补录（项目编号 {project.project_id}）。")
                    except Exception as warn_err:
                        logger.error(f"PM补录告警发送失败: {warn_err}")
                return

            if not chat_id:
                # 建群失败：记录保留（与历史行为一致），不回写 group_id、不流转运行中
                logger.warning(f"项目 {project.project_id} 记录已写入但建群失败，等待人工处理")
                return

            try:
                self.state_engine.update_project_chat_id(
                    project.project_id, chat_id, record_id=record_id)
            except Exception as e:
                logger.error(f"回写group_id失败: {e}")
            try:
                if pm_open_id:
                    self._sync_pm_to_bitable(project.project_id, pm_open_id, chat_id=chat_id)
                self.state_engine.transition(
                    project.project_id, "运行中", record_id=record_id)
            except Exception as e:
                logger.error(f"建群收尾(sync_pm+transition)失败: {e}")

        # ---- T_chat：建群链路（纯 IM API，不依赖多维表）----
        def _chat_worker():
            try:
                group_name = _make_group_name(project.project_short_name)
                _t = time.time()
                data = self.chat_client.create_chat(
                    name=group_name,
                    description=f"项目管理Agent群 - {project.project_name}",
                )
                chat_id = data.get("chat_id", "") if isinstance(data, dict) else ""
                if not chat_id:
                    raise GroupCreationError("create_chat", "未返回chat_id")
                logger.info(f"[PERF] parallel_register_create T建群 create_chat 耗时 {time.time()-_t:.2f}s")

                all_members = list(member_open_ids or [])
                if pm_open_id and pm_open_id not in all_members:
                    all_members.insert(0, pm_open_id)
                if all_members:
                    _t = time.time()
                    self._do_add_members(project, chat_id, all_members)
                    logger.info(f"[PERF] parallel_register_create T建群 add_members({len(all_members)}人) 耗时 {time.time()-_t:.2f}s")
                _t = time.time()
                self._do_deploy_agent(project, chat_id)
                logger.info(f"[PERF] parallel_register_create T建群 deploy_agent 耗时 {time.time()-_t:.2f}s")
                _t = time.time()
                self._send_agent_welcome(project, chat_id)
                logger.info(f"[PERF] parallel_register_create T建群 send_welcome 耗时 {time.time()-_t:.2f}s")
                chat_box["chat_id"] = chat_id
            except FeishuAPIError as e:
                gname = _make_group_name(project.project_short_name)
                if "已存在" in e.msg or "duplicate" in e.msg.lower():
                    chat_box["error"] = GroupCreationError(
                        "create_chat", f"群名 '{gname}' 已存在，请修改项目简称")
                else:
                    chat_box["error"] = GroupCreationError("create_chat", str(e))
            except Exception as e:
                # GroupCreationError（如未返回chat_id）保持原样，其余包装
                chat_box["error"] = e if isinstance(e, GroupCreationError) \
                    else GroupCreationError("create_chat", str(e))
            finally:
                chat_ready.set()

        threading.Thread(target=_record_worker, daemon=True).start()
        threading.Thread(target=_chat_worker, daemon=True).start()

        # 主流程只等建群链路（关键路径）；记录写入与收尾在后台继续
        if not chat_ready.wait(timeout=self._CHAIN_WAIT_TIMEOUT):
            raise GroupCreationError("create_chat", "建群超时，请稍后在多维表格核对或重试")
        logger.info(f"[PERF] parallel_register_create 关键路径(查重+建群) 耗时 {time.time()-_t_total:.2f}s")
        if "error" in chat_box:
            raise chat_box["error"]
        return project, {"chat_id": chat_box.get("chat_id", ""), "status": "completed"}

    def add_project_members(self, project_id, members, dept_name="",
                            lead_name="", lead_open_id="", chat_id=""):
        """批量写入项目成员到「项目成员表」。

        Args:
            project_id: 项目ID
            members: list[dict]，每个含 name, open_id, dept, position
            dept_name: 部门名称
            lead_name: 部门负责人姓名
            lead_open_id: 部门负责人 open_id（用于 User 类型字段）
            chat_id: 群聊ID
        """
        import time as _time
        table_id = self.f01_tables['project_members']
        records = []
        now_ms = int(_time.time() * 1000)
        for m in members:
            member = Member(
                open_id=m.get("open_id", ""),
                name=m.get("name", "未知"),
                department=m.get("dept", "") or dept_name or "未知部门",
                department_lead=lead_open_id,
                department_lead_name=lead_name,
                role="成员",
                join_status="已入群",
                collected=True,
                added_time=now_ms,
                project_id=project_id,
                chat_id=chat_id,
            )
            records.append(member.to_bitable_fields())

        if not records:
            return 0

        # 去重：检查 open_id + 项目ID 是否已存在，避免重复写入
        existing_ids = set()
        try:
            condition = {
                "conjunction": "and",
                "conditions": [
                    {"field_name": "项目ID", "operator": "is", "value": [project_id]}
                ],
            }
            existing_records = self.base_client.list_all_records(
                self.f01_app_token, table_id, filter_condition=condition)
            for r in existing_records:
                f = r.get("fields", {})
                oid = self._extract_text(f.get("open_id", ""))
                if oid:
                    existing_ids.add(oid)
        except Exception as e:
            logger.warning(f"查询已有成员失败，跳过去重: {e}")

        new_records = [r for r in records if r.get("open_id") not in existing_ids]
        if not new_records:
            logger.info(f"所有成员已存在于项目成员表，跳过: project_id={project_id}")
            return 0

        try:
            self.base_client.batch_create_records(self.f01_app_token, table_id, new_records)
            logger.info(f"写入项目成员表成功: project_id={project_id}, count={len(new_records)}, 跳过重复={len(records)-len(new_records)}")
            return len(new_records)
        except FeishuAPIError as e:
            logger.error(f"写入项目成员表失败: {e}")
            return 0

    @staticmethod
    def _extract_text(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in value)
        return str(value) if value else ""
