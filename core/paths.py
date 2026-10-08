"""路径真源 —— 全部路径只在这里定义一次。

从 voxflow 拆出来的独立仓。数据目录从 `~/.voxflow/` 换成 `~/.suno-publisher/`：
两个仓各自独立发版、共用一个 sqlite 文件会在两边都写时撞 `database is locked`
（voxflow 的 core/db.py:184-192 注释写明它的设计前提是「本地单用户工具」）。
"""
from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
PROJECT_DIR = Path(__file__).resolve().parent.parent


# ── 本机数据（不进 git）──────────────────────────────────
# SUNO_PUBLISHER_HOME 可以整体改掉数据目录。存在的理由很实际：
# 测试要往临时目录写，而测试经常要**起子进程**跑脚本 —— 子进程看不到父进程里
# 改过的模块属性，只认得到环境变量。没有这个出口，测试只能往真实的
# ~/.suno-publisher 里写，那等于用测试污染真台账。
DATA_DIR = Path(os.environ.get("SUNO_PUBLISHER_HOME") or (HOME / ".suno-publisher"))
CONFIG_DIR = DATA_DIR / "configs"
DB_PATH = DATA_DIR / "suno-publisher.db"
OUT_DIR = DATA_DIR / "out"
# 平台账号台账只存显示名和状态元数据，**不存凭据**（cookie/token 走系统钥匙串）。
PLATFORM_ACCOUNTS_FILE = CONFIG_DIR / "platform_accounts.json"
# 艺人档案含真实姓名（版权登记和收益结算按法律姓名走），属于个人信息。
ARTIST_FILE = CONFIG_DIR / "artist.json"
# 平台发行知识是**个人资产**：含签约模式、账号入驻名额、收益区间，
# 不进任何 git 仓。没有它时按 EXAMPLE_FILE 兜底，见 core/platforms.py。
PLATFORMS_FILE = CONFIG_DIR / "platforms.json"
NOTIFY_FILE = CONFIG_DIR / "notify.json"


# ── 代码自带的资源（跟着版本走，进 git）──────────────────
PLATFORMS_EXAMPLE_FILE = PROJECT_DIR / "configs" / "platforms.example.json"
PLATFORMS_SCHEMA_FILE = PROJECT_DIR / "schemas" / "platforms.schema.json"
TEMPLATES_DIR = PROJECT_DIR / "templates"
SCHEMA_VERSION_FILE = PROJECT_DIR / "schemas" / "VERSION"


# ── 跨仓契约 ────────────────────────────────────────────
# voxflow 作为本机长驻服务提供发行资产，本仓通过 HTTP 取，**不读它的数据目录**。
# 端点：GET /api/release/pending?platform=<p>
#       GET /api/release/{track_id}/asset/{kind}   kind ∈ audio|cover
VOXFLOW_API = os.environ.get("VOXFLOW_API", "http://127.0.0.1:8866")


# 必需目录 —— **只在这里定义一次**。
REQUIRED_DIRS = (DATA_DIR, CONFIG_DIR, OUT_DIR)


def ensure_dirs() -> None:
    for d in REQUIRED_DIRS:
        d.mkdir(parents=True, exist_ok=True)
