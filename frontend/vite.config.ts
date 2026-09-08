import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// M1 前端：dev server 5173，/api 代理到本地后端 127.0.0.1:8000
// 后端未启动时前端自动降级为内置 mock 演示数据（见 src/lib/api.ts）
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
