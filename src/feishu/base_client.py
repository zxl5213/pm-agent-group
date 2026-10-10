from src.feishu.client import FeishuClient


class BaseClient(FeishuClient):
    """多维表格API封装 - 直接操作已创建的表，不建表/建字段"""

    def get_record(self, app_token, table_id, record_id):
        path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
        params = {"user_id_type": "open_id"}
        return self._request("GET", path, params=params, token_type="tenant")

    def list_records(self, app_token, table_id, page_size=100, page_token="",
                     filter_condition=None, sort_condition=None):
        params = {"page_size": page_size, "user_id_type": "open_id"}
        if page_token:
            params["page_token"] = page_token
        body = {}
        if filter_condition:
            body["filter"] = filter_condition
        if sort_condition:
            body["sort"] = sort_condition

        if filter_condition or sort_condition:
            path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"
            return self._request("POST", path, params=params, json_body=body, token_type="tenant")
        else:
            path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records"
            return self._request("GET", path, params=params, token_type="tenant")

    def list_all_records(self, app_token, table_id, filter_condition=None):
        records = []
        page_token = ""
        while True:
            data = self.list_records(
                app_token, table_id,
                page_token=page_token,
                filter_condition=filter_condition,
            )
            records.extend(data.get("items") or [])
            page_token = data.get("page_token", "")
            if not data.get("has_more", False):
                break
        return records

    def create_record(self, app_token, table_id, fields):
        path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        body = {"fields": fields}
        params = {"user_id_type": "open_id"}
        return self._request("POST", path, params=params, json_body=body, token_type="tenant")

    def update_record(self, app_token, table_id, record_id, fields):
        path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
        body = {"fields": fields}
        params = {"user_id_type": "open_id"}
        return self._request("PUT", path, params=params, json_body=body, token_type="tenant")

    def batch_create_records(self, app_token, table_id, records_fields, batch_size=500):
        path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
        params = {"user_id_type": "open_id"}
        results = []
        for i in range(0, len(records_fields), batch_size):
            batch = records_fields[i:i + batch_size]
            body = {"records": [{"fields": f} for f in batch]}
            data = self._request("POST", path, params=params, json_body=body, token_type="tenant")
            results.extend(data.get("records", []))
        return results

    def batch_delete_records(self, app_token, table_id, record_ids, batch_size=500):
        """批量删除记录，每批最多500条，返回各批删除结果列表。"""
        path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete"
        params = {"user_id_type": "open_id"}
        results = []
        for i in range(0, len(record_ids), batch_size):
            batch = record_ids[i:i + batch_size]
            body = {"records": batch}
            data = self._request("POST", path, params=params, json_body=body, token_type="tenant")
            results.append(data)
        return results

    def find_record_by_field(self, app_token, table_id, field_name, field_value):
        condition = {
            "conjunction": "and",
            "conditions": [
                {"field_name": field_name, "operator": "is", "value": [field_value]}
            ],
        }
        # 仅取第一页：按字段精确过滤后命中记录通常在首页，避免 list_all_records 遍历全部分页
        data = self.list_records(app_token, table_id, filter_condition=condition)
        items = data.get("items") or []
        return items[0] if items else None
