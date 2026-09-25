import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  // The backend deliberately sets no CORS headers at all (see
  // docs/architecture/security-boundaries-v1.md) -- browsers therefore
  // cannot call it cross-origin directly. Proxying /api through Vite's own
  // dev server keeps every request same-origin from the browser's
  // perspective; this proxy target is a server-to-server hop (Node -> the
  // backend), never subject to browser CORS. Read from the environment,
  // never hardcoded -- VITE_API_PROXY_TARGET in .env controls it.
  const apiProxyTarget = env.VITE_API_PROXY_TARGET

  return {
    plugins: [react()],
    server: apiProxyTarget
      ? {
          proxy: {
            '/api': {
              target: apiProxyTarget,
              changeOrigin: true,
            },
          },
        }
      : undefined,
  }
})
