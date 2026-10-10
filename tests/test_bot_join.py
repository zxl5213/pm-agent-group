import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import load_config
from src.utils.logger import setup_logger
from src.feishu.chat_client import ChatClient
from src.models.exceptions import FeishuAPIError

config = load_config()
setup_logger(config)
logger = logging.getLogger(__name__)

print("=" * 60)
print("Agent入群测试（完整流程：建群 → 入群 → 验证）")
print("=" * 60)

chat_client = ChatClient(config)

# 第1步：验证tenant_access_token和app_secret配置
print("\n[1/4] 验证tenant_access_token...")
try:
    token = chat_client._get_tenant_token()
    print(f"  ✅ tenant_access_token获取成功: {token[:20]}...")
    print(f"  app_id: {chat_client.app_id}")
except Exception as e:
    print(f"  ❌ 获取tenant_access_token失败: {e}")
    sys.exit(1)

# 第2步：用user_token创建测试群聊（PM自动成为群主，即在群内）
print("\n[2/4] 创建测试群聊（PM身份）...")
try:
    chat_result = chat_client.create_chat(
        name="Agent入群测试群",
        description="用于测试Agent自动入群功能",
    )
    chat_id = chat_result.get("chat_id", "")
    if not chat_id:
        print(f"  ❌ 建群失败：未返回chat_id，响应: {chat_result}")
        sys.exit(1)
    print(f"  ✅ 群聊创建成功: chat_id={chat_id}")
except FeishuAPIError as e:
    print(f"  ❌ 建群失败: [{e.code}] {e.msg}")
    if e.context:
        print(f"  上下文: {e.context}")
    sys.exit(1)
except Exception as e:
    print(f"  ❌ 意外错误: {type(e).__name__}: {e}")
    sys.exit(1)

# 第3步：将机器人添加到群聊（使用app_id + user_token）
print(f"\n[3/4] 将机器人添加到群聊（app_id方式）...")
try:
    result = chat_client.add_bot_to_chat(chat_id)
    print(f"  ✅ Agent入群成功！")
except FeishuAPIError as e:
    print(f"  ❌ Agent入群失败: [{e.code}] {e.msg}")
    if e.context:
        print(f"  上下文: {e.context}")
except Exception as e:
    print(f"  ❌ 意外错误: {type(e).__name__}: {e}")

# 第4步：验证群成员列表
print(f"\n[4/4] 验证群成员列表...")
try:
    members = chat_client.get_all_members(chat_id)
    print(f"  群内共 {len(members)} 名成员:")
    for m in members:
        name = m.get("name", "未知")
        member_id = m.get("member_id", m.get("open_id", ""))
        m_type = m.get("member_type", "")
        print(f"    - {name} ({member_id}) type={m_type}")
    print("\n  ✅ 验证完成")
except FeishuAPIError as e:
    print(f"  ❌ 获取群成员失败: [{e.code}] {e.msg}")
except Exception as e:
    print(f"  ❌ 意外错误: {type(e).__name__}: {e}")

print("\n" + "=" * 60)
print("测试完成")
print(f"测试群聊ID: {chat_id}（可手动删除）")
print("=" * 60)
