#!/usr/bin/env python3
"""汽水音乐发布 —— 把整张专辑填完，**停在签署协议前**。

    .venv/bin/python scripts/publish_qishui.py <album_id> [--dry-run]

从 `publish_qishui_ego.py` 移植（2026-10-11）：驱动从 `ego-browser nodejs`
换成 `core/browser.py`（lite-browser）—— 本机 `find_exe("ego-browser")` 返回空，
原版**在拆分之前就已经跑不了**。

⚠️ **本移植版尚未在真实发布页上验证过。** 驱动映射是逐条对照做的，但页面
自动化只有在真表单上跑才知道对不对。所以：
  · 默认 `--dry-run`：**不带 `--no-dry-run` 不会动页面**
  · 全程打印每一步的结果（成/败），失败不静默往下走
  · 停在「签署协议」—— 那是嵌 letsign.com 的电子签名，签的是 3 年独家代理
    合同，后面还有手机验证码，必须本人。

## 五条踩出来的规则（改这个脚本前先读）

1. **`input.files` 在 React 表单里不可信**。组件接收文件后会把 files 清空、
   转存到自己的 state，所以「哪个槽还空着」不能靠它判断 ——
   要按页面上有没有显示文件名，或者直接看红字。
2. **缺什么用红字定位，不要用关键词扫文案**。「请选择音乐类型」既是错误提示
   也是**字段标签**。用 `/(请选择|请填写|必填)/` 扫正文会把标签当成缺失项，
   追一个不存在的问题。判断依据是 `getComputedStyle(el).color` 是不是红色。
3. **React 受控组件不认 `el.click()`**。必须发真实鼠标事件（`browser.click`）。
4. **中文整段 `insertText`，不要逐字符键入**。逐字输入经常只落下第一个字
   （实测歌名变成「竹」），页面红字报错还看不出原因。
5. **WAV 先转 320k MP3**。汽水解析大 WAV 极慢，会在解析到半截时同时报
   「音频无效」和「非纯音乐请填歌词」两条错 —— 后一条尤其误导。
   （这条由 voxflow 的资产端点负责：`/api/catalog/{id}/asset/audio` 返回的
   已经是 320k MP3。）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser, catalog, paths  # noqa: E402
from core import voxflow_client as vc  # noqa: E402

DRY = "--no-dry-run" not in sys.argv
PUBLISH_URL = "https://music.douyin.com/console/publish"

# 页面上真正的错误只看红字（规则 2）
_REDS = r"""JSON.stringify([...new Set([...document.querySelectorAll('*')].filter(e => {
  if (e.children.length) return false;
  const x = (e.textContent || '').trim();
  if (!x || x.length >= 60) return false;
  const c = getComputedStyle(e).color;
  return /rgb\(2[0-9]{2},\s*[0-9]{1,2},\s*[0-9]{1,2}\)/.test(c)
    && e.getBoundingClientRect().width > 0;
}).map(e => (e.textContent || '').trim()))])"""


def reds() -> list[str]:
    """页面上的红字（真正的错误提示）。"""
    v = browser.evaluate(_REDS)
    return v if isinstance(v, list) else []


def _mark(tag: str, js_find: str) -> bool:
    """在页面里找到目标并打标记，返回是否找到。

    先打标记再用 CSS 选择器点，是为了让 `browser.click` 发真实鼠标事件
    （规则 3）—— React 受控组件不认原生 `el.click()`。
    """
    return bool(browser.evaluate(
        "JSON.stringify((() => { const el = %s; if (!el) return false;"
        " el.setAttribute('data-vf-%s','1'); return true; })())" % (js_find, tag)))


def _clear_mark(tag: str) -> None:
    browser.evaluate(f"document.querySelector(\"[data-vf-{tag}='1']\")"
                     f"?.removeAttribute('data-vf-{tag}')")


def click_text(text: str, exact: bool = True) -> bool:
    """按文本点击。找不到返回 False，**不抛**。

    先打标记再用 CSS 选择器点，是为了让 `browser.click` 发真实鼠标事件
    （规则 3）—— React 受控组件不认原生 `el.click()`。
    """
    cond = ("(e.textContent || '').trim() === %s" % repr(text) if exact
            else "(e.textContent || '').includes(%s)" % repr(text))
    js = ("[...document.querySelectorAll('*')].find(e => !e.children.length && "
          + cond + " && e.getBoundingClientRect().width > 0)")
    if not _mark("ct", js):
        return False
    try:
        return browser.click("[data-vf-ct='1']")
    finally:
        _clear_mark("ct")


def fill_by(label: str, value: str, by_placeholder: bool = False) -> bool:
    """按字段标签找一个空输入框，整段插入文本（规则 4）。

    跳过已有值的框 —— 表单里可能预填了身份字段，覆盖它会改掉账号信息。
    """
    js = (
        "(() => { for (const el of document.querySelectorAll("
        "\"input[type=text], textarea\")) { if (el.value) continue; "
        + ("if ((el.placeholder || '').includes(%s)) { return el; } continue; " % repr(label)
           if by_placeholder else
           "let n = el; for (let i = 0; i < 6 && n; i++) { "
           "if ((n.textContent || '').includes(%s)) return el; n = n.parentElement; } " % repr(label))
        + "} return null; })()")
    if not _mark("fill", js):
        return False
    try:
        browser.click("[data-vf-fill='1']")
        time.sleep(0.3)
        return browser.insert_text(value)
    finally:
        _clear_mark("fill")


def step(msg: str, ok: bool) -> bool:
    print(f"  {'✓' if ok else '✗'} {msg}")
    return ok


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1].startswith("--"):
        print("用法：publish_qishui.py <album_id> [--no-dry-run]")
        return 2
    album_id = sys.argv[1]

    album = catalog.get_album_with_tracks(album_id, "qishui")
    if not album:
        print(f"✗ 本仓没有这张专辑：{album_id}")
        print("  先 `catalog.list_albums('qishui')` 看看有哪些。")
        return 1

    tracks = _collect(album)
    print(f"专辑《{album.get('title')}》  {len(tracks)} 首")
    for t in tracks:
        print(f"    {t['title']}  {t['mb']:.1f}MB  {Path(t['audio']).name}")
    covers = [t["cover"] for t in tracks if t["cover"]]
    print(f"    封面 {len(covers)} 张（逐首取，走 VoxFlow 资产端点）")

    if DRY:
        print("\n（默认 dry-run，没有打开浏览器、没有动页面。）")
        print("  确认上面这些对，再加 --no-dry-run 真跑。")
        return 0

    browser.open_page(PUBLISH_URL)
    browser.require_logged_in("汽水音乐控制台")
    if not browser.wait_for("!!document.body", timeout=30, desc="发布页主体"):
        print("✗ 发布页 30 秒没出来")
        return 1
    time.sleep(5)
    if not click_text("发布全曲"):
        print("  （没有「发布全曲」，可能已经在表单里）")
    time.sleep(7)

    # ── 逐首：加槽位 → 传音频 → 填名 ──
    for i, tr in enumerate(tracks, 1):
        if i > 1:
            click_text("点击添加歌曲")
            time.sleep(4)
        slot = _find_empty_slot(tr["audio"])
        if slot == "marked":
            browser.upload("input[data-vf-slot='1']", tr["audio"])
            _clear_mark("slot")
            print(f"  [{i}] 音频已投递，等平台解析…")
            # 平台按体积花时间解析；给足但不无限等
            time.sleep(min(45, max(18, tr["mb"] * 3)))
        else:
            print(f"  [{i}] 音频槽：{slot}")
        step(f"[{i}] 歌名「{tr['title']}」",
             fill_by("歌曲名称", tr["title"]))
        time.sleep(0.6)

    # ── 音乐类型：卡片单选，不是下拉 ──
    mt = browser.evaluate(
        """JSON.stringify((() => {
            const type = %s;
            const cards = [...document.querySelectorAll('.douyin-music-radio-addon-buttonRadio')]
              .filter(e => (e.textContent || '').trim().startsWith(type)
                && (type !== '原创' || !(e.textContent || '').includes('伴奏')));
            cards.forEach((e, i) => e.setAttribute('data-vf-mt', String(i)));
            return cards.length;
        })())""" % repr(album.get("music_type") or "原创"))
    n = int(mt or 0)
    for i in range(n):
        browser.click(f"[data-vf-mt='{i}']")
        time.sleep(0.9)
    browser.evaluate("document.querySelectorAll('[data-vf-mt]')"
                     ".forEach(e => e.removeAttribute('data-vf-mt'))")
    step(f"音乐类型「{album.get('music_type') or '原创'}」 点了 {n} 处", n > 0)

    _ai_declaration(album)
    _album_fields(album, [x["cover"] for x in tracks if x["cover"]])

    # ── 推进 ──
    time.sleep(1.5)
    r = reds()
    if r:
        print(f"  ⚠ 还有红字，没往下走：{r}")
    else:
        click_text("下一步")
        time.sleep(8)
        r = reds()
        if r:
            print(f"  ⚠ 第 1 步校验没过：{r}")
        else:
            print("  → 第 2 步：授权作品")
            click_text("独家代理发行（3年）")
            time.sleep(2)
            click_text("下一步")
            time.sleep(8)
            print("  → 第 3 步：签署协议")

    print()
    print("表已填完，**停在签署协议**。接下来要你本人做：")
    print("  1) 协议区滚到底（签署按钮旁的「滚到协议区」提示就是这个意思）")
    print("  2) 点「签署」，输入手机验证码")
    print("  3) 签完把台账改成 reviewing")
    print()
    print("  那一步不做自动化是设计，不是能力问题：签的是 3 年独家代理合同。")
    return 0


def _find_empty_slot(audio_path: str) -> str:
    """找一个还空着的「完整版」音频槽并打标记。

    **不能用 `input.files` 判断空不空**（规则 1）—— React 组件接收后会把
    files 清空、转存到自己的 state。所以按「这个槽所在区块里还没有文件名」判断。
    """
    name = Path(audio_path).stem
    js = ("""(() => {
        if (document.body.innerText.includes(%s)) return 'already';
        const fi = [...document.querySelectorAll('input[type=file]')]
          .filter(e => (e.accept || '').includes('audio'));
        for (const e of fi) {
          let n = e, full = false;
          for (let k = 0; k < 7 && n; k++) {
            if ((n.textContent || '').includes('完整版')) { full = true; break; }
            n = n.parentElement;
          }
          if (!full) continue;
          let blk = e;
          for (let k = 0; k < 6 && blk; k++) blk = blk.parentElement;
          if (blk && /\\.(mp3|wav|m4a)\\b/i.test(blk.textContent || '')) continue;
          e.setAttribute('data-vf-slot', '1');
          return 'marked';
        }
        return 'none';
      })()""" % repr(name))
    return str(browser.evaluate(f"JSON.stringify({js})") or "none")


def _ai_declaration(album: dict) -> None:
    """AI 声明 + AI 工具 —— **必须如实填，平台有官方选项，瞒报影响账号**。"""
    tool = album.get("ai_tool") or "Suno"
    ok = _mark("ai", (
        "[...document.querySelectorAll('*')].find(e => "
        "(e.textContent || '').includes('以下歌曲均使用AI创作') && "
        "(e.textContent || '').length < 200)?.querySelector"
        "('[class*=radio i]')"))
    if ok:
        # 上面只找到了容器，这里再定位「是」那个选项
        ok = _mark("ai", (
            "(function(){const row=[...document.querySelectorAll('*')].find(e => "
            "(e.textContent||'').includes('以下歌曲均使用AI创作') && "
            "(e.textContent||'').length<200); if(!row) return null;"
            " return [...row.querySelectorAll('[class*=radio i]')]"
            ".find(e => (e.textContent||'').trim() === '是');})()"))
    if ok:
        browser.click("[data-vf-ai='1']")
        time.sleep(1.5)
        _clear_mark("ai")
    step("AI 创作声明「是」", ok)

    opened = _mark("tool", (
        "(function(){const lab=[...document.querySelectorAll('*')].find(e => "
        "!e.children.length && (e.textContent||'').trim() === '使用的AI工具');"
        " if(!lab) return null; let row=lab.parentElement;"
        " for(let i=0;i<5&&row;i++){ if((row.textContent||'').includes('请选择')"
        " || row.querySelector('input,[class*=select]')) break; row=row.parentElement;}"
        " return row && row.querySelector('[class*=select],[class*=Select],input');})()"))
    if opened:
        browser.click("[data-vf-tool='1']")
        time.sleep(2)
        _clear_mark("tool")
        picked = _mark("opt", (
            "[...document.querySelectorAll('[class*=option],[class*=Option],"
            "[role=option],li')].find(e => e.offsetParent && "
            "(e.innerText||'').trim() === %s)" % repr(tool)))
        if picked:
            browser.click("[data-vf-opt='1']")
            _clear_mark("opt")
        step(f"AI 工具「{tool}」", picked)
    else:
        step(f"AI 工具「{tool}」（没找到选择器）", False)


def _album_fields(album: dict, covers: list[str]) -> None:
    """专辑名 / 介绍 / 封面。"""
    step("专辑名称", fill_by("专辑名称", album.get("title") or ""))
    time.sleep(0.6)
    if album.get("description"):
        step("专辑介绍", fill_by("关于专辑的介绍", album["description"], True))

    # 用第一首的封面 —— 同一张专辑的封面是同一张图
    if not covers:
        step("封面（没取到 —— 曲目里没有封面文件？）", False)
        return
    cover = Path(covers[0])
    if not cover.is_file():
        step(f"封面（文件不在：{cover}）", False)
        return
    ok = _mark("cover", (
        "[...document.querySelectorAll('input[type=file]')]"
        ".find(e => (e.accept || '').includes('jpg'))"))
    if not ok:
        step("封面（没找到文件输入框）", False)
        return
    browser.upload("input[data-vf-cover='1']", cover)
    _clear_mark("cover")
    time.sleep(5)
    # 上传后会弹「调整图片」，**不点确定会挡住后面所有点击**
    click_text("确定")
    time.sleep(2.5)
    step("专辑封面（含裁剪确认）", True)


def _collect(album: dict) -> list[dict]:
    """备料：把每一步要用到的都先取到本地。

    浏览器驱动要**真实文件路径**（CDP 的文件选择器只认本地文件），
    所以音频和封面都得先落盘。

    ## ⚠️ 专辑里的「曲目」是 listings 行，不是 voxflow 曲目

    `get_album_with_tracks` 返回的每条带 `id`（那是 listings 主键，如 45）、
    `clip_id`、`source_track_id`。**不能拿 `id` 去取资产** —— 它会被当成
    voxflow 的 track_id，实测直接 `TypeError: quote_from_bytes() expected bytes`
    （进 URL 编码的不是字符串）。

    正确做法是按**关联键**回曲库找那一首：`clip_id` 优先、`audio_sha256` 兜底。
    `source_track_id` 只是提示不是权威键（UUID 重建库会变），不拿它当主路径。
    """
    try:
        catalogue = list(vc.catalog())
    except (vc.VoxflowUnavailable, vc.VoxflowError) as e:
        print(f"✗ 连不上 VoxFlow，取不到曲库：{e}")
        return []
    by_clip = {t.clip_id: t for t in catalogue if t.clip_id}
    by_hash = {t.audio_sha256: t for t in catalogue if t.audio_sha256}

    out = []
    for row in album.get("tracks") or []:
        cid, sha = row.get("clip_id") or "", row.get("audio_sha256") or ""
        hit = (by_clip.get(cid) if cid else None) or (by_hash.get(sha) if sha else None)
        title = row.get("platform_title") or row.get("source_hint") or ""
        if not hit:
            print(f"  ⚠ 《{title}》还没认回作品（无关联键）—— 先认领再发：")
            print("     SELECT * FROM listings WHERE link_state='unlinked'")
            continue
        try:
            audio = vc.fetch_asset(hit.id, "audio", dest_dir=paths.OUT_DIR / "待上传")
        except (vc.VoxflowUnavailable, vc.VoxflowError) as e:
            print(f"  ⚠ 《{title}》取音频失败 {str(e)[:60]}")
            continue
        item = {
            "title": title or hit.title,
            "audio": str(audio),
            "mb": audio.stat().st_size / 1048576,
            "cover": "",
        }
        if hit.cover_file:
            # ⚠️ 封面**不能**用 album["cover_local"] 拼本地路径：那是相对
            # **voxflow** 数据目录的相对路径（迁移时照搬过来的），
            # 在本仓拼出来必然指到一个不存在的文件。走 HTTP 取。
            try:
                cv = vc.fetch_asset(hit.id, "cover",
                                    dest_dir=paths.OUT_DIR / "待上传")
                item["cover"] = str(cv)
            except (vc.VoxflowUnavailable, vc.VoxflowError) as e:
                print(f"  ⚠ 《{title}》取封面失败 {str(e)[:50]}")
        out.append(item)
    return out


if __name__ == "__main__":
    try:
        sys.exit(main())
    except browser.NotLoggedIn as e:
        print(f"✗ {e}")
        sys.exit(3)
    except browser.BrowserUnavailable as e:
        print(f"✗ {e}")
        sys.exit(1)
    finally:
        if not DRY:
            browser.close()
