# legacy_voxflow —— 从 VoxFlow 搬来的发行脚本（**待移植，现在跑不了**）

2026-10-10 发行拆分时，这 14 个脚本跟着发行一起从 VoxFlow 搬了过来。

## 移植进度

| 原脚本 | 现在 | 状态 |
|---|---|---|
| `ncm_stats.py` | `../sync_netease_account.py` | ✅ **已移植**（账号级：播放/粉丝/可提现收益） |
| `ncm_track_stats.py` | `../sync_netease_stats.py` | ✅ **已移植**（单曲级：近 30 日播放量） |
| 其余 12 个 | —— | ⬜ 待移植 |

**先跑账号级、再跑单曲级**：单曲收益 = 单曲播放量 × 实测千播单价，而那个单价
是账号级脚本算出来的（可提现 ÷ 累计播放）。账号级没跑过时，单曲侧只能回填
播放量、折不出收益。

## 为什么不能直接跑

它们的写法是「直连 voxflow.db，往 `track_platforms` / `albums` /
`platform_accounts` 里写」。那三张表在 VoxFlow 侧**已经删掉了**（数据迁到本仓
的 `listings` / `albums` / `platform_accounts`），所以现在跑必然报
`no such table`。

搬过来而不是就地删掉，是因为**它们承载的功能还是需要的** ——
尤其平台后台数据同步（播放量、收益），本仓的 `core/revenue.py` 和发行台的
收益页正等着这些数据。

## 剩下的 12 个要做的事

| 脚本 | 干什么 | 移植到哪 |
|---|---|---|
| `sync_qq.py` `sync_qishui.py` | 从音乐人后台同步播放量/收益/上架状态 | 照 `sync_netease_stats.py` 的样子写（同样用 `core/browser.py`） |
| `publish_qishui*.py` `auto_publish_*.py` `check_login.py` | 浏览器驱动填表 | ⚠️ **先解决驱动**：它们依赖 `ego-browser`，而本机 `find_exe("ego-browser")` 返回空 —— 也就是说这些脚本**在搬过来之前就已经跑不了**。要么按 `core/browser.py` 重写，要么换回 ego-browser |
| `pack_upload.py` `backup_ledger.py` `sync_r2_ledger.py` `sync_lyrics.py` | 打包上传 / 台账备份 / 歌词同步 | 按需，多半要重写键 |

## 移植时的提醒

- **① 关联键用 `clip_id` / `audio_sha256`，绝不用标题。** Suno 一次出两首同名歌，
  按标题匹配必然串行 —— VoxFlow 那边的 `scripts/sync_suno.py` 顶部写着这条教训。
  （已移植的两个脚本是「先按 platform_title 找到行，再要求有真实关联键」，
  两道都要过；同名多行时宁可都不写。）
- **② 别再从 VoxFlow 的库读。** 那边的表没了。曲目元数据走
  `GET /api/catalog/tracks`（见 `core/voxflow_client.py`）。
- **③ 平台知识读 `configs/platforms.json`**（本仓的，含 `revenue` 段），
  不要硬编码平台名和分成率。
- **④ 驱动统一走 `core/browser.py`**（lite-browser）。别引 playwright：
  登录态在 lite-browser 那个浏览器的 profile 里，另起进程等于什么都抓不到。

移植完一个就删一个 —— 留着一堆跑不了的脚本，下次没人分得清哪个是活的。
