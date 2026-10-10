import sys
import os
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import load_config
from src.utils.logger import setup_logger
from src.modules.group_manager import GroupManager
from src.feishu.chat_client import ChatClient
from src.feishu.base_client import BaseClient
from src.models.exceptions import FeishuAPIError, GroupCreationError
from src.models.project import _extract_text

config = load_config()
setup_logger(config)
logger = logging.getLogger(__name__)

# 测试参数
TEST_PM_OPEN_ID = "ou_855b3d803ce05cd265ac285263e86996"  # 张晓丽（PM Agent 应用 open_id）
import time as _time
_ts = _time.strftime("%m%d%H%M")
TEST_PROJECT_NAME = f"建群修复验证项目{_ts}"
TEST_PROJECT_SHORT_NAME = f"FT{_ts}"

results = []

def record(step, status, detail=""):
    results.append({"step": step, "status": status, "detail": detail})
    icon = "✅" if status == "PASS" else "❌" if status == "FAIL" else "⚠️"
    print(f"  {icon} {step}: {status} {detail}")

print("=" * 60)
print("端到端建群测试")
print("流程: 注册项目 → 创建群聊 → Agent入群 → 状态更新 → 数据验证")
print("=" * 60)

# 初始化
gm = GroupManager(config)
chat_client = ChatClient(config)
base_client = BaseClient(config)

# ========== 第1步：注册项目 ==========
print("\n[1/6] 注册项目到项目配置表...")
try:
    project = gm.register_project(
        project_name=TEST_PROJECT_NAME,
        project_short_name=TEST_PROJECT_SHORT_NAME,
        pm_open_id=TEST_PM_OPEN_ID,
        description="端到端建群测试，验证完整流程",
    )
    record("注册项目", "PASS", f"project_id={project.project_id}")
    project_id = project.project_id
except Exception as e:
    record("注册项目", "FAIL", str(e))
    sys.exit(1)

# ========== 第2步：创建群聊（快速模式） ==========
print(f"\n[2/6] 创建群聊（快速模式，project_id={project_id}）...")
try:
    result = gm.create_project_group(project_id, quick_mode=True)
    chat_id = result.get("chat_id", "")
    if chat_id:
        record("创建群聊", "PASS", f"chat_id={chat_id}")
    else:
        record("创建群聊", "FAIL", f"未返回chat_id，result={result}")
        sys.exit(1)
except GroupCreationError as e:
    record("创建群聊", "FAIL", f"{e.step}: {e.reason}")
    sys.exit(1)
except Exception as e:
    record("创建群聊", "FAIL", f"{type(e).__name__}: {e}")
    sys.exit(1)

# ========== 第3步：验证Agent入群 ==========
print(f"\n[3/6] 验证Agent是否已入群...")
# 等待异步收尾完成（sync_pm + transition运行中 约需6-8s）
# 依据：create_project_group 将 sync_pm_bitable + transition 异步化不阻塞返回
time.sleep(10)
try:
    chat_info = chat_client.get_chat_info(chat_id)
    chat_name = chat_info.get("name", "")
    record("获取群信息", "PASS", f"群名={chat_name}")

    members = chat_client.get_all_members(chat_id)
    member_names = [m.get("name", "") for m in members]
    record("群成员列表", "PASS", f"共{len(members)}人: {', '.join(member_names)}")

    # 获取机器人信息验证
    bot_info = chat_client._request_raw("GET", "/bot/v3/info", token_type="tenant")
    if bot_info.get("code") == 0:
        bot_open_id = bot_info.get("bot", {}).get("open_id", "")
        bot_name = bot_info.get("bot", {}).get("app_name", "")
        record("机器人信息", "PASS", f"name={bot_name}, open_id={bot_open_id}")
    else:
        record("机器人信息", "WARN", f"code={bot_info.get('code')}")

except FeishuAPIError as e:
    record("验证Agent入群", "FAIL", f"[{e.code}] {e.msg}")
except Exception as e:
    record("验证Agent入群", "FAIL", f"{type(e).__name__}: {e}")

# ========== 第4步：验证项目配置表数据 ==========
print(f"\n[4/6] 验证项目配置表数据一致性...")
try:
    table_id = config['bitable']['f01_tables']['project_config']
    app_token = config['bitable']['f01_app_token']

    record_data = base_client.find_record_by_field(
        app_token, table_id, "project_id", project_id
    )
    if not record_data:
        record("查询项目记录", "FAIL", "未找到项目记录")
    else:
        fields = record_data.get("fields", {})
        db_chat_id = _extract_text(fields.get("group_id", ""))
        db_status = _extract_text(fields.get("status", ""))
        db_project_name = _extract_text(fields.get("project_name", ""))
        db_short_name = _extract_text(fields.get("project_abbr", ""))

        record("项目ID", "PASS" if db_project_name == TEST_PROJECT_NAME else "FAIL",
               f"DB={db_project_name}")
        record("项目简称", "PASS" if db_short_name == TEST_PROJECT_SHORT_NAME else "FAIL",
               f"DB={db_short_name}")
        record("群聊ID", "PASS" if db_chat_id == chat_id else "FAIL",
               f"DB={db_chat_id} vs 实际={chat_id}")
        record("项目状态", "PASS" if db_status == "运行中" else "FAIL",
               f"DB={db_status}")

except Exception as e:
    record("验证项目配置表", "FAIL", f"{type(e).__name__}: {e}")

# ========== 第5步：验证群聊可发消息 ==========
print(f"\n[5/6] 验证Agent能否在群内发消息...")
try:
    from src.feishu.im_client import IMClient
    im_client = IMClient(config)
    msg_result = im_client.send_text(
        chat_id,
        f"📦 项目群已创建\n项目: {TEST_PROJECT_NAME}\nPM: 张晓丽\nAgent已就位，项目启动！"
    )
    record("Agent发消息", "PASS", f"message_id={msg_result.get('message_id', '')}")
except FeishuAPIError as e:
    record("Agent发消息", "FAIL", f"[{e.code}] {e.msg}")
except Exception as e:
    record("Agent发消息", "FAIL", f"{type(e).__name__}: {e}")

# ========== 第6步：汇总 ==========
print(f"\n[6/6] 测试汇总")
print("=" * 60)
pass_count = sum(1 for r in results if r["status"] == "PASS")
fail_count = sum(1 for r in results if r["status"] == "FAIL")
warn_count = sum(1 for r in results if r["status"] == "WARN")
total = len(results)

for r in results:
    icon = "✅" if r["status"] == "PASS" else "❌" if r["status"] == "FAIL" else "⚠️"
    print(f"  {icon} {r['step']}: {r['status']} {r['detail']}")

print(f"\n  总计: {total}项 | ✅ {pass_count}通过 | ❌ {fail_count}失败 | ⚠️ {warn_count}警告")
print(f"\n  项目ID: {project_id}")
print(f"  群聊ID: {chat_id}")
print("=" * 60)

if fail_count == 0:
    print("\n  🎉 端到端建群测试通过！")
else:
    print(f"\n  ⚠️ 有{fail_count}项失败，需要排查")
