import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import load_config
from src.feishu.im_client import IMClient
from src.feishu.card_builder import CardBuilder

config = load_config()
im = IMClient(config)
cb = CardBuilder()

PM_OPEN_ID = "ou_0d55926175a842f131c8a7fb8af319de"

result = im.send_text_to_user(PM_OPEN_ID, "测试：如果你能看到这条消息，说明私聊通道正常。请回复 /帮助 查看指令列表。")
msg_id = result.get("message_id", "failed")
print(f"文本消息: {msg_id}")

result = im.send_card_to_user(PM_OPEN_ID, cb.welcome_card())
msg_id = result.get("message_id", "failed")
print(f"欢迎卡片: {msg_id}")
