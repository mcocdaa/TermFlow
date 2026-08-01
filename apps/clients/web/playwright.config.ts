import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.TERMFLOW_E2E_BASE_URL
if (!baseURL) throw new Error('TERMFLOW_E2E_BASE_URL is required')

export default defineConfig({
  testDir: './e2e',
  outputDir: process.env.TERMFLOW_E2E_ARTIFACT_DIR ?? 'test-results',
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 900 } } },
    { name: 'mobile-portrait', use: { ...devices['Pixel 7'], viewport: { width: 390, height: 844 } } },
    { name: 'mobile-landscape', use: { ...devices['Pixel 7 landscape'], viewport: { width: 844, height: 390 } } },
  ],
})
