"""suno-publisher 的发行服务 —— 发行侧的**唯一**入口。

## 为什么要有这一层

拆分之前，发行界面和发行端点都在 voxflow 里，浏览器扩展直接打 voxflow 的
`/api/release/*`。那样等于「发行」这件事仍然长在 voxflow 身上，只是台账搬了家 ——
拆了一半。

现在改成**单向依赖**：

    Chrome 扩展 ──► suno-publisher（本服务，发行唯一入口）
                        │
                        └──► voxflow（只取生成产物：曲目元数据 + 音频/封面字节）

voxflow 不再知道「发行」的存在；扩展不再需要知道 voxflow 在哪。扩展只认这一个
端口，两个仓各自的 UI 也各归各。

## 端口

voxflow 占 8866，本服务默认 **8867**。两边同时起互不干扰。
`SUNO_PUBLISHER_PORT` 可覆盖。

## 一条硬规矩：区分「连不上」和「没有」

`core/voxflow_client.py:22` 的 `VoxflowUnavailable` 存在的全部理由就是这条 ——
「voxflow 没起来」和「没有歌要发」在界面上长得一模一样（都是空列表），
混起来会让人以为「发行队列清空了」而不是「服务挂了」。所以：

- voxflow 连不上 → **503** + 明确文案（不是 200 空数组）
- 没有待发行曲目 → 200 + `[]`
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# 允许 `python web/app.py` 直接跑（本仓没有装成包的场景）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from fastapi.responses import (  # noqa: E402
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)

from core import catalog, paths  # noqa: E402
from core import voxflow_client as vc  # noqa: E402

app = FastAPI(
    title="suno-publisher",
    description="AI 音乐发行 —— 填表到只差最后一下，那一下留给人",
    version="0.1.0",
)

UI_DIR = Path(__file__).resolve().parent / "ui"


# ── 健康 ────────────────────────────────────────────────
@app.get("/api/health")
def health() -> dict[str, Any]:
    """本服务活着没有 + 上游（voxflow）够不够得着。

    把上游状态放进健康检查而不是等用户点发行才发现 —— 「扩展填到一半发现
    取不到音频」比「打开就知道上游没起」贵得多。
    """
    ok = vc.ping()
    return {
        "ok": True,
        "service": "suno-publisher",
        "voxflow": {
            "reachable": ok,
            "api": paths.VOXFLOW_API,
            "hint": "" if ok else (
                "voxflow 没在跑 —— 发行需要它提供曲目元数据与音频。"
                "起法：cd ~/dev/video/voxflow && .venv/bin/voxflow web"),
        },
        "counts": _safe_counts(),
    }


def _safe_counts() -> dict[str, int]:
    """台账计数。库还没建时返回空 dict 而不是抛 —— 健康检查不该因为
    「还没迁过数据」就 500。"""
    try:
        return db_summary()
    except Exception:
        return {}


def db_summary() -> dict[str, int]:
    from core import db
    db.init()
    return db.summary()


# ── 发行契约（扩展只认这几个）────────────────────────────
@app.get("/api/release/pending")
def release_pending(platform: str = Query("qishui")) -> JSONResponse:
    """该平台待发行的曲目 + 填表需要的全部元数据。

    **判据在本仓**（2026-10-10 起）：本仓拿 voxflow 的曲库减去自己台账里该平台
    已交出去的，算出「待发行」。voxflow 只回答「生成了什么」，不认识平台。
    **voxflow 连不上要 503，不能返回空数组** —— 见模块头。
    """
    try:
        rows = catalog.pending(platform)
    except vc.VoxflowUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except vc.VoxflowError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return JSONResponse([_track_payload(t, platform) for t in rows])


def _track_payload(t: vc.CatalogTrack, platform: str) -> dict[str, Any]:
    """曲库字段原样透传，再补本仓才知道的那一项。

    透传而不是重新组装：字段漏一个，扩展那边的填表就会少填一项，
    而症状是「平台上某个框空着」—— 提交之后才发现。

    `already_released` 必须由本仓补：它问的是「这首在**别的**平台有没有上线」，
    是发行侧的事实，voxflow 已经不提供也不再知道。
    """
    payload = dict(t.raw) if t.raw else {}
    payload.setdefault("id", t.id)
    payload.setdefault("title", t.title)
    payload.setdefault("clip_id", t.clip_id)
    payload.setdefault("audio_sha256", t.audio_sha256)
    payload["already_released"] = _released_elsewhere(t, platform)
    return payload


def _released_elsewhere(t: vc.CatalogTrack, platform: str) -> bool:
    """这首在**除该平台以外**有没有已上线记录。

    只算 online/published，不算 submitted —— 提交了但还没上线，对「是否已发行」
    这个问题来说还不算。这一项会影响平台表单上那个「是否已发行」开关，
    报错了等于替用户做了一个错误的版权声明。
    """
    from core import db
    db.init()
    with db.connect() as c:
        rows = c.execute(
            "SELECT clip_id, audio_sha256 FROM listings "
            "WHERE platform != ? AND status IN ('online','published')",
            (platform,)).fetchall()
    for r in rows:
        if r["clip_id"] and r["clip_id"] == t.clip_id:
            return True
        if r["audio_sha256"] and r["audio_sha256"] == t.audio_sha256:
            return True
    return False


@app.get("/api/release/{track_id}/asset/{kind}")
def release_asset(track_id: str, kind: str) -> Response:
    """音频 / 封面的字节流（kind ∈ audio|cover）。代理到 voxflow。

    **必须原样带上 Content-Disposition 里的文件名**：平台靠文件名判断有没有
    真的收下文件（读不到 `input.files`，React 组件收下后会清空它）。所以这里
    转发而不是重新生成文件名 —— 中间任何一层丢了名字，平台侧就变成
    「上传了但没识别」，而报错是「音频无效」，看不出是文件名丢了。
    """
    if kind not in ("audio", "cover"):
        raise HTTPException(status_code=400, detail="kind 只能是 audio 或 cover")
    url = (f"{paths.VOXFLOW_API.rstrip('/')}/api/catalog/"
           f"{urllib.parse.quote(track_id)}/asset/{kind}")
    try:
        upstream = urllib.request.urlopen(url, timeout=120)  # noqa: S310
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=e.code,
                            detail=f"voxflow 取 {kind} 失败：{e.read()[:200]!r}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise HTTPException(
            status_code=503,
            detail=f"连不上 voxflow，取不到 {kind}：{e}") from e

    headers: dict[str, str] = {}
    disp = upstream.headers.get("Content-Disposition")
    if disp:
        headers["Content-Disposition"] = disp
    mime = upstream.headers.get("Content-Type") or "application/octet-stream"
    return StreamingResponse(upstream, media_type=mime, headers=headers)


# ── 台账（本仓自有数据，不依赖 voxflow）────────────────────
@app.get("/api/board")
def board() -> dict[str, Any]:
    """发行看板：平台账号 + 已上架记录 + 台账计数。"""
    return catalog.publication_board()


@app.get("/api/albums")
def albums(platform: str = "") -> dict[str, Any]:
    return {"albums": catalog.list_albums(platform)}


@app.get("/api/platform-accounts")
def platform_accounts() -> dict[str, Any]:
    return catalog.list_platform_accounts()


@app.get("/api/listings")
def listings(platform: str = "", link_state: str = "") -> dict[str, Any]:
    """上架台账。`link_state=unlinked` 能看到还没认回作品的那批 ——
    它们带着真实的播放量与收益，不是垃圾数据，等人认领。"""
    from core import db
    db.init()
    sql = "SELECT * FROM listings WHERE 1=1"
    args: list[Any] = []
    if platform:
        sql += " AND platform=?"
        args.append(platform)
    if link_state:
        sql += " AND link_state=?"
        args.append(link_state)
    sql += " ORDER BY updated_at DESC"
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    return {"listings": rows, "count": len(rows)}


@app.get("/api/events")
def events(platform: str = "", limit: int = 100) -> dict[str, Any]:
    from core import db
    db.init()
    sql = "SELECT * FROM publish_events"
    args: list[Any] = []
    if platform:
        sql += " WHERE platform=?"
        args.append(platform)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    return {"events": rows}


@app.get("/api/artist")
def artist() -> dict[str, Any]:
    return catalog.artist_identity()


# ── 单位经济学 ──────────────────────────────────────────
@app.get("/api/economics")
def economics() -> dict[str, Any]:
    """「这门生意的账」—— 发行侧的那本。

    为什么归本仓：要回答「这首歌花了多少、发出去回本了吗」，需要**成本**配
    **收益**。收益是上架之后平台报的数（本仓的 listings / platform_accounts），
    成本是生成花的钱（voxflow 的 usage_events，从 /api/catalog/costs 取）。

    两边各算各的那半边，**回本结论只在这里产生** —— 两个仓各给一个数迟早对不上，
    而没人知道该信谁。

    voxflow 连不上时：收益部分照常给（本仓自有数据），成本与回本留空并说明原因。
    不是 503 —— 这一屏看的主要是收益，上游挂了不该整屏打不开。
    """
    from core import revenue

    rates = revenue.platform_rates()
    per_track = revenue.track_revenue()

    # 全台账合计（含还没认回作品的记录）。
    # **必须和 per_track 分开报**：那 31 条 unlinked 记录的播放量和收益是真的，
    # 只是归不到某一首上。只报 per_track 会低报一大截（实测 325 报成 16），
    # 而看板数字偏小不会报错，只会让人以为「没赚到钱」。
    ledger = revenue.ledger_totals()

    total_plays = sum(v["plays"] for v in per_track.values())
    total_earned = round(sum(v["earned_cny"] for v in per_track.values()), 2)
    avg_breakeven = 0

    # 实测单价：各平台里取「有实测值的那个」的加权口径，取不到就用配置值
    measured = [v["cny_per_1k_plays"] for v in rates.values()
                if v["rate_source"] == "measured" and v["cny_per_1k_plays"] > 0]
    measured_rate = round(sum(measured) / len(measured), 4) if measured else 0.0

    costs: dict[str, Any] = {}
    cost_error = ""
    try:
        costs = vc.costs()
    except vc.VoxflowUnavailable as e:
        cost_error = f"连不上 VoxFlow，成本与回本算不了：{e}"
    except vc.VoxflowError as e:
        cost_error = f"VoxFlow 取成本失败：{e}"

    total_cost = round(sum(v.get("cost_cny", 0) or 0 for v in costs.values()), 2)
    covered = [k for k in per_track if k in costs]
    if costs and measured_rate:
        per_cost = [v.get("cost_cny", 0) or 0 for v in costs.values()]
        avg_cost = sum(per_cost) / len(per_cost) if per_cost else 0
        avg_breakeven = revenue.breakeven_plays(avg_cost, rate_override=measured_rate)

    return {
        "rates": rates,
        "per_track": per_track,
        "ledger": ledger,
        "summary": {
            # 全台账口径 —— 「这个账号一共被播了多少、赚了多少」
            "ledger_plays": ledger["plays"],
            "ledger_earned_cny": ledger["earned_cny"],
            # 能归到具体作品的口径 —— 「哪首歌值得再做一首同风格的」
            "total_plays": total_plays,
            "total_earned_cny": total_earned,
            "unlinked_plays": ledger["plays"] - total_plays,
            "unlinked_earned_cny": round(ledger["earned_cny"] - total_earned, 2),
            "total_cost_cny": total_cost,
            "cny_per_1k_plays_measured": measured_rate,
            "avg_breakeven_plays": avg_breakeven,
            "tracks_with_cost": len(costs),
            "tracks_with_revenue": len(per_track),
            # 成本和收益都有的作品数 —— 只有这些才算得出真正的单曲盈亏
            "tracks_with_both": len(covered),
        },
        "cost_error": cost_error,
    }


# ── UI ──────────────────────────────────────────────────
@app.get("/")
def index() -> Response:
    f = UI_DIR / "index.html"
    if not f.is_file():
        return JSONResponse({"detail": "UI 还没构建，见 web/ui/index.html"}, 404)
    return FileResponse(f)


def main() -> None:
    import uvicorn
    port = int(os.environ.get("SUNO_PUBLISHER_PORT") or 8867)
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
