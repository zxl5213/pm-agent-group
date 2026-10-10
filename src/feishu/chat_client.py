import logging

from src.feishu.client import FeishuClient
from src.models.exceptions import FeishuAPIError

logger = logging.getLogger(__name__)


class ChatClient(FeishuClient):
    """群聊管理API封装"""

    def get_bot_open_id(self):
        """获取机器人的open_id（使用tenant_access_token）"""
        data = self._request_raw("GET", "/bot/v3/info", token_type="tenant")
        if data.get("code") == 0:
            bot = data.get("bot", {})
            open_id = bot.get("open_id", "")
            if open_id:
                logger.info(f"机器人信息: name={bot.get('app_name')}, open_id={open_id}")
                return open_id
        raise FeishuAPIError(data.get("code", -1), data.get("msg", "Failed to get bot open_id"))

    def add_bot_to_chat(self, chat_id):
        """将机器人添加到群聊：用PM的user_token + app_id添加（member_id_type须作为query参数）"""
        return self._request(
            "POST", f"/im/v1/chats/{chat_id}/members",
            params={"member_id_type": "app_id"},
            json_body={"id_list": [self.app_id]},
            token_type="user",
        )

    def create_chat(self, name, description="", owner_id="", chat_mode="group",
                    chat_type="private", external=False):
        path = "/im/v1/chats"
        body = {
            "name": name,
            "description": description,
            "chat_mode": chat_mode,
            "chat_type": chat_type,
            "external": external,
        }
        if owner_id:
            body["owner_id"] = owner_id
            body["user_id_type"] = "open_id"
        return self._request("POST", path, json_body=body)

    def get_chat_info(self, chat_id):
        path = f"/im/v1/chats/{chat_id}"
        return self._request("GET", path)

    def update_chat(self, chat_id, name=None, description=None):
        path = f"/im/v1/chats/{chat_id}"
        body = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        return self._request("PUT", path, json_body=body)

    def disband_chat(self, chat_id):
        path = f"/im/v1/chats/{chat_id}"
        return self._request("DELETE", path)

    def get_bot_info(self):
        """获取机器人自身信息（open_id）"""
        path = "/bot/v3/info"
        return self._request("GET", path, token_type="tenant")

    def transfer_ownership(self, chat_id, new_owner_open_id):
        """转让群主"""
        path = f"/im/v1/chats/{chat_id}/owners"
        body = {"owner_id": new_owner_open_id, "member_id_type": "open_id"}
        return self._request("POST", path, json_body=body)

    def add_members(self, chat_id, id_list, id_type="open_id"):
        path = f"/im/v1/chats/{chat_id}/members"
        return self._request("POST", path, params={"member_id_type": id_type}, json_body={"id_list": id_list})

    def remove_members(self, chat_id, open_id_list, id_type="open_id"):
        path = f"/im/v1/chats/{chat_id}/members"
        params = {"member_id_type": id_type}
        body = {"id_list": open_id_list}
        return self._request("DELETE", path, params=params, json_body=body)

    def get_members(self, chat_id, page_size=50, page_token=""):
        path = f"/im/v1/chats/{chat_id}/members"
        params = {"page_size": page_size, "member_id_type": "open_id"}
        if page_token:
            params["page_token"] = page_token
        return self._request("GET", path, params=params)

    def get_all_members(self, chat_id):
        members = []
        page_token = ""
        while True:
            data = self.get_members(chat_id, page_token=page_token)
            members.extend(data.get("items", []))
            page_token = data.get("page_token", "")
            if not data.get("has_more", False):
                break
        return members
