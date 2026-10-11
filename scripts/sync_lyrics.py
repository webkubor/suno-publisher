#!/usr/bin/env python3
"""用平台的歌词补本地缺失的歌词。

    .venv/bin/python scripts/sync_lyrics.py [--dry-run]

从 voxflow 的 `scripts/sync_lyrics.py` 移植（2026-10-11）。

## 为什么跨了两个仓

歌词是**生成侧**的数据（voxflow 的 `tracks.lyrics`），但「去哪个平台查」
要用上架记录里的 `song_id` —— 那是**发行侧**的事实，在本仓。

所以是「本仓查、往 voxflow 写」，走 `POST /api/catalog/{track_id}/lyrics`。
**不反过来让 voxflow 读本仓的库** —— 那会把设计好的单向依赖变成环。

那个端点有两条硬约束（在它自己的文档里写明了）：
**只填空的、绝不覆盖**；**不做模糊匹配**。这里只是调用方。

## 匹配靠关联键，不靠歌名

本地曲目 ↔ 上架记录之间只有 `clip_id` / `audio_sha256` 两个键（跨库没有外键）。
**不用歌名** —— Suno 一次出两首同名，按标题匹配必然串行（见
voxflow 的 `scripts/sync_suno.py` 顶部那条教训）。

## 实测：多数歌本来就没有歌词

voxflow 那边 2026-08-31 的实测结论：33 首网易云作品全部返回空歌词 ——
它们是纯音乐/器乐作品，平台本来就没有。脚本仍保留：
将来有带词的作品跑一遍就能补。

歌词存 LRC 格式（带时间戳）。发布表单一般要纯文本，所以这里转成纯文本再写；
时间戳留在平台那边，需要时另取。
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db  # noqa: E402
from core import voxflow_client as vc  # noqa: E402

DRY = "--dry-run" in sys.argv
TIMESTAMP_RE = re.compile(r"^(\[\d+:\d+(\.\d+)?\])+")
SECTION_RE = re.compile(r"^\[(ti|ar|al|by|offset|re|ve):.*\]$", re.I)
PLATFORM = "netease"


def fetch_lyric(song_id: str) -> str:
    """取 LRC 原文。拿不到返回空串 —— 纯音乐没词是正常的，不是错误。"""
    url = (f"https://music.163.com/api/song/lyric"
           f"?id={urllib.parse.quote(song_id)}&lv=1&kv=1&tv=-1")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://music.163.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            data = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001 —— 单首取不到不该中断整批
        print(f"  ⚠ song_id={song_id} 取词失败：{str(e)[:50]}")
        return ""
    return ((data.get("lrc") or {}).get("lyric") or "").strip()


def lyric_to_plain(lrc: str) -> str:
    """LRC → 纯文本：去掉时间戳和元信息行，保留段落。"""
    out = []
    for line in lrc.splitlines():
        line = TIMESTAMP_RE.sub("", line)
        line = SECTION_RE.sub("", line).strip()
        if line:
            out.append(line)
    return "\n".join(out)


def candidates() -> list[dict]:
    """本地缺歌词、且在本平台有 song_id 的曲目。

    两道过滤都在这一步做完，**不靠标题猜**：
      · `link_state='linked'` —— 只有认回作品的记录才知道对应哪首本地曲目
      · `song_id != ''`       —— 没有平台 id 就查不了歌词
    """
    db.init()
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT clip_id, audio_sha256, platform_title, song_id FROM listings "
            "WHERE platform=? AND IFNULL(song_id,'')!='' AND link_state='linked'",
            (PLATFORM,)).fetchall()]

    # 曲库里哪些缺歌词 —— 直接问 voxflow（它才是歌词的真源）
    try:
        tracks = list(vc.catalog())
    except (vc.VoxflowUnavailable, vc.VoxflowError) as e:
        print(f"✗ 连不上 VoxFlow，取不到曲库：{e}")
        return []

    # 关联键 → 曲目。两个键都建索引：有的作品只有 clip_id，有的只有音频哈希。
    by_clip = {t.clip_id: t for t in tracks if t.clip_id}
    by_hash = {t.audio_sha256: t for t in tracks if t.audio_sha256}

    out = []
    for r in rows:
        hit = (by_clip.get(r["clip_id"]) if r["clip_id"] else None) or \
              (by_hash.get(r["audio_sha256"]) if r["audio_sha256"] else None)
        if not hit:
            continue
        if (hit.lyrics or "").strip():
            continue          # 已有歌词 —— 端点也会拒绝覆盖，别白跑
        out.append({"track_id": hit.id, "title": hit.title,
                    "song_id": r["song_id"], "platform_title": r["platform_title"]})
    return out


def main() -> int:
    rows = candidates()
    if not rows:
        print("没有需要补歌词的作品。")
        print("  （判据：本平台上架记录有关联键 + 本地歌词为空）")
        return 0

    print(f"找到 {len(rows)} 首本地缺词、且平台有 song_id 的作品\n")
    got = empty = failed = 0
    for r in rows:
        lrc = fetch_lyric(r["song_id"])
        if not lrc:
            print(f"  · {r['title']}  —— 平台也没词（多半是纯音乐）")
            empty += 1
            continue
        plain = lyric_to_plain(lrc)
        if not plain:
            print(f"  · {r['title']}  —— 平台有 LRC 但去掉时间戳后是空的")
            empty += 1
            continue
        if DRY:
            print(f"  → {r['title']}  会写入 {len(plain)} 字（预演，没写）")
            got += 1
            continue
        try:
            vc.fill_lyrics(r["track_id"], plain)
            print(f"  ✓ {r['title']}  {len(plain)} 字")
            got += 1
        except (vc.VoxflowUnavailable, vc.VoxflowError) as e:
            print(f"  ✗ {r['title']} 写入失败：{str(e)[:60]}")
            failed += 1
        time.sleep(0.3)      # 别把公开接口打太狠

    print(f"\n{'（预演）' if DRY else ''}✓ 回填 {got} 首"
          f" · 平台无词 {empty} 首" + (f" · 失败 {failed} 首" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
