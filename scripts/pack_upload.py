#!/usr/bin/env python3
"""把要手工上传的一首歌打成一个文件夹。

    .venv/bin/python scripts/pack_upload.py [发行歌名 ...]
    #   不带参数 = 所有「备料就绪」的曲目；带参数 = 只打这几个

从 voxflow 的 `scripts/pack_upload.py` 移植（2026-10-11）。

## 为什么需要它

音频在 voxflow 的库里，封面文件名是哈希（`d5217f476aa6_1440.jpg`）——
人要手动上传时，根本找不到哪张图配哪首歌。自动填表跑得通时无所谓，
**跑不通就得手工，那时候这个目录是救命的**。

产出：`~/.suno-publisher/out/待上传/<发行歌名>/`
  歌名.mp3 / 歌名_封面.jpg / 上传信息.txt
一个文件夹拖完，不用在几个目录之间找。

## 移植时改了什么

| 原来 | 现在 | 为什么 |
|---|---|---|
| 直读 `~/.voxflow/out/music/*.mp3` | `voxflow_client.fetch_asset()` 走 HTTP | **不读另一个仓的数据目录** —— 那是两边各自独立发版的前提 |
| 输出到 `~/.voxflow/out/待上传/` | `~/.suno-publisher/out/待上传/` | 产物归本仓，跟 voxflow 的数据目录不搅在一起 |
| `release_title` 取自 voxflow 的 tracks 列 | 取本仓 `releases` 表 | 发行身份在本仓（那列在 voxflow 已删） |
| 专辑信息从 `track["platforms"]` 取 | 从本仓 `listings` 取 | 上架台账在本仓 |

## 发行名回退

优先用 `releases.release_title`；没锁过发行名的退回曲库的生成名。
**不是「随便取一个」**：没锁发行名说明这首歌还没走到定名那一步，
用生成名打出来只是让人先看着，真正上传前仍要走 `lock_release_title`。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import catalog, db, paths  # noqa: E402
from core import voxflow_client as vc  # noqa: E402

DEST_ROOT = paths.OUT_DIR / "待上传"


def _release_title_for(t: vc.CatalogTrack) -> str:
    """本仓锁过的发行名；没锁过退回生成名。"""
    rel = catalog.get_release(clip_id=t.clip_id, audio_sha256=t.audio_sha256)
    return ((rel or {}).get("release_title") or "").strip() or t.title or t.id[:8]


def _artist() -> dict:
    try:
        return json.loads(paths.ARTIST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _album_of(t: vc.CatalogTrack) -> str:
    """这首歌挂在哪个专辑下（取本仓 listings 里最近一条）。"""
    db.init()
    with db.connect() as c:
        row = c.execute(
            "SELECT album_name FROM listings WHERE "
            "(clip_id!='' AND clip_id=?) OR (audio_sha256!='' AND audio_sha256=?) "
            "AND IFNULL(album_name,'')!='' LIMIT 1",
            (t.clip_id, t.audio_sha256)).fetchone()
    return (row["album_name"] if row else "") or ""


def pack(t: vc.CatalogTrack) -> Path | None:
    name = _release_title_for(t)
    d = DEST_ROOT / name
    d.mkdir(parents=True, exist_ok=True)

    # 音频和封面都走 HTTP 取 —— 本仓不读 voxflow 的数据目录。
    # fetch_asset 会先写 .part 再 rename：中途断了不会留半个文件被当成完整音频传上去。
    try:
        audio = vc.fetch_asset(t.id, "audio", dest_dir=d)
    except (vc.VoxflowUnavailable, vc.VoxflowError) as e:
        print(f"  ✗ {name}：取音频失败 {str(e)[:60]}")
        return None
    dst = d / f"{name}{audio.suffix}"
    audio.replace(dst)
    # 文件名里带 track_id 前缀，去掉 —— 上传时平台看到的就是歌名
    if audio.exists():
        audio.unlink()

    cover_note = ""
    if t.cover_file:
        try:
            cover = vc.fetch_asset(t.id, "cover", dest_dir=d)
            cdst = d / f"{name}_封面{cover.suffix}"
            cover.replace(cdst)
            if cover.exists():
                cover.unlink()
            cover_note = cdst.name
        except (vc.VoxflowUnavailable, vc.VoxflowError) as e:
            print(f"  ⚠ {name}：取封面失败 {str(e)[:50]}")

    artist = _artist()
    roles = artist.get("roles") or {}
    dur = int(t.duration or 0)
    instrumental = (t.lyrics or "").strip().lower() in ("", "[instrumental]")
    lines = [
        f"歌曲标题：{name}",
        f"时长：{dur // 60}:{dur % 60:02d}",
        f"表演者 / 词作者 / 曲作者 / 制作人："
        f"{roles.get('performer') or artist.get('stage_name','')}",
        f"专辑名称：{_album_of(t) or t.album_desc}",
        f"专辑歌手：{roles.get('album_artist') or artist.get('stage_name','')}",
        f"是否是纯音乐：{'是' if instrumental else '否'}",
        f"歌词：{t.lyrics or '[Instrumental]'}",
        f"AI 创作声明：是（使用的AI工具：{t.ai_tool or 'Suno'}）",
        f"音乐类型：{t.music_type or '原创'}",
        "",
        "── 第二步 · 授权作品 ──",
        # 真实姓名不能用艺名。版权登记和收益结算按法律姓名走，
        # 填成艺名会在结算时对不上人。
        f"词作者真实姓名：{artist.get('real_name','')}",
        f"曲作者真实姓名：{artist.get('real_name','')}",
        "授权比例：100%",
    ]
    (d / "上传信息.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✓ {name} → {d}" + (f"（封面 {cover_note}）" if cover_note else ""))
    return d


def main() -> int:
    want = set(sys.argv[1:])
    DEST_ROOT.mkdir(parents=True, exist_ok=True)

    try:
        tracks = list(vc.catalog())
    except (vc.VoxflowUnavailable, vc.VoxflowError) as e:
        print(f"✗ 连不上 VoxFlow，取不到曲库：{e}")
        print("  打包要音频和封面，两者都从那边取。")
        return 1

    n = 0
    for t in tracks:
        name = _release_title_for(t)
        if want and name not in want:
            continue
        if not want:
            # 不带参数时只打「备料就绪」的 —— 缺封面之类的打出来也用不了
            r = catalog.readiness(t, "qishui")
            if not r.get("ok"):
                continue
        if pack(t):
            n += 1
    print(f"\n打包 {n} 首 → {DEST_ROOT}")
    if n == 0:
        print("（没打到东西。不带参数时只打包备料就绪的曲目；"
              "也可以直接给发行歌名：pack_upload.py 歌名）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
