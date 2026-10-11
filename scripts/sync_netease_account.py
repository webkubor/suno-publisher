#!/usr/bin/env python3
"""抓网易云音乐人后台的**账号级**指标（播放量 / 粉丝 / 收益 / 指数）。

    .venv/bin/python scripts/sync_netease_account.py [--dry-run]

从 voxflow 的 `scripts/ncm_stats.py` 移植：驱动换成 lite-browser，写目标换成
本仓的 `platform_accounts`（`catalog.upsert_platform_account`）。

## 为什么和单曲级分开两个脚本

作品目录走公开 API，纯 HTTP 就够（`sync_netease.py`）；只有这些指标**必须
登录**、而且后台是 SPA 没有稳定接口，只能从渲染后的文本里抠。混在一起会让
不需要浏览器的部分也受跨域和登录态的连累。

## 账号级收益是「实测单价」的唯一来源

后台同时给出累计播放量和可提现税前收益，两个一除就是这个账号**实际拿到的
千播单价** —— 比任何公开资料都准，因为它含了实际权益档位。

单曲收益 = 单曲播放量 × 这个单价（见 `sync_netease_stats.py`）。所以
**这个脚本要先跑**，否则单曲侧只能回填播放量、折不出收益。

## ⚠️ 后台入口是 `/musician/artist/home`

`/musician` 是老路径，会跳登录页 —— **看起来像「没登录」，其实登录着**。
原脚本在这上面误判过一次，所以这里把入口写死成新路径，不要「优化」成短路径。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser, catalog, db  # noqa: E402

DRY = "--dry-run" in sys.argv
# 入口写死新路径 —— 见文件头那条 ⚠️
HOME = "https://music.163.com/musician/artist/home"

_JS = r"""JSON.stringify((() => {
  const t = document.body.innerText
  const g = (re) => { const m = t.match(re); return m ? m[1] : null }
  return {
    play_count: g(/播放量[^\d]*([\d.]+[wW万]?)/),
    fans: g(/粉丝\s*\n\s*([\d.]+[wW万]?)/),
    works: g(/作品\(首\)\s*\n\s*(\d+)/),
    withdrawable_cny: g(/可提现税前收益\(元\)[^\d]*([\d.]+)/),
    musician_index: g(/音乐人指数\s*\n\s*(\d+)/),
    play_7d: g(/近7日播放量\s*([\d.]+)\s*次/),
    play_yesterday_delta: g(/昨日新增\s*[↑↓]?\s*(\d+)/),
    roles: g(/网易音乐人\s*\n\s*([^\n]{0,20})/),
  }
})())"""


def main() -> int:
    browser.open_page(HOME)
    browser.require_logged_in("网易云音乐人后台")
    time.sleep(9)      # 后台是 SPA，等到首屏数据渲染完

    stats = browser.evaluate(_JS)
    if not isinstance(stats, dict) or not stats.get("works"):
        print("✗ 抓不到 —— 页面在但没有指标。两种可能，别混为一谈：")
        print("  · 没登录：会被重定向到登录页（那种情况前面已经拦下了，所以多半不是这个）")
        print("  · 平台改版：选择器/文案变了，需要更新 _JS 里的正则")
        print(f"  当前 URL：{browser.evaluate('location.href')}")
        return 1

    stats["synced_at"] = db._now()
    print(f"{'（预演）' if DRY else ''}✓ 播放 {stats['play_count']}"
          f"（昨日 +{stats.get('play_yesterday_delta')}）"
          f" · 粉丝 {stats['fans']} · 作品 {stats['works']} 首")
    print(f"  可提现 ¥{stats['withdrawable_cny']} · 指数 {stats.get('musician_index')}")

    if DRY:
        print("\n（预演，没写库。加 --commit? 这个脚本默认就写；去掉 --dry-run 即可。）")
        return 0

    catalog.upsert_platform_account("netease", stats=stats,
                                    song_count=int(stats.get("works") or 0))

    # 立刻回报这次算出来的实测单价 —— 它是单曲收益折算的分母，
    # 看一眼就知道下一步能不能折收益，不用再跑一次查询。
    from core import revenue
    rev = (revenue.platform_rates() or {}).get("netease") or {}
    if rev.get("rate_source") == "measured":
        print(f"  → 实测千播单价 ¥{rev['cny_per_1k_plays']:.4f}"
              f"（可提现 ÷ 累计播放）。跑 sync_netease_stats.py 折单曲收益。")
    else:
        print("  ⚠ 还没算出实测单价（累计播放量为 0？）—— 单曲收益暂时折不了")
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
