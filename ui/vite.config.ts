/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const defaultApiPort = '8000'
const apiTarget = process.env.OPENMATRIX_API_TARGET || `http://127.0.0.1:${defaultApiPort}`

// https://vite.dev/config/
export default defineConfig({
  base: '/ui/',
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  test: {
    pool: 'threads',
    maxWorkers: 2,
    testTimeout: 15000,
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
    css: false,
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json-summary'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/main.tsx', 'src/test/**'],
      thresholds: {
        lines: 50,
        functions: 40,
        statements: 50,
        branches: 30,
      },
    },
  },
})
