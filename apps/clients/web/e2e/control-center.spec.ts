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
  if (testInfo.project.name === 'desktop') {
    const backgroundBeforeHover = await termRow.evaluate((element) => getComputedStyle(element).backgroundColor)
    await termRow.hover()
    const backgroundAfterHover = await termRow.evaluate((element) => getComputedStyle(element).backgroundColor)
    expect(backgroundAfterHover).not.toBe(backgroundBeforeHover)
  }

  await page.getByRole('link', { name: '电脑管理' }).first().click()
  await page.getByRole('button', { name: '添加电脑' }).click()
  const enrollmentCreated = page.waitForResponse((response) =>
    response.request().method() === 'POST'
    && response.url().endsWith('/api/v1/enrollment-tokens')
    && response.ok(),
  )
  await page.getByRole('button', { name: '创建一次性注册码' }).click()
  const enrollmentResponse = await enrollmentCreated
  const enrollment = await enrollmentResponse.json() as { expires_at: string }
  expect(new Date(enrollment.expires_at).getTime() - Date.now()).toBeLessThanOrEqual(61_000)
  await expect(page.getByRole('heading', { name: '注册码' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '终端执行命令' })).toBeVisible()
  await page.getByRole('button', { name: '终端执行命令说明' }).hover()
  await expect(page.getByRole('tooltip')).toBeVisible()
  await expect(page.getByRole('button', { name: '复制命令' })).toBeVisible()
  const screenshotDir = process.env.TERMFLOW_E2E_SCREENSHOT_DIR
  if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/enrollment-${testInfo.project.name}.png` })
  await page.getByRole('button', { name: '关闭' }).click()
  await page.getByRole('link', { name: '控制中心' }).first().click()
  if (testInfo.project.name === 'desktop') await termRow.hover()
  if (testInfo.project.name === 'mobile-portrait') {
    const countsBox = await termRow.locator('.term-counts').boundingBox()
    const lastSeenBox = await termRow.locator('.term-last-seen').boundingBox()
    expect(countsBox).not.toBeNull()
    expect(lastSeenBox).not.toBeNull()
    expect(countsBox!.y + countsBox!.height).toBeLessThanOrEqual(lastSeenBox!.y)
  }
  if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/dashboard-${testInfo.project.name}.png` })

  await page.getByRole('radio', { name: '云端钴蓝' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'cloud-cobalt')
  await page.getByRole('radio', { name: '午夜靛蓝' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'midnight-indigo')

  await page.locator(`[data-term-id="${termId}"]`).click()
  await expect(page).toHaveURL(new RegExp(`/terms/${termId}$`))
  await expect(page.locator('[data-connection-status]')).toHaveText('已连接', { timeout: 10_000 })
  await expect(page.locator('.terminal-host .xterm')).toBeVisible()

  const projectCode = testInfo.project.name
    .split('-')
    .map((part) => part[0])
    .join('')
    .toUpperCase()
  const marker = `TF${projectCode}${Date.now().toString(36).slice(-4)}`
  await page.locator('.terminal-host textarea').focus()
  await page.keyboard.type(`printf '${marker}\\n'`)
  await page.keyboard.press('Enter')
  await expect(page.locator('.terminal-host')).toContainText(marker, { timeout: 10_000 })

  const displayTrigger = page.getByRole('button', { name: /^显示/ })
  await displayTrigger.hover()
  await expect(page.getByRole('menu', { name: '终端显示比例' })).toBeHidden()
  await displayTrigger.click()
  await page.getByRole('menuitemradio', { name: '50%' }).click()
  await expect(page.locator('.terminal-frame')).toHaveAttribute('data-display-mode', 'scale-50')

  if (testInfo.project.name === 'desktop') {
    const tmuxTrigger = page.getByRole('button', { name: /tmux 操作/i })
    await tmuxTrigger.hover()
    await expect(page.getByRole('menu', { name: /tmux 操作/i })).toBeHidden()
    await tmuxTrigger.click()
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

  await displayTrigger.click()
  await page.getByRole('menuitemradio', { name: '适应窗口' }).click()
  await expect(page.locator('.terminal-frame')).toHaveAttribute('data-display-mode', 'fit')
  const layout = await page.evaluate(() => {
    const documentElement = document.documentElement
    const view = document.querySelector<HTMLElement>('.terminal-view')!
    const frame = document.querySelector<HTMLElement>('.terminal-frame')!
    const xtermViewport = document.querySelector<HTMLElement>('.xterm-viewport')!
    const titlebar = document.querySelector<HTMLElement>('.terminal-titlebar')!
    return {
      documentOverflow: documentElement.scrollHeight - documentElement.clientHeight,
      viewOverflow: view.scrollHeight - view.clientHeight,
      frameOverflow: frame.scrollHeight - frame.clientHeight,
      frameOverflowY: getComputedStyle(frame).overflowY,
      xtermOverflowY: getComputedStyle(xtermViewport).overflowY,
      titlebarJustify: getComputedStyle(titlebar).justifyContent,
    }
  })
  expect(layout.documentOverflow).toBeLessThanOrEqual(1)
  expect(layout.viewOverflow).toBeLessThanOrEqual(1)
  expect(layout.frameOverflow).toBeLessThanOrEqual(1)
  expect(layout.frameOverflowY).toBe('hidden')
  expect(layout.xtermOverflowY).toBe('hidden')
  expect(layout.titlebarJustify).toBe('flex-start')

  if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/${testInfo.project.name}.png` })
})
