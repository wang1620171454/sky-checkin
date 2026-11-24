#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sky.skyxing.cn 多账号签到 —— 青龙版（替代 KSU 模块里的 shell 脚本）

为什么单独写一版：
  shell 版靠 busybox wget 抓 Set-Cookie，依赖设备上的 busybox 实现，
  而且 JSON 全靠 sed/grep 抠，字段一变就崩。青龙里直接跑 Python 更省事。

青龙配置：
  1) 脚本放到 /ql/data/scripts/（网页版文件管理可上传）
  2) 环境变量 SKY_ACCOUNTS，多账号用英文分号分隔：
       user1|pass1|备注1;user2|pass2|备注2
     （也支持把账号写进同目录 sky_accounts.conf，一行一个 user|pass|note）
  3) 可选 SKY_WEBHOOK：企业微信/飞书/钉钉机器人地址，结果推一份过去
  4) 定时规则示例：0 8 * * *

退出码：0 全部成功；1 有失败；2 配置缺失
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

HOST = "sky.skyxing.cn"
ALT_IP = "111.223.13.41"          # 备用直连 IP，可用环境变量 SKY_ALT_IP 覆盖
UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")
TIMEOUT = 20
PROXY = (os.getenv("SKY_PROXY") or "").strip()   # 可选：http://user:pass@host:port
RETRY = int(os.getenv("SKY_RETRY") or 3)         # 连接被重置时的重试次数
GAP = float(os.getenv("SKY_GAP") or 6)           # 每次请求之间的间隔（秒）

_OPENER = None


def opener():
    """有代理就走代理。站点对境外 IP 直接 502（实测 GitHub runner 全站 502），
    所以境外机器必须借国内出口。"""
    global _OPENER
    if _OPENER is None:
        handlers = []
        if PROXY:
            handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
        _OPENER = urllib.request.build_opener(*handlers)
    return _OPENER


def log(msg):
    import datetime
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def _ctx(verify):
    if verify:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def request(path, *, method="GET", payload=None, cookie=None, via_ip=False):
    """返回 (status, headers, body_text)。via_ip=True 时直连 IP + 手写 Host 头。"""
    if via_ip:
        url = f"https://{ALT_IP}{path}"
        verify = False
    else:
        url = f"https://{HOST}{path}"
        verify = True

    data = None
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if via_ip:
        headers["Host"] = HOST

    # 站点对突发请求会直接 RST（SSL EOF），所以连接层错误要退避重试。
    # HTTP 4xx/5xx 不算连接错误，重试没意义，直接返回。
    last = (0, {}, "no attempt")
    for attempt in range(1, RETRY + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with opener().open(req, timeout=TIMEOUT) as r:
                return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            return e.code, e.headers, body
        except Exception as e:
            last = (0, {}, f"EXC {type(e).__name__}: {e}")
            if attempt < RETRY:
                wait = min(15, 3 * attempt)
                log(f"    连接被重置，{wait}s 后重试（{attempt}/{RETRY}）")
                time.sleep(wait)
    return last


def login(user, pw):
    """登录并返回 cookie 串；两个入口（域名 / 直连 IP）都试。"""
    for via_ip in (False, True):
        status, headers, body = request(
            "/api/auth/login", method="POST",
            payload={"username": user, "password": pw, "remember": True},
            via_ip=via_ip,
        )
        tag = "ip" if via_ip else "domain"
        # 注意：服务端对失败请求也会下发游客会话 cookie（server_session_xxx），
        # 所以「有 cookie」不等于「登录成功」，必须看响应体里的 ok 字段。
        pairs = []
        try:
            cookies = headers.get_all("Set-Cookie") if hasattr(headers, "get_all") else []
        except Exception:
            cookies = []
        for c in cookies or []:
            head = c.split(";", 1)[0].strip()
            if head and "=" in head:
                pairs.append(head)

        try:
            obj = json.loads(body)
        except Exception:
            obj = {}

        if obj.get("ok") is True and pairs:
            log(f"  登录[{tag}] ok，cookie {len(pairs)} 段")
            return "; ".join(pairs), status

        msg = obj.get("message") or obj.get("code") or body[:100]
        log(f"  登录[{tag}] 失败 status={status} {msg}")
    return "", 0


def checkin(cookie):
    """签到；返回 (是否成功, points, balance, 原始响应)。"""
    for via_ip in (False, True):
        status, _headers, body = request(
            "/api/points/check-in", method="POST", payload={}, cookie=cookie, via_ip=via_ip,
        )
        tag = "ip" if via_ip else "domain"
        try:
            obj = json.loads(body)
        except Exception:
            log(f"  签到[{tag}] 非 JSON status={status} body={body[:120]}")
            continue

        # 注意：不能用 '"ok"' 匹配，否则 "ok":false 也会被当成成功
        if obj.get("ok") is True:
            return True, obj.get("points"), obj.get("balance"), body
        log(f"  签到[{tag}] 返回 ok=false body={body[:160]}")
    return False, None, None, ""


def load_accounts():
    raw = (os.getenv("SKY_ACCOUNTS") or "").strip()
    rows = []
    if raw:
        rows = [x.strip() for x in raw.split(";") if x.strip()]
    else:
        conf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sky_accounts.conf")
        if os.path.isfile(conf):
            with open(conf, "r", encoding="utf-8") as f:
                rows = [ln.strip() for ln in f if ln.strip()]
    accounts = []
    for ln in rows:
        if ln.startswith("#"):
            continue
        parts = ln.split("|")
        if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
            log(f"跳过格式不对的账号行: {ln[:24]}")
            continue
        user = parts[0].strip()
        pw = parts[1].strip()
        note = parts[2].strip() if len(parts) > 2 else ""
        accounts.append((user, pw, note or user))
    return accounts


def notify(webhook, title, text):
    if not webhook:
        return
    body = json.dumps({"title": title, "body": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
        log("webhook 已推送")
    except Exception as e:
        log(f"webhook 推送失败: {e}")


def main():
    global ALT_IP
    ALT_IP = (os.getenv("SKY_ALT_IP") or ALT_IP).strip()
    webhook = (os.getenv("SKY_WEBHOOK") or "").strip()

    accounts = load_accounts()
    if not accounts:
        log("没有读到账号：配 SKY_ACCOUNTS 或同目录 sky_accounts.conf")
        return 2

    log(f"共 {len(accounts)} 个账号，开始")
    ok = fail = 0
    total_points = 0
    lines = []

    for idx, (user, pw, name) in enumerate(accounts):
        if idx:
            time.sleep(GAP)
        log(f"账号 {user} 开始")
        cookie, _st = login(user, pw)
        if not cookie:
            fail += 1
            lines.append(f"{name} 登录失败")
            continue
        good, points, balance, raw = checkin(cookie)
        if good:
            ok += 1
            p = points if isinstance(points, int) else 0
            total_points += p
            lines.append(f"{name} +{p}（余{balance if balance is not None else '?'}）")
            log(f"  {user} 签到成功 {raw[:120]}")
        else:
            fail += 1
            lines.append(f"{name} 签到失败")
            log(f"  {user} 签到失败")

    summary = "\n".join(lines)
    log(f"完成：成功 {ok} 失败 {fail} 合计 +{total_points}")
    title = f"Sky 签到完成 +{total_points}" if fail == 0 else f"Sky 签到：失败 {fail} 个"
    notify(webhook, title, summary)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
