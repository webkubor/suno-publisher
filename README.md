# Suno Publisher

AI 音乐发行台 —— 把生成好的歌送进音乐平台的发行表单，**填到只差最后一下，那一下留给人**。

从 [VoxFlow](https://github.com/webkubor/voxflow) 拆出来的独立仓。VoxFlow 收敛为纯 AI
音频生成工具（语音克隆 / 音色设计 / 播客 / BGM / Suno 音乐生成），发行这半截整体搬到这里，
**连界面一起**。

## 和 VoxFlow 的关系

单向依赖：本仓依赖 VoxFlow 提供生成产物，VoxFlow **不知道「发行」存在**。

```
┌─────────────────┐   HTTP    ┌──────────────────┐
│    VoxFlow      │ ◄──────── │  Suno Publisher  │
│  只管生成        │  取资产    │  只管发行        │
│                 │           │                  │
│ tracks / usage  │  clip_id  │ releases         │
│ events / 音色库  │  音频      │ listings         │
└─────────────────┘           │ albums           │
                              │ platform_accounts│
      Chrome 扩展 ──────────► │ publish_events   │
      （只认这一个端口）        └──────────────────┘
```

两个仓**不共用 sqlite 文件**。VoxFlow 的 `core/db.py` 写明它的设计前提是「本地单用户工具」，
两个独立发版的仓同时开一个库，`timeout=10` 的锁等待迟早炸 `database is locked`。

VoxFlow 作为本机长驻服务提供**生成产物**，本仓通过 HTTP 取，**不读它的数据目录**：

| 端点 | 用途 |
|---|---|
| `GET /api/catalog/tracks` | 有音频的作品 + 生成元数据（含跨库关联键） |
| `GET /api/catalog/{track_id}/asset/{kind}` | 音频 / 封面字节流（`kind ∈ audio\|cover`） |

默认 `http://127.0.0.1:8866`，用 `VOXFLOW_API` 环境变量覆盖。

### 「待发行」是本仓算的，不是 VoxFlow 说的

VoxFlow 只回答「生成了什么」，**响应里不出现 platform / release_title / already_released**。
「这首在汽水还没发过」由本仓拿曲库减去自己 `listings` 里的记录算出来
（`core/catalog.py: pending()`）—— 发行状态的**唯一真源**在本仓。

## 已经有什么

- **发行服务**（`web/app.py`，默认 8867）：发行侧的唯一入口，Chrome 扩展和发行台都打它。
- **发行台**（`web/ui/index.html`）：待发行 / 待认领 / 已上架 / 平台账号 / 状态流转，无构建步骤。
- **台账**（`core/db.py`）：releases / listings / albums / platform_accounts / publish_events 五张表。
- **Chrome 扩展**（`extension/`）：填表自动化，含不可逆动作护栏。

## 怎么关联回 VoxFlow 的作品

跨库没有外键，关联键按可靠性排序，**绝不用标题**（Suno 一次出两首同名歌，按标题匹配必然串行）：

1. `clip_id` —— Suno 生成的作品有，天然唯一
2. `audio_sha256` —— 本地 TTS 合成的没有 clip_id，用音频内容哈希兜底
3. `source_track_id` —— VoxFlow 的 `tracks.id`，**仅作展示提示不是权威键**（UUID 会变）
4. `source_hint` —— 标题快照，纯给人看

两个键同时为空时 `core/db.py:link_clause()` **当场报错**，不静默写库。

**关联不上的历史记录照搬不丢**，标 `link_state='unlinked'` 待人工认领
（它们带着真实的 song_id / 上线日期 / 播放量 / 收益，不是垃圾数据）：

```sql
SELECT * FROM listings WHERE link_state = 'unlinked';
```

## 平台知识是个人资产，不进仓库

`configs/platforms.json` 含签约模式、账号入驻名额、收益区间这些商业状态，
**只保存在本机** `~/.suno-publisher/configs/platforms.json`。

仓库里的是脱敏示例 `configs/platforms.example.json`（结构完整，商业状态全抹，`verified_at`
一律 `null` 提醒你「这份没实测过」）。加载顺序：

```
用户目录 platforms.json  →  仓库示例 platforms.example.json
```

没有第二级的话，全新 clone 的人拿到的是空 dict，发行页面**静默退化成空白且不报错**。

```bash
cp configs/platforms.example.json ~/.suno-publisher/configs/platforms.json
# 然后把自己的平台信息填进去
```

`schemas/platforms.schema.json` 用来验结构 —— 但它只管字段在不在、类型对不对，
**不管平台页面是否还是这样**。后者只有人能做，靠每个平台的 `verified_at` 记录实测时间。

## 安全边界

不可逆动作一律停手：

- **提交审核 / 同意并签署** —— 提交后进审核队列，撤回要走流程；签的是 3 年独家代理合同，
  后面还有手机验证码，必须本人。自动化到「填表完成」为止不是能力问题。
- 扩展里靠 `transformPageContent` 把这些词从送进 LLM 的页面内容里替换掉 ——
  **看不见就点不了**，比在 prompt 里写「不要点」硬。
- `experimentalScriptExecutionTool` 不开：它是 page-agent 里唯一用 `eval` 的路径，
  既受页面 CSP 限制，也会绕过上面的护栏。

## 跑起来

```bash
python3 -m venv .venv && .venv/bin/pip install -e .

# 1) 起发行服务（发行侧唯一入口，扩展和发行台都打它）
.venv/bin/python -m uvicorn web.app:app --port 8867
#    然后打开 http://127.0.0.1:8867 就是发行台

# 2) 自检
.venv/bin/python tests/test_db.py        # 台账（不需 VoxFlow）
.venv/bin/python tests/test_contract.py  # 跨仓契约（VoxFlow 没起会跳过）
.venv/bin/python tests/test_web.py       # 发行服务契约
.venv/bin/python tests/test_browser.py   # 浏览器驱动的解析逻辑（不需真浏览器）
```

前置：VoxFlow 在跑（`cd ~/dev/video/voxflow && .venv/bin/voxflow web`）、
浏览器已登录音乐平台（用日常那个浏览器，登录态天然可用）。

发行台的「待发行」要 VoxFlow 在场才能算；VoxFlow 没起时**台账类页面照常可看**，
待发行那一栏会明确说「连不上 VoxFlow」而不是显示成空列表。

## 收益数据怎么刷新

后台数据**没有公开接口**，只能登录后从渲染页面里抠。两个脚本，**按顺序跑**：

```bash
# ① 账号级：播放量 / 粉丝 / 可提现收益 —— 实测千播单价的唯一来源
.venv/bin/python scripts/sync_netease_account.py

# ② 单曲级：近 30 日播放量（收益 = 播放量 × ①算出的单价）
.venv/bin/python scripts/sync_netease_stats.py
```

先跑 ①：单曲收益要拿「可提现 ÷ 累计播放」得到的**实测单价**去折算，
①没跑过时 ②只能回填播放量、折不出收益。

两个都支持 `--dry-run`。驱动走 `core/browser.py`（lite-browser），
**登录态要人手动登一次** —— 脚本不代持，也不会因为「没登记过」就误报「没登录」。
没登录时它们干净退出（码 3）并说清怎么做，不给堆栈。

> 其余 12 个同步/发布脚本还在 `scripts/legacy_voxflow/`，**待移植** ——
> 它们写的是 VoxFlow 已删的表。见那个目录的 README：按类写清了各自该移植到哪。

## License

Apache-2.0
