/**
 * Service worker —— 唯一能访问本机发行服务的地方
 *
 * 为什么不在 content script 里直接 fetch：MV3 起 content script 的跨域请求受 CORS 约束，
 * 不再继承扩展权限。background 有 host_permissions，不受此限。
 */
import type { Msg, AssetReply } from '@/lib/publisher'

/**
 * suno-publisher 发行服务的地址。
 *
 * 2026-10-10 改指向：原来打的是 VoxFlow 的 8866，那时发行端点还长在 voxflow 上。
 * 拆仓之后发行归本仓，voxflow 不再暴露 `/api/release/*` —— 继续打 8866 会在
 * voxflow 删掉那些端点后变成 404，而症状是「填表取不到音频」而不是「配置过期」。
 *
 * 本仓的发行服务默认 8867，它是发行侧的**唯一**入口：扩展只认这一个端口，
 * 音频由它向 voxflow 转取（voxflow 仍在 8866 提供生成产物）。
 *
 * 放进 chrome.storage 是为了改地址不必重新打包，popup 里可写。
 */
const DEFAULT_BASE = 'http://127.0.0.1:8867'

/** 旧版本存的是 `voxflowBase`（那时地址指向 voxflow）。老用户升级后配置还在，
 *  直接换 key 会让他们手里改过的地址**静默失效**、退回默认值 —— 所以两个都读。 */
const STORAGE_KEY = 'publisherBase'
const LEGACY_KEY = 'voxflowBase'

async function baseUrl(): Promise<string> {
  const got = await chrome.storage.local.get([STORAGE_KEY, LEGACY_KEY])
  return (got[STORAGE_KEY] as string) || (got[LEGACY_KEY] as string) || DEFAULT_BASE
}

/**
 * 从 Content-Disposition 取文件名。
 *
 * 平台靠文件名判断有没有真的收下文件 —— 不能读 `input.files`，React 组件接收后
 * 会把 files 清空转存到自己的 state。拿不到文件名等于没法确认上传成功。
 *
 * `filename*` 是 RFC 5987（`UTF-8''%E5%A4%9C%E8%88%AA.mp3`），中文名走它，
 * 必须 decodeURIComponent —— 不解的话平台收到的是一堆百分号。
 */
function filenameFrom(disposition: string, fallback: string): string {
  for (const part of disposition.split(';')) {
    const p = part.trim()
    const low = p.toLowerCase()
    if (low.startsWith('filename*=')) {
      let v = p.slice('filename*='.length).trim().replace(/^"|"$/g, '')
      const i = v.indexOf("''")
      if (i >= 0) v = v.slice(i + 2)
      try { return decodeURIComponent(v) } catch { return v }
    }
    if (low.startsWith('filename=')) {
      return p.slice('filename='.length).trim().replace(/^"|"$/g, '')
    }
  }
  return fallback
}

async function toB64(res: Response): Promise<string> {
  const buf = new Uint8Array(await res.arrayBuffer())
  let s = ''
  // 分块拼接：一次 apply 整个大数组会爆栈
  for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000))
  return btoa(s)
}

export default defineBackground(() => {
  chrome.runtime.onMessage.addListener((msg: Msg, _sender, sendResponse) => {
    ;(async () => {
      const base = await baseUrl()
      try {
        if (msg.kind === 'pending') {
          const r = await fetch(`${base}/api/release/pending?platform=${encodeURIComponent(msg.platform)}`)
          if (!r.ok) throw new Error(`发行服务 ${r.status}`)
          sendResponse({ ok: true, tracks: await r.json() })
          return
        }
        const r = await fetch(`${base}/api/release/${msg.trackId}/asset/${msg.asset}`)
        if (!r.ok) throw new Error(`发行服务 ${r.status}`)
        const reply: AssetReply = {
          ok: true,
          b64: await toB64(r),
          mime: r.headers.get('content-type') || 'application/octet-stream',
          name: filenameFrom(r.headers.get('content-disposition') || '', msg.asset),
        }
        sendResponse(reply)
      } catch (e) {
        // 服务没起是最常见的失败，错误要说人话 —— 尤其要把地址写出来，
        // 因为「连不上」最常见的原因就是端口不对
        const port = (() => { try { return new URL(base).port || '8867' } catch { return '8867' } })()
        const hint = String(e).includes('Failed to fetch')
          ? `连不上发行服务（${base}）—— 先起它：cd ~/dev/video/suno-publisher && .venv/bin/python -m uvicorn web.app:app --port ${port}`
          : String(e)
        sendResponse({ ok: false, error: hint })
      }
    })()
    return true // 异步 sendResponse
  })
})
