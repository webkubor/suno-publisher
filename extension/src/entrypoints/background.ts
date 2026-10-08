/**
 * Service worker —— 唯一能访问 VoxFlow 本地 API 的地方
 *
 * 为什么不在 content script 里直接 fetch：MV3 起 content script 的跨域请求受 CORS 约束，
 * 不再继承扩展权限。background 有 host_permissions，不受此限。
 */
import type { Msg, AssetReply } from '@/lib/voxflow'

/**
 * VoxFlow 的本地服务地址。
 *
 * 端口从 voxflow-publisher 挪过来时改过一次：那边硬编码 8000，而 VoxFlow 的 web
 * 入口起在 8866（`uvicorn.run(app, host="0.0.0.0", port=8866)`）—— 8000 那个端口
 * 根本没人监听。也就是说原扩展在默认配置下**连不上**。
 *
 * 放进 chrome.storage 是为了改端口不必重新打包，popup 里可写。
 */
const DEFAULT_BASE = 'http://127.0.0.1:8866'

async function baseUrl(): Promise<string> {
  const got = await chrome.storage.local.get('voxflowBase')
  return (got.voxflowBase as string) || DEFAULT_BASE
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
          if (!r.ok) throw new Error(`VoxFlow ${r.status}`)
          sendResponse({ ok: true, tracks: await r.json() })
          return
        }
        const r = await fetch(`${base}/api/release/${msg.trackId}/asset/${msg.asset}`)
        if (!r.ok) throw new Error(`VoxFlow ${r.status}`)
        const reply: AssetReply = {
          ok: true,
          b64: await toB64(r),
          mime: r.headers.get('content-type') || 'application/octet-stream',
          name: filenameFrom(r.headers.get('content-disposition') || '', msg.asset),
        }
        sendResponse(reply)
      } catch (e) {
        // VoxFlow 没起是最常见的失败，错误要说人话 —— 尤其要把端口写出来，
        // 因为「连不上」最常见的原因就是端口不对
        const port = (() => { try { return new URL(base).port || '8866' } catch { return '8866' } })()
        const hint = String(e).includes('Failed to fetch')
          ? `连不上 VoxFlow（${base}）—— 先起服务：cd ~/dev/video/voxflow && .venv/bin/python -m uvicorn web.app:app --port ${port}`
          : String(e)
        sendResponse({ ok: false, error: hint })
      }
    })()
    return true // 异步 sendResponse
  })
})
