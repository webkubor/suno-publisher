"""发行目录层自检。

跑法：`.venv/bin/python tests/test_catalog.py`

只测不依赖 VoxFlow 的部分（albums / platform_accounts / listings / 事件流）。
依赖 VoxFlow 的路径在 tests/test_contract.py 里测。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import catalog as C  # noqa: E402
from core import db  # noqa: E402
from core import paths  # noqa: E402


REL_A = {"clip_id": "clip-aaa", "audio_sha256": "sha-aaa",
         "source_track_id": "vox-1", "source_hint": "夜航"}
REL_B = {"clip_id": "", "audio_sha256": "sha-bbb",
         "source_track_id": "vox-2", "source_hint": "水乡月夜"}


def _fresh_db():
    td = tempfile.TemporaryDirectory()
    db.DB_PATH = Path(td.name) / "t.db"
    db.init()
    return td          # 调用方负责 .cleanup()


def test_album_upsert_is_idempotent():
    """平台专辑反复同步不能每次插一行 —— key 是 <platform>-<album_id>。"""
    td = _fresh_db()
    try:
        k = C.album_key("qishui", "alb-1")
        C.upsert_album(k, platform="qishui", album_id="alb-1", title="甜酷上线", track_count=2)
        C.upsert_album(k, platform="qishui", album_id="alb-1", title="甜酷上线", track_count=3)
        rows = C.list_albums("qishui")
        assert len(rows) == 1, rows
        assert rows[0]["track_count"] == 3, "第二次同步应覆盖曲数"
        assert C.get_album("alb-1", "qishui")["title"] == "甜酷上线"
        # 另一个平台的同名专辑不该被覆盖
        C.upsert_album(C.album_key("netease", "alb-1"), platform="netease",
                       album_id="alb-1", title="另一个辑")
        assert len(C.list_albums()) == 2
    finally:
        td.cleanup()
    print("  ✓ 专辑 upsert 幂等，跨平台不串")


def test_platform_account_json_roundtrip():
    """alias / stats 是 JSON 字符串存库、取用时解回来。"""
    td = _fresh_db()
    try:
        C.upsert_platform_account("qishui", artist_name="某音乐人",
                                  alias=["别名A", "别名B"], stats={"plays": 1200})
        got = C.list_platform_accounts()["qishui"]
        assert got["alias"] == ["别名A", "别名B"], got["alias"]
        assert got["stats"]["plays"] == 1200, got["stats"]
        # 没给 alias/stats 时要有默认值，不能是 None
        C.upsert_platform_account("tencent", artist_name="某人")
        got2 = C.list_platform_accounts()["tencent"]
        assert got2["alias"] == [] and got2["stats"] == {}, got2
    finally:
        td.cleanup()
    print("  ✓ 平台账号 JSON 字段往返正常")


def test_status_transition_writes_events():
    """状态流转必须同时落 publish_events —— 只存最后一次就答不了「卡了几天」。"""
    td = _fresh_db()
    try:
        C.set_platform_status(REL_A, "qishui", "draft")
        C.set_platform_status(REL_A, "qishui", "submitted", note="已提交")
        C.set_platform_status(REL_A, "qishui", "reviewing")
        evs = C.events(clip_id="clip-aaa")
        assert len(evs) == 3, f"应有 3 条事件，实际 {len(evs)}"
        # 最新的在前
        assert evs[0]["to_status"] == "reviewing", evs[0]
        assert evs[1]["from_status"] == "draft" and evs[1]["to_status"] == "submitted"
        # 同一首 + 同一平台只有一行上架记录
        ls = C.listings_for(clip_id="clip-aaa", platform="qishui")
        assert len(ls) == 1 and ls[0]["status"] == "reviewing", ls
    finally:
        td.cleanup()
    print("  ✓ 状态流转落事件流，上架记录不重复")


def test_two_platforms_are_separate_listings():
    """一首投两个平台 = 两行 listings，各自独立状态。"""
    td = _fresh_db()
    try:
        C.set_platform_status(REL_A, "qishui", "online")
        C.set_platform_status(REL_A, "netease", "submitted")
        assert len(C.listings_for(clip_id="clip-aaa")) == 2
        assert C.listings_for(clip_id="clip-aaa", platform="qishui")[0]["status"] == "online"
        assert C.listings_for(clip_id="clip-aaa", platform="netease")[0]["status"] == "submitted"
    finally:
        td.cleanup()
    print("  ✓ 一首投多平台各自独立")


def test_works_without_clip_id():
    """本地 TTS 合成的歌没有 clip_id，只靠音频哈希 —— 事件和台账都要能定位它。"""
    td = _fresh_db()
    try:
        C.set_platform_status(REL_B, "qishui", "draft")
        evs = C.events(audio_sha256="sha-bbb")
        assert len(evs) == 1 and evs[0]["clip_id"] == "", evs
        assert len(C.listings_for(audio_sha256="sha-bbb")) == 1
    finally:
        td.cleanup()
    print("  ✓ 无 clip_id 的作品靠音频哈希定位")


def test_release_title_unique_blocks_duplicates():
    """锁定发行名时撞名必须报错，不能自动改名 —— 发行名是要出现在平台页面上的，
    自动加后缀等于替你做了个业务决定。"""
    import sqlite3
    td = _fresh_db()
    try:
        with db.connect() as c:
            id1 = c.execute("INSERT INTO releases (clip_id) VALUES ('c1')").lastrowid
            id2 = c.execute("INSERT INTO releases (clip_id) VALUES ('c2')").lastrowid
        C.lock_release_title(id1, "夜航", "qishui")
        assert C.get_release(clip_id="c1")["release_title"] == "夜航"

        try:
            C.lock_release_title(id2, "夜航", "netease")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("第二个作品锁同名发行名居然没报错")
        # 撞名的那条不能被写脏
        assert C.get_release(clip_id="c2")["release_title"] == ""

        # 同一条重复锁同名是安全的（备料流程会重跑）
        C.lock_release_title(id1, "夜航", "qishui")
        assert db.summary()["releases"] == 2
    finally:
        td.cleanup()
    print("  ✓ 发行名撞车被唯一索引拦下，未自动改名；重复锁同名安全")


def test_publication_board_shape():
    td = _fresh_db()
    try:
        C.upsert_platform_account("qishui", artist_name="某音乐人")
        C.set_platform_status(REL_A, "qishui", "online")
        b = C.publication_board()
        assert len(b["accounts"]) == 1 and b["accounts"][0]["platform"] == "qishui"
        assert b["platforms"]["qishui"]["label"] == "汽水音乐", b["platforms"]
        assert len(b["platforms"]["qishui"]["listings"]) == 1
        assert b["counts"]["listings"] == 1
    finally:
        td.cleanup()
    print("  ✓ 发行看板结构完整")


def test_cover_size_gate():
    """封面尺寸闸门：读不出尺寸放行，声明的最小值比实际小则放行。
    Suno 自带 360×360 不能用 —— 平台会打回，只判「有封面」是查不出来的。"""
    assert C._cover_big_enough(None, "1440x1440") is False, "没封面应不通过"
    assert C._cover_big_enough("whatever.png", "待确认") is True, "规格未确认应放行"
    assert C._cover_big_enough("whatever.png", "garbage") is True, "解析不出应放行"
    assert C._cover_big_enough("/nonexistent/x.png", "1440x1440") is True, \
        "文件读不到应放行，不因本地缺 PIL 卡人"
    print("  ✓ 封面尺寸闸门：缺封面拦、规格不明放行")


def test_readiness_lyrics_branch():
    """纯音乐不该被「歌词必填」卡住 —— 那是从 voxflow 搬过来的假缺口。

    背景：汽水的「是否是纯音乐」开关不设对时，页面报
    「您上传的音频非纯音乐，请填写歌词」，读起来像检测到人声，
    照着错误提示去贴歌词是白费功夫。纯音乐本来就没歌词。
    """
    from core.voxflow_client import PendingTrack

    base = dict(id="t1", title="夜航", audio_name="a.m4a", cover_name="c.png",
                audio_sha256="sha-x", lyrics="", instrumental=True)
    inst = C.readiness(PendingTrack(**base), "qishui")
    names = [i["名称"] for i in inst["items"]]
    assert "歌词" not in names, f"纯音乐不该有歌词检查项: {names}"
    assert "已标记为纯音乐（无歌词）" in names, names

    vocal = C.readiness(PendingTrack(**{**base, "instrumental": False,
                                        "lyrics": ""}), "qishui")
    vnames = [i["名称"] for i in vocal["items"]]
    assert "歌词" in vnames, f"有词作品必须查歌词: {vnames}"
    lyric_item = next(i for i in vocal["items"] if i["名称"] == "歌词")
    assert lyric_item["就绪"] is False, "有词作品没歌词应报缺口"

    vocal_ok = C.readiness(PendingTrack(**{**base, "instrumental": False,
                                           "lyrics": "夜航不停"}), "qishui")
    assert next(i for i in vocal_ok["items"] if i["名称"] == "歌词")["就绪"] is True
    print("  ✓ 纯音乐走「已标记为纯音乐」，有词作品才查歌词")


def test_readiness_flags_missing_link():
    """关联键缺失要报出来 —— 这种歌入库时会被 link_clause 拦下，
    提前在备料这步看到比入库炸掉好。"""
    from core.voxflow_client import PendingTrack

    t = PendingTrack(id="t2", title="x", audio_name="a.m4a", cover_name="c.png",
                     clip_id="", audio_sha256="")
    r = C.readiness(t, "qishui")
    item = next(i for i in r["items"] if i["名称"].startswith("关联键"))
    assert item["就绪"] is False, item
    assert r["缺口数"] >= 1
    print("  ✓ 关联键缺失在备料阶段就报出来")


if __name__ == "__main__":
    for fn in (test_album_upsert_is_idempotent, test_platform_account_json_roundtrip,
               test_status_transition_writes_events, test_two_platforms_are_separate_listings,
               test_works_without_clip_id, test_release_title_unique_blocks_duplicates,
               test_publication_board_shape, test_cover_size_gate,
               test_readiness_lyrics_branch, test_readiness_flags_missing_link):
        print(f"\n{fn.__name__}:")
        fn()
    print("\n全部通过")
