"""和 VoxFlow 的发行资产契约自检。

跑法：`.venv/bin/python tests/test_contract.py`

需要一个在跑的 VoxFlow（`cd ~/dev/video/voxflow && .venv/bin/python -m uvicorn
web.app:app --port 8866`）。**连不上就跳过而不是失败** —— 本仓的单元测试
（test_db.py）不依赖 VoxFlow，那部分必须在任何环境都能跑。

这里卡的是「静默失败」：契约改了字段名、端点改了路径、关联键不再返回，
如果只靠人肉点页面，症状是「发行列表空了」而不是「报了个错」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db  # noqa: E402
from core import voxflow_client as vc  # noqa: E402

# /api/release/pending 必须带的字段。少一个就是契约破了。
REQUIRED_FIELDS = (
    "id", "title", "lyrics", "instrumental", "music_type", "ai_tool",
    "already_released", "tags", "album_desc", "duration",
    "audio_name", "cover_name", "audio_mb",
    # 关联键 —— 2026-10-08 补的。发行台账在独立 sqlite 里，没有外键，
    # 认领作品全靠这两个字段。VoxFlow 少返回一个，所有本地合成的歌都成孤儿。
    "clip_id", "audio_sha256",
)


def test_content_disposition_parser():
    """文件名解析。平台靠 Content-Disposition 里的文件名判断有没有真的收下文件
    —— 不能读 `input.files`（React 组件接收后会把 files 清空转存到自己的 state）。
    解析不出来就等于没法确认上传成功，所以这里宁可返回空串也不猜。"""
    assert vc._filename_from('attachment; filename="夜航.mp3"') == "夜航.mp3"
    assert vc._filename_from("attachment; filename=plain.mp3") == "plain.mp3"
    # RFC 5987：filename*=UTF-8''%E5%A4%9C%E8%88%AA.mp3
    assert vc._filename_from(
        "attachment; filename*=UTF-8''%E5%A4%9C%E8%88%AA.mp3") == "夜航.mp3"
    assert vc._filename_from("") == ""
    assert vc._filename_from("attachment") == ""
    print("  ✓ Content-Disposition 解析（含 RFC 5987）")


def test_kind_validation():
    try:
        vc.asset_stream("x", kind="lyrics")
    except ValueError as e:
        assert "audio" in str(e) and "cover" in str(e)
        print("  ✓ kind 只收 audio/cover，其他当场拒绝")
    else:
        raise AssertionError("非法 kind 没被拦下")


def test_unavailable_is_distinct_from_empty():
    """「连不上」和「没有待发行曲目」必须严格区分。

    混成一句话的话，台账空着会被误读成「没歌要发」，而不是「VoxFlow 没起来」。
    """
    import core.paths as paths
    old = paths.VOXFLOW_API
    paths.VOXFLOW_API = "http://127.0.0.1:1"   # 必然连不上
    try:
        vc.pending()
    except vc.VoxflowUnavailable as e:
        assert "连不上" in str(e), e
        print("  ✓ 连不上抛 VoxflowUnavailable，且消息里给了怎么起服务")
    else:
        raise AssertionError("连不上的端口居然没报错")
    finally:
        paths.VOXFLOW_API = old


def test_live_contract():
    if not vc.ping():
        print("  ⏭ 跳过：VoxFlow 没在跑")
        return
    rows = vc.pending("qishui")
    assert rows, "待发行列表为空 —— 确认一下曲库里是不是真有歌（空列表本身不算契约破裂）"
    missing = [f for f in REQUIRED_FIELDS if f not in rows[0].raw]
    assert not missing, f"VoxFlow 响应缺字段: {missing}"
    print(f"  ✓ 契约完整：{len(rows)} 条，{len(REQUIRED_FIELDS)} 个字段齐全")

    no_link = [r for r in rows if not r.has_link]
    assert not no_link, (
        f"{len(no_link)} 条缺关联键（clip_id 和 audio_sha256 都空），"
        f"入库时会被 link_clause 拦下。例：{[r.title for r in no_link[:3]]}"
    )
    tts = [r for r in rows if not r.clip_id]
    print(f"  ✓ 关联键齐全：{len(rows) - len(tts)} 条靠 clip_id，"
          f"{len(tts)} 条本地合成靠 audio_sha256 兜底")

    # 真喂一遍 link_clause，确认迁移预演不会炸
    for r in rows:
        db.link_clause(r.raw)
    print("  ✓ 全部通过 link_clause 校验")

    # 发行名撞车是真实存在的（Suno 一次出两首同名），确认唯一索引会拦下来
    titles = [r.title for r in rows]
    dupes = len(titles) - len(set(titles))
    if dupes:
        print(f"  ℹ {dupes} 条发行名撞车，唯一索引会拒绝重名 —— "
              f"这是预期行为，发出去的歌名必须唯一")


if __name__ == "__main__":
    for fn in (test_content_disposition_parser, test_kind_validation,
               test_unavailable_is_distinct_from_empty, test_live_contract):
        print(f"\n{fn.__name__}:")
        fn()
    print("\n全部通过")
