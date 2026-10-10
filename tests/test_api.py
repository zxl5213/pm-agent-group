import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import load_config
from src.feishu.base_client import BaseClient

config = load_config()
client = BaseClient(config)
app_token = config['bitable']['f01_app_token']
table_id = config['bitable']['f01_tables']['project_config']

try:
    data = client.list_records(app_token, table_id)
    print(f"返回数据类型: {type(data)}")
    print(f"返回数据: {data}")
    items = (data.get("items") or []) if data else []
    print(f"F-01项目配置表连接成功！当前记录数: {len(items)}")
    if items:
        for item in items:
            print(f"  - {item.get('fields', {}).get('project_name', 'N/A')}")
except Exception as e:
    import traceback
    traceback.print_exc()
    print(f"连接失败: {type(e).__name__}: {e}")
