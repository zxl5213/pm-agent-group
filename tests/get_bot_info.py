import sys
import os
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import load_config

config = load_config()
token = os.environ.get(config['feishu']['token_env_var'], '')

# 尝试获取机器人信息（通常需要tenant_access_token）
resp = requests.get(
    f"{config['feishu']['api_base']}/bot/v3/info",
    headers={"Authorization": f"Bearer {token}"},
    timeout=10,
)
print(f"Bot info status: {resp.status_code}")
print(f"Bot info response: {resp.text[:500]}")
