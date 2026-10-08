/**
 * 汽水音乐发行 content script
 * 页面：https://music.douyin.com/console/*
 *
 * 装配 page-agent，并补上它做不到 / 做不对的三件事：
 *   ① 领域知识  instructions       —— LLM 看 DOM 推不出平台的坑
 *   ② 文件投递  upload_track_asset —— LLM 碰不到本地文件系统
 *   ③ 不可逆护栏 transformPageContent + ask_user
 */
import { PageAgent, tool } from 'page-agent'
import { z } from 'zod/v4'
import { setFile, pageShowsFile, redErrors, sleep } from '@/lib/dom'
import { b64ToFile, type AssetReply, type Track } from '@/lib/voxflow'
import { SYSTEM, pageInstructions, maskIrreversible } from '@/platforms/qishui'

export default defineContentScript({
  matches: ['https://music.douyin.com/*'],
  async main() {
    // 由 popup 发起；页面加载时不自动跑——发行是有后果的动作，得有人按下开始
    chrome.runtime.onMessage.addListener((msg: { kind: string; track?: Track; llm?: LlmCfg }) => {
      if (msg.kind === 'start' && msg.track && msg.llm) void run(msg.track, msg.llm)
    })
  },
})

interface LlmCfg { model: string; baseURL: string; apiKey: string }

/** 向 background 要一个本地资产，还原成 File */
async function fetchAsset(trackId: string, asset: 'audio' | 'cover'): Promise<File> {
  const r: AssetReply = await chrome.runtime.sendMessage({ kind: 'asset', trackId, asset })
  if (!r.ok || !r.b64) throw new Error(r.error || '取资产失败')
  return b64ToFile(r.b64, r.name || asset, r.mime || 'application/octet-stream')
}

/** 找到该放这个资产的 file input：音频认「完整版」区块，封面认 accept=image */
function findSlot(asset: 'audio' | 'cover'): HTMLInputElement | null {
  const inputs = [...document.querySelectorAll<HTMLInputElement>('input[type=file]')]
  if (asset === 'cover') return inputs.find((e) => (e.accept || '').includes('image')) || null
  for (const el of inputs) {
    if (!(el.accept || '').includes('audio')) continue
    let n: HTMLElement | null = el
    for (let k = 0; k < 7 && n; k++) {
      if ((n.textContent || '').includes('完整版')) return el
      n = n.parentElement
    }
  }
  return inputs.find((e) => (e.accept || '').includes('audio')) || null
}

async function run(track: Track, llm: LlmCfg) {
  const agent = new PageAgent({
    ...llm,
    language: 'zh-CN',
    maxSteps: 60,
    instructions: {
      system: SYSTEM,
      getPageInstructions: (url) => pageInstructions(url, track),
    },

    // ③ 不可逆动作从 LLM 视野里抹掉
    transformPageContent: (content) => maskIrreversible(content),

    customTools: {
      // ② LLM 摸不到本地文件，这一步只能由代码做
      upload_track_asset: tool({
        description:
          '把本机 VoxFlow 曲库里的音频或封面投递进页面的上传框。页面上的文件选择对话框你点不开，传文件一律用这个工具。',
        inputSchema: z.object({
          asset: z.enum(['audio', 'cover']).describe('audio = 完整版音频，cover = 专辑封面'),
        }),
        execute: async (input) => {
          const slot = findSlot(input.asset)
          if (!slot) return `❌ 页面上找不到 ${input.asset} 的上传框，先确认已经进到有上传区的那一屏`
          const file = await fetchAsset(track.id, input.asset)
          setFile(slot, file)
          // 平台解析音频要时间，按体积给，别写死
          await sleep(input.asset === 'audio' ? Math.min(45000, Math.max(18000, track.audio_mb * 3000)) : 3000)
          const shown = pageShowsFile(file.name)
          return shown
            ? `✅ ${file.name} 已投递并显示在页面上${input.asset === 'cover' ? '。注意：接下来会弹「调整图片」裁剪框，必须点确定' : ''}`
            : `⚠️ ${file.name} 已投递但页面还没显示出文件名，可能仍在解析，也可能被平台拒了——去看字段下面的提示文字`
        },
      }),
    },

    // 每步之后把真实错误（只认红字）喂回去，省得它对着灰色的字段标签乱猜
    onAfterStep: (a) => {
      const errs = redErrors()
      // pushObservation 运行时有，但被 d.ts 标成 "Excluded from this release type"
      if (errs.length) (a as unknown as { pushObservation?: (s: string) => void }).pushObservation?.(`页面上的红字错误：${errs.join(' / ')}`)
    },
  })

  // 需要人拍板时问人，而不是自己猜
  agent.onAskUser = async (q) => window.prompt(`VoxFlow 发行助手需要你确认：\n\n${q}`) || ''

  const result = await agent.execute(
    `把《${track.title}》填进汽水音乐的发行表单。一路填到「签署协议」那一步之前为止，不要提交。`,
  )
  console.log('[suno-publisher]', result.success ? '填表完成，等人检查' : '中断', result.data)
}
