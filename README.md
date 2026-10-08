# Suno Publisher

音乐发行工具 —— 把 AI 生成的歌填进音乐平台的发行表单，**填到只差最后一下，那一下留给人**。

从 [VoxFlow](https://github.com/webkubor/voxflow) 拆出来的独立仓。VoxFlow 收敛为纯 AI
音频生成工具（语音克隆 / 音色设计 / 播客 / BGM / Suno 音乐生成），发行这半截整体搬到这里。

## 和 VoxFlow 的关系

```
┌─────────────────┐   HTTP    ┌──────────────────┐
│    VoxFlow      │ ────────► │  Suno Publisher  │
│  只管生成        │  取资产    │  只管发行        │
│                 │           │                  │
│ tracks / usage  │  clip_id  │ releases         │
│ events / 音色库  │  音频      │ listings         │
└─────────────────┘           │ albums           │
                              │ platform_accounts│
                              │ publish_events   │
                              └──────────────────┘
```

两个仓**不共用 sqlite 文件**。VoxFlow 的 `core/db.py` 写明它的设计前提是「本地单用户工具」，
两个独立发版的仓同时开一个库，`timeout=10` 的锁等待迟早炸 `database is locked`。

VoxFlow 作为本机长驻服务提供发行资产，本仓通过 HTTP 取，**不读它的数据目录**：

| 端点 | 用途 |
|---|---|
| `GET /api/release/pending?platform=<p>` | 该平台待发行的曲目 + 填表需要的全部元数据 |
| `GET /api/release/{track_id}/asset/{kind}` | 音频 / 封面字节流（`kind ∈ audio\|cover`） |

默认 `http://127.0.0.1:8866`，用 `VOXFLOW_API` 环境变量覆盖。

## 怎么关联回 VoxFlow 的作品

跨库没有外键，关联键按可靠性排序，**绝不用标题**（Suno 一次出两首同名歌，按标题匹配必然串行）：

1. `clip_id` —— Suno 生成的作品有，天然唯一
2. `audio_sha256` —— 本地 TTS 合成的没有 clip_id，用音频内容哈希兜底
3. `source_track_id` —— VoxFlow 的 `tracks.id`，**仅作展示提示不是权威键**（UUID 会变）
4. `source_hint` —— 标题快照，纯给人看

两个键同时为空时 `core/db.py:link_clause()` **当场报错**，不静默写库 —— 孤儿记录在
「查这首歌发到哪了」时表现为「查无此曲」，极难排查。

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
.venv/bin/python tests/test_db.py     # 自检
```

前置：VoxFlow 在跑、浏览器已登录音乐平台（用日常那个浏览器，登录态天然可用）。

## License

Apache-2.0
