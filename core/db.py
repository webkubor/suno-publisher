"""发行台账存储层 —— suno-publisher 自己的 sqlite，不与 voxflow 共用文件。

## 为什么不与 voxflow 共用一个库

voxflow 的 `core/db.py:184-192` 写明它的 `connect()` 每次开新连接、不用全局单例，
设计前提是「本地单用户工具」。两个独立发版的仓同时打开同一个 sqlite 文件，等于把两个
「单用户工具」的假设叠在一起：`timeout=10` 的锁等待在两边都频繁写时会炸
`database is locked`，而任何一边改 schema 都会在不知情的情况下冲击另一边。

所以这里是**快照式**的：P3 阶段从 voxflow 导出一次发行数据落地，之后两边各自演进。

## 怎么关联回 voxflow 的作品 —— 不用外键，用内容键

跨 sqlite 文件的 `FOREIGN KEY ... ON DELETE CASCADE` 物理上不成立（`core/db.py:102`
那条外键在原库里就会让 voxflow 删曲时级联删掉发行记录，这里刻意不要那个行为）。

关联键按可靠性排序，**绝不用标题**：

1. `clip_id` —— Suno 生成的作品有，天然唯一
2. `audio_sha256` —— 本地 TTS 合成的没有 clip_id，用音频内容哈希兜底
   （`core/attest.py:47` 已有现成的 `_sha256_file()` 可复用）
3. `source_track_id` —— voxflow 的 tracks.id，**仅作展示提示，不是权威键**：
   它是 UUID，重建库时可能变，拿它当权威键等于把两边重新绑死
4. `source_hint` —— 标题快照，纯给人看

为什么不能只靠标题：`scripts/sync_suno.py:23` 原文写着「Suno 一次出两首同名歌，
按标题匹配必然串行」。`core/pipeline.py:752-798` 的 `resolve_track_for_listing`
已经在用 song_id → release_title → 标题+唯一原曲 → 时长±1秒 的多级兜底，
那套启发式原样搬过来了。
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from core.paths import DB_PATH, ensure_dirs


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """拿一条连接，出作用域自动提交/回滚。

    每次开新连接而不是全局单例：同 voxflow 的理由 —— 本地单用户工具，连接开销可忽略，
    而全局连接会在多线程下踩 sqlite 的线程限制。
    """
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── schema ──────────────────────────────────────────────
# 关联键三件套在每张作品相关的表里都出现，所以抽成一段 SQL 复用。
_LINK_COLS = """
    -- 关联 voxflow 作品：clip_id 优先，audio_sha256 兜底，source_track_id 仅展示
    clip_id        TEXT DEFAULT '',
    audio_sha256   TEXT DEFAULT '',
    source_track_id TEXT DEFAULT '',
    source_hint    TEXT DEFAULT '',
"""

SCHEMA = f"""
-- 发行身份： voxflow tracks.release_title / release_platform 两列搬到了这里。
--
-- 为什么单独一张表而不是往 listings 加列：发行身份是「一首歌」级别的，
-- 而上架台账是「一首歌 × 一个平台」级别的 —— 一首投三个平台就是三行 listings，
-- 但发行名只有一个。混在一张表里要么冗余要么拆不干净。
--
-- release_title 唯一：生成歌名可以重复（Suno 一次出两首同名），但**发出去的歌名
-- 必须唯一**。这是从 voxflow 的 idx_tracks_release_title 搬过来的约束。
CREATE TABLE IF NOT EXISTS releases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
{_LINK_COLS}
    release_title    TEXT NOT NULL DEFAULT '',
    -- 独家授权：一首只能投一个平台。JSON 数组，如 ["qishui"]
    locked_platforms TEXT DEFAULT '[]',
    -- draft → prepping → submitted → reviewing → live
    state            TEXT DEFAULT 'draft',
    created_at       TEXT,
    updated_at       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rel_title ON releases(release_title)
    WHERE release_title != '';
CREATE INDEX IF NOT EXISTS idx_rel_link ON releases(clip_id, audio_sha256);

-- 上架台账：一条记录 = 一个作品在某个平台上的一次上架。
-- 字段从 voxflow 的 track_platforms 搬来，去掉 track_id 外键换成关联键三件套。
CREATE TABLE IF NOT EXISTS listings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
{_LINK_COLS}
    platform       TEXT NOT NULL,
    platform_title TEXT DEFAULT '',    -- 平台上的歌名，可以跟 release_title 不同
    status         TEXT NOT NULL,
    song_id        TEXT DEFAULT '',
    song_url       TEXT DEFAULT '',
    album_key      TEXT DEFAULT '',    -- → albums.key
    album_name     TEXT DEFAULT '',
    track_no       INTEGER,
    duration       INTEGER,
    publish_date   TEXT DEFAULT '',
    cover_url      TEXT DEFAULT '',
    cover_local    TEXT DEFAULT '',
    config         TEXT DEFAULT '{{}}',
    note           TEXT DEFAULT '',
    submitted_at   TEXT DEFAULT '',
    plays          INTEGER DEFAULT 0,
    earned_cny     REAL DEFAULT 0,
    stats_at       TEXT DEFAULT '',
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ls_platform ON listings(platform, status);
CREATE INDEX IF NOT EXISTS idx_ls_link ON listings(clip_id, audio_sha256, platform);
-- 同一首歌在同一平台，**同一个 song_id 只能有一条**
CREATE UNIQUE INDEX IF NOT EXISTS idx_ls_song ON listings(platform, song_id)
    WHERE song_id != '';

-- 平台专辑
CREATE TABLE IF NOT EXISTS albums (
    key          TEXT PRIMARY KEY,      -- <platform>-<album_id>
    platform     TEXT NOT NULL,
    album_id     TEXT NOT NULL,
    title        TEXT NOT NULL,
    track_count  INTEGER DEFAULT 0,
    publish_date TEXT DEFAULT '',
    company      TEXT DEFAULT '',
    description  TEXT DEFAULT '',
    tags         TEXT DEFAULT '',
    cover_url    TEXT DEFAULT '',
    cover_local  TEXT DEFAULT '',
    url          TEXT DEFAULT '',
    synced_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_alb_platform ON albums(platform, publish_date DESC);

-- 平台账号。凭据不在这里 —— cookie/token 走系统钥匙串。
CREATE TABLE IF NOT EXISTS platform_accounts (
    platform     TEXT PRIMARY KEY,
    label        TEXT DEFAULT '',
    artist_id    TEXT DEFAULT '',
    artist_name  TEXT DEFAULT '',
    alias        TEXT DEFAULT '[]',     -- JSON 数组
    avatar_url   TEXT DEFAULT '',
    brief        TEXT DEFAULT '',
    artist_url   TEXT DEFAULT '',
    user_id      TEXT DEFAULT '',
    user_url     TEXT DEFAULT '',
    song_count   INTEGER DEFAULT 0,
    album_count  INTEGER DEFAULT 0,
    stats        TEXT DEFAULT '{{}}',   -- JSON：播放量/粉丝/收益，只有后台有
    synced_at    TEXT
);

-- 状态流转事件流。单独一张表而不是往 listings 加列：状态变更是**多条**，
-- 加列只能存最后一次，历史就没了 —— 而「这首歌卡了几天」「驳回过几次」
-- 恰好是运营最常问的。
CREATE TABLE IF NOT EXISTS publish_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id     TEXT DEFAULT '',
    audio_sha256 TEXT DEFAULT '',
    platform    TEXT NOT NULL,
    from_status TEXT DEFAULT '',
    to_status   TEXT NOT NULL,
    actor       TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pe_link ON publish_events(clip_id, audio_sha256, ts DESC);
"""


def init() -> None:
    """建库。幂等 —— 每次启动都跑。"""
    with connect() as conn:
        conn.executescript(SCHEMA)


# ── 小工具 ──────────────────────────────────────────────
def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def link_clause(track: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """从一条作品记录里取出关联键三件套。

    `clip_id` 和 `audio_sha256` **至少要有一个非空** —— 两个都空的话这条记录
    永远关联不回任何作品，是个孤儿。宁可当场报错也不要静默写进去：
    孤儿记录在「查这首歌发到哪了」时会表现为「查无此曲」，很难排查。
    """
    clip_id = (track.get("clip_id") or "").strip()
    audio_sha256 = (track.get("audio_sha256") or "").strip()
    if not clip_id and not audio_sha256:
        raise ValueError(
            "关联键缺失：clip_id 和 audio_sha256 不能同时为空。"
            "没有 clip_id 时用音频内容哈希兜底（见 core/attest.py 的 _sha256_file）。"
        )
    return (
        "",
        {
            "clip_id": clip_id,
            "audio_sha256": audio_sha256,
            "source_track_id": track.get("source_track_id") or "",
            "source_hint": track.get("title") or track.get("source_hint") or "",
        },
    )


def summary() -> dict[str, int]:
    """对账用 —— P3 迁移前后各跑一次，数字对不上就是迁漏了。"""
    with connect() as conn:
        cur = conn.cursor()
        out: dict[str, int] = {}
        for t in ("releases", "listings", "albums", "platform_accounts", "publish_events"):
            cur.execute(f"SELECT COUNT(*) FROM {t}")  # 表名来自白名单常量，非用户输入
            out[t] = cur.fetchone()[0]
        return out


def orphans() -> list[dict[str, Any]]:
    """找出关联键双空的记录 —— 迁移出错时它们会先冒出来。"""
    with connect() as conn:
        cur = conn.cursor()
        found: list[dict[str, Any]] = []
        for t in ("releases", "listings"):
            cur.execute(
                f"SELECT * FROM {t} WHERE clip_id = '' AND audio_sha256 = ''"  # noqa: S608
            )
            found.extend(_rows(cur))
        return found
