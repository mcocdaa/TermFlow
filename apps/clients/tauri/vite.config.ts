import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const packageManifest: unknown = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'))
if (typeof packageManifest !== 'object' || packageManifest === null || !('version' in packageManifest) || typeof packageManifest.version !== 'string') {
  throw new Error('package.json must contain a string version')
}

export default defineConfig({
  plugins: [vue()],
  clearScreen: false,
  define: { __TERMFLOW_BUILD_VERSION__: JSON.stringify(packageManifest.version) },
  server: { port: 1420, strictPort: true },
  envPrefix: ['VITE_', 'TAURI_'],
  build: { target: ['es2022'], minify: 'esbuild', sourcemap: false },
})
