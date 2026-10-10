#!/usr/bin/env python3
"""一次性迁移：把 voxflow 的发行台账搬进本仓。

    SUNO_PUBLISHER_HOME=/tmp/x .venv/bin/python scripts/migrate_from_voxflow.py --dry-run
    SUNO_PUBLISHER_HOME=/tmp/x .venv/bin/python scripts/migrate_from_voxflow.py --commit

## 为什么能直接读 voxflow 的库

「不共用 sqlite」说的是**长期运行**时两边不同时开一个文件。迁移是一次性的
快照操作，读它的文件没问题 —— 做完两边就各走各的了。

## 关联键怎么算

`track_platforms.track_id` 指向 voxflow 的 `tracks.id`，跨库用不了。换成：

- `clip_id` —— 从 `tracks.clip_id` 取，Suno 生成的有
- `audio_sha256` —— 本地 TTS 合成的没有 clip_id，按 `tracks.audio_file` 现算哈希

**绝不用标题去猜一个 clip_id 填进去。** 实测有一批网易云 2026-01 的上架记录
指向的 tracks 行是**空壳**（clip_id 和 audio_file 都空，duration 也是 NULL），
同名的真作品另存一行。

拿标题去认，19 条会「认回自己」（关联键依然双空，零信息），剩下几条会认到
**同名但不是这一首**的 clip_id —— 《雪沸·命如刃》两个真作品（170s/180s）
共用同一个 `song_id`，原理上无法区分。猜错的危害比不猜大：日后查「这首歌发到哪了」
会得出一个看起来很确定、实际是错的答案。

所以这些行**照搬，标 `link_state='unlinked'`**，由人认领。

## 一条都不丢

迁移是搬运不是筛选。那批 unlinked 记录带着真实的 song_id、上线日期、播放量和
收益（实测 31 条里 20 条有播放量、20 条有收益），丢不得 —— 丢了就是抹掉真实
收益历史，而且事后极难发现。所以 `commit()` 对它们只标状态不跳过，
对账额外卡一条：写入数 + 待认领数 必须等于源台账行数。

## 幂等

重复跑不会产生重复行：先按关联键查，查到就更新，查不到才插。
所以中断了直接重跑就行，不用回滚重来。

## 对账

`--commit` 前后各跑一次 `summary()`，数字对不上就在结尾报出来。
最关键的是 `lost` 一项 —— 迁移过程中关联不上的记录会被单独列出，
那是**唯一需要人工看一眼**的地方。
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db  # noqa: E402

VOXFLOW_DB = Path.home() / ".voxflow" / "voxflow.db"
VOXFLOW_BASE = Path.home() / ".voxflow"


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def load_tracks(src: sqlite3.Connection) -> dict[str, dict]:
    """voxflow 的 tracks 表 → {track_id: {...含关联键}}。"""
    cols = {d[1] for d in src.execute("PRAGMA table_info(tracks)").fetchall()}
    want = ["id", "title", "clip_id", "audio_file", "release_title",
            "release_platform", "duration"]
    rows = src.execute(f"SELECT {', '.join(want)} FROM tracks").fetchall()
    out: dict[str, dict] = {}
    missing_audio = 0
    for r in rows:
        t = dict(zip(want, r))
        clip_id = (t["clip_id"] or "").strip()
        audio_hash = ""
        if not clip_id:
            rel = (t["audio_file"] or "").strip()
            if rel:
                p = Path(rel)
                if not p.is_absolute():
                    p = VOXFLOW_BASE / p
                if p.is_file():
                    try:
                        audio_hash = _sha256_file(p)
                    except OSError:
                        audio_hash = ""
                else:
                    missing_audio += 1
        out[t["id"]] = {
            **t,
            "clip_id": clip_id,
            "audio_sha256": audio_hash,
            "source_hint": t["title"] or "",
        }
    if missing_audio:
        print(f"  ⚠ {missing_audio} 首没有 clip_id 且音频文件不在磁盘上，"
              f"这类只能靠 title 人工认领")
    return out


def plan(verbose: bool = True) -> dict:
    """算出要写什么，**不落库**。返回统计和认不出的记录。"""
    if not VOXFLOW_DB.is_file():
        raise SystemExit(f"✗ 找不到 voxflow 的库：{VOXFLOW_DB}")
    src = sqlite3.connect(f"file:{VOXFLOW_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    tracks = load_tracks(src)

    tp_cols = [d[1] for d in src.execute("PRAGMA table_info(track_platforms)").fetchall()]
    tp = [dict(r) for r in src.execute("SELECT * FROM track_platforms").fetchall()]
    albums = [dict(r) for r in src.execute("SELECT * FROM albums").fetchall()]
    accts = [dict(r) for r in src.execute("SELECT * FROM platform_accounts").fetchall()]
    events = [dict(r) for r in src.execute("SELECT * FROM publish_events").fetchall()]
    src.close()

    lost: list[dict] = []
    releases: dict[tuple[str, str], dict] = {}
    for tid, t in tracks.items():
        rt = (t["release_title"] or "").strip()
        if not rt:
            continue                      # 没锁过发行名的作品不迁，releases 表只管发行身份
        key = (t["clip_id"], t["audio_sha256"])
        if key == ("", ""):
            continue
        releases[key] = {
            "clip_id": t["clip_id"], "audio_sha256": t["audio_sha256"],
            "source_track_id": tid, "source_hint": t["source_hint"],
            "release_title": rt,
            "locked_platforms": (t["release_platform"] or ""),
        }

    for row in tp:
        t = tracks.get(row["track_id"])
        if not t or (t["clip_id"], t["audio_sha256"]) == ("", ""):
            # 不丢。记成 unlinked 一起搬：这些记录本身是真的（有 song_id、
            # 上线日期、播放量、收益），丢掉等于抹掉真实收益历史。
            # 关联不上就明说关联不上 —— 拿标题猜 clip_id 更糟，同名歌会串行，
            # 且猜错之后再也看不出原来是「没关联」还是「关联错了」。
            lost.append({"表": "track_platforms", "track_id": row["track_id"],
                         "platform": row["platform"],
                         "title": row.get("platform_title") or "",
                         "song_id": row.get("song_id") or "",
                         "plays": row.get("plays") or 0,
                         "earned_cny": row.get("earned_cny") or 0.0,
                         "publish_date": row.get("publish_date") or "",
                         "原因": "关联不上作品（clip_id 与音频哈希都取不到）"})

    if verbose:
        print(f"  源台账：上架 {len(tp)} / 专辑 {len(albums)} / "
              f"账号 {len(accts)} / 事件 {len(events)}")
        print(f"  作品 {len(tracks)} → 有发行名的 {len(releases)} 条发行身份")
        print(f"  认不出的上架记录：{len(lost)}")
    return {"tracks": tracks, "listings": tp, "albums": albums,
            "accounts": accts, "events": events, "releases": releases,
            "lost": lost, "tp_cols": tp_cols}


def commit(plan_data: dict) -> dict:
    tracks = plan_data["tracks"]
    db.init()
    now = db._now()
    stats = {"releases": 0, "listings": 0, "albums": 0, "accounts": 0, "events": 0}

    with db.connect() as c:
        for rel in plan_data["releases"].values():
            locked = rel["locked_platforms"]
            locked_json = (f'["{locked}"]' if locked and locked not in ("[]", "")
                           else "[]")
            # 不用 ON CONFLICT DO NOTHING：没有冲突目标时它是**空操作**，
            # 不报错也不跳过 —— 「24 条发行身份」重跑一次就变 48 条。
            # idx_rel_link / idx_rel_title 都是普通索引不是唯一索引，
            # 必须先查后写。
            seen = c.execute(
                "SELECT 1 FROM releases WHERE release_title=? "
                "OR (clip_id=? AND clip_id!='') "
                "OR (audio_sha256=? AND audio_sha256!='') LIMIT 1",
                (rel["release_title"], rel["clip_id"], rel["audio_sha256"])).fetchone()
            if seen:
                continue
            c.execute(
                "INSERT INTO releases (clip_id, audio_sha256, source_track_id, "
                "source_hint, release_title, locked_platforms, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rel["clip_id"], rel["audio_sha256"], rel["source_track_id"],
                 rel["source_hint"], rel["release_title"], locked_json, now, now))
            stats["releases"] += 1

        for row in plan_data["listings"]:
            t = tracks.get(row["track_id"])
            # 关联不上的照搬，只是标 link_state='unlinked'。
            # 这些行带着真实的 song_id / 上线日期 / 播放量 / 收益，
            # **是发行历史本身**，不是垃圾数据 —— 丢不得。
            linked = bool(t and (t["clip_id"], t["audio_sha256"]) != ("", ""))
            link_state = "linked" if linked else "unlinked"
            clip_id = t["clip_id"] if linked else ""
            audio_sha256 = t["audio_sha256"] if linked else ""
            song_id = row.get("song_id") or ""
            # 去重：song_id 非空的受 idx_ls_song 保护；空的靠 (平台,关联键,标题) 认。
            if song_id:
                seen = c.execute(
                    "SELECT 1 FROM listings WHERE platform=? AND song_id=? LIMIT 1",
                    (row["platform"], song_id)).fetchone()
            else:
                seen = c.execute(
                    "SELECT 1 FROM listings WHERE platform=? AND platform_title=? "
                    "AND COALESCE(clip_id,'')=? AND COALESCE(audio_sha256,'')=? LIMIT 1",
                    (row["platform"], row.get("platform_title") or "",
                     clip_id, audio_sha256)).fetchone()
            if seen:
                continue
            c.execute(
                "INSERT INTO listings (clip_id, audio_sha256, source_track_id, "
                "source_hint, link_state, platform, platform_title, status, song_id, "
                "song_url, album_key, album_name, track_no, duration, publish_date, "
                "cover_url, cover_local, config, note, submitted_at, plays, "
                "earned_cny, stats_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (clip_id, audio_sha256, row["track_id"] if linked else "",
                 t["source_hint"] if t else (row.get("platform_title") or ""),
                 link_state,
                 row["platform"], row.get("platform_title") or "", row["status"],
                 song_id, row.get("song_url") or "",
                 f"{row['platform']}-{row['album_id']}" if row.get("album_id") else "",
                 row.get("album_name") or "", row.get("track_no"),
                 row.get("duration"), row.get("publish_date") or "",
                 row.get("cover_url") or "", row.get("cover_local") or "",
                 row.get("config") or "{}", row.get("note") or "",
                 row.get("submitted_at") or "", row.get("plays") or 0,
                 row.get("earned_cny") or 0.0, row.get("stats_at") or "",
                 row.get("updated_at") or now))
            stats["listings"] += 1

        for a in plan_data["albums"]:
            # albums.key 是主键，这里的 ON CONFLICT(key) 才是真的会跳过。
            cur = c.execute(
                "INSERT INTO albums (key, platform, album_id, title, track_count, "
                "publish_date, company, description, tags, cover_url, cover_local, "
                "url, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO NOTHING",
                (a["key"], a["platform"], a["album_id"], a["title"],
                 a.get("track_count") or 0, a.get("publish_date") or "",
                 a.get("company") or "", a.get("description") or "",
                 a.get("tags") or "", a.get("cover_url") or "",
                 a.get("cover_local") or "", a.get("url") or "",
                 a.get("synced_at") or now))
            # 统计实际写入行数，不是尝试次数 —— 否则重跑时增量对不上，
            # 会把「已经是最新的」误报成「对账不平」。
            stats["albums"] += cur.rowcount if cur.rowcount > 0 else 0

        for a in plan_data["accounts"]:
            cur = c.execute(
                "INSERT INTO platform_accounts (platform, label, artist_id, "
                "artist_name, alias, avatar_url, brief, artist_url, user_id, "
                "user_url, song_count, album_count, stats, synced_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform) DO NOTHING",
                (a["platform"], a.get("label") or "", a.get("artist_id") or "",
                 a.get("artist_name") or "", a.get("alias") or "[]",
                 a.get("avatar_url") or "", a.get("brief") or "",
                 a.get("artist_url") or "", a.get("user_id") or "",
                 a.get("user_url") or "", a.get("song_count") or 0,
                 a.get("album_count") or 0, a.get("stats") or "{}", now))
            stats["accounts"] += cur.rowcount if cur.rowcount > 0 else 0

        for e in plan_data["events"]:
            t = tracks.get(e["track_id"])
            linked = bool(t and (t["clip_id"], t["audio_sha256"]) != ("", ""))
            clip_id = t["clip_id"] if linked else ""
            audio_sha256 = t["audio_sha256"] if linked else ""
            # publish_events 没有唯一约束，ON CONFLICT 帮不上忙。重跑一遍会
            # 把 47 条历史全复制一份 —— 「状态流转是**多条**」不代表允许重复的同一事件。
            # 用 (关联键, 平台, from, to, ts) 当天然去重键，与 source_track_id 无关，
            # 所以换了 tracks.id 重跑也不会写出重复。
            # 关联不上也搬（关联键留空）——「谁在什么时候把它从 A 推到 B」
            # 本身就是历史，丢了就再也拼不回来。靠 note/source_hint 留线索。
            dup = c.execute(
                "SELECT 1 FROM publish_events WHERE clip_id=? AND audio_sha256=? "
                "AND platform=? AND COALESCE(from_status,'')=? AND to_status=? AND ts=?",
                (clip_id, audio_sha256, e["platform"],
                 e.get("from_status") or "", e["to_status"], e["ts"])).fetchone()
            if dup:
                continue
            c.execute(
                "INSERT INTO publish_events (clip_id, audio_sha256, platform, "
                "from_status, to_status, actor, note, ts) VALUES (?,?,?,?,?,?,?,?)",
                (clip_id, audio_sha256, e["platform"],
                 e.get("from_status") or "", e["to_status"], e.get("actor") or "",
                 (e.get("note") or "") or ("" if linked else
                  f"[未关联作品] track_id={e['track_id']}"),
                 e["ts"]))
            stats["events"] += 1

    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真的写库（默认只演练）")
    # 默认就是演练，显式 --dry-run 也认：docstring 里写的就是 --dry-run，
    # 打错旗标报错比默默按默认走安全 —— 迁移写错库是覆不回来的。
    ap.add_argument("--dry-run", action="store_true",
                    help="只演练（默认行为，写了它是为了对齐文档里的跑法）")
    args = ap.parse_args()

    print("=== 1. 源台账现状 ===")
    # 先 init 再 summary：summary() 直接 COUNT 表，全新库还没有表，
    # 第一次跑就崩在「迁移前：本仓迁移前」这行 —— 而这恰恰是最该跑通的那一次。
    db.init()
    before = db.summary() if args.commit else {}
    if args.commit:
        print(f"  本仓迁移前：{before}")

    print("\n=== 2. 迁移计划 ===")
    p = plan()
    if not args.commit:
        print("\n（dry-run，没写任何东西。加 --commit 真写。）")
        if p["lost"]:
            n = len(p["lost"])
            plays = sum(x.get("plays") or 0 for x in p["lost"])
            earned = sum(x.get("earned_cny") or 0.0 for x in p["lost"])
            print(f"\nℹ {n} 条上架记录关联不上作品，但**会照搬不丢**"
                  f"（link_state=unlinked，待人工认领）。")
            print(f"   这些带着真实的 song_id / 上线日期 / 播放量 {plays} / 收益 ¥{earned:.2f}，")
            print(f"   拿标题猜 clip_id 是错的 —— 同名歌会串行，猜错比不猜更难查。")
            for x in p["lost"][:8]:
                print(f"    {x['platform']:<9}《{x['title']}》 song_id={x['song_id'] or '—'}")
            if n > 8:
                print(f"    …还有 {n - 8} 条")
        return 0

    print("\n=== 3. 写入 ===")
    stats = commit(p)
    for k, v in stats.items():
        print(f"  {k:<10} {v}")

    print("\n=== 4. 对账 ===")
    after = db.summary()
    for k in after:
        print(f"  {k:<20} {before.get(k, 0):>4} → {after[k]:>4}  (+{after[k] - before.get(k, 0)})")
    # 比增量而不是比总数：脚本是幂等的（重复跑不写重），重跑时 after 会停在原数，
    # 拿它跟「源台账总数」比就会误报不平。真正该确认的是「这轮该写的都写进去了」。
    delta = {k: after[k] - before.get(k, 0) for k in after}
    # stats 里的 accounts 对应表名 platform_accounts，键名对不上，
    # 直接拿 stats[k] 取 delta 会 KeyError。
    expect = {"releases": "releases", "listings": "listings", "albums": "albums",
              "accounts": "platform_accounts", "events": "publish_events"}
    bad = [f"{tbl}: 本轮实写 {stats[k]} 但增量 {delta.get(tbl, '?')}"
           for k, tbl in expect.items() if delta.get(tbl) != stats[k]]
    if bad:
        print("\n✗ 对账不平：")
        for b in bad:
            print("    " + b)
        return 1

    # 关键一条：**源台账的每一条都得在**。迁移是搬运，不是筛选 ——
    # 「关联不上就不搬」会让真实收益历史凭空消失，而这恰恰是最难事后发现的。
    # unlinked 行也走同一条 INSERT。
    # 判据用「库里的总数」而不是「这轮写了几条」：脚本幂等，重跑时本轮写入 0
    # 但库里仍是全的，用本轮写入数去比会误报成「丢了 60 条」。
    src = p["listings"]
    if after["listings"] < len(src):
        print(f"\n✗ 上架记录数对不上：源 {len(src)} → 库里 {after['listings']}"
              f"（丢 {len(src) - after['listings']} 条）")
        return 1
    unlinked = db.unlinked_count()
    print(f"\n✓ 对账平：{len(src)} 条上架一条没丢"
          f"（本轮写入 {stats['listings']}，"
          f"其中 {unlinked} 条 link_state=unlinked，待人工认领）")
    if unlinked:
        print(f"  这些带着真实的 song_id / 上线日期 / 播放量 / 收益，只是还没认回作品。")
        print(f"  认领入口：SELECT * FROM listings WHERE link_state='unlinked'")
        print(f"  **别拿标题猜 clip_id** —— 同名歌会串行，猜错比不猜更难查。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
