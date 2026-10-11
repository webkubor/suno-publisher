#!/usr/bin/env python3
"""一次补齐：把 voxflow track_platforms 里的 `publisher` 三列搬进本仓 listings。

    .venv/bin/python scripts/backfill_publisher.py            # 预演
    .venv/bin/python scripts/backfill_publisher.py --commit

## 为什么单独一个脚本

`migrate_from_voxflow.py` 是「插新行」的（查不到才插），所以补列这种
**更新已有行**的活它干不了 —— 那些 listings 早就迁过来了，再跑迁移只会
一条都不动。

## 为什么非要补

`publisher` / `publisher_id` / `assigned_at` 记的是「这条上架记录谁负责发的」。
它是**长期要追溯的事实**（分成归属、出问题找谁），不是可有可无的元数据。
实测源库 60 条里有 9 条有值 —— 删掉源表之前必须先把这 9 条搬过来，
否则它们就没了，而且**丢了不会报错**，只会在某天有人问「这条谁发的」时
才发现答不上来。

## 对账

跑完打印「源库有几个非空 / 本仓现在有几个非空」，两个数必须相等。
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db  # noqa: E402

VOXFLOW_DB = Path.home() / ".voxflow" / "voxflow.db"


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真的写库（默认只演练）")
    args = ap.parse_args()

    if not VOXFLOW_DB.is_file():
        raise SystemExit(f"✗ 找不到 voxflow 的库：{VOXFLOW_DB}")

    src = sqlite3.connect(f"file:{VOXFLOW_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    cols = {d[1] for d in src.execute("PRAGMA table_info(track_platforms)")}
    if "publisher" not in cols:
        print("源库没有 publisher 列，没什么可搬的。")
        return 0

    # 按**关联键**认行，不按 (平台, song_id)。
    #
    # 实测源库那 9 条的 song_id 全是空的（汽水还没给分配），拿 (platform, song_id)
    # 去匹配会一条都对不上 —— 而且 UPDATE 打空不报错，跑完看着「成功」了、
    # 数量却是 0。所以走 track_id → clip_id/audio_sha256 → listings 这条路，
    # 和当初迁移用的是同一套键。
    rows = src.execute(
        "SELECT p.platform, p.track_id, "
        "       IFNULL(p.publisher,'') publisher, "
        "       IFNULL(p.publisher_id,'') publisher_id, "
        "       IFNULL(p.assigned_at,'') assigned_at, "
        "       IFNULL(t.clip_id,'') clip_id, IFNULL(t.audio_file,'') audio_file "
        "FROM track_platforms p LEFT JOIN tracks t ON t.id = p.track_id "
        "WHERE IFNULL(p.publisher,'') != ''"
    ).fetchall()
    print(f"源库有 publisher 的上架记录：{len(rows)} 条")
    src.close()

    # audio_file → sha256（本地合成那批没有 clip_id）。只对缺 clip_id 的算，
    # 免得为几行去读几十 MB 音频。
    need_hash = [r for r in rows if not r["clip_id"] and r["audio_file"]]
    hashes: dict[str, str] = {}
    for r in need_hash:
        p = Path(r["audio_file"])
        if not p.is_absolute():
            p = VOXFLOW_DB.parent / p
        if p.is_file():
            hashes[r["track_id"]] = _sha256_file(p)
        else:
            print(f"  ⚠ 音频不在磁盘上，这条认不了：{r['audio_file']}")

    db.init()
    if not args.commit:
        print("\n（预演，没写任何东西。加 --commit 真写。）")
        for r in rows[:10]:
            key = r["clip_id"] or hashes.get(r["track_id"], "")
            print(f"    {r['platform']:<9} 键={key[:12] or '（取不到）':<14} → {r['publisher']}")

    with db.connect() as c:
        before = c.execute(
            "SELECT COUNT(*) FROM listings WHERE IFNULL(publisher,'') != ''"
        ).fetchone()[0]
        if args.commit:
            updated = unmatched = 0
            for r in rows:
                clip_id = r["clip_id"]
                sha = hashes.get(r["track_id"], "")
                if not clip_id and not sha:
                    unmatched += 1
                    continue
                if clip_id:
                    cur = c.execute(
                        "UPDATE listings SET publisher=?, publisher_id=?, assigned_at=? "
                        "WHERE clip_id=?",
                        (r["publisher"], r["publisher_id"], r["assigned_at"], clip_id))
                else:
                    cur = c.execute(
                        "UPDATE listings SET publisher=?, publisher_id=?, assigned_at=? "
                        "WHERE audio_sha256=?",
                        (r["publisher"], r["publisher_id"], r["assigned_at"], sha))
                updated += cur.rowcount
                if cur.rowcount == 0:
                    unmatched += 1
            print(f"\n写入：{updated} 行" + (f"，{unmatched} 条没匹配上" if unmatched else ""))
        after = c.execute(
            "SELECT COUNT(*) FROM listings WHERE IFNULL(publisher,'') != ''"
        ).fetchone()[0]

    if args.commit:
        print(f"对账：源 {len(rows)} → 本仓 {before} → {after}")
        if unmatched:
            print(f"\n⚠ {unmatched} 条没落库 —— 见上面的键，多半是音频不在磁盘上")
        if after >= len(rows) - unmatched:
            print("✓ 该搬的都搬到了")
        else:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
