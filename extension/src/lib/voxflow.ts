/**
 * VoxFlow 本地曲库客户端
 * API 来源：VoxFlow 的 FastAPI（默认 http://127.0.0.1:8000）
 *   GET /api/release/pending?platform=qishui   待发行曲目 + 元数据
 *   GET /api/release/{track_id}/asset/{kind}   音频或封面的字节流（kind: audio | cover）
 *
 * 资产只在本机 127.0.0.1 上流转，不出网 —— 这是 VoxFlow「音频不出本机」的延续。
 */

/** 一首待发行曲目的完整元数据，字段对齐 voxflow 的 tracks 表 */
export interface Track {
  id: string
  /** 发行用歌名（release_title 优先，退回 title）。发出去的歌名必须唯一 */
  title: string
  lyrics: string
  /** 空歌词或 [Instrumental] = 纯音乐，决定平台上那个「是否是纯音乐」开关 */
  instrumental: boolean
  tags: string
  album_desc: string
  duration: number
  /** 音频文件名（已由 voxflow 侧转成 320k mp3） */
  audio_name: string
  cover_name: string
  audio_mb: number
}

/** background 与 content script 之间的消息协议 */
export type Msg =
  | { kind: 'pending'; platform: string }
  | { kind: 'asset'; trackId: string; asset: 'audio' | 'cover' }

export interface AssetReply {
  ok: boolean
  /** base64 编码的文件内容。走 mp3 后一般 6MB 上下，sendMessage 扛得住 */
  b64?: string
  mime?: string
  name?: string
  error?: string
}

/** base64 还原成 File，交给 setFile 投递 */
export function b64ToFile(b64: string, name: string, mime: string): File {
  const bin = atob(b64)
  const buf = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i)
  return new File([buf], name, { type: mime })
}
