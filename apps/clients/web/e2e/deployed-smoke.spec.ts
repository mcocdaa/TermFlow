import { expect, test, type Page } from '@playwright/test'

const adminToken = process.env.TERMFLOW_E2E_ADMIN_TOKEN ?? ''
test.skip(!adminToken, 'A deployed admin token is required')

async function login(page: Page) {
  await page.goto('/login')
  await page.getByLabel('管理员令牌').fill(adminToken)
  const sessionCreated = page.waitForResponse((response) =>
    response.request().method() === 'POST'
    && response.url().endsWith('/api/v1/admin/sessions')
    && response.ok(),
  )
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await sessionCreated
  await expect(page.getByRole('heading', { name: '控制中心' })).toBeVisible()
}

test('deployed read-only smoke preserves dashboard and terminal route state', async ({ page }) => {
  await login(page)

  await expect(page.locator('.metric-card')).toHaveCount(4)
  await expect(page.locator('[data-action="logout"] svg')).toHaveCount(1)
  await expect(page.locator('.side-nav:visible svg, .mobile-nav:visible svg')).toHaveCount(3)
  for (const root of ['html', 'body', '#app']) {
    await expect(page.locator(root)).not.toHaveClass(/termflow-terminal-route/)
  }

  const firstOnlineTerm = page.locator('.term-row-link').first()
  if (await firstOnlineTerm.count() === 0) return

  await firstOnlineTerm.click()
  await expect(page).toHaveURL(/\/terms\/[^/]+$/)
  const lock = page.locator('[data-action="toggle-viewport-lock"]')
  await expect(lock).toHaveAttribute('aria-pressed', 'false')
  for (const root of ['html', 'body', '#app']) {
    await expect(page.locator(root)).toHaveClass(/termflow-terminal-route/)
  }

  await page.goto('/')
  await expect(page.getByRole('heading', { name: '控制中心' })).toBeVisible()
  for (const root of ['html', 'body', '#app']) {
    await expect(page.locator(root)).not.toHaveClass(/termflow-terminal-route/)
  }
})
