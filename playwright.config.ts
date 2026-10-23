import { defineConfig } from '@playwright/test'

const python = process.env.LISA_TEST_PYTHON
if (!python || !process.env.LISA_TEST_INVITES) {
  throw new Error('Run Python scripts/test_app.py --browser to use an isolated database.')
}

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'list',
  use: {
    baseURL: process.env.LISA_TEST_BASE_URL,
    browserName: 'chromium',
    viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command: `"${python}" -m uvicorn lisa.app:create_app --factory --host 127.0.0.1 --port 8011 --no-access-log`,
      url: 'http://127.0.0.1:8011/api/health',
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5174 --strictPort',
      url: process.env.LISA_TEST_BASE_URL,
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
})
