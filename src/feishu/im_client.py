import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from src.feishu.client import FeishuClient

logger = logging.getLogger(__name__)

# 卡片注册表磁盘路径（data/ 已在 .gitignore）
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CARD_REGISTRY_FILE = os.path.join(_PROJECT_ROOT, "data", "card_registry.json")
_CARD_REGISTRY_MAX = 1000   # 最多保留最近1000条映射


class IMClient(FeishuClient):
    """消息发送API封装"""

    # 类级缓存：用户信息 open_id -> (info, expire_at)；部门 dept_id -> (name, expire_at)
    # 避免每次添加/移除都重复请求（部门无权限时也缓存失败结果，不再干等1秒）
    _user_cache = {}
    _dept_cache = {}
    _USER_CACHE_TTL = 1800      # 用户信息成功缓存30分钟
    _USER_FAIL_TTL = 120        # 查询失败缓存2分钟
    _DEPT_CACHE_TTL = 3600      # 部门名成功缓存1小时
    _DEPT_FAIL_TTL = 60         # 部门无权限等失败缓存1分钟（授权恢复后很快自动重试）

    def _card_registry(self):
        """消息 message_id -> card_id 的映射（懒加载磁盘注册表，
        服务重启后仍可定位历史卡片）。"""
        if not hasattr(self, "_card_msg_map"):
            self._card_msg_map = self._load_card_registry()
            self._card_seq = {}          # card_id -> 已用序号（内存即可）
        return self._card_msg_map

    def _load_card_registry(self):
        """从磁盘加载注册表；文件不存在或损坏时返回空映射。"""
        try:
            with open(_CARD_REGISTRY_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"加载卡片注册表失败: {e}")
            return {}

    def _save_card_registry(self):
        """原子写盘，避免并发/中断造成文件损坏。"""
        try:
            os.makedirs(os.path.dirname(_CARD_REGISTRY_FILE), exist_ok=True)
            tmp_path = _CARD_REGISTRY_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._card_msg_map, f, ensure_ascii=False)
            os.replace(tmp_path, _CARD_REGISTRY_FILE)
        except Exception as e:
            logger.warning(f"持久化卡片注册表失败: {e}")

    def _register_sent_card(self, resp, card_id):
        """发卡成功后登记 message_id -> card_id 并落盘，供回调精确定位卡片。"""
        if isinstance(resp, dict):
            mid = resp.get("message_id", "")
            if mid:
                mapping = self._card_registry()
                mapping[mid] = card_id
                # 超出上限时淘汰最早的记录（dict 保持插入顺序）
                overflow = len(mapping) - _CARD_REGISTRY_MAX
                for old_mid in list(mapping)[:max(overflow, 0)]:
                    mapping.pop(old_mid)
                self._save_card_registry()

    def resolve_card_id(self, message_id):
        """按被交互消息ID查 cardkit card_id；查不到返回空串。"""
        if not message_id:
            return ""
        return self._card_registry().get(message_id, "")

    def send_text(self, chat_id, text):
        return self._send_message(chat_id, "text", {"text": text})

    def send_card(self, chat_id, card_content):
        if isinstance(card_content, dict):
            card_content = json.dumps(card_content)
        return self._send_message(chat_id, "interactive", card_content)

    def send_text_to_user(self, open_id, text):
        """发送P2P文本消息给指定用户（机器人身份）"""
        path = "/im/v1/messages"
        body = {
            "receive_id": open_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        params = {"receive_id_type": "open_id"}
        return self._request("POST", path, params=params, json_body=body, token_type="tenant")

    def send_card_to_user(self, open_id, card_content):
        """发送P2P卡片消息给指定用户（机器人身份）"""
        if isinstance(card_content, dict):
            card_content = json.dumps(card_content)
        path = "/im/v1/messages"
        body = {
            "receive_id": open_id,
            "msg_type": "interactive",
            "content": card_content,
        }
        params = {"receive_id_type": "open_id"}
        return self._request("POST", path, params=params, json_body=body, token_type="tenant")

    def update_card(self, message_id, card_content):
        """更新已发送的卡片消息"""
        if isinstance(card_content, dict):
            card_content = json.dumps(card_content)
        path = f"/im/v1/messages/{message_id}"
        body = {"content": card_content}
        return self._request("PATCH", path, json_body=body, token_type="tenant")

    def create_card_entity(self, card):
        """创建 cardkit 卡片实体，返回 card_id。

        卡片实体仅能被发送一次（有效期14天），故每次发送前创建。
        要求 schema 2.0、update_multi=true。
        """
        if isinstance(card, dict):
            card = json.dumps(card, ensure_ascii=False)
        resp = self._request(
            "POST", "/cardkit/v1/cards",
            json_body={"type": "card_json", "data": card},
            token_type="tenant")
        return resp["card_id"]

    def send_cardkit(self, chat_id, card):
        """以 cardkit 实体方式向群发卡片：建实体 → 发送。
        返回发送结果并附带 card_id（供后续 batch_update）。
        """
        card_id = self.create_card_entity(card)
        content = json.dumps({"type": "card", "data": {"card_id": card_id}})
        resp = self._send_message(chat_id, "interactive", content)
        if isinstance(resp, dict):
            resp["card_id"] = card_id
        self._register_sent_card(resp, card_id)
        return resp

    def send_cardkit_to_user(self, open_id, card):
        """以 cardkit 实体方式向指定用户发卡片，返回结果并附带 card_id。"""
        card_id = self.create_card_entity(card)
        content = json.dumps({"type": "card", "data": {"card_id": card_id}})
        resp = self.send_card_to_user(open_id, content)
        if isinstance(resp, dict):
            resp["card_id"] = card_id
        self._register_sent_card(resp, card_id)
        return resp

    def batch_update_card(self, card_id, actions, sequence=None):
        """对 cardkit 卡片实体执行局部更新（增删组件、改属性）。

        Args:
            actions: list[dict]，每项 {"action":..., "params":{...}}
            sequence: 严格递增的正整数；不传则按 card_id 自动递增
        """
        self._card_registry()
        if sequence is None:
            sequence = self._card_seq.get(card_id, 0) + 1
        self._card_seq[card_id] = sequence
        path = f"/cardkit/v1/cards/{card_id}/batch_update"
        body = {
            "sequence": sequence,
            "actions": json.dumps(actions, ensure_ascii=False),
        }
        return self._request("POST", path, json_body=body, token_type="tenant")

    def reply_text(self, message_id, text):
        return self._reply_message(message_id, "text", {"text": text})

    def reply_card(self, message_id, card_content):
        if isinstance(card_content, dict):
            card_content = json.dumps(card_content)
        return self._reply_message(message_id, "interactive", card_content)

    def get_messages(self, chat_id, page_size=50, page_token=""):
        path = f"/im/v1/chats/{chat_id}/messages"
        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        return self._request("GET", path, params=params, token_type="tenant")

    def _send_message(self, chat_id, msg_type, content):
        path = "/im/v1/messages"
        body = {
            "receive_id": chat_id,
            "msg_type": msg_type,
            "content": json.dumps(content) if isinstance(content, dict) else content,
        }
        params = {"receive_id_type": "chat_id"}
        return self._request("POST", path, params=params, json_body=body, token_type="tenant")

    def _reply_message(self, message_id, msg_type, content):
        path = f"/im/v1/messages/{message_id}/reply"
        body = {
            "msg_type": msg_type,
            "content": json.dumps(content) if isinstance(content, dict) else content,
        }
        return self._request("POST", path, json_body=body, token_type="tenant")

    def list_scope_users(self, force_refresh=False):
        """遍历应用可见范围的整棵部门树，收集全部用户（机器人身份）。

        scopes 接口的 user_ids 名单同步可能长期延迟，因此不依赖它：
        1. scopes 取根部门（open_department_id）；
        2. 每个根递归拉子部门（fetch_child）；
        3. 逐部门列出用户并去重。

        Returns:
            list: [{"open_id", "name", "department", "job_title"}]
        """
        cache = getattr(IMClient, "_scope_users_cache", None)
        if cache is None:
            cache = {"users": [], "expire_at": 0}
            IMClient._scope_users_cache = cache
        now = time.time()
        if not force_refresh and cache["users"] and now < cache["expire_at"]:
            logger.info("[PERF] list_scope_users 缓存命中")
            return cache["users"]
        logger.info("[PERF] list_scope_users 缓存未命中，开始遍历部门树")
        _t_total = time.time()

        # 1. 根部门
        _t = time.time()
        data = self._request("GET", "/contact/v3/scopes",
                             params={"page_size": 100,
                                     "department_id_type": "open_department_id"},
                             token_type="tenant")
        root_ids = data.get("department_ids", []) or []
        logger.info(f"[PERF] list_scope_users 阶段①scopes({len(root_ids)}根部门) 耗时 {time.time()-_t:.2f}s")
        if not root_ids:
            logger.info("[PERF] list_scope_users 总耗时 0.00s, 应用可见范围无根部门")
            return []

        # 2. 收集全部部门ID（根 + 递归子部门），多根部门并发递归
        _t = time.time()

        def _fetch_child_depts(root):
            ids = [root]
            page_token = ""
            while True:
                params = {"parent_department_id": root,
                          "department_id_type": "open_department_id",
                          "fetch_child": True, "page_size": 50}
                if page_token:
                    params["page_token"] = page_token
                try:
                    d = self._request(
                        "GET", "/contact/v3/departments",
                        params=params, token_type="tenant")
                    for it in d.get("items", []):
                        oid = it.get("open_department_id", "")
                        if oid:
                            ids.append(oid)
                    if not d.get("has_more"):
                        break
                    page_token = d.get("page_token", "")
                except Exception as e:
                    logger.warning(f"递归子部门失败 (root={root}): {e}")
                    break
            return ids

        # ex.map 保持输入顺序，结果稳定
        with ThreadPoolExecutor(max_workers=min(len(root_ids), 5)) as ex:
            root_results = list(ex.map(_fetch_child_depts, root_ids))
        all_dept_ids = []
        for ids in root_results:
            all_dept_ids.extend(ids)

        # 3. 各部门并发列用户，主线程按部门顺序合并并按open_id去重
        logger.info(f"[PERF] list_scope_users 阶段②departments({len(all_dept_ids)}部门) 耗时 {time.time()-_t:.2f}s")
        _t = time.time()

        def _fetch_dept_users(did):
            """拉取单个部门下全部用户（分页）；部门名查询有类级缓存，并发安全"""
            dept_name = self._get_department_name(did)
            dept_users = []
            page_token = ""
            while True:
                params = {"department_id": did,
                          "department_id_type": "open_department_id",
                          "user_id_type": "open_id",
                          "page_size": 50}
                if page_token:
                    params["page_token"] = page_token
                try:
                    d = self._request(
                        "GET", "/contact/v3/users",
                        params=params, token_type="tenant")
                    for u in d.get("items", []):
                        oid = u.get("open_id", "")
                        if oid:
                            dept_users.append({
                                "open_id": oid,
                                "name": u.get("name", ""),
                                "department": dept_name,
                                "job_title": u.get("job_title", ""),
                            })
                    if not d.get("has_more"):
                        break
                    page_token = d.get("page_token", "")
                except Exception as e:
                    logger.warning(f"列部门用户失败 (dept={did}): {e}")
                    break
            return dept_users

        with ThreadPoolExecutor(max_workers=min(len(all_dept_ids), 8)) as ex:
            dept_user_lists = list(ex.map(_fetch_dept_users, all_dept_ids))

        seen = set()
        users = []
        for dept_users in dept_user_lists:
            for u in dept_users:
                if u["open_id"] not in seen:
                    seen.add(u["open_id"])
                    users.append(u)

        logger.info(f"[PERF] list_scope_users 阶段③users({len(users)}人) 耗时 {time.time()-_t:.2f}s")
        cache["users"] = users
        cache["expire_at"] = now + 600
        logger.info(f"[PERF] list_scope_users 总耗时 {time.time()-_t_total:.2f}s, 收集 {len(users)}人")
        return users

    def search_user_by_name(self, name):
        """通过姓名搜索飞书用户（用user_access_token调用通讯录搜索API）

        Returns:
            dict: {"open_id": "ou_xxx", "name": "张伟", "department": "前端开发部", "job_title": "工程师"} 或 None
        """
        try:
            data = self._request(
                "POST", "/contact/v3/users/search",
                params={"page_size": 3, "user_id_type": "open_id"},
                json_body={"query": name},
                token_type="user",
                max_retries=2,
                timeout_override=8,
            )
            items = data.get("items", [])
            if not items:
                return None
            # 取第一个匹配结果
            user = items[0]
            # 搜索接口返回 id 字段（即 open_id）
            open_id = user.get("id", "")
            meta = user.get("meta_data", {})
            user_name = meta.get("i18n_names", {}).get("zh_cn", "") or user.get("display_info", name)

            # 优先从 display_info 提取部门名称（格式: <h>姓名</h>\n部门名\n）
            department = ""
            display_info = user.get("display_info", "")
            if display_info:
                # 去掉 <h>姓名</h> 标签，剩余文本即为部门等附加信息
                parts = re.sub(r"<[^>]+>", "", display_info).strip().split("\n")
                if len(parts) > 1:
                    department = parts[1].strip()

            # 调用用户详情接口获取职位，同时作为部门的备用来源
            detail = self._get_user_detail(open_id)
            job_title = detail.get("job_title", "")
            if not department:
                department = detail.get("department", "")

            return {
                "open_id": open_id,
                "name": user_name,
                "department": department,
                "job_title": job_title,
            }
        except Exception as e:
            logger.warning(f"搜索用户 '{name}' 失败: {e}")
            return None

    def get_user_by_open_id(self, open_id):
        """通过open_id获取用户信息（姓名、部门、职位）— 带缓存，快速降级

        Returns:
            dict: {"name": "张伟", "department": "前端部", "job_title": "工程师", "open_id": "ou_xxx"}
        """
        now = time.time()
        cached = IMClient._user_cache.get(open_id)
        if cached and now < cached[1]:
            return cached[0]

        try:
            data = self._request(
                "GET", f"/contact/v3/users/{open_id}",
                params={"user_id_type": "open_id",
                        "department_id_type": "open_department_id"},
                token_type="tenant",
                max_retries=1,
                timeout_override=3,
            )
            user = data.get("user", {})
            dept_ids = user.get("department_ids", [])
            department = ""
            if dept_ids:
                department = self._get_department_name(dept_ids[0])
            info = {
                "name": user.get("name", ""),
                "department": department,
                "job_title": user.get("job_title", ""),
                "open_id": open_id,
            }
            IMClient._user_cache[open_id] = (info, now + IMClient._USER_CACHE_TTL)
            return info
        except Exception as e:
            logger.warning(f"获取用户详情失败 (open_id={open_id}): {e}")
            info = {"name": "", "department": "", "job_title": "", "open_id": open_id}
            IMClient._user_cache[open_id] = (info, now + IMClient._USER_FAIL_TTL)
            return info

    def _get_user_detail(self, open_id):
        """通过用户详情接口获取部门和职位 — 快速降级，3秒超时"""
        try:
            data = self._request(
                "GET", f"/contact/v3/users/{open_id}",
                params={"user_id_type": "open_id",
                        "department_id_type": "open_department_id"},
                token_type="tenant",
                max_retries=1,
                timeout_override=3,
            )
            user = data.get("user", {})
            dept_ids = user.get("department_ids", [])
            department = ""
            if dept_ids:
                department = self._get_department_name(dept_ids[0])
            return {
                "department": department,
                "job_title": user.get("job_title", ""),
            }
        except Exception as e:
            logger.warning(f"获取用户详情失败 (open_id={open_id}): {e}")
            return {"department": "", "job_title": ""}

    def create_task(self, summary, assignee_open_id, deadline_ts,
                    description="", reminder_minutes_before=60):
        """创建飞书任务（机器人身份 tenant_access_token）。

        Args:
            summary: 任务标题。
            assignee_open_id: 负责人 open_id。
            deadline_ts: 截止时间戳（秒）。
            description: 任务描述。
            reminder_minutes_before: 截止前提前提醒的分钟数（默认 60 分钟）。

        Returns:
            dict: 成功返回 {"task_guid": "...", "task_url": "..."}；
            失败返回 {}（调用方自行降级，如改发提醒消息）。
        """
        path = "/task/v2/tasks"
        body = {
            "summary": summary,
            "description": description,
            "due": {
                "timestamp": str(int(deadline_ts * 1000)),  # API 要求 ms 字符串
                "is_all_day": False,
            },
            "members": [
                {"id": assignee_open_id, "type": "user", "role": "assignee"}
            ],
            "reminders": [
                {"relative_fire_minute": reminder_minutes_before}
            ],
        }
        try:
            resp = self._request(
                "POST", path,
                params={"user_id_type": "open_id"},
                json_body=body, token_type="tenant",
            )
            task = resp.get("task", {}) if isinstance(resp, dict) else {}
            guid = task.get("guid", "")
            if not guid:
                logger.warning(f"create_task 响应缺少 task.guid: {resp}")
                return {}
            return {
                "task_guid": guid,
                "task_url": f"https://applink.feishu.cn/client/todo/detail?guid={guid}",
            }
        except Exception as e:
            logger.warning(f"create_task 失败（可能未开通 task:task:write 权限）: {e}")
            return {}

    def complete_task(self, task_guid):
        """标记飞书任务为已完成（机器人身份）。

        Args:
            task_guid: 任务的 GUID（create_task 返回的 task_guid）。

        Returns:
            bool: 是否成功标记。
        """
        if not task_guid:
            return False
        path = f"/task/v2/tasks/{task_guid}"
        body = {
            "task": {"completed_at": str(int(time.time() * 1000))},
            "update_fields": ["completed_at"],
        }
        try:
            self._request("PATCH", path, json_body=body, token_type="tenant")
            return True
        except Exception as e:
            logger.warning(f"complete_task 失败 (guid={task_guid}): {e}")
            return False

    def _get_department_name(self, dept_id):
        """通过部门ID获取部门名称 — 带缓存（含失败负缓存），3秒超时"""
        # 根部门 "0" 无查询权限，直接返回"公司"
        if dept_id == "0":
            return "公司"

        now = time.time()
        cached = IMClient._dept_cache.get(dept_id)
        if cached and now < cached[1]:
            return cached[0]

        try:
            data = self._request(
                "GET", f"/contact/v3/departments/{dept_id}",
                params={"department_id_type": "open_department_id"},
                token_type="tenant",
                max_retries=1,
                timeout_override=3,
            )
            dept = data.get("department", {})
            name = dept.get("name", "")
            ttl = IMClient._DEPT_CACHE_TTL if name else IMClient._DEPT_FAIL_TTL
            IMClient._dept_cache[dept_id] = (name, now + ttl)
            return name
        except Exception as e:
            logger.warning(f"获取部门信息失败 (dept_id={dept_id}): {e}")
            IMClient._dept_cache[dept_id] = ("", now + IMClient._DEPT_FAIL_TTL)
            return ""
