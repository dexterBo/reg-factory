# -*- coding: utf-8 -*-
"""
common/oauth_codex.py — 走 Codex CLI OAuth 给 SUB2API 创建带 refresh_token 的 openai 账号。

为什么:网页 /api/auth/session 的 accessToken 没有 refresh_token，SUB2API 当 oauth 账号
无法续期 → 401。正确做法是走 OAuth 授权码换取正式凭据(含 refresh_token)。

SUB2API 包办 PKCE/换码，三步:
  1. POST /api/v1/admin/openai/generate-auth-url {redirect_uri} -> {auth_url, session_id}
     auth_url 走 auth.openai.com/oauth/authorize?client_id=app_EMoamEEZ73f0CkXaXp7hrann
     &scope=openid profile email offline_access&code_challenge=...(S256)&state=...
  2. [浏览器] 在已登录该账号的窗口打开 auth_url → 同意 → 跳
     http://localhost:1455/auth/callback?code=...&state=...(浏览器拦截此 URL 拿 code/state)
  3. POST /api/v1/admin/openai/exchange-code {session_id, code, state} -> 凭据(含 refresh_token)
     再 POST /api/v1/admin/accounts 建 type=oauth 账号。
"""

import asyncio
import sys
import time
from urllib.parse import urlparse, parse_qs

from common.uploaders import _origin, _sub2api_request, DEFAULT_TIMEOUT

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_CONCURRENCY = 10
DEFAULT_PRIORITY = 1
DEFAULT_RATE_MULTIPLIER = 1
# 授权页可能出现的"同意/继续"按钮文案(多语言)
CONSENT_LABELS = [
    "Authorize", "Allow", "Continue", "Approve", "Yes", "Accept",
    "Continue with ChatGPT", "Log in with ChatGPT", "Authorize access",
    "同意", "授权", "允许", "继续", "确认", "登录",
]


# ============================================================ SUB2API 调用
def sub2api_login(origin, email, password, timeout=DEFAULT_TIMEOUT):
    d = _sub2api_request(origin, "/api/v1/auth/login", method="POST",
                         body={"email": email, "password": password}, timeout=timeout)
    token = ""
    if isinstance(d, dict):
        token = str(d.get("access_token") or d.get("accessToken") or "").strip()
    if not token:
        raise RuntimeError("SUB2API 登录失败，无 access_token")
    return token


def find_group_id(origin, token, group_name, timeout=DEFAULT_TIMEOUT):
    target = str(group_name or "codex").strip().lower()
    groups = _sub2api_request(origin, "/api/v1/admin/groups/all", token=token, timeout=timeout) or []
    for g in groups:
        name = str(g.get("name") or "").strip().lower()
        platform = g.get("platform")
        if name == target and (not platform or platform == "openai"):
            return g.get("id")
    raise RuntimeError(f"SUB2API 未找到 openai 分组: {group_name}")


def generate_auth_url(origin, token, redirect_uri=REDIRECT_URI, timeout=DEFAULT_TIMEOUT):
    body = {"redirect_uri": redirect_uri}
    d = _sub2api_request(origin, "/api/v1/admin/openai/generate-auth-url",
                         token=token, method="POST", body=body, timeout=timeout)
    auth_url = str((d or {}).get("auth_url") or (d or {}).get("authUrl") or "").strip()
    session_id = str((d or {}).get("session_id") or (d or {}).get("sessionId") or "").strip()
    state = str((d or {}).get("state") or "").strip() or _state_from_url(auth_url)
    if not auth_url or not session_id:
        raise RuntimeError("SUB2API 未返回完整 auth_url / session_id")
    return auth_url, session_id, state


def _state_from_url(url):
    try:
        return parse_qs(urlparse(url).query).get("state", [""])[0]
    except Exception:
        return ""


def exchange_code(origin, token, session_id, code, state, timeout=60):
    body = {"session_id": session_id, "code": code, "state": state}
    return _sub2api_request(origin, "/api/v1/admin/openai/exchange-code",
                            token=token, method="POST", body=body, timeout=timeout)


def build_oauth_credentials(exchange_data):
    """对齐 sub2api-api.js buildOpenAiCredentials。无 access_token 抛错。"""
    cred = {}
    for k in ("access_token", "refresh_token", "id_token", "expires_at", "email",
              "chatgpt_account_id", "chatgpt_user_id", "organization_id", "plan_type", "client_id"):
        v = (exchange_data or {}).get(k)
        if v not in (None, "", []):
            cred[k] = v
    if not cred.get("access_token"):
        raise RuntimeError("exchange-code 未返回 access_token")
    return cred


def create_oauth_account(origin, token, credentials, group_ids, name="",
                         priority=DEFAULT_PRIORITY, timeout=60):
    payload = {
        "name": name or credentials.get("email") or "codex-oauth",
        "notes": "",
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "concurrency": DEFAULT_CONCURRENCY,
        "priority": int(priority),
        "rate_multiplier": DEFAULT_RATE_MULTIPLIER,
        "group_ids": [int(g) for g in group_ids if g],
        "auto_pause_on_expired": True,
    }
    extra = {}
    for k in ("email", "plan_type"):
        if credentials.get(k):
            extra[k] = credentials[k]
    if extra:
        payload["extra"] = extra
    return _sub2api_request(origin, "/api/v1/admin/accounts",
                            token=token, method="POST", body=payload, timeout=timeout)


# ============================================================ 浏览器驱动授权
async def _has_phone_error(page):
    """add-phone 页是否出现"号码不可用/无效"类报错。"""
    try:
        txt = (await page.inner_text("body")).lower()
    except Exception:
        return False
    for kw in ["can't be used", "cannot be used", "not valid", "invalid", "unable to",
               "try another", "different phone", "not supported", "already", "too many"]:
        if kw in txt:
            return True
    return False


async def _fill_phone_continue(page, country_code, national):
    """填手机号(优先整条 E.164，react-aria 多数会自动识别国家)并点 Continue。"""
    full = ("+" + country_code + national) if country_code else ("+" + national)
    tel = page.locator("#tel")
    await tel.wait_for(state="visible", timeout=15000)
    await tel.click()
    try:
        await tel.fill("")
    except Exception:
        pass
    await tel.type(full, delay=25)
    await asyncio.sleep(1.0)
    btn = page.locator('button[data-dd-action-name="Continue"], button[type="submit"]')
    await btn.first.click(timeout=6000)


async def _enter_otp(page, code):
    """验证码页:填 OTP(单框或分段)，必要时点提交。"""
    inp = page.locator('input[autocomplete="one-time-code"], input[name*="code" i], input[inputmode="numeric"], input[type="tel"]')
    await inp.first.wait_for(state="visible", timeout=20000)
    cnt = await inp.count()
    if cnt > 1 and cnt >= len(code):
        for i, ch in enumerate(code):
            try:
                await inp.nth(i).fill(ch)
            except Exception:
                pass
    else:
        await inp.first.fill(code)
    await asyncio.sleep(1.0)
    try:
        b = page.locator('button[type="submit"], button[data-dd-action-name="Continue"]')
        if await b.count() and await b.first.is_visible():
            await b.first.click(timeout=4000)
    except Exception:
        pass


async def _goto_add_phone(page, auth_url, account_email, timeout=45):
    """(重新)走到 add-phone 输手机号页:导航 auth_url → 选账号 → 落到 add-phone 且 #tel 可见。
    用于换号前把页面退回干净的输手机号状态。"""
    # 若 OTP 页有"换个号码/返回"入口，先点(更轻);点不到就重新导航
    for lbl in ["Use a different phone number", "Change phone number", "Edit", "Back",
                "换个号码", "更改手机号", "返回", "重新输入"]:
        try:
            loc = page.get_by_role("link", name=lbl, exact=False)
            if await loc.count() == 0:
                loc = page.get_by_role("button", name=lbl, exact=False)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=2500)
                await asyncio.sleep(1.5)
                break
        except Exception:
            pass
    try:
        await page.goto(auth_url, timeout=30000, wait_until="domcontentloaded")
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = page.url
        if "add-phone" in url or "/phone" in url:
            try:
                await page.locator("#tel").wait_for(state="visible", timeout=4000)
                return True
            except Exception:
                pass
        if "choose-an-account" in url or "/account" in url:
            await _click_account(page, account_email)
            await asyncio.sleep(2)
            continue
        # 其它中间页(同意等)，点推进按钮
        for lbl in CONSENT_LABELS:
            try:
                b = page.get_by_role("button", name=lbl, exact=False)
                if await b.count() > 0 and await b.first.is_visible():
                    await b.first.click(timeout=2000)
                    break
            except Exception:
                pass
        await asyncio.sleep(1.5)
    return False


async def handle_add_phone(page, auth_url="", account_email="", attempts=5, sms_timeout=180):
    """auth.openai.com/add-phone:接码平台租号→填→收码→提交，被拒/收不到码就**回退页面**换号重试。
    成功(离开 add-phone)返回 True。

    ⚠️ WIP：自动接码路径未充分验证（OpenAI 对普通虚拟号风控严，SMS_PROJECT_ID_OPENAI 默认空）。
    当前推荐用 oauth_codex.py --manual-phone 手动填号 + 输 WhatsApp 码；全自动接码版后续完善。
    """
    from common import sms
    from config import (SMS_PROJECT_ID_OPENAI, HERO_SMS_SERVICE_OPENAI,
                        SMS_MAXPRICE_OPENAI, SMS_COUNTRY_BLACKLIST_OPENAI)
    for i in range(attempts):
        pkey = None
        try:
            # 换号前必须把页面退回"输手机号"页(否则 #tel 找不到)
            need_reset = i > 0
            if not need_reset:
                try:
                    await page.locator("#tel").wait_for(state="visible", timeout=5000)
                except Exception:
                    need_reset = True
            if need_reset:
                if not auth_url:
                    print("  [add-phone] 缺 auth_url 无法回退，终止")
                    break
                print("  [add-phone] 回退到输手机号页...")
                if not await _goto_add_phone(page, auth_url, account_email):
                    if "add-phone" not in page.url:
                        print("  [add-phone] 已离开 add-phone，终止重试")
                        break
                    print("  [add-phone] 回退后仍找不到 #tel，跳过本次")
                    continue

            # 任意国家(库存动态，指定具体国家常无货) + 拉黑垃圾号段 + 给够价格上限
            phone, cc, pkey = sms.get_phone(SMS_PROJECT_ID_OPENAI, HERO_SMS_SERVICE_OPENAI,
                                            country_prefer=[""], country_blacklist=SMS_COUNTRY_BLACKLIST_OPENAI,
                                            max_retries=4, max_price=SMS_MAXPRICE_OPENAI)
            print(f"  [add-phone] 尝试 {i+1}/{attempts}: +{cc}{phone}")
            await _fill_phone_continue(page, cc, phone)
            await asyncio.sleep(4)
            if "add-phone" in page.url and await _has_phone_error(page):
                print("  [add-phone] 号码被拒，换号重试")
                sms.release(pkey)
                continue
            code = sms.get_code(pkey, max_wait=sms_timeout)
            if not code:
                print("  [add-phone] 未收到验证码，换号重试")
                sms.release(pkey)
                continue
            await _enter_otp(page, code)
            await asyncio.sleep(4)
            if "add-phone" not in page.url:
                print("  [add-phone] 手机验证通过 ✅")
                return True
            print("  [add-phone] 验证码未通过，换号重试")
            sms.release(pkey)
        except Exception as e:
            print(f"  [add-phone] err: {str(e)[:80]}")
            if pkey:
                try:
                    sms.release(pkey)
                except Exception:
                    pass
    return False


async def _click_account(page, account_email=""):
    """choose-an-account 账号选择页:优先点中目标邮箱的账号，否则点第一个账号按钮。"""
    # 1) 含邮箱文本的按钮
    if account_email:
        try:
            loc = page.locator("button", has_text=account_email)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=2500)
                return True
        except Exception:
            pass
    # 2) 含 "Select account" 无障碍文案的账号按钮(取第一个)
    for sel in ['button:has-text("Select account")', 'button:has(span:has-text("@"))']:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=2500)
                return True
        except Exception:
            pass
    return False


async def drive_authorize(page, auth_url, timeout=120, debug_dump=None, account_email="", manual_phone=False):
    """在已登录该账号的页面打开 auth_url，处理账号选择/同意页，捕获 localhost:1455 回调。
    manual_phone=True 时遇到 add-phone 不自动接码，由用户在浏览器手动填号收码，脚本轮询等待。
    返回 (code, state, msg)。失败 code/state 为 None。"""
    captured = {}
    manual_hint_shown = False

    async def _handle(route):
        captured["url"] = route.request.url
        try:
            await route.fulfill(status=200, content_type="text/html", body="<html>captured</html>")
        except Exception:
            try:
                await route.abort()
            except Exception:
                pass

    for pat in ("http://localhost:1455/**", "http://127.0.0.1:1455/**"):
        await page.context.route(pat, _handle)

    try:
        try:
            await page.goto(auth_url, timeout=45000, wait_until="domcontentloaded")
        except Exception:
            pass  # 可能被重定向到 localhost 打断，正常

        deadline = time.time() + timeout
        while time.time() < deadline:
            if captured.get("url"):
                break
            # 账号选择页:先选账号
            try:
                if "choose-an-account" in page.url or "/account" in page.url:
                    if await _click_account(page, account_email):
                        await asyncio.sleep(2.0)
                        continue
            except Exception:
                pass
            # add-phone 页:manual_phone=True 时不接码，由用户在浏览器手动填号+输码(如 WhatsApp 码)，
            # 脚本只轮询等待离开 add-phone 页；否则走接码自动过。
            try:
                if "add-phone" in page.url or "/phone" in page.url:
                    if manual_phone:
                        if not manual_hint_shown:
                            print("  [add-phone] 手动模式:请在浏览器里自行填写手机号并输入收到的验证码(如 WhatsApp 码)。")
                            print(f"             脚本会轮询等待,直到离开 add-phone 页(上限 {timeout}s)。")
                            manual_hint_shown = True
                        await asyncio.sleep(2.0)
                        continue
                    ok = await handle_add_phone(page, auth_url=auth_url, account_email=account_email)
                    if not ok:
                        return None, None, "add-phone 手机验证失败(接码换号都没过)"
                    await asyncio.sleep(2.0)
                    continue
            except Exception as e:
                return None, None, f"add-phone 处理异常: {str(e)[:80]}"
            # 尝试点同意/授权按钮
            for lbl in CONSENT_LABELS:
                try:
                    loc = page.get_by_role("button", name=lbl, exact=False)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click(timeout=2500)
                        await asyncio.sleep(1.5)
                        break
                except Exception:
                    pass
            else:
                # 再试 link 角色
                for lbl in CONSENT_LABELS:
                    try:
                        loc = page.get_by_role("link", name=lbl, exact=False)
                        if await loc.count() > 0 and await loc.first.is_visible():
                            await loc.first.click(timeout=2500)
                            await asyncio.sleep(1.5)
                            break
                    except Exception:
                        pass
            await asyncio.sleep(1.0)

        url = captured.get("url")
        if not url:
            if debug_dump:
                try:
                    html = await page.content()
                    with open(debug_dump, "w", encoding="utf-8") as f:
                        f.write(f"<!-- url: {page.url} -->\n" + html)
                except Exception:
                    pass
            return None, None, f"未捕获 localhost:1455 回调(当前页 {page.url[:80]})"

        q = parse_qs(urlparse(url).query)
        code = q.get("code", [None])[0]
        state = q.get("state", [None])[0]
        err = q.get("error", [None])[0]
        if err:
            return None, None, f"授权返回 error={err}"
        if not code:
            return None, None, "回调缺少 code"
        return code, state, "ok"
    finally:
        for pat in ("http://localhost:1455/**", "http://127.0.0.1:1455/**"):
            try:
                await page.context.unroute(pat, _handle)
            except Exception:
                pass
