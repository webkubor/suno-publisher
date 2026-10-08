"""VoxFlow 客户端 —— 发行资产的唯一入口。

只走 HTTP，不读 VoxFlow 的数据目录。两个仓各有各的 sqlite（见 core/db.py 顶部
「为什么不共用一个库」），文件系统也各自独立，唯一的耦合点就是这两个端点。

**资产只在本机流转，不出网** —— 沿用 VoxFlow「音频不出本机」的口径。
所以这里只用标准库 urllib，不引 requests/httpx：少一个依赖就少一处
「万一代理把 127.0.0.1 的请求也劫走」的面。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from core import paths


class VoxflowUnavailable(RuntimeError):
    """连不上 VoxFlow。**必须跟「连上了但没有待发行曲目」区分开** ——
    后者是正常的空列表，前者是故障。混成一句话会导致「台账空着」被误读成
    「没有歌要发」。"""


class VoxflowError(RuntimeError):
    """VoxFlow 答了但报错了（4xx/5xx）。"""


@dataclass
class PendingTrack:
    """一条待发行曲目。字段与 VoxFlow `web/app.py` 的 release_pending 响应一一对应。"""

    id: str
    title: str
    lyrics: str = ""
    instrumental: bool = False
    music_type: str = ""
    ai_tool: str = ""
    already_released: bool = False
    tags: str = ""
    album_desc: str = ""
    duration: int = 0
    audio_name: str = ""
    cover_name: str = ""
    audio_mb: float = 0.0
    # 关联键 —— 2026-10-08 给 VoxFlow 的端点补的字段。旧版本 VoxFlow 不返回，
    # 这时两个都是空串，link_clause() 会当场报错而不是写孤儿记录。
    clip_id: str = ""
    audio_sha256: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_link(self) -> bool:
        return bool(self.clip_id or self.audio_sha256)


def _url(path: str, **params: Any) -> str:
    # 调用时读而不是 import 时拷一份：VOXFLOW_API 改环境变量后要立刻生效，
    # 测试也要能把它指向一个假端口。
    url = f"{paths.VOXFLOW_API.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def _get(url: str, timeout: float = 30.0) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.read()
    except urllib.error.HTTPError as e:
        raise VoxflowError(f"VoxFlow 返回 {e.code}：{url}\n{e.read()[:200]!r}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise VoxflowUnavailable(
            f"连不上 VoxFlow（{paths.VOXFLOW_API}）：{e}\n"
            f"先起服务：cd ~/dev/video/voxflow && .venv/bin/voxflow web"
        ) from e


def ping() -> bool:
    """VoxFlow 活着吗。返回 False 而不是抛异常 —— 调用方通常只是想提前打个招呼。"""
    try:
        urllib.request.urlopen(_url("/docs"), timeout=5)  # noqa: S310
        return True
    except Exception:
        return False


def pending(platform: str = "qishui") -> list[PendingTrack]:
    """该平台待发行的曲目。

    判据在 VoxFlow 那边：「有音频、且这个平台还没有上架/已提交记录」。
    返回空列表是正常的（没歌要发），跟 VoxflowUnavailable 严格区分。
    """
    payload = json.loads(_get(_url("/api/release/pending", platform=platform)))
    if not isinstance(payload, list):
        raise VoxflowError(f"预期返回数组，实际是 {type(payload).__name__}")
    out: list[PendingTrack] = []
    for item in payload:
        out.append(PendingTrack(
            id=item["id"],
            title=item.get("title", ""),
            lyrics=item.get("lyrics", ""),
            instrumental=bool(item.get("instrumental")),
            music_type=item.get("music_type", ""),
            ai_tool=item.get("ai_tool", ""),
            already_released=bool(item.get("already_released")),
            tags=item.get("tags", ""),
            album_desc=item.get("album_desc", ""),
            duration=int(item.get("duration") or 0),
            audio_name=item.get("audio_name", ""),
            cover_name=item.get("cover_name", ""),
            audio_mb=float(item.get("audio_mb") or 0),
            clip_id=item.get("clip_id", ""),
            audio_sha256=item.get("audio_sha256", ""),
            raw=item,
        ))
    return out


def asset_stream(track_id: str, kind: str = "audio") -> tuple[BinaryIO, str]:
    """取音频/封面字节流。

    返回 (流, 文件名)。文件名是从 Content-Disposition 解析的 —— 平台靠它判断
    有没有真的收下文件（不能读 `input.files`：React 组件接收后会把 files 清空、
    转存到自己的 state）。所以**没有文件名就等于没法确认上传成功**。

    注意音频在 VoxFlow 那边已经转过 320k MP3（core/release.py），这里拿到的
    就是可直接上传的成品，不要再转一次。
    """
    if kind not in ("audio", "cover"):
        raise ValueError("kind 只能是 audio 或 cover")
    url = _url(f"/api/release/{urllib.parse.quote(track_id)}/asset/{kind}")
    try:
        resp = urllib.request.urlopen(url, timeout=120)  # noqa: S310
    except urllib.error.HTTPError as e:
        raise VoxflowError(f"VoxFlow 返回 {e.code}：取 {kind} 失败\n{e.read()[:200]!r}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise VoxflowUnavailable(f"连不上 VoxFlow：{e}") from e
    return resp, _filename_from(resp.headers.get("Content-Disposition", ""))


def _filename_from(disposition: str) -> str:
    """从 Content-Disposition 取文件名。解析不出来返回空串，不猜。

    `filename*` 优先于 `filename` —— 前者是 RFC 5987 编码，中文文件名走它。
    格式是 `filename*=UTF-8''<百分号编码>`，必须解码否则拿到的是
    `%E5%A4%9C%E8%88%AA.mp3` 这种字符串，上传时平台会看到乱码文件名。
    """
    if not disposition:
        return ""
    for part in disposition.split(";"):
        part = part.strip()
        low = part.lower()
        if low.startswith("filename*="):
            val = part[len("filename*="):].strip().strip('"')
            if "''" in val:                       # RFC 5987: charset'lang'value
                val = val.split("''", 1)[1]
            return urllib.parse.unquote(val)
        if low.startswith("filename="):
            return part[len("filename="):].strip().strip('"')
    return ""
