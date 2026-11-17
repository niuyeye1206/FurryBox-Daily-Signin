import os
import sys
import json
import time
import base64
import requests

SUPABASE_URL = "https://pwfcgdrorwpdzatxavdt.supabase.co"
AUTH_FN = f"{SUPABASE_URL}/functions/v1/auth"
TOKEN_ENDPOINT = f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token"

# Gist 持久化
GIST_ID = os.environ.get("GIST_ID", "").strip()
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GIST_FILENAME = "furrybox.json"

HEADERS_BASE = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://my.furry.box",
    "referer": "https://my.furry.box/",
    "user-agent": (
        "Mozilla/5.0 (Linux; Android 16; Pixel 10) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/157.0.0.0 Mobile Safari/537.36"
    ),
}


def decode_jwt_payload(token: str):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def jwt_exp(token: str):
    return int(decode_jwt_payload(token).get("exp", 0) or 0)


# ---------------- Gist 持久化 ----------------

def gist_load():
    if not (GIST_ID and GH_TOKEN):
        return None
    try:
        r = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if r.status_code == 200:
            files = r.json().get("files", {})
            if GIST_FILENAME in files:
                return json.loads(files[GIST_FILENAME]["content"])
    except Exception as e:
        print(f"⚠️ 读取 Gist 失败: {e}")
    return None


def gist_save(tokens: dict):
    if not (GIST_ID and GH_TOKEN):
        print("ℹ️ 未配置 Gist，跳过持久化")
        return
    try:
        r = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"files": {GIST_FILENAME: {"content": json.dumps(tokens, indent=2)}}},
            timeout=15,
        )
        if r.status_code == 200:
            print("💾 Token 已写回 Gist")
        else:
            print(f"⚠️ 写 Gist 失败: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ 写 Gist 异常: {e}")


# ---------------- Telegram ----------------

def get_wallet(access: str):
    """查钱包余额，返回 (balance, total_earned) 或 (None, None)。"""
    try:
        r = requests.post(
            AUTH_FN,
            headers={**HEADERS_BASE, "authorization": f"Bearer {access}"},
            json={"action": "wallet"},
            timeout=20,
        )
        if r.status_code == 200:
            w = r.json().get("wallet", {})
            return w.get("balance"), w.get("total_earned")
    except Exception as e:
        print(f"⚠️ 查钱包失败: {e}")
    return None, None


def notify(text: str):
    bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    if not (bot_token and chat_id):
        print("ℹ️ 未配置 TG，跳过通知")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        print("📨 TG 已发送" if r.status_code == 200 else f"⚠️ TG 失败: {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ TG 异常: {e}")


# ---------------- Refresh ----------------

def refresh_tokens(refresh_token: str):
    """
    返回新的 dict {access_token, refresh_token} 或 None。
    先试 Edge Function（自定义），再试 Supabase 标准端点（需要 apikey）。
    """
    # ---- 方式 A: Edge Function ----
    try:
        r = requests.post(
            AUTH_FN,
            headers={**HEADERS_BASE, "authorization": "Bearer anonymous"},
            json={"action": "refresh", "refresh_token": refresh_token},
            timeout=20,
        )
        print(f"[refresh-A] HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code == 200:
            d = r.json()
            if d.get("access_token"):
                return {
                    "access_token": d["access_token"],
                    "refresh_token": d.get("refresh_token", refresh_token),
                }
    except Exception as e:
        print(f"[refresh-A] 异常: {e}")

    # ---- 方式 B: Supabase 标准端点 ----
    apikey = os.environ.get("FURRYBOX_APIKEY", "").strip()
    if apikey:
        try:
            r = requests.post(
                TOKEN_ENDPOINT,
                headers={"apikey": apikey, "content-type": "application/json"},
                json={"refresh_token": refresh_token},
                timeout=20,
            )
            print(f"[refresh-B] HTTP {r.status_code}: {r.text[:300]}")
            if r.status_code == 200:
                d = r.json()
                if d.get("access_token"):
                    return {
                        "access_token": d["access_token"],
                        "refresh_token": d.get("refresh_token", refresh_token),
                    }
        except Exception as e:
            print(f"[refresh-B] 异常: {e}")
    else:
        print("ℹ️ 未提供 FURRYBOX_APIKEY，跳过标准端点刷新")

    return None


# ---------------- 主流程 ----------------

def main():
    # 1. 读 token（Gist 优先，env 兜底）
    tokens = gist_load() or {}
    access = (tokens.get("access_token") or os.environ.get("FURRYBOX_ACCESS_TOKEN", "")).strip()
    refresh = (tokens.get("refresh_token") or os.environ.get("FURRYBOX_REFRESH_TOKEN", "")).strip()

    if access.lower().startswith("bearer "):
        access = access[7:].strip()

    if not access and not refresh:
        msg = "❌ <b>FurryBox</b>\n未找到任何 Token"
        print(msg); notify(msg); return 1

    # 2. 判断是否要刷新（提前 5 分钟）
    now = int(time.time())
    exp = jwt_exp(access) if access else 0
    print(f"当前 access_token exp={exp}, now={now}, 剩余 {exp - now} 秒")
    refreshed = False

    if exp - now < 86400:
        if not refresh:
            msg = "❌ <b>FurryBox</b>\nAccess Token 已过期且无 refresh_token"
            print(msg); notify(msg); return 1
        print("🔄 需要刷新 Token...")
        new = refresh_tokens(refresh)
        if not new:
            msg = (
                "❌ <b>FurryBox 签到失败</b>\n"
                "Token 已过期且刷新失败，需要重新抓包\n"
                "请更新 Secret <code>FURRYBOX_ACCESS_TOKEN</code> / <code>FURRYBOX_REFRESH_TOKEN</code>"
            )
            print(msg); notify(msg); return 1
        access, refresh = new["access_token"], new["refresh_token"]
        gist_save({"access_token": access, "refresh_token": refresh})
        refreshed = True
        print(f"✅ 刷新成功，新 exp 剩余 {jwt_exp(access) - int(time.time())} 秒")

    # 3. 签到
    headers = {**HEADERS_BASE, "authorization": f"Bearer {access}"}
    try:
        resp = requests.post(AUTH_FN, headers=headers, json={"action": "signin"}, timeout=30)
    except requests.RequestException as e:
        msg = f"❌ <b>FurryBox 签到异常</b>\n<code>{e}</code>"
        print(msg); notify(msg); return 1

    print(f"HTTP {resp.status_code}\nBody: {resp.text[:1000]}")
    try:
        data = resp.json()
    except Exception:
        data = {}

    # 4. 判定结果 + 通知
    # token 状态行：让每条通知都能看出本次运行干了什么
    exp_left = jwt_exp(access) - int(time.time())
    if refreshed:
        token_line = f"Token 已刷新，有效期还剩 {exp_left // 60} 分钟"
    else:
        token_line = f"Token 正常，有效期还剩 {exp_left // 60} 分钟"
    # 查一次钱包余额，通知里带上
    balance, _total = get_wallet(access)
    balance_line = f"当前余额：<b>{balance}</b> boxes" if balance is not None else ""

    if data.get("error") == "ALREADY_SIGNED_IN":
        msg = (
            "✅ <b>FurryBox 签到检查完成</b>\n"
            "今日已签到，无需重复操作。\n"
            f"{balance_line}\n"
            f"{token_line}"
        )
        print(msg); notify(msg); return 0

    if resp.status_code in (200, 201, 204) and not data.get("error"):
        reward = data.get("reward") or data.get("boxes") or 5
        if balance is None:
            balance = data.get("balance") or data.get("total") or "?"
        msg = (
            "✅ <b>FurryBox 签到成功</b>\n"
            f"本次奖励：<b>{reward}</b> boxes\n"
            f"当前余额：<b>{balance}</b> boxes\n"
            f"{token_line}"
        )
        print(msg); notify(msg); return 0

    err = data.get("error") or f"HTTP_{resp.status_code}"
    detail = data.get("message", "")
    hint = ""
    if resp.status_code in (401, 403):
        hint = "\n提示：Token 可能已失效，请重新抓包"
    msg = f"❌ <b>FurryBox 签到失败</b>\n错误：<code>{err}</code>\n{detail}{hint}"
    print(msg); notify(msg); return 1


if __name__ == "__main__":
    sys.exit(main())
