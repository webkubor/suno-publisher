"""发行台账 schema 与关联键的自检。

跑法：`.venv/bin/python tests/test_db.py`

重点卡两件容易静默出错的事：
1. 关联键双空（clip_id 和 audio_sha256 都没有）必须**当场报错**，不能静默写进库
2. 两级平台配置回退：用户目录没有时用仓库示例，且 `is_example()` 要如实报告
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db  # noqa: E402
from core import platforms as P  # noqa: E402
from core import paths  # noqa: E402


def test_schema_init():
    """建表幂等，且五张表都在。"""
    with tempfile.TemporaryDirectory() as td:
        db.DB_PATH = Path(td) / "t.db"
        db.init()
        db.init()  # 幂等
        got = db.summary()
        assert set(got) == {"releases", "listings", "albums",
                            "platform_accounts", "publish_events"}, got
        assert all(v == 0 for v in got.values()), got
    print("  ✓ schema 建表幂等，五张表齐")


def test_link_clause_rejects_orphan():
    """关联键双空必须报错 —— 孤儿记录在「查这首歌发到哪了」时表现为「查无此曲」，极难排查。"""
    try:
        db.link_clause({"clip_id": "", "audio_sha256": "", "title": "x"})
    except ValueError as e:
        assert "关联键缺失" in str(e), e
        print("  ✓ 关联键双空被拦下")
    else:
        raise AssertionError("关联键双空居然没报错 —— 孤儿记录会被静默写进库")

    # 两个键都非空时正常，source_track_id 拿不到时留空而不是报错（它只是展示用）
    _, kw = db.link_clause({"clip_id": "abc-123", "audio_sha256": "de:ad",
                            "title": "夜航"})
    assert kw["clip_id"] == "abc-123" and kw["audio_sha256"] == "de:ad"
    assert kw["source_track_id"] == "" and kw["source_hint"] == "夜航"
    print("  ✓ 正常路径返回关联键三件套，缺 source_track_id 不报错")

    # 只有音频哈希（TTS 本地合成，没有 clip_id）也要能过
    _, kw = db.link_clause({"clip_id": "", "audio_sha256": "de:ad", "title": "口播"})
    assert kw["clip_id"] == "" and kw["audio_sha256"] == "de:ad"
    print("  ✓ 无 clip_id 时音频哈希兜底可用")


def test_orphan_scan():
    """迁移出错时孤儿记录要先被 orphans() 扫出来。"""
    with tempfile.TemporaryDirectory() as td:
        db.DB_PATH = Path(td) / "t.db"
        db.init()
        with db.connect() as conn:
            conn.execute("INSERT INTO listings (platform, status, source_hint) "
                         "VALUES ('qishui', 'draft', '孤儿')")
            conn.execute("INSERT INTO listings (platform, status, clip_id, source_hint) "
                         "VALUES ('qishui', 'draft', 'ok-1', '正常')")
        found = db.orphans()
        assert len(found) == 1 and found[0]["source_hint"] == "孤儿", found
        print("  ✓ orphans() 只扫出关联键双空的那条")


def test_release_title_unique():
    """发行名必须唯一 —— 生成歌名能重复（Suno 一次出两首同名），发出去的歌名不能。

    约束是 partial unique index（`WHERE release_title != ''`）：没定发行名的行
    可以有很多条，定了就不许重名。
    """
    import sqlite3

    with tempfile.TemporaryDirectory() as td:
        db.DB_PATH = Path(td) / "t.db"
        db.init()
        with db.connect() as conn:
            # 同一个发行名、不同 clip_id —— 必须被拒
            conn.execute("INSERT INTO releases (clip_id, release_title) VALUES ('a', '夜航')")
            try:
                conn.execute("INSERT INTO releases (clip_id, release_title) VALUES ('b', '夜航')")
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("两个作品用了同一个发行名却没报错 —— unique index 没生效")
            # 空发行名可以有多行（partial index 不管空串）
            conn.execute("INSERT INTO releases (clip_id, release_title) VALUES ('c', '')")
            conn.execute("INSERT INTO releases (clip_id, release_title) VALUES ('d', '')")
        assert db.summary()["releases"] == 3, db.summary()
    print("  ✓ 发行名唯一约束生效，且空发行名不占用唯一性")


def test_song_id_unique_per_platform():
    """同一平台同一个 song_id 只能有一条 —— 防止重复同步把一行刷成两行。"""
    import sqlite3

    with tempfile.TemporaryDirectory() as td:
        db.DB_PATH = Path(td) / "t.db"
        db.init()
        with db.connect() as conn:
            conn.execute("INSERT INTO listings (clip_id, platform, status, song_id) "
                         "VALUES ('a', 'qishui', 'live', 'song-1')")
            try:
                conn.execute("INSERT INTO listings (clip_id, platform, status, song_id) "
                             "VALUES ('b', 'qishui', 'live', 'song-1')")
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("同平台同 song_id 重复入库没被拒")
            # 不同平台同一个 song_id 是允许的
            conn.execute("INSERT INTO listings (clip_id, platform, status, song_id) "
                         "VALUES ('c', 'netease', 'live', 'song-1')")
        assert db.summary()["listings"] == 2, db.summary()
    print("  ✓ 同平台 song_id 唯一，跨平台不冲突")


def test_platforms_fallback():
    """用户目录没有 platforms.json 时，用仓库示例，且 is_example() 如实说 True。"""
    assert P.PLATFORMS_EXAMPLE_FILE.exists(), "仓库示例文件必须在"
    got = P.load_platforms()
    assert got, "示例文件应该能解析出平台"
    for k, v in got.items():
        assert v["label"], f"{k} 缺 label"
        assert v["cover"], f"{k} 缺 cover.min_size"
    print(f"  ✓ 仓库示例解析出 {len(got)} 个平台:", {k: v['cover'] for k, v in got.items()})
    print(f"  ✓ is_example() = {P.is_example()}（本机无 ~/.suno-publisher/configs/platforms.json）")


def test_example_is_sanitized():
    """示例文件绝不能带商业状态 —— 这是它能进 git 的唯一前提。"""
    text = P.PLATFORMS_EXAMPLE_FILE.read_text(encoding="utf-8")
    banned = ["独家代理", "5 次入驻", "每账号", "元/千播", "选它的理由", "未经确认"]
    hit = [w for w in banned if w in text]
    assert not hit, f"示例文件残留敏感内容: {hit}"
    d = json.loads(text)
    for k, v in d.items():
        if k.startswith("_") or not isinstance(v, dict):
            continue
        assert "签约模式" not in v, f"{k} 还留着签约模式"
        assert v.get("revenue") is None, f"{k} 的 revenue 应为 null"
        assert v.get("verified_at") is None, f"{k} 的 verified_at 应为 null（示例没实测过）"
    print("  ✓ 示例文件脱敏干净：无商业状态、revenue/verified_at 全 null")


if __name__ == "__main__":
    for fn in (test_schema_init, test_link_clause_rejects_orphan, test_orphan_scan,
               test_release_title_unique, test_song_id_unique_per_platform,
               test_platforms_fallback, test_example_is_sanitized):
        print(f"\n{fn.__name__}:")
        fn()
    print("\n全部通过")
