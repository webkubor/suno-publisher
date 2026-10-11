"""发行服务的契约自检。

跑法：`.venv/bin/python tests/test_web.py`

这一层卡的是**拆仓之后最容易静默失效的东西**：扩展只认 suno-publisher 这一个
端口了，所以这里任何一个端点坏掉，症状都不是「报错」而是「发行列表空了」或
「填表少填一项」。两类必须显式测：

1. **voxflow 连不上必须是 503，不能是 200 空数组。**
   `core/voxflow_client.py:22` 的 `VoxflowUnavailable` 存在的全部理由就是这条。
   混成空数组的话，「服务挂了」会被读成「发行队列清空了」。

2. **资产的文件名必须原样转发。**
   平台靠 Content-Disposition 里的文件名判断有没有真的收下文件（读不到
   `input.files`，React 组件收下后会清空它）。中间丢了名字 → 平台侧报
   「音频无效」，从报错看不出是文件名丢了。

不需要 voxflow 在跑：连不上的路径用假端口，连得上的路径没起就跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.paths as paths  # noqa: E402
from core import voxflow_client as vc  # noqa: E402


def _client():
    from fastapi.testclient import TestClient

    from web.app import app
    return TestClient(app)


def test_health_shape():
    c = _client()
    r = c.get("/api/health")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert "voxflow" in d, "健康检查必须报上游状态，否则只能等点发行才发现上游没起"
    assert "reachable" in d["voxflow"]
    print(f"  ✓ /api/health 报上游状态（reachable={d['voxflow']['reachable']}）")


def test_voxflow_down_is_503_not_empty():
    """最重要的一条：上游挂了不能伪装成「没有歌要发」。"""
    old = paths.VOXFLOW_API
    paths.VOXFLOW_API = "http://127.0.0.1:1"      # 必然连不上
    try:
        c = _client()
        r = c.get("/api/release/pending?platform=qishui")
        assert r.status_code == 503, (
            f"voxflow 连不上时必须 503，实际 {r.status_code} —— "
            f"返回 200 空数组会让人以为发行队列清空了")
        assert "连不上" in r.text or "voxflow" in r.text.lower()
        print("  ✓ voxflow 连不上 → 503（不是 200 空数组）")
    finally:
        paths.VOXFLOW_API = old


def test_asset_kind_validation():
    c = _client()
    r = c.get("/api/release/whatever/asset/lyrics")
    assert r.status_code == 400, f"非法 kind 必须 400，实际 {r.status_code}"
    print("  ✓ 非法 kind → 400")


def test_local_endpoints_do_not_need_voxflow():
    """台账是本仓自有数据 —— voxflow 挂了也该读得到。

    这条防的是「把自有数据也做成了上游依赖」：发行台账已经迁过来了，
    上游不可用不该连带看不了自己的历史。
    """
    old = paths.VOXFLOW_API
    paths.VOXFLOW_API = "http://127.0.0.1:1"
    try:
        c = _client()
        for ep in ("/api/board", "/api/listings", "/api/albums",
                   "/api/platform-accounts", "/api/events", "/api/artist"):
            r = c.get(ep)
            assert r.status_code == 200, f"{ep} 不该依赖 voxflow：{r.status_code} {r.text[:120]}"
        print("  ✓ 台账类端点不依赖 voxflow（上游挂了仍可读自有数据）")
    finally:
        paths.VOXFLOW_API = old


def test_live_contract_and_asset_filename():
    """voxflow 在跑时，把扩展真正依赖的两件事验一遍。"""
    if not vc.ping():
        print("  ⏭ 跳过：voxflow 没在跑")
        return
    c = _client()
    r = c.get("/api/release/pending?platform=qishui")
    assert r.status_code == 200, r.text
    rows = r.json()
    if not rows:
        print("  ⏭ 跳过：没有待发行曲目")
        return
    need = ("id", "title", "clip_id", "audio_sha256", "instrumental", "lyrics")
    missing = [f for f in need if f not in rows[0]]
    assert not missing, f"透传缺字段: {missing}"
    print(f"  ✓ pending 契约完整：{len(rows)} 条")

    a = c.get(f"/api/release/{rows[0]['id']}/asset/audio")
    assert a.status_code == 200, a.text[:200]
    disp = a.headers.get("content-disposition", "")
    assert "filename" in disp.lower(), (
        "资产必须带 Content-Disposition 文件名 —— 平台靠它判断有没有真的收下文件")
    assert len(a.content) > 1000, f"音频太小（{len(a.content)} 字节），像是取空了"
    print(f"  ✓ 资产转发保留文件名（{len(a.content) // 1024} KB）")


if __name__ == "__main__":
    for fn in (test_health_shape, test_voxflow_down_is_503_not_empty,
               test_asset_kind_validation, test_local_endpoints_do_not_need_voxflow,
               test_live_contract_and_asset_filename):
        print(f"\n{fn.__name__}:")
        fn()
    print("\n全部通过")
