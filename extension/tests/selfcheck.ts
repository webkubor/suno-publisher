/**
 * 自检 —— 只测「坏了会静默出事」的那两处纯逻辑
 *   跑法：npm test（node --experimental-strip-types，不引测试框架）
 *
 * 不测 DOM 原语：那些必须在真实页面上验，装上插件跑一次比任何 mock 都准。
 */
import assert from 'node:assert/strict'
import { maskIrreversible, pageInstructions } from '../src/platforms/qishui.ts'
import { b64ToFile } from '../src/lib/voxflow.ts'

const track = {
  id: 't1', title: '快乐的社恐', lyrics: '', instrumental: true, tags: '',
  album_desc: '', duration: 51, audio_name: 'a.m4a', cover_name: '', audio_mb: 0.9,
}

// ① 护栏：不可逆按钮必须从喂给 LLM 的页面内容里消失
{
  const page = '保存草稿 | 下一步 | 提交审核 | 同意并签署'
  const masked = maskIrreversible(page)
  for (const forbidden of ['提交审核', '同意并签署']) {
    assert.ok(!masked.includes(forbidden), `护栏漏了「${forbidden}」——LLM 看得见就点得到`)
  }
  // 可逆动作必须留着，否则 agent 寸步难行
  assert.ok(masked.includes('下一步') && masked.includes('保存草稿'), '把可逆按钮也遮了')
}

// ② 纯音乐开关：instrumental 与指令里的说法必须一致
//    说反了会让 agent 去死磕「非纯音乐请填歌词」那条误导性报错
{
  const ins = pageInstructions('https://music.douyin.com/console/publish', track)!
  assert.ok(ins.includes('必须把它切到「是」'), '纯音乐曲目没被告知要打开开关')

  const withLyrics = pageInstructions('https://music.douyin.com/console/publish',
    { ...track, instrumental: false, lyrics: '词' })!
  assert.ok(withLyrics.includes('保持「否」'), '有词的曲目被当成纯音乐了')

  assert.equal(pageInstructions('https://example.com', track), null, '非目标站点不该注入指令')
}

// ③ base64 还原：音频经 background 转 base64 过来，错一个字节平台就报「音频无效」
{
  const bytes = new Uint8Array([0, 255, 16, 32, 127, 128])
  const b64 = Buffer.from(bytes).toString('base64')
  const file = b64ToFile(b64, 'x.mp3', 'audio/mpeg')
  const back = new Uint8Array(await file.arrayBuffer())
  assert.deepEqual([...back], [...bytes], 'base64 往返不是原字节')
  assert.equal(file.name, 'x.mp3')
}

console.log('✅ selfcheck 全过（护栏 / 纯音乐开关 / base64 往返）')
