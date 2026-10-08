#!/usr/bin/env python3
"""
汽水音乐专辑发行（ego-browser 版）—— 整张专辑一次填完，停在签署协议前。

    .venv/bin/python scripts/publish_qishui_ego.py <album_id> [--dry-run]

从 voxflow 搬过来的。原版直接读 voxflow 的 DATA_DIR 取音频，本版改成按
`source_track_id` 走 HTTP 从 VoxFlow 端点取（`core/voxflow_client.fetch_asset`）——
曲库不在本仓了。JS 载荷原样保留：它里面是踩了坑的页面操作规则，见下面五条。

## 五条踩出来的规则（改这个脚本前先读）

1. **`input.files` 在 React 表单里不可信**。组件接收文件后会把 files 清空、
   转存到自己的 state，所以「哪个槽还空着」不能靠它判断 ——
   要按页面上有没有显示文件名，或者直接看红字。
2. **缺什么用红字定位，不要用关键词扫文案**。「请选择音乐类型」既是错误提示
   也是**字段标签**。用 `/(请选择|请填写|必填)/` 扫正文会把标签当成缺失项，
   追一个不存在的问题。判断依据是 `getComputedStyle(el).color` 是不是红色。
3. **React 受控组件不认 `el.click()`**。在 `page.evaluate` 里调原生 click，
   状态不会变（AI 声明就是这么静默失败的）。必须 `page.click(selector)` 发真实鼠标事件。
4. **中文整段 `insertText`，不要逐字符键入**。逐字输入经常只落下第一个字
   （实测歌名变成「竹」），页面红字报错还看不出原因。
5. **WAV 先转 320k MP3**。汽水解析大 WAV 极慢，会在解析到半截时同时报
   「音频无效」和「非纯音乐请填歌词」两条错——后一条尤其误导。**转之前先确认文件存在**：
   路径拼个 `.mp3` 后缀但文件没转过，平台同样报「音频无效」。
   （本版这条由 VoxFlow 端点负责：`/api/release/{id}/asset/audio` 返回的已经是
   320k MP3，见 voxflow 的 core/release.py。）

## 停在哪

停在第 3 步「签署协议」。那是嵌 letsign.com 的电子签名，签的是 3 年独家代理合同，
后面还有手机验证码 —— 本人验证环节在那里，所以填表可以全自动，签名交给人。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import catalog as C  # noqa: E402
from core.exe import find_exe  # noqa: E402
from core import voxflow_client as vc  # noqa: E402
from core.paths import OUT_DIR  # noqa: E402

DRY = "--dry-run" in sys.argv

_JS = r"""
const D = __PAYLOAD__;
const log = (...a) => console.log("VF:", ...a);

const spaces = await listTaskSpaces();
let task = null;
for (const s of spaces) {
  try {
    const t = s.ownership === "user" ? await takeOverTaskSpace(s.spaceId ?? s.id) : await taskSpace(s.spaceId ?? s.id);
    if ((await t.page("p1").url()).includes("music.douyin.com")) { task = t; break; }
  } catch (e) { /* 空间不可用 */ }
}
if (!task) task = await taskSpace("qishui publish");
const page = task.page("p1");

// 页面上真正的错误只看红字 —— 「请选择xx」同样出现在字段标签里（规则 2）
const reds = () => page.evaluate(() => [...new Set([...document.querySelectorAll('*')].filter(e => {
  if (e.children.length) return false;
  const x = (e.textContent || '').trim();
  return x && x.length < 60 && /rgb\(2[0-9]{2},\s*[0-9]{1,2},\s*[0-9]{1,2}\)/.test(getComputedStyle(e).color)
    && e.getBoundingClientRect().width > 0;
}).map(e => (e.textContent || '').trim()))]);

// React 受控组件要真实鼠标事件（规则 3）：先打标记，再用选择器点
const clickText = async (text, exact = true) => {
  const ok = await page.evaluate(({ t, ex }) => {
    const el = [...document.querySelectorAll('*')].find(e =>
      !e.children.length && (ex ? (e.textContent || '').trim() === t : (e.textContent || '').includes(t))
      && e.getBoundingClientRect().width > 0);
    if (!el) return false;
    let box = el;
    for (let i = 0; i < 3 && box.parentElement; i++) box = box.parentElement;
    box.setAttribute('data-vf-hit', '1');
    return true;
  }, { t: text, ex: exact });
  if (!ok) return false;
  try { await page.click("[data-vf-hit='1']"); return true; }
  catch (e) { return false; }
  finally { await page.evaluate(() => document.querySelector("[data-vf-hit='1']")?.removeAttribute("data-vf-hit")); }
};

// 中文整段插入（规则 4）
const fillBy = async (label, value, byPlaceholder = false) => {
  const ok = await page.evaluate(({ lb, ph }) => {
    for (const el of document.querySelectorAll("input[type=text], textarea")) {
      if (el.value) continue;
      if (ph) { if ((el.placeholder || "").includes(lb)) { el.setAttribute("data-vf-fill", "1"); return true; } continue; }
      let n = el;
      for (let i = 0; i < 6 && n; i++) {
        if ((n.textContent || "").includes(lb)) { el.setAttribute("data-vf-fill", "1"); return true; }
        n = n.parentElement;
      }
    }
    return false;
  }, { lb: label, ph: byPlaceholder });
  if (!ok) return false;
  try {
    await page.click("[data-vf-fill='1']");
    await page.keyboard.insertText(value);
    await page.waitForTimeout(500);
    return true;
  } finally {
    await page.evaluate(() => document.querySelector("[data-vf-fill='1']")?.removeAttribute("data-vf-fill"));
  }
};

// ── 进发布页 ──
await page.goto("https://music.douyin.com/console/publish");
await page.waitForLoadState();
await page.waitForTimeout(5000);
try { await page.click("text=发布全曲"); await page.waitForTimeout(7000); } catch (e) { log("已在表单里"); }

const who = await page.evaluate(() => {
  const t = [...document.querySelectorAll('input[type=text]')].map(e => e.value).filter(Boolean);
  return t.join(' | ').slice(0, 60);
});
log("表单里的既有身份字段:", who || "(空)");

// ── 逐首：加槽位 → 传音频 → 填名 → 选音乐类型 ──
for (let i = 0; i < D.tracks.length; i++) {
  const tr = D.tracks[i];
  if (i > 0) { await clickText("点击添加歌曲"); await page.waitForTimeout(4000); }

  // 空槽定位：input.files 不可信（规则 1），改用「这首的区块里还没有文件名」
  const slot = await page.evaluate((name) => {
    const shown = document.body.innerText;
    if (shown.includes(name.replace(/\.[^.]+$/, ''))) return 'already';
    const fi = [...document.querySelectorAll('input[type=file]')].filter(e => (e.accept || '').includes('audio'));
    for (const e of fi) {
      let n = e, full = false;
      for (let k = 0; k < 7 && n; k++) { if ((n.textContent || '').includes('完整版')) { full = true; break; } n = n.parentElement; }
      if (!full) continue;
      // 这个槽所在区块里已经有 [BGM]/文件名就跳过
      let blk = e;
      for (let k = 0; k < 6 && blk; k++) blk = blk.parentElement;
      if (blk && /\.(mp3|wav|m4a)\b/i.test(blk.textContent || '')) continue;
      e.setAttribute('data-vf-slot', '1');
      return 'marked';
    }
    return 'none';
  }, tr.audio.split('/').pop());
  if (slot === 'marked') {
    await page.setInputFiles("input[data-vf-slot='1']", [tr.audio]);
    await page.evaluate(() => document.querySelector("input[data-vf-slot='1']")?.removeAttribute("data-vf-slot"));
    log(`  [${i + 1}] 音频已投递，等平台解析…`);
    await page.waitForTimeout(Math.min(45000, Math.max(18000, tr.mb * 3000)));
  } else log(`  [${i + 1}] 音频槽:`, slot);

  log(`  [${i + 1}] 歌名「${tr.title}」:`, (await fillBy("歌曲名称", tr.title)) ? "✓" : "✗");
  await page.waitForTimeout(600);
}

// 音乐类型是卡片单选（原创 / 原创伴奏 / 翻唱 / Remix），不是下拉
const mt = await page.evaluate((type) => {
  const cards = [...document.querySelectorAll('.douyin-music-radio-addon-buttonRadio')]
    .filter(e => (e.textContent || '').trim().startsWith(type)
      && (type !== '原创' || !(e.textContent || '').includes('伴奏')));
  cards.forEach((e, i) => e.setAttribute('data-vf-mt', String(i)));
  return cards.length;
}, D.music_type);
for (let i = 0; i < mt; i++) {
  try { await page.click(`[data-vf-mt='${i}']`); await page.waitForTimeout(900); } catch (e) { /* 略 */ }
}
await page.evaluate(() => document.querySelectorAll('[data-vf-mt]').forEach(e => e.removeAttribute('data-vf-mt')));
log(`音乐类型「${D.music_type}」: 点了 ${mt} 处`);

// AI 声明 + AI 工具 —— 必须如实填，平台有官方选项，瞒报影响账号
if (D.ai_tool) {
  const aiOk = await page.evaluate(() => {
    const row = [...document.querySelectorAll('*')].find(e =>
      (e.textContent || '').includes('以下歌曲均使用AI创作') && (e.textContent || '').length < 200);
    if (!row) return false;
    const yes = [...row.querySelectorAll('[class*=radio i]')].find(e => (e.textContent || '').trim() === '是');
    if (!yes) return false;
    yes.setAttribute('data-vf-ai', '1');
    return true;
  });
  if (aiOk) { await page.click("[data-vf-ai='1']"); await page.waitForTimeout(1500);
    await page.evaluate(() => document.querySelector("[data-vf-ai='1']")?.removeAttribute("data-vf-ai")); }
  log("AI 创作声明「是」:", aiOk ? "✓" : "✗");

  const open = await page.evaluate(() => {
    const lab = [...document.querySelectorAll('*')].find(e => !e.children.length && (e.textContent || '').trim() === '使用的AI工具');
    if (!lab) return false;
    let row = lab.parentElement;
    for (let i = 0; i < 5 && row; i++) { if ((row.textContent || '').includes('请选择') || row.querySelector('input,[class*=select]')) break; row = row.parentElement; }
    const sel = row && row.querySelector('[class*=select],[class*=Select],input');
    if (!sel) return false;
    sel.click(); return true;
  });
  await page.waitForTimeout(2000);
  const picked = open ? await page.evaluate((tool) => {
    const opt = [...document.querySelectorAll('[class*=option],[class*=Option],[role=option],li')]
      .find(e => e.offsetParent && (e.innerText || '').trim() === tool);
    if (!opt) return false;
    opt.click(); return true;
  }, D.ai_tool) : false;
  log(`AI 工具「${D.ai_tool}」:`, picked ? "✓" : "✗");
}

// 是否已发行：新歌选「否」，选「是」会走另一套版权核验、卡审核
await page.evaluate((rel) => {
  const row = [...document.querySelectorAll('*')].find(e =>
    (e.textContent || '').includes('是否已发行') && (e.textContent || '').length < 80);
  if (!row) return;
  const t = rel ? '是' : '否';
  const btn = [...row.querySelectorAll('[class*=radio i]')].find(e => (e.textContent || '').trim() === t);
  btn?.setAttribute('data-vf-rel', '1');
}, D.already_released);
try { await page.click("[data-vf-rel='1']"); } catch (e) { /* 略 */ }
await page.evaluate(() => document.querySelector("[data-vf-rel='1']")?.removeAttribute("data-vf-rel"));

// ── 专辑信息 ──
log("专辑名称:", (await fillBy("专辑名称", D.album)) ? "✓" : "✗", `（${D.album}）`);
await page.waitForTimeout(600);
if (D.album_desc) log("专辑介绍:", (await fillBy("关于专辑的介绍", D.album_desc, true)) ? "✓" : "✗");

// 封面：上传后会弹「调整图片」，不点确定会挡住后面所有点击
const cv = await page.evaluate(() => {
  const fi = [...document.querySelectorAll('input[type=file]')].filter(e => (e.accept || '').includes('jpg'));
  if (!fi.length) return false;
  fi[0].setAttribute('data-vf-cover', '1');
  return true;
});
if (cv) {
  await page.setInputFiles("input[data-vf-cover='1']", [D.cover]);
  await page.evaluate(() => document.querySelector("input[data-vf-cover='1']")?.removeAttribute("data-vf-cover"));
  await page.waitForTimeout(5000);
  await clickText("确定");
  await page.waitForTimeout(2500);
  log("专辑封面: ✓（含裁剪确认）");
}

// ── 推进 ──
await page.waitForTimeout(1500);
let r = await reds();
if (r.length) { log("⚠️ 还有红字，没往下走:", JSON.stringify(r)); }
else {
  await page.click("text=下一步");
  await page.waitForTimeout(8000);
  r = await reds();
  if (r.length) log("⚠️ 第1步校验没过:", JSON.stringify(r));
  else {
    log("→ 第2步：授权作品");
    await clickText("独家代理发行（3年）");
    await page.waitForTimeout(2000);
    await page.click("text=下一步");
    await page.waitForTimeout(8000);
    log("→ 第3步：签署协议");
  }
}

log("");
log("表已填完，停在签署协议。接下来要你本人做：");
log("  1) 协议区滚到底（签署按钮旁的「滚到协议区」提示就是这个意思）");
log("  2) 点「签署」，输入手机验证码");
log("  3) 签完告诉我，我把台账改成 reviewing");
"""


def _collect(album: dict, want_audio: bool) -> tuple[list[dict], dict | None]:
    """备料：把专辑里每首的音频取到本地，攒成要喂给 JS 的 payload。

    音频走 HTTP 从 VoxFlow 取（那边已经转过 320k MP3），不再读它的数据目录。
    取不到就**当场报错返回 None**，不要跳过那首继续 —— 半张专辑填出去比
    完全不填糟糕得多。
    """
    tracks: list[dict] = []
    meta: dict | None = None
    for row in album["tracks"]:
        vox_id = row.get("source_track_id") or ""
        if not vox_id:
            print(f"  ✗ 曲目 {row.get('track_no') or '?'} 没有 source_track_id，"
                  f"认不回 VoxFlow 的作品")
            return [], None
        title = row.get("platform_title") or row.get("source_hint") or ""
        entry: dict = {"title": title, "track_id": vox_id, "song_id": row.get("song_id") or ""}
        if want_audio:
            try:
                audio = vc.fetch_asset(vox_id, "audio", OUT_DIR / "待上传")
            except (vc.VoxflowError, vc.VoxflowUnavailable) as e:
                print(f"  ✗ 《{title}》取音频失败：{e}")
                return [], None
            entry["audio"] = str(audio)
            entry["mb"] = round(audio.stat().st_size / 1048576, 2)
        tracks.append(entry)
    return tracks, meta


def _album_cover(album: dict) -> Path | None:
    """专辑封面。本地有就用本地的；否则从第一首作品向 VoxFlow 要。"""
    local = album.get("cover_local") or ""
    if local:
        p = Path(local)
        if not p.is_absolute():
            p = OUT_DIR / p
        if p.is_file():
            return p
    for row in album["tracks"]:
        vox_id = row.get("source_track_id") or ""
        if not vox_id:
            continue
        try:
            return vc.fetch_asset(vox_id, "cover", OUT_DIR / "待上传")
        except (vc.VoxflowError, vc.VoxflowUnavailable):
            continue
    return None


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("用法：publish_qishui_ego.py <album_id> [--dry-run]")
        return 2

    if not vc.ping():
        print("✗ 连不上 VoxFlow —— 发行资产在它那边。先起："
              "cd ~/dev/video/voxflow && .venv/bin/python -m uvicorn web.app:app --port 8866")
        return 1

    album = C.get_album_with_tracks(args[0], "qishui")
    if not album:
        print(f"✗ 本地台账里没有这张专辑：{args[0]}")
        return 1
    if not album["tracks"]:
        print("✗ 这张专辑还没有曲目（listings 表里没有 album_key 匹配的行）")
        return 1

    cover = _album_cover(album)
    tracks, _ = _collect(album, want_audio=not DRY)

    print(f"专辑：{album['title']}（{len(tracks)} 首）")
    for t in tracks:
        size = f"{t['mb']}MB  {Path(t['audio']).name}" if "audio" in t else "(dry-run 不取音频)"
        print(f"  · {t['title']}  {size}")
    print(f"  封面 {cover.name if cover else '(无)'}"
          f"  {'✓' if cover and cover.is_file() else '✗ 缺失'}")
    if not cover or not cover.is_file():
        print("✗ 专辑没有封面，先出一张")
        return 1
    if not tracks:
        return 1
    if DRY:
        print("（dry-run，没有打开浏览器）")
        return 0

    ego = find_exe("ego-browser")
    if not ego:
        print("✗ 找不到 ego-browser。它不在最小 PATH 里 —— 见 core/exe.py 的说明")
        return 1

    payload = {
        "album": album["title"], "album_desc": album.get("description") or "",
        "cover": str(cover), "tracks": tracks,
    }
    script = _JS.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    env = {**os.environ, "PATH": f"{Path.home()}/.local/bin:" + os.environ.get("PATH", "")}
    p = subprocess.run([ego, "nodejs"], input=script, capture_output=True,
                       text=True, timeout=1200, env=env)
    out = (p.stdout + "\n" + p.stderr).splitlines()
    for line in out:
        if line.startswith("VF:"):
            print(line[3:].rstrip())
    if not any(l.startswith("VF:") for l in out):
        print("✗ ego-browser 没回话：", (p.stderr or p.stdout)[-400:])
        return 1

    # 表填完了。状态记 `filled`（表已填完，等人点提交），**不是** submitted ——
    # 脚本停在提交之前，标成 submitted 就是撒谎；也不是 reviewing —— 那一步的
    # 依据是「人签了协议」，不是「脚本跑完了」。
    marked = 0
    for t in tracks:
        rel = C.get_release_by_voxflow_id(t.get("track_id", ""))
        if not rel:
            print(f"  · 《{t['title']}》本机没有发行身份记录，跳过状态回写")
            continue
        C.set_platform_status(rel, "qishui", "filled",
                              note="表已填完，停在签署协议前")
        marked += 1
    print(f"\n表已填完，{marked} 首已记为 filled。签完协议后告诉我，"
          f"我把这几张改成 reviewing。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
