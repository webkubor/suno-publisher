#!/usr/bin/env python3
"""从 QQ 音乐公开 API 同步歌手、专辑、作品目录到本仓台账。

    .venv/bin/python scripts/sync_qq.py [--dry-run]

从 voxflow 的 `scripts/sync_qq.py` 移植（2026-10-11），写入规则与
`sync_netease.py` 共用（`catalog.upsert_listing`）。

## 为什么不需要浏览器

同一个道理：作品目录走 musicu.fcg 公开接口就够，绕浏览器只会多一层跨域和
登录态的连累。只有播放量/收益才必须登录后台。

## 只补不改

跟 `sync_suno.py` 同一个原则：只用云端状态覆盖**平台侧字段**，
本地的歌词、封面、认领关系一个字不动。

⚠️ 一条本地记录在后台查不到，**不会**被改成「未发布」—— 可能只是不在这个
账号下。查不到的只做提示，绝不动数据。

## 标题不参与认领

原版按标题精确匹配来「认领原曲」。本仓**不用标题**（Suno 一次出两首同名，
按标题匹配必然串行）—— 只认 `song_id`，认不到就建成 `unlinked` 等人认领。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import catalog, db, paths  # noqa: E402

DRY = "--dry-run" in sys.argv
SINGER_MID = os.environ.get("QQ_MID", "002Rcy0a0YpQ7L")
COVER_DIR = paths.DATA_DIR / "library" / "covers" / "qq"
NOW = db._now()
UA = "Mozilla/5.0"


def musicu(modules: dict) -> dict:
    """调用 musicu.fcg。modules 是 {信封键: {module, method, param}}。"""
    payload = {"comm": {"ct": 24}, **modules}
    data = urllib.parse.quote(json.dumps(payload, ensure_ascii=False), safe="")
    url = f"https://u.y.qq.com/cgi-bin/musicu.fcg?format=json&data={data}"
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://y.qq.com/"})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        return json.loads(r.read().decode())


def get_singer_songs(mid: str, begin: int = 0, num: int = 50) -> list[dict]:
    resp = musicu({"singer": {
        "module": "musichall.song_list_server", "method": "GetSingerSongList",
        "param": {"singerMid": mid, "begin": begin, "num": num}}})
    return (resp.get("singer", {}).get("data", {}).get("songList") or [])


def get_albums(mid: str, begin: int = 0, num: int = 50) -> list[dict]:
    resp = musicu({"alb": {
        "module": "music.musichallAlbum.AlbumListServer", "method": "GetAlbumList",
        "param": {"singerMid": mid, "begin": begin, "num": num}}})
    data = resp.get("alb", {}).get("data", {})
    return data.get("albumList") or data.get("list") or []


def get_singer_info(mid: str) -> dict:
    """歌手信息：名字从歌曲列表反推，头像 URL 按 mid 构造。

    不依赖搜索接口 —— 实测 `SearchCgiService/DoSearchForQQMusicDesktop`
    对同一 query 时好时坏（同样参数有时返回歌手、有时整包空），
    `get_singer_detail_info` 对这个小歌手也是空的。而歌曲列表一定带歌手名，
    头像 URL 规律固定。
    """
    resp = musicu({"singer": {
        "module": "musichall.song_list_server", "method": "GetSingerSongList",
        "param": {"singerMid": mid, "begin": 0, "num": 5}}})
    data = resp.get("singer", {}).get("data", {})
    name = ""
    for s in data.get("songList") or []:
        singers = (s.get("songInfo", {}) or {}).get("singer") or []
        if singers:
            name = singers[0].get("name", "")
            break
    return {
        "singerName": name or mid,
        "singerMID": mid,
        "songNum": data.get("totalNum", 0),
        "singerPic": f"https://y.qq.com/music/photo_new/T001R150x150M000{mid}_2.jpg",
    }


def ymd(s: str) -> str:
    return s[:10] if s else ""


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(paths.DATA_DIR))
    except ValueError:
        return str(p)


def _download(url: str, dst: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r, open(dst, "wb") as f:  # noqa: S310
            f.write(r.read())
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 下载失败 {dst.name}：{str(e)[:40]}")
        return False


def main() -> int:
    albums = get_albums(SINGER_MID)
    info = get_singer_info(SINGER_MID)
    name = info.get("singerName", "?")
    print(f"✓ 歌手：{name}（{info.get('songNum', 0)} 首 · {len(albums)} 张专辑）")

    # 平台图床会变会失效 —— 头像与封面都下到本地
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    singer_pic = info.get("singerPic", "").replace("http://", "https://")
    avatar_dst = COVER_DIR / f"singer_{SINGER_MID}.jpg"
    if singer_pic and not avatar_dst.exists() and not DRY:
        _download(singer_pic, avatar_dst)

    got_cover = 0
    for al in albums:
        amid = al.get("albumMid") or ""
        if not amid:
            continue
        cover = COVER_DIR / f"{amid}.jpg"
        if not cover.exists() and not DRY:
            if _download(f"https://y.qq.com/music/photo_new/T002R300x300M000{amid}.jpg",
                         cover):
                got_cover += 1
        if not DRY:
            catalog.upsert_album(
                f"qq-{amid}", platform="tencent", album_id=amid,
                title=al.get("albumName") or al.get("name") or "",
                track_count=al.get("songNum") or 0,
                publish_date=ymd(al.get("publicTime") or al.get("time_public") or ""),
                cover_url=f"https://y.qq.com/music/photo_new/T002R300x300M000{amid}.jpg",
                cover_local=_rel(cover) if cover.exists() else "",
                url=f"https://y.qq.com/n/ryqq/albumDetail/{amid}",
                synced_at=NOW,
            )
    print(f"✓ 专辑 {len(albums)} 张入库（封面新下 {got_cover}）")

    if not DRY:
        # 不传 stats —— 那只有后台有，传空会冲掉已抓到的指标
        catalog.upsert_platform_account(
            "tencent",
            label="腾讯音乐（QQ 音乐）", artist_id=SINGER_MID,
            artist_name=name, alias=[], avatar_url=singer_pic,
            brief="", artist_url=f"https://y.qq.com/n/ryqq_v2/singer/{SINGER_MID}",
            song_count=info.get("songNum", 0), album_count=len(albums),
            synced_at=NOW,
        )
    print("✓ 账号入库（tencent / 腾讯系）")

    songs = get_singer_songs(SINGER_MID)
    added = updated = 0
    for s in songs:
        si = s.get("songInfo", {})
        title = (si.get("name") or "").strip()
        if not title:
            continue
        sid = str(si.get("id") or "")
        if not sid:
            continue
        album = si.get("album") or {}
        amid = album.get("mid") or ""
        cover = COVER_DIR / f"{amid}.jpg"
        if DRY:
            updated += 1
            continue
        how, _ = catalog.upsert_listing(
            "tencent", sid,
            platform_title=title,
            song_url=f"https://y.qq.com/n/ryqq/songDetail/{si.get('mid')}",
            status="online",
            album_key=f"qq-{amid}" if amid else "",
            album_name=album.get("name") or "",
            track_no=si.get("num"),
            duration=round((si.get("interval") or 0)),
            publish_date=ymd(album.get("time_public") or ""),
            cover_url=(f"https://y.qq.com/music/photo_new/T002R300x300M000{amid}.jpg"
                       if amid else ""),
            cover_local=_rel(cover) if cover.exists() else "",
        )
        if how == "added":
            added += 1
        else:
            updated += 1

    print(f"{'（预演）' if DRY else ''}✓ 作品：新增 {added} 条、更新 {updated} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
