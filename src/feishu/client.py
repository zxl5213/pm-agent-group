import os
import json as json_mod
import time
import random
import logging
import threading
import requests

from src.models.exceptions import FeishuAPIError

logger = logging.getLogger(__name__)


class FeishuClient:
    """飞书API客户端基类 - 封装通用HTTP请求、认证、重试逻辑"""

    _tenant_token_cache = {"token": "", "expire_at": 0}
    _tenant_token_lock = threading.Lock()
    _user_token_valid_cache = {}  # env_var -> (token or None)

    def __init__(self, config):
        self.api_base = config['feishu']['api_base']
        self.token_env_var = config['feishu'].get('token_env_var', 'LARKSUITE_CLI_USER_ACCESS_TOKEN')
        self.app_id = config['feishu'].get('app_id', '')
        self.app_secret = config['feishu'].get('app_secret', '')
        # 一次性检测并缓存 user token 有效性
        self._cached_user_token = self._check_user_token()

    def _check_user_token(self):
        """检测 user token 是否有效（只在初始化时调用一次）"""
        token = os.environ.get(self.token_env_var, "")
        if token and not token.isascii():
            logger.warning(f"Ignoring invalid {self.token_env_var} (contains non-ASCII characters), will use tenant token")
            return None
        return token or None

    def _get_token(self):
        if self._cached_user_token:
            return self._cached_user_token
        # Fallback to tenant token when user token is not available
        return self._get_tenant_token()

    def _get_tenant_token(self):
        """获取tenant_access_token（带缓存，有效期2小时）"""
        now = time.time()
        cache = FeishuClient._tenant_token_cache
        if cache["token"] and now < cache["expire_at"] - 300:
            return cache["token"]

        with FeishuClient._tenant_token_lock:
            cache = FeishuClient._tenant_token_cache
            # double check after acquiring lock
            if cache["token"] and now < cache["expire_at"] - 300:
                return cache["token"]

            if not self.app_id or not self.app_secret:
                raise FeishuAPIError(-1, "Missing app_id or app_secret")

            resp = requests.post(
                f"{self.api_base}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0:
                raise FeishuAPIError(data.get("code", -1), data.get("msg", "Failed to get tenant_access_token"))

            token = data["tenant_access_token"]
            expire = data.get("expire", 7200)
            FeishuClient._tenant_token_cache = {"token": token, "expire_at": now + expire}
            logger.info("tenant_access_token refreshed")
            return token

    def _get_headers(self, token_type="user"):
        if token_type == "tenant":
            token = self._get_tenant_token()
        else:
            token = self._get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _request(self, method, path, params=None, json_body=None, max_retries=5, token_type="user", timeout_override=None):
        url = f"{self.api_base}{path}"
        timeout = timeout_override or 30

        # Serialize JSON body as UTF-8 bytes to avoid latin-1 encoding issues with Chinese characters
        data_bytes = None
        if json_body is not None:
            data_bytes = json_mod.dumps(json_body, ensure_ascii=False).encode('utf-8')

        for attempt in range(max_retries):
            try:
                # 每次重试重新取 headers，确保 token 最新（官方 SDK 自动管理 token；手写需每次刷新）
                headers = self._get_headers(token_type)
                resp = requests.request(
                    method, url,
                    headers=headers,
                    params=params,
                    data=data_bytes,
                    timeout=timeout,
                )

                # 429 限流：按官方文档读取 x-ogw-ratelimit-reset 响应头等待
                # 来源：https://open.feishu.cn/document/server-docs/api-call-guide/frequency-control
                if resp.status_code == 429:
                    wait = self._ratelimit_wait(resp, attempt)
                    logger.warning(f"Rate limited (429), retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait)
                    continue

                data = resp.json()

                if data.get('code') == 0:
                    return data.get('data', {})

                error_code = data.get('code', -1)
                error_msg = data.get('msg', 'Unknown error')

                # 业务码 99991400 也按限流处理（部分旧 API 返回 400 + 该码）
                if error_code == 99991400:
                    wait = self._ratelimit_wait(resp, attempt)
                    logger.warning(f"Rate limited (code 99991400), retrying in {wait:.1f}s")
                    time.sleep(wait)
                    continue

                # 5xx 服务端错误：临时性，重试（指数退避 + 抖动）
                if 500 <= resp.status_code < 600 and attempt < max_retries - 1:
                    wait = min(2 ** attempt, 16) + random.uniform(0, 1)
                    logger.warning(f"Server error {resp.status_code}, retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait)
                    continue

                raise FeishuAPIError(error_code, error_msg, f"{method} {path}")

            except requests.exceptions.JSONDecodeError as e:
                # JSON 解析错误是业务错误（飞书返回非法JSON），非临时性，不重试
                # 依据：实测 archive_messages 因 JSON 错误重试4次白耗18.6s
                raise FeishuAPIError(-1, f"JSON parse error: {e}", f"{method} {path}")
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    # 网络异常（ETIMEDOUT/ECONNRESET）：指数退避 + 抖动，避免惊群
                    wait = min(2 ** attempt, 16) + random.uniform(0, 1)
                    logger.warning(f"Request failed: {e}, retrying in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                raise FeishuAPIError(-1, str(e), f"{method} {path}")

        raise FeishuAPIError(-1, "Max retries exceeded", f"{method} {path}")

    @staticmethod
    def _ratelimit_wait(resp, attempt):
        """按官方文档读取 x-ogw-ratelimit-reset 响应头计算等待时间（秒），并加抖动。
        上限 60s 避免单次等待过长。无响应头时回退到指数退避。"""
        reset = resp.headers.get("x-ogw-ratelimit-reset")
        try:
            base = min(float(reset), 60) if reset else min(2 ** attempt, 16)
        except (TypeError, ValueError):
            base = min(2 ** attempt, 16)
        return base + random.uniform(0, 1)

    def _request_raw(self, method, path, token_type="tenant"):
        """发起请求并返回完整响应（用于非标准API，如bot/v3/info）"""
        url = f"{self.api_base}{path}"
        headers = self._get_headers(token_type)
        resp = requests.request(method, url, headers=headers, timeout=10)
        return resp.json()
