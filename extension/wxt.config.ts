/**
 * WXT 构建配置
 * 宿主：Chrome MV3
 * host_permissions 里的 127.0.0.1 是本机发行服务（suno-publisher，默认 8867）——
 * 资产只在本机流转，不出网。voxflow 的 8866 不再被本扩展访问。
 */
import { defineConfig } from 'wxt'

export default defineConfig({
  srcDir: 'src',
  manifest: {
    name: 'Suno Publisher 发行助手',
    description: '把本机曲库的元数据与音频填进音乐平台发行页，停在提交前',
    permissions: ['storage'],
    host_permissions: [
      'http://127.0.0.1/*',
      'http://localhost/*',
      'https://music.douyin.com/*',
    ],
  },
})
