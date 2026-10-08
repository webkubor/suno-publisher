"""发行目录层 —— 备料、查状态、管专辑和平台账号。

从 voxflow 的 `core/pipeline.py`（1670 行）里摘出来的发行那半截。**不是照搬**：
voxflow 那份里发行语义和生成语义混在一个文件、共享一张 `tracks` 表，这里改成
「作品元数据从 VoxFlow 的 HTTP 端点取，发行台账落自己的库」。

## 搬运时改了什么

| voxflow 原样 | 这里 | 为什么 |
|---|---|---|
| `get_track(track_id)` 查 tracks 表 | `find_pending(track_id)` 走客户端 | 曲库不在本仓了 |
| `publish_fields()` 现场推导 | 直接用端点返回的字段 | 推导规则要读 note/tags/clip_id，作品数据在 VoxFlow 那边；让它一次算好传过来，省一份重复实现漂移 |
| `set_platform_status` 按 track_id 写 | 按关联键写 listings | 跨库没有 track 外键 |
| `readiness` 查 tracks 的 cover_file | 用端点给的封面名 + 尺寸探测 | 同上 |

**推导规则保留在 VoxFlow 那边**是有意的：`is_instrumental`（歌词为空 = 纯音乐）、
`music_type`（note 里标翻唱/Remix 就按标的）、`ai_tool`（有 Suno clip 就算 Suno）
这三条都只看作品自身，两边各写一份必然会漂移 —— 之前 `qishui.ts` 手抄
`platforms.json` 就漂移过一次。
"""
from __future__ import annotations

import json
import re
from typing import Any

from core import db, paths
from core import platforms as P
from core import voxflow_client as vc

# 关联键两条都空的兜底提示 —— 见 core/db.py 的 link_clause
_NO_LINK = "缺关联键：clip_id 和 audio_sha256 都是空的"


# ── 艺人档案 ────────────────────────────────────────────
def artist_identity() -> dict[str, Any]:
    """艺名与各平台主页。真源 artist.json；读不到就空，**不编数据**。

    真实姓名（real_name）单独给，因为版权登记和收益结算按法律姓名走，艺名替不了。
    """
    empty: dict[str, Any] = {"stage_name": "", "real_name": "", "roles": [], "profiles": {}}
    try:
        data = json.loads(paths.ARTIST_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
    roles = data.get("roles") if isinstance(data.get("roles"), dict) else {}
    return {
        "stage_name": str(data.get("stage_name") or ""),
        "real_name": str(data.get("real_name") or ""),
        "roles": [str(v) for k, v in roles.items() if v],
        "profiles": [p for p in (data.get("platform_profiles") or [])
                     if isinstance(p, dict)],
    }


# ── 作品 ────────────────────────────────────────────────
def pending(platform: str = "qishui") -> list[vc.PendingTrack]:
    """该平台待发行的曲目。空列表是正常的，连不上会抛 VoxflowUnavailable。"""
    return vc.pending(platform)


def find_pending(track_id: str, platform: str = "qishui") -> vc.PendingTrack | None:
    """按 id 在待发行列表里找一首。

    注意范围：只覆盖**还没发过的**。已经发出去的曲子不在 `pending` 里
    （VoxFlow 那边按「该平台无 online/published/submitted 记录」过滤），
    要查已发行的记录看本仓的 `listings` 表。
    """
    for t in pending(platform):
        if t.id == track_id:
            return t
    return None


def claim(track: vc.PendingTrack) -> dict[str, Any]:
    """把一首待发行的歌登记成本仓的发行身份（releases 行），返回该行。

    幂等：同一个关联键重复调用返回同一行。所以备料流程可以放心重跑。
    发行名留空 —— 它在「锁定发行身份」那一步才定死，定早了容易和
    别的作品撞名（Suno 一次出两首同名是常态）。
    """
    _, kw = db.link_clause(track.raw)
    db.init()
    with db.connect() as c:
        existing = c.execute(
            "SELECT * FROM releases WHERE clip_id = ? AND audio_sha256 = ?",
            (kw["clip_id"], kw["audio_sha256"]),
        ).fetchone()
        if existing:
            return dict(existing)
        cur = c.execute(
            "INSERT INTO releases (clip_id, audio_sha256, source_track_id, source_hint) "
            "VALUES (?,?,?,?)",
            (kw["clip_id"], kw["audio_sha256"], kw["source_track_id"], kw["source_hint"]),
        )
        return dict(c.execute("SELECT * FROM releases WHERE id=?",
                              (cur.lastrowid,)).fetchone())


def get_release(clip_id: str = "", audio_sha256: str = "") -> dict[str, Any] | None:
    """按关联键取发行身份。两个键都不给就是「查全部」，别那么用。"""
    if not clip_id and not audio_sha256:
        return None
    db.init()
    with db.connect() as c:
        if clip_id:
            row = c.execute("SELECT * FROM releases WHERE clip_id=?", (clip_id,)).fetchone()
            if row:
                return dict(row)
        row = c.execute("SELECT * FROM releases WHERE audio_sha256=?",
                        (audio_sha256,)).fetchone()
        return dict(row) if row else None


def lock_release_title(rel_id: int, release_title: str,
                       platform: str = "") -> dict[str, Any]:
    """锁定发行名（发出去的歌名必须唯一）和独家平台。

    撞名时让 sqlite 的唯一索引报错，**不要在这里自动改名** —— 发行名是要
    出现在平台页面上的，自动加后缀等于替你做了个业务决定。
    重复执行安全：同一个 rel_id 反复锁同一个名不报错。
    """
    db.init()
    with db.connect() as c:
        cur = c.execute(
            "UPDATE releases SET release_title=?, "
            "locked_platforms=json_insert(locked_platforms,'$[#]',?), updated_at=? "
            "WHERE id=?",
            (release_title.strip(), platform, db._now(), rel_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"没有这条发行身份: {rel_id}")
        return dict(c.execute("SELECT * FROM releases WHERE id=?", (rel_id,)).fetchone())


# ── 备料检查 ────────────────────────────────────────────
def _cover_big_enough(cover_path: "str | None", min_size: str) -> bool:
    """封面短边够不够。**读不出尺寸时放行** —— 宁可让平台去判，
    也不要因为本地缺个 PIL 就把人卡在这一步。"""
    try:
        need = int(str(min_size).lower().split("x")[0])
    except (ValueError, IndexError, AttributeError):
        return True
    if not cover_path:
        return False
    try:
        from pathlib import Path as _P
        from PIL import Image
        p = _P(cover_path)
        if not p.is_absolute():
            p = paths.OUT_DIR / p
        with Image.open(p) as im:
            return min(im.size) >= need
    except Exception:  # noqa: BLE001 —— 读不出就不拦
        return True


def readiness(track: vc.PendingTrack, platform: str) -> dict[str, Any]:
    """这首歌发这个平台，备料齐了没有。

    返回 {ok, items: [{名称, 就绪, 说明}], 缺口数}。
    """
    spec = P.raw_platforms().get(platform) or {}
    artist = artist_identity()
    cover_min = (spec.get("cover") or {}).get("min_size", "待确认")

    def item(name: str, ok: bool, hint: str) -> dict[str, Any]:
        return {"名称": name, "就绪": bool(ok), "说明": "" if ok else hint}

    # 封面必须真的够大 —— Suno 自带的是 360×360，放大是糊的。
    # 「有封面」和「封面能用」是两件事，只判存在会在上传时被平台打回。
    cover_ok = bool(track.cover_name) and _cover_big_enough(track.cover_name, cover_min)
    items = [
        item("完整版音频", bool(track.audio_name),
             "没有音频文件 —— 先在 VoxFlow 生成或补录"),
        item(f"专辑封面 ≥{cover_min}", cover_ok,
             f"缺封面或尺寸不足 {cover_min}（Suno 自带的 360×360 不能用）"),
        # 歌词这一项**分两种情况，不能一刀切**。从 voxflow 搬过来时是
        # 「歌词为空 → 报缺口」，那对纯音乐是假缺口：纯音乐本来就没歌词。
        # 更坑的是汽水那边「是否是纯音乐」开关不设对时，页面会报
        # 「您上传的音频非纯音乐，请填写歌词」—— 那句话读起来像检测到人声，
        # 实际是开关没设。照着错误提示去贴歌词是白费功夫。
        (item("已标记为纯音乐（无歌词）", track.instrumental,
              "VoxFlow 那边把这首判成了有词作品 —— 先确认它到底有没有人声")
         if track.instrumental else
         item("歌词", bool(track.lyrics), "歌词为空 —— 平台必填")),
        item("歌曲标题", bool(track.title.strip()), "标题为空"),
        item("艺名（表演者/词曲作者）", bool(artist.get("stage_name")),
             "artist.json 里没有 stage_name"),
        item("法律姓名（版权登记用）", bool(artist.get("real_name")),
             "artist.json 里没有 real_name —— 结算要它，艺名不能替"),
        item("关联键（clip_id 或音频哈希）", track.has_link, _NO_LINK),
    ]
    missing = [i for i in items if not i["就绪"]]
    return {
        "ok": not missing,
        "items": items,
        "缺口数": len(missing),
        "平台": spec.get("label", platform),
        "控制台": (spec.get("entries", {}).get("single", {}) or {}).get("url")
                  or spec.get("console", ""),
        # 备料齐了给出那条真正能把表填完的命令。自动化的价值在填表这 10 分钟，
        # 最后点提交那一秒留给人 —— 提交进审核队列是不可逆的。
        "发布脚本": (paths.PROJECT_DIR / f"scripts/publish_{platform}.py").name
                    if (paths.PROJECT_DIR / f"scripts/publish_{platform}.py").exists() else "",
    }


# ── 上架台账 ────────────────────────────────────────────
def set_platform_status(rel: dict[str, Any], platform: str, status: str,
                        **extra: Any) -> dict[str, Any]:
    """记一条上架状态。同一首 + 同一平台只有一行（没有 song_id 就按首次建的行更新）。

    状态流转会同时写 publish_events —— 状态变更是**多条**的，只存最后一次的话
    「这首歌卡了几天」「驳回过几次」就答不了了。
    """
    db.init()
    with db.connect() as c:
        row = c.execute(
            "SELECT * FROM listings WHERE clip_id=? AND audio_sha256=? AND platform=?",
            (rel.get("clip_id", ""), rel.get("audio_sha256", ""), platform),
        ).fetchone()
        before = row["status"] if row else ""
        fields = {k: v for k, v in extra.items() if k != "note" and v is not None}
        if row:
            sets = ["status=?"] + [f"{k}=?" for k in fields] + ["updated_at=?"]
            c.execute(f"UPDATE listings SET {', '.join(sets)} WHERE id=?",  # noqa: S608
                      (status, *fields.values(), db._now(), row["id"]))
            lid = row["id"]
        else:
            cols = ["clip_id", "audio_sha256", "source_track_id", "source_hint",
                    "platform", "status", *fields, "updated_at"]
            vals = [rel.get("clip_id", ""), rel.get("audio_sha256", ""),
                    rel.get("source_track_id", ""), rel.get("source_hint", ""),
                    platform, status, *fields.values(), db._now()]
            cur = c.execute(
                f"INSERT INTO listings ({', '.join(cols)}) "  # noqa: S608
                f"VALUES ({', '.join('?' * len(cols))})",
                vals,
            )
            lid = cur.lastrowid
        _log_event(c, rel, platform, before, status, extra.get("note", ""))
        return dict(c.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone())


def listings_for(clip_id: str = "", audio_sha256: str = "",
                 platform: str = "") -> list[dict[str, Any]]:
    """查上架记录。给了哪个键就按哪个查，都不给就按平台列全部。"""
    db.init()
    where, params = [], []
    if clip_id:
        where.append("clip_id=?")
        params.append(clip_id)
    if audio_sha256:
        where.append("audio_sha256=?")
        params.append(audio_sha256)
    if platform:
        where.append("platform=?")
        params.append(platform)
    sql = "SELECT * FROM listings" + (f" WHERE {' AND '.join(where)}" if where else "")
    with db.connect() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def _log_event(c, rel: dict[str, Any], platform: str, before: str, after: str,
               note: str = "", actor: str = "") -> None:
    c.execute(
        "INSERT INTO publish_events (clip_id, audio_sha256, platform, from_status, "
        "to_status, actor, note, ts) VALUES (?,?,?,?,?,?,?,?)",
        (rel.get("clip_id", ""), rel.get("audio_sha256", ""), platform,
         before, after, actor, note, db._now()),
    )


def events(clip_id: str = "", audio_sha256: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """事件流，最新的在前。

    `ts` 是秒级精度 —— 备料流程一连写三四条状态时它们会同秒，
    只按 ts 排会并列（SQLite 不保证并列项的相对顺序，实测取回了正序）。
    所以加 id 兜底做稳定排序。
    """
    db.init()
    where, params = [], []
    if clip_id:
        where.append("clip_id=?")
        params.append(clip_id)
    if audio_sha256:
        where.append("audio_sha256=?")
        params.append(audio_sha256)
    sql = ("SELECT * FROM publish_events" + (f" WHERE {' AND '.join(where)}" if where else "")
           + " ORDER BY ts DESC, id DESC LIMIT ?")
    with db.connect() as c:
        return [dict(r) for r in c.execute(sql, (*params, limit)).fetchall()]


# ── 专辑 ────────────────────────────────────────────────
def album_key(platform: str, album_id: str) -> str:
    return f"{platform}-{album_id}"


def upsert_album(key: str, **fields: Any) -> dict[str, Any]:
    """写平台专辑。平台明说发行后不可增删，所以专辑一旦建了歌就定死了。"""
    db.init()
    with db.connect() as c:
        c.execute(
            "INSERT INTO albums (key, platform, album_id, title, track_count, "
            "publish_date, company, description, tags, cover_url, cover_local, url, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET title=excluded.title, "
            "track_count=excluded.track_count, publish_date=excluded.publish_date, "
            "company=excluded.company, description=excluded.description, "
            "tags=excluded.tags, cover_url=excluded.cover_url, "
            "cover_local=excluded.cover_local, url=excluded.url, synced_at=excluded.synced_at",
            (key, fields.get("platform", key.split("-", 1)[0]),
             fields.get("album_id", key.split("-", 1)[-1]), fields.get("title", ""),
             fields.get("track_count", 0), fields.get("publish_date", ""),
             fields.get("company", ""), fields.get("description", ""),
             fields.get("tags", ""), fields.get("cover_url", ""),
             fields.get("cover_local", ""), fields.get("url", ""), db._now()),
        )
        return dict(c.execute("SELECT * FROM albums WHERE key=?", (key,)).fetchone())


def get_album(album_id: str, platform: str) -> dict[str, Any] | None:
    db.init()
    with db.connect() as c:
        row = c.execute("SELECT * FROM albums WHERE key=?",
                        (album_key(platform, album_id),)).fetchone()
        return dict(row) if row else None


def list_albums(platform: str = "") -> list[dict[str, Any]]:
    db.init()
    sql = "SELECT * FROM albums"
    params: tuple = ()
    if platform:
        sql += " WHERE platform=?"
        params = (platform,)
    sql += " ORDER BY publish_date DESC, title"
    with db.connect() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


# ── 平台账号 ────────────────────────────────────────────
def plat_label(platform: str) -> str:
    return (P.PLATFORMS.get(platform) or {}).get("label", platform)


def upsert_platform_account(platform: str, **fields: Any) -> dict[str, Any]:
    """写平台账号。**凭据不在这里** —— cookie/token 走系统钥匙串。"""
    db.init()
    with db.connect() as c:
        c.execute(
            "INSERT INTO platform_accounts (platform, label, artist_id, artist_name, "
            "alias, avatar_url, brief, artist_url, user_id, user_url, song_count, "
            "album_count, stats, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(platform) DO UPDATE SET label=excluded.label, "
            "artist_id=excluded.artist_id, artist_name=excluded.artist_name, "
            "alias=excluded.alias, avatar_url=excluded.avatar_url, brief=excluded.brief, "
            "artist_url=excluded.artist_url, user_id=excluded.user_id, "
            "user_url=excluded.user_url, song_count=excluded.song_count, "
            "album_count=excluded.album_count, stats=excluded.stats, synced_at=excluded.synced_at",
            (platform, fields.get("label", plat_label(platform)),
             fields.get("artist_id", ""), fields.get("artist_name", ""),
             json.dumps(fields.get("alias", []), ensure_ascii=False),
             fields.get("avatar_url", ""), fields.get("brief", ""),
             fields.get("artist_url", ""), fields.get("user_id", ""),
             fields.get("user_url", ""), fields.get("song_count", 0),
             fields.get("album_count", 0),
             json.dumps(fields.get("stats", {}), ensure_ascii=False), db._now()),
        )
        return dict(c.execute("SELECT * FROM platform_accounts WHERE platform=?",
                              (platform,)).fetchone())


def list_platform_accounts() -> dict[str, Any]:
    db.init()
    with db.connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM platform_accounts").fetchall()]
    out: dict[str, Any] = {}
    for r in rows:
        for key in ("alias", "stats"):
            try:
                r[key] = json.loads(r.get(key) or ("[]" if key == "alias" else "{}"))
            except json.JSONDecodeError:
                r[key] = [] if key == "alias" else {}
        out[r["platform"]] = r
    return out


def publication_board() -> dict[str, Any]:
    """发行看板：本仓的账号 + 已上架记录。按 platform 分组。"""
    accounts = list_platform_accounts()
    db.init()
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM listings ORDER BY updated_at DESC").fetchall()]
    by_platform: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_platform.setdefault(r["platform"], []).append(r)
    return {
        "accounts": [accounts[p] for p in sorted(accounts)],
        "platforms": {p: {"label": plat_label(p), "listings": by_platform[p]}
                      for p in by_platform},
        "counts": db.summary(),
    }
