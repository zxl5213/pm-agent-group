import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import load_config
from src.utils.logger import setup_logger
from src.feishu.chat_client import ChatClient
from src.feishu.base_client import BaseClient
from src.feishu.client import FeishuClient
from src.models.project import _extract_text
from src.models.exceptions import FeishuAPIError

config = load_config()
setup_logger(config)
logger = logging.getLogger(__name__)

chat_client = ChatClient(config)
base_client = BaseClient(config)

# 已知的测试群ID
TEST_CHAT_IDS = [
    "oc_2541737ed6d03d8529802fa7b0f90344",  # Agent入群测试群 17:01
    "oc_7f805f5f68e7f22b05fc5987f497a441",  # Agent入群测试群 17:03
    "oc_895cd14381ea9a458cb84a62cac827fe",  # Agent入群测试群 17:04
    "oc_94d076c88b2416b16303402c1cc9d95b",  # E2E测试 17:13
    "oc_5d3875a53cc6ef506ff6d15aad913d42",  # E2E测试 17:14
]

print("=" * 60)
print("清理测试数据")
print("=" * 60)

# ========== 第1步：解散测试群 ==========
print(f"\n[1/2] 解散 {len(TEST_CHAT_IDS)} 个测试群...")
disbanded = 0
for chat_id in TEST_CHAT_IDS:
    try:
        chat_info = chat_client.get_chat_info(chat_id)
        chat_name = chat_info.get("name", "未知")
        chat_client.disband_chat(chat_id)
        print(f"  ✅ 已解散: {chat_name} ({chat_id})")
        disbanded += 1
    except FeishuAPIError as e:
        print(f"  ⚠️ 解散失败 {chat_id}: [{e.code}] {e.msg}")
    except Exception as e:
        print(f"  ⚠️ 解散失败 {chat_id}: {type(e).__name__}: {e}")

print(f"\n  共解散 {disbanded}/{len(TEST_CHAT_IDS)} 个群")

# ========== 第2步：清理多维表格中的测试项目记录 ==========
print(f"\n[2/2] 清理项目配置表中的测试记录...")
app_token = config['bitable']['f01_app_token']
table_id = config['bitable']['f01_tables']['project_config']

try:
    records = base_client.list_all_records(app_token, table_id)
    test_records = []
    for r in records:
        fields = r.get("fields", {})
        project_name = _extract_text(fields.get("project_name", ""))
        project_id = _extract_text(fields.get("project_id", ""))
        if "测试" in project_name or "E2E" in project_name:
            test_records.append({
                "record_id": r["record_id"],
                "project_id": project_id,
                "project_name": project_name,
            })

    if not test_records:
        print("  没有找到测试项目记录")
    else:
        print(f"  找到 {len(test_records)} 条测试记录:")
        for tr in test_records:
            print(f"    - {tr['project_id']}: {tr['project_name']}")

        # 批量删除记录
        path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete"
        body = {"records": [tr["record_id"] for tr in test_records]}
        try:
            base_client._request("POST", path, json_body=body)
            print(f"  ✅ 已删除 {len(test_records)} 条测试记录")
        except FeishuAPIError as e:
            print(f"  ❌ 批量删除失败: [{e.code}] {e.msg}")
            # 逐条删除
            for tr in test_records:
                try:
                    del_path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{tr['record_id']}"
                    base_client._request("DELETE", del_path)
                    print(f"  ✅ 已删除: {tr['project_id']}")
                except Exception as e2:
                    print(f"  ❌ 删除失败 {tr['project_id']}: {e2}")

except Exception as e:
    print(f"  ❌ 查询记录失败: {type(e).__name__}: {e}")

print("\n" + "=" * 60)
print("清理完成")
print("=" * 60)
