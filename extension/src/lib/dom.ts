/**
 * 页面原语 —— 只保留 page-agent 做不到或做不对的两件事
 * 用在：自定义 tool（src/platforms/*）与执行后校验
 *
 * page-agent 自带点击/输入/下拉/滚动，那些不用我们写。
 * 这里剩下的两条来自 voxflow `scripts/publish_qishui_ego.py`
 * 在汽水后台走完两次真实发行踩出来的实测结论。
 */

export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

/**
 * 页面上真正的错误只看红字。
 * 「请选择音乐类型」既是错误提示也是字段标签（灰色）——扫关键词会去追一个不存在的问题。
 * 判据是 computed color 落在红色区间。用于每步执行后把真实错误喂回 agent。
 */
export function redErrors(): string[] {
  const out = new Set<string>()
  for (const el of document.querySelectorAll<HTMLElement>('*')) {
    if (el.children.length) continue
    const text = (el.textContent || '').trim()
    if (!text || text.length >= 60) continue
    if (!/rgb\(2\d{2},\s*\d{1,2},\s*\d{1,2}\)/.test(getComputedStyle(el).color)) continue
    if (el.getBoundingClientRect().width <= 0) continue
    out.add(text)
  }
  return [...out]
}

/**
 * 把文件投递进 file input。
 *
 * LLM 操作不了本地文件系统，这一步只能由代码做 —— 这也是插件形态相对外部 CDP 驱动的
 * 实质优势：文件在页面内用 DataTransfer 直接投递，不过 CDP 管道。
 * 原脚本为此要把 browser-harness 的 socket 超时从 5 秒抬到 180 秒
 * （大文件必然 TimeoutError，报错长得像网络问题），这条坑在这里不存在。
 *
 * ⚠️ 投完不要读 `input.files` 判断成功 —— React 组件收下文件后会把它清空、
 * 转存进自己的 state。要看页面上有没有显示出文件名。
 */
export function setFile(input: HTMLInputElement, file: File): void {
  const dt = new DataTransfer()
  dt.items.add(file)
  input.files = dt.files
  input.dispatchEvent(new Event('input', { bubbles: true }))
  input.dispatchEvent(new Event('change', { bubbles: true }))
}

/** 页面正文里是否已经出现这个文件名（去掉扩展名匹配）——判断槽位占没占用它比 files 可信。 */
export function pageShowsFile(filename: string): boolean {
  return document.body.innerText.includes(filename.replace(/\.[^.]+$/, ''))
}
