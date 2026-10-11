"""VoxFlow 客户端 —— 生成产物的唯一入口。

只走 HTTP，不读 VoxFlow 的数据目录。两个仓各有各的 sqlite（见 core/db.py 顶部
「为什么不共用一个库」），文件系统也各自独立，唯一的耦合点就是这两个端点：

    GET /api/catalog/tracks               有音频的作品 + 生成元数据
    GET /api/catalog/{track_id}/asset/{kind}   音频/封面字节流（kind ∈ audio|cover）

## 为什么是 catalog 而不是 release

拆分之前 VoxFlow 暴露的是 `/api/release/pending?platform=<p>` —— 那个端点里
含**平台判断**（这个平台有没有上架过）、`release_title`（发行名）、
`already_released`。也就是说「发行」这件事还长在 voxflow 身上。

2026-10-10 换成纯生成侧的 `/api/catalog/*`：voxflow 只回答「生成了什么」，
不出现任何平台概念。「待发行」由本仓拿这份列表减去自己台账里已上架的算出来
（见 core/catalog.py 的 pending）—— 发行状态的**唯一真源**在本仓。

## 资产只在本机流转，不出网

沿用 VoxFlow「音频不出本机」的口径。所以这里只用标准库 urllib，不引
requests/httpx：少一个依赖就少一处「万一代理把 127.0.0.1 的请求也劫走」的面。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from core import paths


class VoxflowUnavailable(RuntimeError):
    """连不上 VoxFlow。**必须跟「连上了但没有曲目」区分开** ——
    后者是正常的空列表，前者是故障。混成一句话会导致「待发行列表空了」被误读成
    「没有歌要发」。"""


class VoxflowError(RuntimeError):
    """VoxFlow 答了但报错了（4xx/5xx）。"""


@dataclass
class CatalogTrack:
    """一条可发行的作品（生成侧事实 + 关联键）。

    字段与 VoxFlow `web/app.py` 的 catalog_tracks 响应一一对应。
    **没有 release_title / already_released / platform 状态** —— 那些是发行侧
    自己算的，voxflow 不再提供。
    """

    id: str
    title: str
    lyrics: str = ""
    instrumental: bool = False
    music_type: str = ""
    ai_tool: str = ""
    tags: str = ""
    album_desc: str = ""
    duration: int = 0
    audio_name: str = ""
    cover_name: str = ""
    cover_file: str = ""
    audio_mb: float = 0.0
    # 关联键 —— 跨库没有外键，认领作品全靠这两个。旧版本 VoxFlow 不返回时都是
    # 空串，link_clause() 会当场报错而不是写孤儿记录。
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


def _get(url: str, timeout: float = 300.0) -> Any:
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


def catalog() -> list[CatalogTrack]:
    """曲库里有音频的全部作品。

    超时给到 300 秒：这个端点要对每首音频现算 sha256（几十首、每首几 MB），
    冷启动时会明显超过默认的 30 秒。给短了会变成「间歇性连不上」——
    最难查的那种故障。
    """
    payload = json.loads(_get(_url("/api/catalog/tracks")))
    if not isinstance(payload, list):
        raise VoxflowError(f"预期返回数组，实际是 {type(payload).__name__}")
    out: list[CatalogTrack] = []
    for item in payload:
        out.append(CatalogTrack(
            id=item["id"],
            title=item.get("title", ""),
            lyrics=item.get("lyrics", ""),
            instrumental=bool(item.get("instrumental")),
            music_type=item.get("music_type", ""),
            ai_tool=item.get("ai_tool", ""),
            tags=item.get("tags", ""),
            album_desc=item.get("album_desc", ""),
            duration=int(item.get("duration") or 0),
            audio_name=item.get("audio_name", ""),
            cover_name=item.get("cover_name", ""),
            cover_file=item.get("cover_file", ""),
            audio_mb=float(item.get("audio_mb") or 0),
            clip_id=item.get("clip_id", ""),
            audio_sha256=item.get("audio_sha256", ""),
            raw=item,
        ))
    return out


def costs() -> dict[str, dict]:
    """每首作品的生成成本（按 provider 拆）。

    成本是生成侧事实（voxflow 的 usage_events），收益是发行侧事实（本仓的
    listings）。「回本」要两个数配起来才答得出来 —— 所以成本从这边取，
    结论在本仓算。
    """
    payload = json.loads(_get(_url("/api/catalog/costs")))
    costs_map = payload.get("costs") if isinstance(payload, dict) else None
    if not isinstance(costs_map, dict):
        raise VoxflowError(f"预期 {{'costs': {{...}}}}，实际是 {type(payload).__name__}")
    return costs_map


def fill_lyrics(track_id: str, lyrics: str) -> dict:
    """把歌词补进一首作品。

    **这是唯二的写入口**（另一个是取资产时的转码副作用）。为什么允许写：
    歌词是生成侧的数据（在 voxflow 的 tracks 里），而「去哪个平台查」要用
    本仓的 song_id —— 于是只能由本仓查、往那边写。
    反方向（voxflow 读本仓的库）会把单向依赖变成环。

    那个端点**只填空的、绝不覆盖**已有歌词，也不做模糊匹配 ——
    本地的是生成时的原文，平台那份可能是编辑过或截断的版本。
    """
    body = json.dumps({"lyrics": lyrics}).encode()
    req = urllib.request.Request(
        _url(f"/api/catalog/{urllib.parse.quote(track_id)}/lyrics"),
        data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise VoxflowError(f"VoxFlow 返回 {e.code}：写歌词失败\n{e.read()[:200]!r}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise VoxflowUnavailable(f"连不上 VoxFlow：{e}") from e


def asset_stream(track_id: str, kind: str = "audio") -> tuple[BinaryIO, str]:
    """取音频/封面字节流。

    返回 (流, 文件名)。文件名是从 Content-Disposition 解析的 —— 平台靠它判断
    有没有真的收下文件（不能读 `input.files`：React 组件接收后会把 files 清空、
    转存到自己的 state）。所以**没有文件名就等于没法确认上传成功**。

    注意音频在 VoxFlow 那边已经转过 320k MP3，这里拿到的就是可直接上传的成品，
    不要再转一次。
    """
    if kind not in ("audio", "cover"):
        raise ValueError("kind 只能是 audio 或 cover")
    url = _url(f"/api/catalog/{urllib.parse.quote(track_id)}/asset/{kind}")
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


def fetch_asset(track_id: str, kind: str = "audio",
                dest_dir: "Path | None" = None) -> Path:
    """取资产并落盘，返回本地路径。

    浏览器驱动填表需要**真实文件路径**（DataTransfer 和 CDP 的文件选择器都只认
    本地文件），所以 HTTP 取回来必须先落地。

    先写 `.part` 再 rename：中途断掉不会留下半个文件被当成完整音频传上去 ——
    平台对损坏的音频报的是「音频无效」，看不出是本地没下完。
    """
    import shutil

    dest_dir = Path(dest_dir) if dest_dir else paths.OUT_DIR / "待上传"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stream, filename = asset_stream(track_id, kind)
    if not filename:
        # 文件名是判断「平台有没有真的收下」的依据（不能读 input.files），
        # 拿不到就不能瞎编一个 —— 让它显式失败。
        stream.close()
        raise VoxflowError(
            f"VoxFlow 没给 Content-Disposition 文件名（track={track_id}, kind={kind}）。"
            f"平台靠文件名判断有没有收下文件，没有名字等于没法确认上传成功。")
    dest = dest_dir / f"{track_id[:8]}-{filename}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with open(tmp, "wb") as f:
            shutil.copyfileobj(stream, f)
        stream.close()
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return dest
