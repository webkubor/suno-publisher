#!/usr/bin/env python3
"""从网易云公开 API 同步账号、专辑、作品目录到本仓台账。

    .venv/bin/python scripts/sync_netease.py [--dry-run]

从 voxflow 的 `scripts/sync_netease.py` 移植（2026-10-11）。

## 为什么这个脚本不需要浏览器

一开始它被写成 browser-harness 脚本，结果作品目录也跟着受浏览器跨域限制的
连累 —— fetch 必须在同源页面下发，标签页一飘就 Failed to fetch。后来发现
**公开 API 用 urllib 直接就能拿**，压根没有理由绕浏览器。

| 数据 | 通道 | 要登录 | 稳定性 |
|---|---|---|---|
| 艺人、专辑、作品目录 | 公开 API `/api/artist/{id}` | 否 | 纯 HTTP，稳 |
| 播放量、粉丝、收益、指数 | 音乐人后台页面 | **是** | 随改版失效 |

所以日常同步这一条命令跑完就够了，只有想更新播放量时才开浏览器
（`sync_netease_account.py` / `sync_netease_stats.py`）。

## 为什么不解析艺人页

网易云对艺人页做了**反爬字符插入** —— 歌名里夹随机汉字，「雪夜无名」在 DOM
里是「雪·要治提·夜无名」。同一份数据在 API 里是干净 JSON。
**能拿数据源就别解析渲染结果。**

## 移植时改了什么（都是必须改的）

| 原来（voxflow） | 现在 |
|---|---|
| `pipeline.upsert_album` | 本仓 `catalog.upsert_album`（同一套字段） |
| `pipeline.upsert_platform_account` | 本仓 `catalog.upsert_platform_account` |
| `pipeline.resolve_track_for_listing` | 换成本仓的**关联键**认领（见下） |
| `pipeline.upsert(tid, ...)` 建孤儿曲目 | **不再建**。曲目是生成侧的事实，本仓不凭空造 voxflow 的 tracks 行 |
| `pipeline.set_platform_status` | 直写本仓 `listings` |

## ⚠️ 孤儿作品不再自动创建

原版对认不出的作品会 `upsert` 出一行 voxflow 曲目（`stage="published"`，
note 写「从网易云回填」）。那是**把平台上的历史作品伪装成本地生成的作品** ——
而本仓根本没有 voxflow 的写权限（曲库不在本仓）。

所以现在这类记录建成 `listings` 行，`link_state='unlinked'`，等人认领。
它的 song_id / 上线日期 / 播放量都是真的，只是还不知道对应哪首本地作品。

幂等，可以随时重跑。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import catalog, db, paths  # noqa: E402

DRY = "--dry-run" in sys.argv
ARTIST_ID = os.environ.get("NCM_ARTIST", "32462959")
# 个人主页和艺人主页是**两个不同的 id**：
#   artist?id=    艺人主页 → 发布表单填它，平台据此核实音乐人身份
#   user/home?id= 个人主页 → 听歌记录/动态，证明不了音乐人身份
USER_ID = os.environ.get("NCM_USER", "116974627")

COVER_DIR = paths.DATA_DIR / "library" / "covers" / "netease"
NOW = db._now()


def fetch(url: str) -> dict:
    """网易云公开接口。必须带 Referer —— 不带会被当盗链拒掉。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://music.163.com/",
    })
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        return json.loads(r.read().decode())


def ymd(ms) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000)) if ms else ""


def _rel(p: Path) -> str:
    """相对本仓数据目录的路径（listings.cover_local 存的是相对路径）。"""
    try:
        return str(p.relative_to(paths.DATA_DIR))
    except ValueError:
        return str(p)


def sync_albums(albums: list[dict]) -> int:
    for al in albums:
        cover = COVER_DIR / f"{al['id']}.jpg"
        if DRY:
            continue
        catalog.upsert_album(
            f"netease-{al['id']}",
            platform="netease", album_id=str(al["id"]), title=al["name"],
            track_count=al.get("size", 0), publish_date=ymd(al.get("publishTime")),
            company=al.get("company") or "",
            description=(al.get("description") or "")[:500],
            tags=al.get("tags") or "", cover_url=al.get("picUrl") or "",
            cover_local=_rel(cover) if cover.exists() else "",
            url=f"https://music.163.com/#/album?id={al['id']}", synced_at=NOW,
        )
    return len(albums)


def sync_listings(songs: list[dict]) -> tuple[int, int, int]:
    """作品 → listings（走 catalog.upsert_listing，三个平台共用同一套写入规则）。

    **不再凭空造 voxflow 曲目**（原版会 `pipeline.upsert` 一行 stage=published
    的本地曲目 —— 那是把平台上的历史作品伪装成本地生成的，而曲库不在本仓）。

    返回 (新增, 更新, 无关联键)。
    """
    added = updated = orphan = 0
    unlinked_ids: set[int] = set()
    for s in songs:
        title = (s["name"] or "").strip()
        sid = str(s["id"])
        album = s.get("album") or {}
        cover = COVER_DIR / f"{album.get('id')}.jpg"
        if DRY:
            updated += 1
            continue
        how, rid = catalog.upsert_listing(
            "netease", sid,
            platform_title=title,
            song_url=f"https://music.163.com/#/song?id={sid}",
            status="online",
            album_key=f"netease-{album.get('id')}" if album.get("id") else "",
            album_name=album.get("name") or "",
            track_no=s.get("no"),
            duration=round((s.get("duration") or 0) / 1000),
            publish_date=ymd(album.get("publishTime")),
            cover_url=album.get("picUrl") or "",
            cover_local=_rel(cover) if cover.exists() else "",
        )
        if how == "added":
            added += 1
        else:
            updated += 1
        unlinked_ids.add(rid)

    # 数一遍这批里还有多少没认领（认领状态不受本次写入影响，所以查一次就够）
    if not DRY and unlinked_ids:
        db.init()
        ph = ",".join("?" * len(unlinked_ids))
        with db.connect() as c:
            orphan = c.execute(
                f"SELECT COUNT(*) FROM listings WHERE id IN ({ph}) "  # noqa: S608
                f"AND link_state='unlinked'", tuple(unlinked_ids)).fetchone()[0]
    return added, updated, orphan


def main() -> int:
    print(f"艺人 {ARTIST_ID} —— 拉作品目录")
    a_resp = fetch(f"https://music.163.com/api/artist/{ARTIST_ID}")
    al_resp = fetch(f"https://music.163.com/api/artist/albums/{ARTIST_ID}?limit=100")

    art = a_resp.get("artist")
    if not art:
        print("✗ 拿不到艺人数据")
        return 1
    songs = a_resp.get("hotSongs") or []
    albums = al_resp.get("hotAlbums") or []
    print(f"✓ {art['name']}（{'/'.join(art.get('alias') or [])}）  "
          f"{art['musicSize']} 首 · {art['albumSize']} 张专辑")

    # 封面下到本地：平台图床 URL 会变会失效，而封面是发别的平台要复用的物料。
    # 只存 URL 等于把资产押在别人的 CDN 上。
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    got = 0
    for al in albums:
        if not al.get("picUrl"):
            continue
        dst = COVER_DIR / f"{al['id']}.jpg"
        if dst.exists():
            continue
        if DRY:
            continue
        for attempt in range(3):        # 网易云图床偶发 SSL 断连，重试就好
            try:
                req = urllib.request.Request(
                    al["picUrl"], headers={"User-Agent": "suno-publisher"})
                with urllib.request.urlopen(req, timeout=30) as r, open(dst, "wb") as f:  # noqa: S310
                    f.write(r.read())
                got += 1
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    print(f"  ✗ 封面 {al['name']}：{str(e)[:40]}")
                time.sleep(1)
    print(f"✓ 封面：新下 {got} 张，本地共 {len(list(COVER_DIR.glob('*.jpg')))} 张")

    n_alb = sync_albums(albums)
    print(f"✓ 专辑 {n_alb} 张入库")

    # 账号入库。**不传 stats** —— 播放量那些只有后台有，由
    # sync_netease_account.py 单独更新；这里传空会把已抓到的指标冲掉。
    if not DRY:
        catalog.upsert_platform_account(
            "netease",
            label="网易云音乐", artist_id=str(art["id"]), artist_name=art["name"],
            alias=art.get("alias") or [], avatar_url=art.get("picUrl") or "",
            brief=art.get("briefDesc") or "",
            artist_url=f"https://music.163.com/#/artist?id={art['id']}",
            user_id=USER_ID, user_url=f"https://music.163.com/#/user/home?id={USER_ID}",
            song_count=art["musicSize"], album_count=art["albumSize"], synced_at=NOW,
        )
    print("✓ 账号入库")

    added, updated, orphan = sync_listings(songs)
    print(f"{'（预演）' if DRY else ''}✓ 作品：新增 {added} 条、更新 {updated} 条")
    if orphan:
        print(f"  ⚠ {orphan} 条还没有关联键（link_state='unlinked'）—— "
              f"它们是平台上的作品，还不知道对应哪首本地作品，等人认领")
        print("     SELECT * FROM listings WHERE link_state='unlinked'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
