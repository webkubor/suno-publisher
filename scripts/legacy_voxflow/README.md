# legacy_voxflow —— 从 VoxFlow 搬来的发行脚本（**待移植，现在跑不了**）

2026-10-10 发行拆分时，这 14 个脚本跟着发行一起从 VoxFlow 搬了过来。

## 为什么不能直接跑

它们的写法是「直连 voxflow.db，往 `track_platforms` / `albums` /
`platform_accounts` 里写」。那三张表在 VoxFlow 侧**已经删掉了**（数据迁到本仓
的 `listings` / `albums` / `platform_accounts`），所以现在跑必然报
`no such table`。

搬过来而不是就地删掉，是因为**它们承载的功能还是需要的** ——
尤其平台后台数据同步（播放量、收益），本仓的 `core/revenue.py` 和发行台的
收益页正等着这些数据。

## 要做的事

按功能分三类，移植时各走各的：

| 脚本 | 干什么 | 移植到哪 |
|---|---|---|
| `sync_netease.py` `sync_qq.py` `sync_qishui.py` | 从音乐人后台同步播放量/收益/上架状态 | 改写成写本仓 `listings`（键用 `clip_id`/`audio_sha256`，**不要用 track_id**） |
| `ncm_stats.py` `ncm_track_stats.py` | 网易云单曲统计抓取 | 同上 |
| `publish_qishui*.py` `auto_publish_*.py` `check_login.py` | 浏览器驱动填表 | 已被 `scripts/publish_qishui_ego.py`（ego-browser 版，本仓维护中）取代，移植时**先看它** |
| `pack_upload.py` `backup_ledger.py` `sync_r2_ledger.py` `sync_lyrics.py` | 打包上传 / 台账备份 / 歌词同步 | 按需，多半要重写键 |

## 移植时的提醒

- **① 关联键用 `clip_id` / `audio_sha256`，绝不用标题。** Suno 一次出两首同名歌，
  按标题匹配必然串行 —— VoxFlow 那边的 `scripts/sync_suno.py` 顶部写着这条教训。
- **② 别再从 VoxFlow 的库读。** 那边的表没了。曲目元数据走
  `GET /api/catalog/tracks`（见 `core/voxflow_client.py`）。
- **③ 平台知识读 `configs/platforms.json`**（本仓的，含 `revenue` 段），
  不要硬编码平台名和分成率。

移植完一个就删一个 —— 留着一堆跑不了的脚本，下次没人分得清哪个是活的。
