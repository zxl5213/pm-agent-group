import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import load_config
from src.feishu.im_client import IMClient
from src.feishu.card_builder import CardBuilder

config = load_config()
im = IMClient(config)
cb = CardBuilder()

PM_OPEN_ID = "ou_855b3d803ce05cd265ac285263e86996"

print("=" * 60)
print("卡片发送测试")
print("=" * 60)

# 1. 发送欢迎卡片
print("\n[1/3] 发送欢迎卡片到PM私聊...")
try:
    result = im.send_card_to_user(PM_OPEN_ID, cb.welcome_card())
    print(f"  ✅ 欢迎卡片发送成功: message_id={result.get('message_id', '')}")
except Exception as e:
    print(f"  ❌ 发送失败: {type(e).__name__}: {e}")

# 2. 发送项目信息表单卡片(v2格式)
print("\n[2/3] 发送项目信息表单卡片(v2)...")
try:
    result = im.send_card_to_user(PM_OPEN_ID, cb.project_info_form_card())
    print(f"  ✅ 项目信息表单卡片发送成功: message_id={result.get('message_id', '')}")
except Exception as e:
    print(f"  ❌ 发送失败: {type(e).__name__}: {e}")

# 3. 发送帮助卡片
print("\n[3/3] 发送帮助卡片...")
try:
    result = im.send_card_to_user(PM_OPEN_ID, cb.help_card())
    print(f"  ✅ 帮助卡片发送成功: message_id={result.get('message_id', '')}")
except Exception as e:
    print(f"  ❌ 发送失败: {type(e).__name__}: {e}")

print("\n" + "=" * 60)
print("请检查飞书私聊是否收到3张卡片")
print("=" * 60)
