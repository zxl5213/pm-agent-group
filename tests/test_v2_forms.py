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
print("v2 表单卡片渲染测试")
print("=" * 60)

cards = [
    ("项目信息表单", cb.project_info_form_card()),
    ("快速建群表单", cb.quick_create_form_card()),
    ("部门负责人收集表单", cb.dept_lead_collect_card("测试项目", "测试")),
]

for name, card in cards:
    print(f"\n发送 {name}...")
    try:
        result = im.send_card_to_user(PM_OPEN_ID, card)
        msg_id = result.get("message_id", "")
        print(f"  OK  message_id={msg_id}")
    except Exception as e:
        print(f"  FAIL  {type(e).__name__}: {e}")

print("\n" + "=" * 60)
print("请检查飞书私聊是否收到3张v2表单卡片")
print("验证要点：")
print("  1. input输入框是否正常渲染")
print("  2. 提交按钮是否可点击")
print("  3. 填写表单后点击提交，检查日志中 form_value 是否正确")
print("=" * 60)
