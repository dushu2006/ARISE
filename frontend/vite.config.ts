import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const backend = env.ARISE_BACKEND_TARGET || 'http://127.0.0.1:8765';
  const websocketBackend = backend.replace(/^http/, 'ws');

  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      port: 5173,
      strictPort: true,
      allowedHosts: ['.e2b.app', 'localhost', '127.0.0.1'],
      proxy: {
        '/api': { target: backend, changeOrigin: true },
        '/healthz': { target: backend, changeOrigin: true },
        '/ws/v1': { target: websocketBackend, ws: true, changeOrigin: true },
      },
    },
    preview: {
      host: '0.0.0.0',
      allowedHosts: ['.e2b.app', 'localhost', '127.0.0.1'],
    },
  };
});
