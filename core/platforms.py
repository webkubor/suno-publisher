"""平台知识加载 —— 用户目录 → 仓库示例，三级回退。

## 为什么要三级

平台发行知识是**个人项目资产**：`configs/platforms.json` 含签约模式、账号入驻名额、
收益区间这些商业状态，2026-10-08 起从 voxflow 脱管出来，不进任何 git 仓。

但没有兜底的话全新 clone 的人拿到的是空 dict，发行页面**静默退化成空白且不报错**
（这是 voxflow 那边真实踩过的坑：`_load_platforms()` 只有两级回退时，
git rm 掉项目内那份之后 `publication_board()` 返回 `{'accounts': [], 'tracks': []}`）。

所以第三级是仓库里的 `platforms.example.json` —— 结构完整的脱敏骨架，
保留「加平台要填什么」，抹掉「我签了什么、还剩几次、赚多少」。

## schema 校验做什么、不做什么

`schemas/platforms.schema.json` 只检查**结构**（字段在不在、类型对不对），
不检查「平台页面是否还是这样」。后者只有人能做，靠 `verified_at` 记录实测时间 ——
示例文件里它一律是 `null`，提醒使用者「这份没实测过」。
"""
from __future__ import annotations

import json
from typing import Any

from core.paths import PLATFORMS_EXAMPLE_FILE, PLATFORMS_FILE

# 加载器只派生这几个字段，够跑通发行流程的骨架。
_DERIVED = ("label", "cover", "ai_field", "console")


def _load_one(path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for key, spec in raw.items():
        if key.startswith("_") or not isinstance(spec, dict):
            continue  # `_说明` 这类注释键不是平台
        out[key] = {
            "label": spec.get("label", key),
            "cover": (spec.get("cover") or {}).get("min_size", "待确认"),
            "ai_field": (spec.get("ai_declaration") or {}).get("field", "待确认")
            if isinstance(spec.get("ai_declaration"), dict)
            else (spec.get("ai_declaration") or "待确认"),
            "console": (spec.get("entries", {}).get("single", {}) or {}).get("url")
            or spec.get("console", ""),
        }
    return out


def load_platforms() -> dict[str, dict[str, Any]]:
    """返回 {key: 派生字段}。用户目录优先，找到就不再看仓库里的。"""
    for f in (PLATFORMS_FILE, PLATFORMS_EXAMPLE_FILE):
        if not f.exists():
            continue
        try:
            out = _load_one(f)
        except (OSError, json.JSONDecodeError):
            continue
        if out:
            return out
    return {}


def raw_platforms() -> dict[str, Any]:
    """返回完整 JSON（发行脚本要读表单字段、封面尺寸这些派生之外的细节）。"""
    for f in (PLATFORMS_FILE, PLATFORMS_EXAMPLE_FILE):
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return {}


def is_example() -> bool:
    """当前用的是否是仓库示例 —— 是就说明用户还没配自己的平台文件。"""
    return not PLATFORMS_FILE.exists() or not _load_one(PLATFORMS_FILE)


PLATFORMS = load_platforms()
