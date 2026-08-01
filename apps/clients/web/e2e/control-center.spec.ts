import { expect, test, type Page } from '@playwright/test'

const adminToken = process.env.TERMFLOW_E2E_ADMIN_TOKEN
const termId = process.env.TERMFLOW_E2E_TERM_ID
const termName = process.env.TERMFLOW_E2E_TERM_NAME
if (!adminToken || !termId || !termName) throw new Error('TermFlow browser fixture variables are required')

async function login(page: Page) {
  await page.goto('/login')
  await page.getByLabel('管理员令牌').fill(adminToken)
  const sessionCreated = page.waitForResponse((response) =>
    response.request().method() === 'POST'
    && response.url().endsWith('/api/v1/admin/sessions')
    && response.ok(),
  )
  await page.getByRole('button', { name: '创建会话' }).click()
  await sessionCreated
  await expect(page.getByRole('heading', { name: '控制中心' })).toBeVisible()
}

test('uses the real dashboard, themes, terminal transport, and responsive controls', async ({ page }, testInfo) => {
  await login(page)
  const termRow = page.locator(`[data-term-id="${termId}"]`)
  await expect(termRow).toBeVisible()
  await expect(termRow.getByText(termName, { exact: true })).toBeVisible()
  await expect(termRow.getByText(/\d+ Panes/)).toBeVisible()

  await page.getByRole('radio', { name: '云端钴蓝' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'cloud-cobalt')
  await page.getByRole('radio', { name: '午夜靛蓝' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'midnight-indigo')

  await page.locator(`[data-term-id="${termId}"]`).getByRole('link', { name: /打开终端/ }).click()
  await expect(page).toHaveURL(new RegExp(`/terms/${termId}$`))
  await expect(page.locator('[data-connection-status]')).toHaveText('已连接', { timeout: 10_000 })
  await expect(page.locator('.terminal-host .xterm')).toBeVisible()

  const marker = `WEB_E2E_${testInfo.project.name.replaceAll('-', '_')}`
  await page.locator('.terminal-host textarea').focus()
  await page.keyboard.type(`printf '${marker}\\n'`)
  await page.keyboard.press('Enter')
  await expect(page.locator('.terminal-host')).toContainText(marker, { timeout: 10_000 })

  await page.getByRole('button', { name: /^显示/ }).click()
  await page.getByRole('menuitemradio', { name: '50%' }).click()
  await expect(page.locator('.terminal-frame')).toHaveAttribute('data-display-mode', 'scale-50')

  if (testInfo.project.name === 'desktop') {
    await page.getByRole('button', { name: /Tmux 操作/ }).click()
    await page.getByRole('menuitem', { name: /左右切分 Pane/ }).click()
    await expect.poll(async () => {
      const response = await page.request.get('/api/v1/instances/' + termId + '/topology')
      const body = await response.json()
      return body.topology.windows.flatMap((window: { panes: unknown[] }) => window.panes).length
    }).toBeGreaterThanOrEqual(2)
  } else {
    await page.getByRole('button', { name: '快捷操作' }).click()
    await expect(page.getByLabel('移动端 Tmux 操作')).toBeVisible()
    await page.getByRole('button', { name: 'Ctrl' }).click()
    await expect(page.getByRole('button', { name: 'Ctrl' })).toHaveAttribute('aria-pressed', 'true')
    await page.getByRole('button', { name: '收起' }).click()
    await expect(page.getByLabel('移动端 Tmux 操作')).toBeHidden()
  }

  await page.getByRole('button', { name: /^显示/ }).click()
  await page.getByRole('menuitemradio', { name: '适应窗口' }).click()
  await expect(page.locator('.terminal-frame')).toHaveAttribute('data-display-mode', 'fit')

  const screenshotDir = process.env.TERMFLOW_E2E_SCREENSHOT_DIR
  if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/${testInfo.project.name}.png` })
})
