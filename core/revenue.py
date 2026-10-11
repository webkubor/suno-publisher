"""收益核算 —— 上架之后赚了多少、回本了没有。

2026-10-10 从 voxflow 的 `core/obs.py` 搬过来。**为什么归本仓**：收益的输入是
「上架之后平台报的播放量和提现金额」，那是发行侧的事实（本仓的 listings 与
platform_accounts）。voxflow 那边只有生成成本，没有收益，也就答不出「回本」。

## 成本从哪来

生成成本在 voxflow（`usage_events` 记了每次调用花了多少 credits、按当时单价
折多少钱）。本仓通过 `GET /api/catalog/costs` 取。

两边各算各的那半边，「回本」这个结论只在本仓产生 —— 否则两个仓各给一个数，
迟早对不上而没人知道该信谁。

## 三个数一定要分清来源

`cny_per_1k_plays` 可能来自两处，返回时必须带上 `rate_source`：

- `measured` —— 平台后台同时给了累计播放量和可提现金额，两个一除就是这个账号
  实际拿到的单价。比任何公开资料都准，因为它含了这个账号的实际权益档位。
- `configured` —— 没实测过，用平台配置里那个从公开资料抄来的区间中位数。

分不清的后果是：过一阵子没人知道看到的是实测还是猜测，而这两者的决策价值完全不同。
"""
from __future__ import annotations

import json
from typing import Any

from core import db
from core import platforms as P


def _parse_cn_number(v: object) -> float:
    """解析平台后台那种中文缩写数字：'3.4w' → 34000、'1.2万' → 12000、'567' → 567。

    平台自己就是这么显示的，抓下来是什么样就存什么样（原样存是对的 ——
    改写会让「台账和后台对不上」变得无法排查）。转换放在读取侧。
    """
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v or "").strip().replace(",", "")
    if not t:
        return 0.0
    mult = 1.0
    for suf, m in (("亿", 1e8), ("万", 1e4), ("w", 1e4), ("W", 1e4),
                   ("k", 1e3), ("K", 1e3)):
        if t.endswith(suf):
            t, mult = t[: -len(suf)], m
            break
    try:
        return float(t) * mult
    except ValueError:
        return 0.0


def _revenue_cfg() -> dict[str, dict[str, Any]]:
    """各平台的分成配置。真源是平台知识文件里的 `revenue` 段。

    用 `raw_platforms()` 而不是 `PLATFORMS`：后者只暴露 label/cover/ai_field/console
    四个派生字段，`revenue` 会被丢掉（派生字段是给表单用的，不是给算账用的）。
    """
    out: dict[str, dict[str, Any]] = {}
    for key, spec in (P.raw_platforms() or {}).items():
        if key.startswith("_") or not isinstance(spec, dict):
            continue
        rev = spec.get("revenue")
        if isinstance(rev, dict):
            out[key] = {**rev, "label": spec.get("label", key)}
    return out


def platform_rates() -> dict[str, dict[str, Any]]:
    """各平台的播放量 / 收益 / 实测单价。

    单价优先用**实测**（后台累计播放量与可提现金额一除），没有实测才退回配置里的
    估算值，并标明 configured。
    """
    db.init()
    cfg = _revenue_cfg()
    out: dict[str, dict[str, Any]] = {}
    with db.connect() as c:
        rows = c.execute(
            "SELECT platform, artist_name, song_count, stats FROM platform_accounts"
        ).fetchall()

    for r in rows:
        try:
            st = json.loads(r["stats"] or "{}")
        except (json.JSONDecodeError, TypeError):
            st = {}
        plays = _parse_cn_number(st.get("play_count"))
        earned = _parse_cn_number(st.get("withdrawable_cny"))
        pc = cfg.get(r["platform"]) or {}

        if plays > 0 and earned > 0:
            rate, source = round(earned / plays * 1000, 4), "measured"
        else:
            rate, source = float(pc.get("cny_per_1k_plays", 0) or 0), "configured"

        out[r["platform"]] = {
            "label": pc.get("label") or r["platform"],
            "artist": r["artist_name"],
            "songs": r["song_count"],
            "plays": int(plays),
            "earned_cny": round(earned, 2),
            "cny_per_1k_plays": rate,
            "rate_source": source,
            "plays_7d": int(_parse_cn_number(st.get("play_7d"))),
            "fans": int(_parse_cn_number(st.get("fans"))),
            "synced_at": st.get("synced_at", ""),
        }
    return out


def track_revenue() -> dict[str, dict]:
    """按**关联键**聚合每首歌实际赚了多少、被播了多少次。

    键是 `(clip_id, audio_sha256)` 里非空的那个 —— 跨库没有外键，这是唯一能
    把一条上架记录认回一首作品的东西。同一首歌可能投了多个平台，所以按平台拆开
    再汇总。

    **没有数据就返回空，不做按比例分摊。** 用账号总收益按播放占比摊到单曲，
    算出来的数字看着很合理，但它其实只是「总收益 ÷ 首数」的变体，完全无法回答
    「哪首歌值得再做一首同风格的」—— 而那正是要它的唯一理由。一个能误导选题的
    数字，比没有数字糟得多。
    """
    db.init()
    out: dict[str, dict] = {}
    with db.connect() as c:
        rows = c.execute(
            "SELECT clip_id, audio_sha256, platform, platform_title, "
            "plays, earned_cny, stats_at FROM listings "
            "WHERE plays > 0 OR earned_cny > 0"
        ).fetchall()
    for r in rows:
        key = (r["clip_id"] or "") or (r["audio_sha256"] or "")
        if not key:
            # 关联键双空 = 还没认回作品的老记录。它们的收益是真的，但归不到
            # 某一首上，所以进不了「单曲收益」—— 见 platform_rates 看平台维度。
            continue
        t = out.setdefault(key, {
            "plays": 0, "earned_cny": 0.0, "by_platform": {},
            "title": r["platform_title"] or "",
        })
        t["by_platform"][r["platform"]] = {
            "plays": r["plays"] or 0,
            "earned_cny": round(r["earned_cny"] or 0, 4),
            "stats_at": r["stats_at"] or "",
        }
        t["plays"] += r["plays"] or 0
        t["earned_cny"] = round(t["earned_cny"] + (r["earned_cny"] or 0), 4)
    return out


def ledger_totals() -> dict[str, Any]:
    """全台账合计 —— 含**还没认回作品**的记录。

    为什么不能只报 `track_revenue()` 的合计：那 31 条 unlinked 记录的播放量和
    收益是真的（实测台账 325 次播放 / ¥0.14，能归到单曲的只有 16 次 / ¥0.01）。
    只报后者会低报一个数量级，而**看板数字偏小不会报错** —— 只会让人以为
    「这门生意不赚钱」，然后基于错的前提做决定。
    """
    db.init()
    with db.connect() as c:
        r = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(plays),0) plays, "
            "COALESCE(SUM(earned_cny),0) earned, "
            "SUM(CASE WHEN status IN ('online','published') THEN 1 ELSE 0 END) online "
            "FROM listings"
        ).fetchone()
    return {
        "listings": r["n"],
        "online": r["online"] or 0,
        "plays": int(r["plays"] or 0),
        "earned_cny": round(r["earned"] or 0, 2),
    }


def breakeven_plays(cost_cny: float, platform: str = "qishui",
                    rate_override: float | None = None) -> int:
    """回本需要多少播放量。成本 ÷ 千播分成 × 1000。

    分成率未证实的平台（配置里 cny_per_1k_plays = 0）返回 0 表示「算不了」，
    而不是拿一个猜的数算出个像模像样的结果 —— 那种数字会被当真。

    `rate_override` 用来传实测单价（见 `platform_rates`）。
    """
    rate = float((_revenue_cfg().get(platform) or {}).get("cny_per_1k_plays", 0) or 0)
    if rate_override is not None:
        rate = rate_override
    if rate <= 0 or cost_cny <= 0:
        return 0
    return int(cost_cny / rate * 1000)
