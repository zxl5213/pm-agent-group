import sys
import os
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import load_config

config = load_config()
token = os.environ.get(config['feishu']['token_env_var'], '')

resp = requests.get(
    f"{config['feishu']['api_base']}/authen/v1/user_info",
    headers={"Authorization": f"Bearer {token}"},
    timeout=10,
)
data = resp.json()
print(f"code: {data.get('code')}")
if data.get('code') == 0:
    user = data.get('data', {})
    print(f"open_id: {user.get('open_id')}")
    print(f"name: {user.get('name')}")
    print(f"user_id: {user.get('user_id')}")
    print(f"email: {user.get('email')}")
else:
    print(f"Error: {data}")
