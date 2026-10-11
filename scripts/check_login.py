#!/usr/bin/env python3
"""探一次平台控制台的登录态，输出 JSON。

    .venv/bin/python scripts/check_login.py <platform>

    {"ok": true, "platform": "qishui", "logged_in": true, "url": "...",
     "title": "...", "account": "月栖洲", "hint": "已登录：月栖洲"}

从 voxflow 的 `scripts/check_login.py` 移植（2026-10-11）：驱动换成
`core/browser.py`（lite-browser），不再是被 browser-harness 喂进去的 stdin 程序。

## 判断依据：看最终 URL，**不看页面上有没有「登录」二字**

已登录的页面上也可能有「登录设备管理」这类字样 —— 按文本判断会误判成没登录，
然后让人反复去登录一个本来就登录着的账号。

所以主判据是最终 URL：跳到 `/login`、`/passport`、`/sign` 就是没登录。
（`core/browser.py` 的 `probe_login` 里还留了一条「页面上有没有**恰好是**
『登录』的入口」作为补充判据 —— 它要求整段文本精确等于「登录」，
所以「登录设备管理」不会命中。）

## 为什么还要读账号名

「已登录」只说对了一半：这台机器上**哪个账号**登着，决定这次发布会不会发错人名下。
所以尽量把账号名也读回来；读不到就如实说「没读到」，不编。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser  # noqa: E402

# 各平台的控制台入口。**必须是真控制台** —— 有些平台的短路径会跳登录页，
# 看起来像「没登录」，其实登录着（网易云的 /musician 就是，见 README）。
CONSOLES = {
    "qishui": "https://music.douyin.com/",
    "netease": "https://music.163.com/musician/artist/home",
    "tencent": "https://y.qq.com/n/ryqq_v2/singer",
}

# 被踢回登录页时 URL 里会出现的片段
LOGIN_MARKERS = ("/login", "/passport", "/sign", "targetUrl")

# 读账号名：点开账号菜单，找一个含「退出登录」的浮层，取它的首段文字。
# 读不到就返回空串 —— **不编一个名字**，那比「没读到」危险。
_READ_ACCOUNT = r"""JSON.stringify((() => {
  const pops = [...document.querySelectorAll('[class*=dropdown],[class*=popover],[class*=menu]')]
    .filter(e => e.offsetParent)
    .map(e => e.innerText.replace(/\s+/g, ' ').trim());
  const hit = pops.find(t => /退出登录|退出账号|登出/.test(t));
  return hit ? hit.split(' ')[0] : '';
})())"""


def main() -> int:
    platform = (sys.argv[1] if len(sys.argv) > 1 else "qishui").strip()
    url = CONSOLES.get(platform)
    if not url:
        print(json.dumps({"ok": False, "platform": platform,
                          "hint": f"不认识的平台：{platform}（认识：{'/'.join(CONSOLES)}）"},
                         ensure_ascii=False))
        return 2

    browser.open_page(url)
    time.sleep(3)
    final = str(browser.evaluate("location.href") or "")
    title = str(browser.evaluate("document.title") or "")

    logged_in = not any(m in final for m in LOGIN_MARKERS)

    account = ""
    if logged_in:
        try:
            # 点一下右上角，把账号浮层叫出来
            browser.evaluate("""(() => {
              const t = [...document.querySelectorAll('header *, [class*=avatar], [class*=user]')]
                .filter(e => e.offsetParent).pop();
              if (t) t.click();
              return true;
            })()""")
            time.sleep(1.5)
            account = str(browser.evaluate(_READ_ACCOUNT) or "")
            browser.evaluate("document.body.click()")
        except Exception:  # noqa: BLE001 —— 读不到账号不该让「已登录」这个结论失效
            account = ""

    print(json.dumps({
        "ok": True,
        "platform": platform,
        "logged_in": logged_in,
        "url": final,
        "title": title,
        "account": account,
        "hint": (f"已登录：{account}" if account else "已登录（没读到账号名）")
                if logged_in else "被踢回登录页 —— 在浏览器里登录后重试",
    }, ensure_ascii=False))
    return 0 if logged_in else 3      # 3 = 需要人介入


if __name__ == "__main__":
    try:
        sys.exit(main())
    except browser.BrowserUnavailable as e:
        print(json.dumps({"ok": False, "hint": str(e)}, ensure_ascii=False))
        sys.exit(1)
    finally:
        browser.close()
