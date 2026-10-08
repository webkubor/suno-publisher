/**
 * WXT 构建配置
 * 宿主：Chrome MV3
 * host_permissions 里的 127.0.0.1 是 VoxFlow 本地 FastAPI —— 资产只在本机流转，不出网。
 */
import { defineConfig } from 'wxt'

export default defineConfig({
  srcDir: 'src',
  manifest: {
    name: 'Suno Publisher 发行助手',
    description: '把 VoxFlow 曲库的元数据与音频填进音乐平台发行页，停在提交前',
    permissions: ['storage'],
    host_permissions: [
      'http://127.0.0.1/*',
      'http://localhost/*',
      'https://music.douyin.com/*',
    ],
  },
})
