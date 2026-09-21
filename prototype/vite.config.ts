import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

const at = (path: string) => fileURLToPath(new URL(path, import.meta.url));

export default defineConfig({
  plugins: [react()],
  // 相对路径，这样 bridge.py 把 dist/ 挂在哪个路径下都能用
  base: './',
  build: {
    outDir: 'dist',
    // 两个入口：index.html 是预览页，embed.html 是嵌进 overlayd 的透明浮标页
    rollupOptions: { input: { index: at('./index.html'), embed: at('./embed.html') } },
  },
  server: {
    // 显式绑 IPv4 回环：默认的 "localhost" 在这台机器上会解析成 ::1，
    // 只监听 IPv6，浏览器和端口转发（走 127.0.0.1）都连不上。
    host: '127.0.0.1',
    // 开发时页面用相对路径 /events，由 vite 转发到 bridge.py，省掉跨域
    proxy: { '/events': 'http://127.0.0.1:8765' },
  },
});
