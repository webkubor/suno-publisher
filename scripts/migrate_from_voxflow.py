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

**不能用标题**：实测曲库里有 4 组同名作品（Suno 一次出两首同名），
`scripts/sync_suno.py` 顶部就写了这条。

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
            # 占位行标记：既没 clip_id 也没音频文件。多半是为同名作品预留的空壳。
            "placeholder": not clip_id and not audio_hash,
        }
    if missing_audio:
        print(f"  ⚠ {missing_audio} 首没有 clip_id 且音频文件不在磁盘上，"
              f"这类只能靠标题 + 时长人工认领")
    return out


def resolve_orphan(track: dict, candidates: list[dict]) -> tuple[dict | None, str]:
    """给既没 clip_id 也没音频的老单找回归属 —— 2026-10-10 实测 31/31 可判定。

    ## 为什么需要这一步

    `track_platforms` 里有 31 条上架记录指向的作品**既无 clip_id 也无音频文件**
    （Suno 生成之前的老单）。这两条关联键都取不到，按理是孤儿。

    实际不是：这些 `tracks` 行是**同名成对的占位壳** —— 迁移前后各有一条同名行，
    迁移前那条只有标题（duration/clip_id/audio_file 全空），迁移后那条才是真作品。
    台账指向的恰恰是壳。

    ## 判据（按优先级）

    1. **标题唯一命中** → 直接认。31 条里 19 条走这条。
    2. **标题有多个候选 → 用 `duration` + `clip_id` 非空筛**。
       时长对得上且带 clip_id 的才是真作品。剩下 12 条走这条。
    3. 仍分不清 → 返回 None，列进 `lost` 让���看。

    为什么时长可靠：壳的 duration 是 NULL（从没生成过），真作品有 Suno 给的
    真实秒数。31 条里 9 条靠它定，剩下 3 条在候选里只有一条带 clip_id。

    **不用 song_id 反查** —— 那要先从平台回调同步回来，是另一条链路，
    这里能定就别引新依赖。
    """
    cands = candidates
    if not cands:
        return None, "无候选"
    if len(cands) == 1:
        return cands[0], "标题唯一"

    # 时长是第一判据 —— 台账自己记的 duration 就是权威答案，不是启发式。
    # 2026-10-10 实测 3 条多候选全靠这一条定下来（dur=170/200/170
    # 各自在候选里唯一命中）。之前把它们丢给 owner 认领是错的：
    # 数据里就有答案，是没查就问。owner-identity 规则写的是
    # 「给推荐不给菜单，拍板只问不可逆/设计/范围」，这三条都不是。
    dur = track.get("duration")
    if dur is not None:
        hit = [c for c in cands if c.get("duration") == dur]
        if len(hit) == 1:
            return hit[0], "duration 唯一匹配"
        if len(hit) > 1:                    # 时长也撞：再按带 clip_id 筛
            wc = [c for c in hit if c.get("clip_id")]
            if len(wc) == 1:
                return wc[0], "duration+clip_id"

    with_clip = [c for c in cands if c.get("clip_id")]
    if len(with_clip) == 1:
        return with_clip[0], "唯一带 clip_id"

    # 兜底：壳（duration 和 clip_id 全空）不可能是真实作品
    real = [c for c in cands if c.get("clip_id") or c.get("duration")]
    if len(real) == 1:
        return real[0], "排除空壳后唯一"

    return None, f"{len(cands)} 个候选无法区分"


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

    # 上架记录认回归属 —— 拿不到关联键的先试标题 + 时长兜底
    by_title: dict[str, list[dict]] = {}
    for t in tracks.values():
        by_title.setdefault(t["title"] or "", []).append(t)

    recovered = 0
    for row in tp:
        t = tracks.get(row["track_id"])
        if t and (t["clip_id"], t["audio_sha256"]) != ("", ""):
            continue
        # 壳本身没有键，按标题去真实作品里认
        real, how = resolve_orphan(row, by_title.get(t["title"] if t else "", []))
        if not real:
            lost.append({"表": "track_platforms", "track_id": row["track_id"],
                         "platform": row["platform"],
                         "title": row.get("platform_title") or "",
                         "song_id": row.get("song_id") or "",
                         "原因": f"归属不明：{how}"})
            continue
        row["_resolved_to"] = real["id"]
        row["_resolved_by"] = how
        t = real
        recovered += 1

    if verbose:
        print(f"  源台账：上架 {len(tp)} / 专辑 {len(albums)} / "
              f"账号 {len(accts)} / 事件 {len(events)}")
        print(f"  作品 {len(tracks)} → 有发行名的 {len(releases)} 条发行身份")
        print(f"  归属兜底认回 {recovered} 条（标题/时长判据）")
        print(f"  认不出的上架记录：{len(lost)}")
    return {"tracks": tracks, "listings": tp, "albums": albums,
            "accounts": accts, "events": events, "releases": releases,
            "lost": lost, "tp_cols": tp_cols}


def commit(plan_data: dict) -> dict:
    tracks = plan_data["tracks"]
    db.init()
    now = db._now()
    stats = {"releases": 0, "listings": 0, "albums": 0, "accounts": 0, "events": 0}
    lost_events = 0

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

        # song_id 为空的行不迁。
        #
        # 为什么不迁：平台还没分配 song_id 的单（qishui 18 条 + netease 2 条）
        # 意味着这条上架记录**还没有平台身份**。迁过来它既不能和平台对上，
        # 也会和新库 `idx_ls_song`（partial unique，WHERE song_id != ''）打架 ——
        # 源库那条索引把空串也纳入了唯一性，同一平台多条空 song_id 只剩第一条能进。
        # 新库的条件更严，行为其实更对：不迁等于明确说「这条还没上市」。
        skipped_no_song_id = 0
        for row in plan_data["listings"]:
            if not (row.get("song_id") or "").strip():
                skipped_no_song_id += 1
                continue
            tid = row.get("_resolved_to") or row["track_id"]
            t = tracks.get(tid)
            if not t or (t["clip_id"], t["audio_sha256"]) == ("", ""):
                continue
            album_key = (f"{row['platform']}-{row['album_id']}"
                         if row.get("album_id") else "")
            # song_id 为空的记录不受 idx_ls_song 保护，得靠关联键+平台去重
            song_id = row.get("song_id") or ""
            seen = c.execute(
                "SELECT 1 FROM listings WHERE platform=? AND ("
                "  (song_id!='' AND song_id=?) OR"
                "  (song_id='' AND clip_id=? AND clip_id!='' AND audio_sha256=?)"
                ") LIMIT 1",
                (row["platform"], song_id, t["clip_id"], t["audio_sha256"])).fetchone()
            if seen:
                continue
            c.execute(
                "INSERT INTO listings (clip_id, audio_sha256, source_track_id, "
                "source_hint, platform, platform_title, status, song_id, song_url, "
                "album_key, album_name, track_no, duration, publish_date, cover_url, "
                "cover_local, config, note, submitted_at, plays, earned_cny, stats_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["clip_id"], t["audio_sha256"], row["track_id"], t["source_hint"],
                 row["platform"], row.get("platform_title") or "", row["status"],
                 song_id, row.get("song_url") or "", album_key,
                 row.get("album_name") or "", row.get("track_no"),
                 row.get("duration"), row.get("publish_date") or "",
                 row.get("cover_url") or "", row.get("cover_local") or "",
                 row.get("config") or "{}", row.get("note") or "",
                 row.get("submitted_at") or "", row.get("plays") or 0,
                 row.get("earned_cny") or 0.0, row.get("stats_at") or "",
                 row.get("updated_at") or now))
            stats["listings"] += 1
        stats["skipped_no_song_id"] = skipped_no_song_id

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

        by_title_ev: dict[str, list[dict]] = {}
        for tt in tracks.values():
            by_title_ev.setdefault(tt["title"] or "", []).append(tt)
        resolved_map = {r["track_id"]: r.get("_resolved_to") for r in plan_data["listings"]}
        # 悬空 track_id 的兜底索引。
        #
        # 2026-10-10 实测：有条事件的 track_id 是 'qishui-4'，而 tracks 表里
        # 根本**没有这一行**（真实 id 是 'qishui-481ec5b1'，标题「温柔角落」）。
        # 是早期手工造数据留下的坏引用。不是数据缺失，是引用指错了地方 ——
        # 所以按 platform_title 反查 titles 能认回，不该丢。
        # 2026-01 的历史数据里有一条真的迁不了：《温柔角落》上线于 2026-01-17，
        # 那时 voxflow 还没有 clip_id / 音频哈希这套关联键，tracks 里那条也是空壳，
        # 同名候选只有它自己。它没有任何可关联的内容键 —— 这是数据事实，不是待决事项。
        legacy_unlinkable: list[dict] = []
        for e in plan_data["events"]:
            t = tracks.get(resolved_map.get(e["track_id"]) or e["track_id"])
            # 关联键双空就按壳的标题 + 时长兜底认，和 listings 同一套判据。
            # 不能只依赖 resolved_map —— 有事件的 track 可能压根没上架记录
            # （提了单还没提交也算事件），那条只能直接查 tracks。
            if not t or (t["clip_id"], t["audio_sha256"]) == ("", ""):
                t, _w = resolve_orphan(e, by_title_ev.get((t or {}).get("title", ""), []))
            if not t or (t["clip_id"], t["audio_sha256"]) == ("", ""):
                legacy_unlinkable.append({
                    "event_id": e["id"], "track_id": e["track_id"],
                    "platform": e["platform"], "ts": e["ts"],
                    "track_title": (t or {}).get("title", ""),
                    "原因": "2026-01 历史数据，无 clip_id / 音频哈希 / song_id",
                })
                continue
            # publish_events 没有唯一约束，ON CONFLICT 帮不上忙。重跑一遍会
            # 把 47 条历史全复制一份 —— 「状态流转是**多条**」不代表允许重复的同一事件。
            # 用 (关联键, 平台, from, to, ts) 当天然去重键，与 source_track_id 无关，
            # 所以换了 tracks.id 重跑也不会写出重复。
            dup = c.execute(
                "SELECT 1 FROM publish_events WHERE clip_id=? AND audio_sha256=? "
                "AND platform=? AND COALESCE(from_status,'')=? AND to_status=? AND ts=?",
                (t["clip_id"], t["audio_sha256"], e["platform"],
                 e.get("from_status") or "", e["to_status"], e["ts"])).fetchone()
            if dup:
                continue
            c.execute(
                "INSERT INTO publish_events (clip_id, audio_sha256, platform, "
                "from_status, to_status, actor, note, ts) VALUES (?,?,?,?,?,?,?,?)",
                (t["clip_id"], t["audio_sha256"], e["platform"],
                 e.get("from_status") or "", e["to_status"], e.get("actor") or "",
                 e.get("note") or "", e["ts"]))
            stats["events"] += 1
        stats["legacy_unlinkable"] = len(legacy_unlinkable)

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
            print(f"\n⚠ 有 {len(p['lost'])} 条认不出，需要人工处理：")
            for x in p["lost"][:10]:
                print(f"    {x['表']} {x['platform']} 《{x['title']}》 {x['原因']}")
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
    orph = db.orphans()
    print(f"\n✓ 对账平。孤儿记录 {len(orph)}")
    if p["lost"]:
        print(f"⚠ 另有 {len(p['lost'])} 条认不出，未迁入（见 dry-run 输出）")
        return 2      # 别让「丢了一半台账」被 exit 0 读成「迁移成功」
    return 0


if __name__ == "__main__":
    sys.exit(main())
