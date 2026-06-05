# -*- coding: utf-8 -*-
"""
oauth_codex.py — 用已存 cookie 重登 ChatGPT 账号，走 Codex OAuth 给 SUB2API 建带 refresh_token 的账号。

解决:网页 session token 无 refresh_token → SUB2API oauth 账号 401。

用法:
    python oauth_codex.py --cookie cookies/chatgpt/full_xxx.json     # 指定 cookie 文件
    python oauth_codex.py                                            # 默认用最新的 full_*.json
    python oauth_codex.py --keep                                     # 失败保留窗口排查

前置:account 已是 Plus(否则 OAuth 能成但无 codex 额度);.env 配好 SUB2API_*。
"""

import argparse
import asyncio
import glob
import json
import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from playwright.async_api import async_playwright

from config import (SUB2API_URL, SUB2API_EMAIL, SUB2API_PASSWORD, SUB2API_GROUP,
                    CPA_URL, CPA_MGMT_KEY)
from common.browser import open_and_connect, teardown
from common.uploaders import _origin, upload_cpa
from common.session_export import build_cpa_codex_json_from_oauth
from common import oauth_codex as ox


def _sanitize(cookies):
    out = []
    for c in cookies:
        nc = {k: c[k] for k in ("name", "value", "domain", "path", "httpOnly", "secure") if k in c}
        if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0:
            nc["expires"] = c["expires"]
        ss = c.get("sameSite")
        nc["sameSite"] = ss if ss in ("Strict", "Lax", "None") else "Lax"
        out.append(nc)
    return out


def _pick_cookie_file(arg):
    if arg:
        return arg
    fs = sorted(glob.glob("cookies/chatgpt/full_*.json"), key=os.path.getmtime, reverse=True)
    return fs[0] if fs else None


async def main():
    parser = argparse.ArgumentParser(description="Codex OAuth -> SUB2API(带 refresh_token)")
    parser.add_argument("--cookie", help="cookie 文件路径(默认最新 full_*.json)")
    parser.add_argument("--group", default=SUB2API_GROUP, help="SUB2API 目标分组(默认 config)")
    parser.add_argument("--timeout", type=int, default=120, help="授权捕获超时秒")
    parser.add_argument("--manual-phone", action="store_true",
                        help="add-phone 手动模式:不接码,自己在浏览器填号+输码(如 WhatsApp 码)")
    parser.add_argument("--skip-cpa", action="store_true",
                        help="不把 OAuth 凭据推到 CPA(默认 CPA 配好就推,带真 refresh_token)")
    parser.add_argument("--keep", action="store_true", help="失败保留窗口")
    args = parser.parse_args()

    # 手动填号收码需要人操作时间，超时给足
    timeout = max(args.timeout, 300) if args.manual_phone else args.timeout

    if not (SUB2API_URL and SUB2API_EMAIL and SUB2API_PASSWORD):
        print("  [FAIL] SUB2API 未配置(.env: SUB2API_URL/EMAIL/PASSWORD)")
        sys.exit(1)

    cookie_file = _pick_cookie_file(args.cookie)
    if not cookie_file or not os.path.isfile(cookie_file):
        print("  [FAIL] 找不到 cookie 文件")
        sys.exit(1)
    print(f"  cookie: {cookie_file}")
    cookies = _sanitize(json.load(open(cookie_file, encoding="utf-8")))

    origin = _origin(SUB2API_URL)
    ok = False
    async with async_playwright() as p:
        bb = pid = None
        try:
            bb, pid, browser, ctx, page = await open_and_connect(name="codex_oauth", p=p)
            await ctx.clear_cookies()
            await ctx.add_cookies(cookies)
            await page.goto("https://chatgpt.com/", timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(4)

            # 确认登录态。chatgpt.com 带 cookie 进来常有客户端重定向(onboarding/auth)，
            # evaluate 撞上导航会 "Execution context was destroyed" —— 重试几次等页面稳定。
            sess = None
            for attempt in range(6):
                try:
                    sess = await page.evaluate(
                        "() => fetch('/api/auth/session',{credentials:'include'}).then(r=>r.ok?r.json():null).catch(()=>null)")
                except Exception as e:
                    print(f"  session 抓取重试 {attempt+1}/6: {str(e)[:60]}")
                    await asyncio.sleep(3)
                    continue
                if sess and sess.get("accessToken"):
                    break
                await asyncio.sleep(3)
            if not sess or not sess.get("accessToken"):
                print("  [FAIL] cookie 未生效/已登出，拿不到 session")
                return
            email = sess.get("user", {}).get("email", "")
            plan = sess.get("account", {}).get("planType")
            print(f"  登录态 OK: {email}  planType={plan}")
            if plan != "plus":
                print(f"  [WARN] 当前 planType={plan}，非 plus —— OAuth 能成但可能无 codex 额度")

            # SUB2API: 登录 + 找分组 + 生成授权链接
            token = ox.sub2api_login(origin, SUB2API_EMAIL, SUB2API_PASSWORD)
            group_id = ox.find_group_id(origin, token, args.group)
            auth_url, session_id, state = ox.generate_auth_url(origin, token)
            print(f"  SUB2API: group={args.group}(#{group_id}) session_id={session_id[:12]}...")

            # 浏览器驱动授权
            print(f"  打开授权页，捕获回调(上限 {timeout}s){'，add-phone 手动模式' if args.manual_phone else ''}...")
            code, cb_state, msg = await ox.drive_authorize(page, auth_url, timeout=timeout,
                                                           debug_dump="oauth_authorize_dump.html",
                                                           account_email=email,
                                                           manual_phone=args.manual_phone)
            if not code:
                print(f"  [FAIL] 授权未完成: {msg}")
                return
            print(f"  捕获回调: code={code[:10]}... state匹配={cb_state==state}")

            # 换码 + 建号
            exch = ox.exchange_code(origin, token, session_id, code, cb_state or state)
            cred = ox.build_oauth_credentials(exch)
            print(f"  exchange-code OK: refresh_token={'YES' if cred.get('refresh_token') else 'NO'} "
                  f"plan={cred.get('plan_type')} email={cred.get('email')}")
            acct = ox.create_oauth_account(origin, token, cred, [group_id], name=cred.get("email") or email)
            acct_id = (acct or {}).get("id")
            print(f"  [OK] SUB2API 账号已创建 #{acct_id}（type=oauth，带 refresh_token）✅")
            ok = True

            # 同一份带真 refresh_token 的 OAuth 凭据，也推到 CPA（best-effort，不影响成功判定）
            if args.skip_cpa:
                print("  [CPA] --skip-cpa，跳过 CPA 推送")
            elif not (CPA_URL and CPA_MGMT_KEY):
                print("  [CPA] 未配置(CPA_URL/CPA_MGMT_KEY)，跳过 CPA 推送")
            else:
                try:
                    cpa = build_cpa_codex_json_from_oauth(cred, email=cred.get("email") or email)
                    cok, cmsg = upload_cpa(CPA_URL, CPA_MGMT_KEY, cpa["auth_json"], cpa["file_name"])
                    print(f"  [CPA] {'OK' if cok else 'FAIL'}（refresh_token={'YES' if cpa['has_refresh_token'] else 'NO'}）"
                          f" {cpa['file_name']} - {cmsg}")
                except Exception as e:
                    print(f"  [CPA] 推送异常: {e}")
        except Exception as e:
            print(f"  ERROR: {e}")
        finally:
            if bb and pid:
                await teardown(bb, pid, delete=not (args.keep and not ok))

    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    asyncio.run(main())
