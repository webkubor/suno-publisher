#!/usr/bin/env python3
"""抓网易云音乐人后台的**单曲**播放量，回填本仓台账。

    .venv/bin/python scripts/sync_netease_stats.py [--dry-run]

从 voxflow 的 `scripts/ncm_track_stats.py` 移植。**改了三处，都是必须改的**：

1. **驱动换成 `lite-browser`**（见 `core/browser.py`）。原版靠 `browser-harness`
   的 `js()` / `new_tab()`，那套运行时已卸载。
2. **写目标换成 `listings`**，且**按关联键认领**，不再靠标题匹配 `tracks`。
   原版走 `UPDATE track_platforms ... WHERE track_id=?` —— 那张表已经没了。
3. **不按标题认曲目**。原版用 `{title: track_id}` 精确匹配，注释里写「精确匹配
   就够」—— 那是在台账本来就是按平台歌名建的假设下成立的。现在两边是不同仓的
   两张表，同名歌会串行（Suno 一次出两首同名），所以按 `platform_title` 匹配后
   **还要有 clip_id / audio_sha256 才敢写**，认不出的一律列出来等人看。

## 口径：近 30 日，不是累计

后台的数据中心只给「近 7 日」和「近 30 日」两档，没有累计。这不算损失：
累计播放量里绝大部分是老作品的沉淀，「最近 30 天哪首在涨」才是选题依据。

## 收益是折算的，不是平台给的

后台单曲表里**没有收益列** —— 收益只有账号级的「可提现税前收益」。所以
单曲收益 = 该曲播放量 × 实测千播单价（账号总收益 ÷ 账号总播放）。

这个折算站得住：网易云按播放计费，同账号下各曲单价基本相同。但它**是折算
不是实测** —— 界面上必须标出来，把折算值当平台实付会让人高估某几首的价值。

## 会随平台改版失效

抓的是渲染后的表格（后台是 Ant Design SPA，没有稳定公开接口）。
失效表现为抓不到（不写库），不会写坏已有数据。

## 需要登录态

脚本不代持登录态。先手动在浏览器里登录网易云音乐人并打开一次数据中心，
之后 `--reuse` 会复用它。没登录时脚本明确报「没登录」而不是「解析出空的」。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser, db  # noqa: E402

DRY = "--dry-run" in sys.argv
RANGE = "近30日"          # 后台只有「近7日」「近30日」两档
MAX_PAGES = 20            # 每页 10 首，20 页够到 200 首；到底了自然提前结束
DATA_CENTER = "https://music.163.com/musician/artist/dataCenter"


def _int(v: str) -> int:
    """表格里没有数据是 '--'，不是 0 —— 但对播放量来说两者都当 0 处理。"""
    try:
        return int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0


def scrape() -> list[dict]:
    browser.open_page(DATA_CENTER)
    browser.require_logged_in("网易云音乐人后台")

    # 切到近 30 日。默认是近 7 日，窗口太短，几首歌只有个位数播放，看不出趋势。
    browser.evaluate(f"""(() => {{
      const t = [...document.querySelectorAll('.mc-tabs-item')]
        .find(e => e.innerText.trim() === '{RANGE}')
      if (t) t.click()
      return !!t
    }})()""")
    time.sleep(4)

    if not browser.evaluate("!!document.querySelector('table tbody tr')"):
        raise SystemExit(
            "✗ 页面在但抓不到表格 —— 后台可能改版了（选择器失效）。\n"
            "  注意这和「没登录」是两回事：没登录会被重定向到登录页，前面就拦下了。")

    rows: list[list[str]] = []
    seen_first = None
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            browser.evaluate(f"""(() => {{
              const pag = document.querySelector('.ant-pagination, [class*=pagination]')
              const li = [...(pag?.querySelectorAll('li') || [])]
                .find(e => e.innerText.trim() === '{page}')
              if (li) (li.querySelector('a') || li).click()
              return !!li
            }})()""")
            time.sleep(3.5)

        page_rows = browser.evaluate("""JSON.stringify(
            [...document.querySelectorAll('table tbody tr')]
              .map(tr => [...tr.querySelectorAll('td')].map(td => td.innerText.trim())))""")
        if not page_rows:
            break
        # 翻页失败时页面内容不变，会把同一页重复收进来 —— 用首行判重，
        # 比信任「点了就翻了」可靠（Ant 的分页在末页点击是无响应的）。
        first = page_rows[0][1] if len(page_rows[0]) > 1 else None
        if first and first == seen_first:
            break
        seen_first = first
        rows.extend(page_rows)
        if len(page_rows) < 10:      # 不满一页 = 最后一页
            break

    return [{"song": r[1], "album": r[2], "plays": _int(r[3])}
            for r in rows if len(r) > 3 and r[1]]


def backfill(tracks: list[dict]) -> tuple[int, list[str], list[str]]:
    """按 platform_title 找 listings，**但必须有真实关联键才写**。

    认不出分两类，都要如实报出来而不是静默丢掉：
      · `no_row`      —— 台账里没有这个平台歌名
      · `no_link`     —— 有这一行，但它的 link_state=unlinked（还没认回作品）

    第二类尤其要注意：写进去等于给一条「还不知道是哪首歌」的记录填了播放量，
    看着有数、实际无法回答「哪首歌在赚钱」——而那是这个脚本存在的唯一理由。
    """
    db.init()
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, platform_title, clip_id, audio_sha256, link_state "
            "FROM listings WHERE platform='netease'").fetchall()
    by_title: dict[str, list] = {}
    for r in rows:
        if r["platform_title"]:
            by_title.setdefault(r["platform_title"], []).append(r)

    now = db._now()
    written = 0
    no_row, no_link = [], []
    for t in tracks:
        cands = by_title.get(t["song"]) or []
        if not cands:
            no_row.append(t["song"])
            continue
        # 多行同名 + 都没关联键 = 分不清是哪个，宁可都不写
        usable = [r for r in cands
                  if (r["clip_id"] or r["audio_sha256"]) and r["link_state"] != "unlinked"]
        if not usable:
            no_link.append(t["song"])
            continue
        if len(usable) > 1:
            # 同名多行且都有关联键：这是真实存在的（Suno 一次出两首同名），
            # 但播放量按歌名给的是**一首**的数，摊到两行上就是编数据。
            no_link.append(f"{t['song']}（{len(usable)} 行同名，分不清）")
            continue
        if DRY:
            written += 1
            continue
        with db.connect() as c:
            c.execute(
                "UPDATE listings SET plays=?, stats_at=?, updated_at=? WHERE id=?",
                (t["plays"], now, now, usable[0]["id"]))
            written += c.total_changes
    return written, no_row, no_link


def main() -> int:
    tracks = scrape()
    if not tracks:
        raise SystemExit("✗ 表格解析出来是空的 —— 列的顺序可能变了")

    # 折算单曲收益：实测千播单价（账号总收益 ÷ 账号总播放）× 该曲播放量。
    rev = obs_rates()
    rate = rev.get("cny_per_1k_plays", 0.0) if rev.get("rate_source") == "measured" else 0.0
    if not rate:
        print("⚠ 还没有账号级实测单价（先跑账号级同步），本次只回填播放量、不折算收益")

    written, no_row, no_link = backfill(tracks)

    now = db._now()
    if rate and not DRY:
        with db.connect() as c:
            for t in tracks:
                # 只给已经认回作品的行折算（link_state='linked'）——
                # 给 unlinked 的行算收益，等于给一个还不知道是哪首歌的记录
                # 编一个「赚了多少」，而那是要拿去做选题决策的数。
                c.execute(
                    "UPDATE listings SET earned_cny = ROUND(plays * ? / 1000, 4), "
                    "updated_at=? WHERE platform='netease' AND platform_title=? "
                    "AND link_state = 'linked'",
                    (rate, now, t["song"]))

    total_plays = sum(t["plays"] for t in tracks)
    active = [t for t in tracks if t["plays"] > 0]
    print(f"{'（预演）' if DRY else ''}✓ {RANGE}：{len(tracks)} 首，"
          f"播放 {total_plays} 次，{len(active)} 首有播放")
    print(f"  回填 {written} 首" + ("（按 platform_title + 关联键）" if not DRY else ""))
    if rate:
        print(f"  收益按实测 ¥{rate:.4f}/千播折算 —— **是折算不是平台实付**，界面上标着")
    if no_row:
        print(f"  ⚠ {len(no_row)} 首台账里没有：{'、'.join(no_row[:4])}")
    if no_link:
        print(f"  ⚠ {len(no_link)} 首有记录但还没认回作品（link_state=unlinked），"
              f"没写：{'、'.join(no_link[:4])}")
        print("     认领入口：SELECT * FROM listings WHERE link_state='unlinked'")
    for t in active[:5]:
        print(f"    {t['plays']:>5} 次  {t['song']}  ({t['album']})")

    return 0


def obs_rates() -> dict:
    """账号级实测单价。没有就返回空 —— 调用方据此跳过收益折算。"""
    from core import revenue
    try:
        return (revenue.platform_rates() or {}).get("netease") or {}
    except Exception as e:  # noqa: BLE001 —— 单价读不到不该让播放量也同步不了
        print(f"⚠ 读单价失败（{e}），本次只同步播放量")
        return {}


if __name__ == "__main__":
    try:
        sys.exit(main())
    except browser.NotLoggedIn as e:
        # 干净的退出而不是 traceback：这不是「脚本崩了」，是缺一个前置条件，
        # 而那个条件本人手动做一次就够了。堆栈只会把这句话淹掉。
        print(f"✗ {e}")
        sys.exit(3)          # 3 = 需要人介入（对齐 lite-browser 的退出码约定）
    except browser.BrowserUnavailable as e:
        print(f"✗ {e}")
        sys.exit(1)
    finally:
        browser.close()