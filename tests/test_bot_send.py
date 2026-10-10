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

result = im.send_text_to_user(PM_OPEN_ID, "测试：这条消息应该来自AI项目经理机器人，而不是张晓丽。请回复 /帮助 查看指令列表。")
msg_id = result.get("message_id", "failed")
print("文本消息:", msg_id)

result = im.send_card_to_user(PM_OPEN_ID, cb.welcome_card())
msg_id = result.get("message_id", "failed")
print("欢迎卡片:", msg_id)
