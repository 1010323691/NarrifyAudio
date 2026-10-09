import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite dev server + build config for the Narrify Audio frontend.
//
// In development, proxy API requests through Vite so browser cookies remain
// same-origin. Production can still point directly at the configured API.
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  clearScreen: false,
  // Only scan application entry points. Runtime storage and Python environments
  // can contain tens of thousands of files and are unrelated to frontend HMR.
  optimizeDeps: {
    entries: ['index.html', 'scripts/fixtures/production-workbench.html'],
  },
  server: {
    // Respect a PORT env override (sandboxed previews assign a port this way);
    // defaults to 5173 exactly as before.
    port: process.env.PORT ? Number(process.env.PORT) : 5173,
    strictPort: true,
    watch: {
      ignored: [
        '**/.venv/**',
        '**/logs/**',
        '**/.narrify/**',
        '**/storage/**',
        '**/.backups/**',
        '**/music_library/**',
        '**/config/**',
      ],
    },
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8642',
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'es2020',
    outDir: 'dist',
    sourcemap: false,
    // ECharts is only reachable from the lazily loaded admin console; keeping
    // it in its own vendor chunk lets console updates reuse the cached library.
    // That chunk (~620 kB, ~210 kB gzip) is the only one past Vite's 500 kB hint.
    chunkSizeWarningLimit: 650,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (/node_modules[\\/](echarts|zrender)[\\/]/.test(id)) return 'vendor-echarts'
        },
      },
    },
  },
})
