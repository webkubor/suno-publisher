#!/usr/bin/env python3
"""从汽水音乐后台同步作品状态到本仓台账。

    .venv/bin/python scripts/sync_qishui.py [--dry-run]

从 voxflow 的 `scripts/sync_qishui.py` 移植（2026-10-11）：驱动从
`ego-browser nodejs` 换成 `core/browser.py`（lite-browser）。

## 为什么要浏览器

汽水后台没有开放 API，登录态在 Cookie 里。lite-browser 直接复用浏览器
profile 的登录态，不用存密码、不用扫码。

**ego 版的「选任务空间」那段整个删掉了** —— 那是 ego 特有的概念（每个空间
一个独立 profile，登录态不共享，所以要挑「已登录汽水」的那个）。lite-browser
只有一个 profile，登录态天然共享，那段代码没有对应物。

## 只补不改

跟 `sync_suno.py` 同一个原则：只用云端状态覆盖**平台侧字段**
（状态、发行时间、平台曲名、song_url），本地的歌词、封面、收益记录一个字不动。

⚠️ **多账号**：汽水按登录的账号显示作品。如果一条本地记录在后台查不到，
**不会**把它改成「未发布」—— 它可能只是在另一个账号下。查不到的只做提示。

## 列表不是标准表格

实测 `querySelectorAll("tr")` 取不到行 —— 它是 div 拼的网格。所以从
**状态文案**这个锚点往上找容器，容器文本就是一整行。好处是不依赖 DOM 标签，
平台换布局也不容易断。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser, db  # noqa: E402

DRY = "--dry-run" in sys.argv
CONSOLE = "https://music.douyin.com/console/songs"

# 后台的文案 → 本仓状态。
#
# 「审核中」和「已发行」必须分开：过审之后还要人点一次「确认发行」才真上线，
# 混成 online 会让还没上线的歌在台账里显示已发行，收益口径也跟着多算。
# 真正没过审的是「审核未通过」，它会连带在列表里显示驳回原因。
STATUS_MAP = {
    "字节系平台已发行": "online",
    "全球已发行": "online",
    "已发行": "online",
    "代理发行中": "online",
    "审核中": "reviewing",
    "发行审核中": "reviewing",
    "审核未通过": "draft",
    "草稿": "draft",
}

_EXTRACT = r"""JSON.stringify((() => {
  const STATUS = ["字节系平台已发行", "全球已发行", "已发行", "代理发行中",
                  "发行审核中", "审核中", "审核未通过", "草稿"];
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll("*")) {
    if (el.children.length) continue;
    const txt = (el.textContent || "").trim();
    if (!STATUS.includes(txt)) continue;
    // 往上找到既含状态、又含日期的那一层 —— 那就是一行
    let row = el;
    for (let i = 0; i < 8 && row; i++) {
      const t = row.textContent || "";
      if (t.includes(txt) && /\d{4}-\d{2}-\d{2}/.test(t) && t.length < 400) break;
      row = row.parentElement;
    }
    if (!row) continue;
    const flat = (row.textContent || "").replace(/\s+/g, " ").trim();
    if (seen.has(flat)) continue;
    seen.add(flat);
    const link = row.querySelector('a[href*="/console/songs/detail/"], a[href*="/console/albums/detail/"]');
    const cells = [...row.querySelectorAll("*")]
      .filter(e => !e.children.length && (e.textContent || "").trim())
      .map(e => (e.textContent || "").trim());
    out.push({ cells, detail: link ? link.getAttribute("href") : "" });
  }
  return out;
})())"""


def fetch_rows() -> list[dict]:
    browser.open_page(CONSOLE)
    browser.require_logged_in("汽水音乐控制台")
    # **轮询等，别固定 sleep**：这个控制台是异步渲染的，固定等要么白等、
    # 要么赶不上，而两种失败都表现为「读不到数据」，分不清原因。
    if not browser.wait_for("!!document.body", timeout=30, desc="页面主体"):
        raise SystemExit("✗ 30 秒了页面主体还没出来 —— 控制台可能打不开或卡住了")
    # 等真正要的东西：出现一个已知状态文案
    browser.wait_for(
        """[...document.querySelectorAll("*")].some(e => !e.children.length &&
           ["已发行","审核中","草稿","审核未通过","字节系平台已发行","全球已发行"]
             .includes((e.textContent||"").trim()))""",
        timeout=45, desc="作品列表")
    rows = browser.evaluate(_EXTRACT)
    if not isinstance(rows, list):
        raise SystemExit(f"✗ 读不到后台列表（返回 {type(rows).__name__}）")
    return rows


def parse(row: dict) -> dict | None:
    """一行单元格 → {title, status, date, url, song_id}。

    单元格顺序不固定，所以**按内容特征认**而不是按下标取：
    状态是 STATUS_MAP 的键、日期是 YYYY-MM-DD、其余第一个非空当歌名。
    """
    cells = [c for c in (row.get("cells") or []) if c]
    if not cells:
        return None
    status = next((STATUS_MAP[c] for c in cells if c in STATUS_MAP), "")
    if not status:
        return None
    date = next((c for c in cells if len(c) == 10 and c[4] == "-" and c[7] == "-"), "")
    title = next((c for c in cells
                  if c not in STATUS_MAP and c != date and len(c) > 1), "")
    detail = row.get("detail") or ""
    song_id = ""
    if "/songs/detail/" in detail:
        song_id = detail.rsplit("/", 1)[-1].split("?")[0]
    return {
        "title": title.strip(),
        "status": status,
        "date": date,
        "url": f"https://music.douyin.com{detail}" if detail else "",
        "song_id": song_id,
    }


def local_rows() -> dict[str, dict]:
    """本仓 qishui 的上架记录，按 platform_title 索引。"""
    db.init()
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, platform_title, status, song_id, clip_id, audio_sha256 "
            "FROM listings WHERE platform='qishui'").fetchall()
    return {(r["platform_title"] or "").strip(): dict(r) for r in rows if r["platform_title"]}


def main() -> int:
    raw = fetch_rows()
    parsed = [p for p in (parse(r) for r in raw) if p]
    if not parsed:
        print("✗ 后台一条作品都没读到。两种可能，别混为一谈：")
        print("  · 没登录：会被重定向到登录页（那种情况前面已经拦下了，所以多半不是）")
        print("  · 平台改版：状态文案或 DOM 结构变了，需要更新 _EXTRACT / STATUS_MAP")
        return 1
    print(f"后台读到 {len(parsed)} 条")

    local = local_rows()
    changed = missing = 0
    for r in parsed:
        hit = local.get(r["title"])
        if not hit:
            missing += 1
            continue
        if hit["status"] == r["status"]:
            continue
        print(f"  ~ {r['title']}：{hit['status']} → {r['status']}"
              + (f"  发行 {r['date']}" if r["date"] else ""))
        changed += 1
        if DRY:
            continue
        sets, args = ["status=?", "updated_at=?"], [r["status"], db._now()]
        if r["date"]:
            sets.append("publish_date=?")
            args.append(r["date"])
        if r["url"]:
            sets.append("song_url=?")
            args.append(r["url"])
        if r["song_id"]:
            sets.append("song_id=?")
            args.append(r["song_id"])
        args.append(hit["id"])
        with db.connect() as c:
            c.execute(f"UPDATE listings SET {', '.join(sets)} WHERE id=?", args)  # noqa: S608

    # 本地记着已上线、后台却没有 —— 只提示，**绝不动数据**（可能在别的账号下）
    back = {r["title"] for r in parsed}
    for title, hit in local.items():
        if title not in back and hit["status"] in ("online", "published"):
            print(f"  ! 本地记着已上线、这个账号后台没有：{title}"
                  f"（可能在别的汽水账号下，未改动）")

    print(f"\n{'（预演）' if DRY else ''}状态更新 {changed} 首，"
          f"后台有本地无 {missing} 首")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except browser.NotLoggedIn as e:
        print(f"✗ {e}")
        sys.exit(3)
    except browser.BrowserUnavailable as e:
        print(f"✗ {e}")
        sys.exit(1)
    finally:
        browser.close()
