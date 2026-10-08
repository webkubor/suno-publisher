/**
 * popup —— 选曲、配模型、发起填表
 * API：向 background 要 /api/release/pending，向 content script 发 start
 *
 * API Key 只存 chrome.storage.local，且只在 content script 里用于调模型；不上传任何地方。
 */
import type { Track } from '@/lib/voxflow'

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T
const sel = $<HTMLSelectElement>('track')
const go = $<HTMLButtonElement>('go')
const tip = $<HTMLParagraphElement>('tip')
const fields = ['model', 'baseURL', 'apiKey'] as const

let tracks: Track[] = []

const DEFAULTS = { model: 'qwen3.5-plus', baseURL: 'https://dashscope.aliyuncs.com/compatible-mode/v1', apiKey: '' }

async function init() {
  const saved = { ...DEFAULTS, ...(await chrome.storage.local.get(fields as unknown as string[])) }
  for (const k of fields) $<HTMLInputElement>(k).value = String(saved[k] ?? '')

  const r = await chrome.runtime.sendMessage({ kind: 'pending', platform: 'qishui' })
  if (!r?.ok) {
    sel.innerHTML = '<option>取不到曲目</option>'
    tip.className = 'tip err'
    tip.textContent = r?.error || '连不上 VoxFlow'
    return
  }
  tracks = r.tracks
  if (!tracks.length) { sel.innerHTML = '<option>没有待发行的曲目</option>'; return }
  sel.innerHTML = tracks.map((t, i) => `<option value="${i}">${t.title}</option>`).join('')
  go.disabled = false
}

go.addEventListener('click', async () => {
  const llm = Object.fromEntries(fields.map((k) => [k, $<HTMLInputElement>(k).value.trim()]))
  if (!llm.apiKey) { tip.className = 'tip err'; tip.textContent = '先填 API Key'; return }
  await chrome.storage.local.set(llm)

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true })
  if (!tab?.id || !tab.url?.includes('music.douyin.com')) {
    tip.className = 'tip err'
    tip.textContent = '先打开汽水音乐的发布页再来'
    return
  }
  await chrome.tabs.sendMessage(tab.id, { kind: 'start', track: tracks[Number(sel.value)], llm })
  window.close()
})

void init()
