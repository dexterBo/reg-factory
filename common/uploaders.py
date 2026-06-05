# -*- coding: utf-8 -*-
"""
common/uploaders.py — 把本地标准 token 上传到下游管理接口。

移植自 FlowPilot(QLHazyCoder/FlowPilot):
  - upload_cpa          background/cpa-api.js: importCurrentChatGptSession
  - upload_sub2api      background/sub2api-api.js: loginSub2Api/getGroupsByNames/importCurrentChatGptSession
  - upload_webchat2api  flows/grok/background/publisher-webchat2api.js: uploadGrokSsoToWebchat2Api

每个函数返回 (ok: bool, message: str)。仅在被 upload_tokens.py 调用时使用。
"""

import json
from urllib.parse import urlparse, quote

import requests

DEFAULT_TIMEOUT = 30
DEFAULT_CONCURRENCY = 10
DEFAULT_PRIORITY = 1
DEFAULT_RATE_MULTIPLIER = 1


def _origin(url):
    p = urlparse(url if "://" in (url or "") else f"http://{url}")
    if not p.scheme or not p.netloc:
        raise ValueError(f"地址格式无效: {url}")
    return f"{p.scheme}://{p.netloc}"


def _msg_from_payload(payload, status, fallback=""):
    if isinstance(payload, dict):
        for key in ("message", "detail", "error", "reason"):
            v = payload.get(key)
            if isinstance(v, dict):
                v = v.get("message") or v.get("error")
            if v:
                return str(v).strip()
    return fallback or f"HTTP {status}"


# ============================================================ CPA
def upload_cpa(base_url, mgmt_key, auth_json, file_name, timeout=DEFAULT_TIMEOUT):
    """POST {origin}/v0/management/auth-files?name=<file_name>，body=auth_json。"""
    try:
        origin = _origin(base_url)
        if not mgmt_key:
            return False, "缺少 CPA 管理密钥"
        url = f"{origin}/v0/management/auth-files?name={quote(file_name)}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {mgmt_key}",
            "X-Management-Key": mgmt_key,
        }
        resp = requests.post(url, headers=headers, json=auth_json, timeout=timeout)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if not resp.ok:
            return False, _msg_from_payload(payload, resp.status_code, "CPA 导入失败")
        return True, _msg_from_payload(payload, resp.status_code, "CPA 导入成功") if isinstance(payload, dict) and payload else "CPA 导入成功"
    except requests.RequestException as e:
        return False, f"CPA 请求异常: {e}"
    except Exception as e:
        return False, str(e)


# ============================================================ SUB2API
def _sub2api_request(origin, path, token=None, method="GET", body=None, timeout=DEFAULT_TIMEOUT):
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, f"{origin}{path}", headers=headers,
                            data=None if body is None else json.dumps(body), timeout=timeout)
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    # SUB2API 约定 {code:0, data}
    if isinstance(payload, dict) and "code" in payload:
        if int(payload.get("code")) == 0:
            return payload.get("data")
        raise RuntimeError(_msg_from_payload(payload, resp.status_code, f"SUB2API 失败: {path}"))
    if not resp.ok:
        raise RuntimeError(_msg_from_payload(payload, resp.status_code, f"SUB2API 失败: {path}"))
    return payload


def upload_sub2api(base_url, email, password, group, content,
                   expires_at=None, priority=DEFAULT_PRIORITY, timeout=DEFAULT_TIMEOUT):
    """登录 -> 找分组 -> import/codex-session。group 是分组名(字符串)。"""
    try:
        origin = _origin(base_url)
        if not email or not password:
            return False, "缺少 SUB2API 登录邮箱/密码"

        login = _sub2api_request(origin, "/api/v1/auth/login", method="POST",
                                 body={"email": email, "password": password}, timeout=timeout)
        token = ""
        if isinstance(login, dict):
            token = str(login.get("access_token") or login.get("accessToken") or "").strip()
        if not token:
            return False, "SUB2API 登录返回缺少 access_token"

        target = str(group or "codex").strip().lower()
        groups = _sub2api_request(origin, "/api/v1/admin/groups/all", token=token, timeout=timeout)
        group_id = None
        for item in (groups or []):
            name = str(item.get("name") or "").strip().lower()
            platform = item.get("platform")
            if name == target and (not platform or platform == "openai"):
                group_id = item.get("id")
                break
        if not group_id:
            return False, f"SUB2API 未找到 openai 分组: {group}"

        payload = {
            "content": content,
            "group_ids": [int(group_id)],
            "priority": int(priority),
            "auto_pause_on_expired": True,
            "update_existing": True,
        }
        if expires_at:
            payload["expires_at"] = int(expires_at)

        result = _sub2api_request(origin, "/api/v1/admin/accounts/import/codex-session",
                                  token=token, method="POST", body=payload, timeout=timeout)
        result = result if isinstance(result, dict) else {}
        created = int(result.get("created") or 0)
        updated = int(result.get("updated") or 0)
        failed = int(result.get("failed") or 0)
        if failed > 0 or (created <= 0 and updated <= 0):
            return False, f"SUB2API 导入未成功(新建{created}/更新{updated}/失败{failed})"
        return True, f"SUB2API 导入完成(新建{created}/更新{updated})"
    except requests.RequestException as e:
        return False, f"SUB2API 请求异常: {e}"
    except Exception as e:
        return False, str(e)


# ============================================================ webchat2api (Grok SSO)
def upload_webchat2api(base_url, admin_key, sso, timeout=DEFAULT_TIMEOUT):
    """POST {origin}/api/remote-account/inject，注入 grok sso。"""
    try:
        origin = _origin(base_url)
        if not admin_key:
            return False, "缺少 webchat2api 管理密钥"
        if not sso:
            return False, "缺少 grok sso"
        url = f"{origin}/api/remote-account/inject"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {admin_key}",
        }
        body = {
            "accounts": [{"token": sso, "provider": "grok", "type": "sso"}],
            "strategy": "merge",
            "source_id": "flowpilot-grok-sso",
            "source_name": "FlowPilot Grok SSO",
            "provider": "grok",
        }
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if not resp.ok:
            return False, _msg_from_payload(payload, resp.status_code, "webchat2api 上传失败")
        if isinstance(payload, dict) and "code" in payload and int(payload.get("code")) != 0:
            return False, _msg_from_payload(payload, resp.status_code, f"code={payload.get('code')}")
        return True, _msg_from_payload(payload, resp.status_code, "上传成功") if isinstance(payload, dict) and payload else "上传成功"
    except requests.RequestException as e:
        return False, f"webchat2api 请求异常: {e}"
    except Exception as e:
        return False, str(e)
