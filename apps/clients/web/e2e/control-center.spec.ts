import { expect, test, type Page } from '@playwright/test'

const adminToken = process.env.TERMFLOW_E2E_ADMIN_TOKEN
const termId = process.env.TERMFLOW_E2E_TERM_ID
const termName = process.env.TERMFLOW_E2E_TERM_NAME
if (!adminToken || !termId || !termName) throw new Error('TermFlow browser fixture variables are required')

async function login(page: Page) {
  await page.goto('/login')
  await expect(page.locator('.app-header')).toHaveCount(0)
  await expect(page.locator('.side-nav')).toHaveCount(0)
  await expect(page.locator('.mobile-nav')).toHaveCount(0)
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

test('uses the real dashboard, themes, terminal transport, and responsive controls', async ({ page }, testInfo) => {
  await login(page)
  const termRow = page.locator(`[data-term-id="${termId}"]`)
  await expect(termRow).toBeVisible()
  await expect(termRow.getByText(termName, { exact: true })).toBeVisible()
  await expect(termRow.getByText(/\d+ Panes/)).toBeVisible()
  expect(await termRow.evaluate((element) => parseFloat(getComputedStyle(element).borderRadius))).toBeGreaterThan(0)
  await expect(page.locator('.side-nav a[href="/"] svg')).toHaveCount(1)
  await expect(page.locator('.side-nav a[href="/computers"] svg')).toHaveCount(1)
  if (testInfo.project.name === 'desktop') {
    for (const label of ['在线 Terms', '活动 Panes', '24 小时交互', 'Computers']) {
      const metric = page.locator('.metric-card').filter({ hasText: label })
      const metricBackgroundBeforeHover = await metric.evaluate((element) => getComputedStyle(element).backgroundColor)
      await metric.hover()
      await expect(metric.getByRole('tooltip')).toBeVisible()
      const metricBackgroundAfterHover = await metric.evaluate((element) => getComputedStyle(element).backgroundColor)
      expect(metricBackgroundAfterHover).not.toBe(metricBackgroundBeforeHover)
    }
    const backgroundBeforeHover = await termRow.evaluate((element) => getComputedStyle(element).backgroundColor)
    await termRow.hover()
    const backgroundAfterHover = await termRow.evaluate((element) => getComputedStyle(element).backgroundColor)
    expect(backgroundAfterHover).not.toBe(backgroundBeforeHover)
  }

  await page.getByRole('link', { name: '电脑管理' }).first().click()
  await expect(page.locator('.computer-table-head [role="columnheader"]')).toHaveText(['名称', '终端', '最近在线', '注册时间'])
  const computerRow = page.locator('.computer-table-row').first()
  await expect(computerRow.locator('.status-pill')).toHaveCount(1)
  await expect(computerRow.locator('.status-pill')).toContainText(/在线 \(\d+\)|离线 \(0\)/)
  for (const cell of await computerRow.locator('[role="cell"]').all()) {
    expect(await cell.evaluate((element) => getComputedStyle(element).justifyContent)).toBe('center')
  }
  const renderedTimes = await computerRow.locator('time').allTextContents()
  expect(renderedTimes.join(' ')).not.toMatch(/GMT|UTC|CST/i)
  if (testInfo.project.name === 'mobile-portrait') {
    await expect(page.locator('.mobile-nav a[href="/"] svg')).toBeVisible()
    await expect(page.locator('.mobile-nav a[href="/computers"] svg')).toBeVisible()
  }
  if (testInfo.project.name === 'mobile-landscape') {
    await expect(page.locator('.side-nav a[href="/"] svg')).toBeVisible()
    await expect(page.locator('.side-nav a[href="/computers"] svg')).toBeVisible()
  }
  if (testInfo.project.name !== 'desktop') {
    await expect(computerRow.locator('[role="cell"]').nth(0)).toHaveAttribute('data-label', '名称')
    await expect(computerRow.locator('[role="cell"]').nth(1)).toHaveAttribute('data-label', '终端')
  }
  await page.getByRole('button', { name: '添加电脑' }).click()
  const enrollmentDialog = page.getByRole('dialog', { name: '添加电脑' })
  await expect(enrollmentDialog.getByLabel('电脑名称')).toHaveAttribute('placeholder', '输入电脑名称')
  await enrollmentDialog.getByLabel('电脑名称').fill(`浏览器测试电脑-${testInfo.project.name}`)
  const enrollmentCreated = page.waitForResponse((response) =>
    response.request().method() === 'POST'
    && response.url().endsWith('/api/v1/enrollment-tokens')
    && response.ok(),
  )
  await enrollmentDialog.getByRole('button', { name: '创建', exact: true }).click()
  const enrollmentResponse = await enrollmentCreated
  expect(enrollmentResponse.request().postDataJSON()).toEqual({ display_name: `浏览器测试电脑-${testInfo.project.name}` })
  const enrollment = await enrollmentResponse.json() as { expires_at: string }
  expect(new Date(enrollment.expires_at).getTime() - Date.now()).toBeLessThanOrEqual(61_000)
  await expect(page.getByRole('heading', { name: '注册码' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '终端执行', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '终端执行说明' }).hover()
  const enrollmentTooltip = enrollmentDialog.getByRole('tooltip')
  await expect(enrollmentTooltip).toBeVisible()
  const dialogBox = await enrollmentDialog.boundingBox()
  const tooltipBox = await enrollmentTooltip.boundingBox()
  expect(dialogBox).not.toBeNull()
  expect(tooltipBox).not.toBeNull()
  expect(tooltipBox!.x).toBeGreaterThanOrEqual(dialogBox!.x)
  expect(tooltipBox!.x + tooltipBox!.width).toBeLessThanOrEqual(dialogBox!.x + dialogBox!.width)
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
  const identifiers = page.locator('[data-terminal-identifiers]')
  const termNameControl = identifiers.locator('[data-term-name]')
  const computerName = identifiers.locator('[data-computer-name]')
  await expect(termNameControl).toHaveRole('button')
  await expect(termNameControl.locator('svg')).toHaveCount(0)
  expect(await identifiers.evaluate((element) => getComputedStyle(element).whiteSpace)).toBe('nowrap')
  const termNameBox = await termNameControl.boundingBox()
  const computerNameBox = await computerName.boundingBox()
  expect(termNameBox).not.toBeNull()
  expect(computerNameBox).not.toBeNull()
  const termNameCenter = termNameBox!.y + termNameBox!.height / 2
  const computerNameCenter = computerNameBox!.y + computerNameBox!.height / 2
  expect(Math.abs(termNameCenter - computerNameCenter)).toBeLessThanOrEqual(1)

  const renamedTerm = `${termName}-${testInfo.project.name}`
  await termNameControl.click()
  await page.locator('[data-term-name-input]').fill(renamedTerm)
  const renamed = page.waitForResponse((response) => response.request().method() === 'PATCH' && response.url().endsWith(`/api/v1/terms/${termId}`) && response.ok())
  await page.locator('[data-action="save-term-name"]').click()
  await renamed
  await expect(page.locator('[data-term-name]')).toHaveText(renamedTerm)
  await page.locator('[data-term-name]').click()
  await page.locator('[data-term-name-input]').fill(termName)
  const restoredName = page.waitForResponse((response) => response.request().method() === 'PATCH' && response.url().endsWith(`/api/v1/terms/${termId}`) && response.ok())
  await page.locator('[data-action="save-term-name"]').click()
  await restoredName
  await expect(page.locator('[data-term-name]')).toHaveText(termName)

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
  if (testInfo.project.name === 'desktop') {
    const tmuxTrigger = page.getByRole('button', { name: /tmux 操作/i })
    const focusTrigger = page.getByRole('button', { name: /聚焦 Pane/i })
    await tmuxTrigger.click()
    await expect(page.getByRole('menu', { name: '终端显示比例' })).toBeHidden()
    await expect(page.getByRole('menu', { name: /tmux 操作/i })).toBeVisible()
    await focusTrigger.click()
    await expect(page.getByRole('menu', { name: /tmux 操作/i })).toBeHidden()
    await expect(page.getByRole('menu', { name: /聚焦 Pane/i })).toBeVisible()
    await displayTrigger.click()
    await expect(page.getByRole('menu', { name: /聚焦 Pane/i })).toBeHidden()
    await expect(page.getByRole('menu', { name: '终端显示比例' })).toBeVisible()
  }
  await page.getByRole('menuitemradio', { name: '50%' }).click()
  await expect(page.locator('.terminal-frame')).toHaveAttribute('data-display-mode', 'scale-50')

  if (testInfo.project.name === 'desktop') {
    const tmuxTrigger = page.getByRole('button', { name: /tmux 操作/i })
    await tmuxTrigger.hover()
    await expect(page.getByRole('menu', { name: /tmux 操作/i })).toBeHidden()
    await tmuxTrigger.click()
    const splitAction = page.getByRole('menuitem', { name: /左右切分 Pane/ })
    await expect(splitAction.locator('.action-label')).toHaveText('左右切分 Pane')
    await expect(splitAction.locator('small')).toHaveCount(0)
    await splitAction.hover()
    const bindingTooltip = splitAction.getByRole('tooltip')
    await expect(bindingTooltip).toBeVisible()
    await expect(bindingTooltip.locator('code')).toContainText('Ctrl + b')
    const splitActionBox = await splitAction.boundingBox()
    const bindingTooltipBox = await bindingTooltip.boundingBox()
    expect(splitActionBox).not.toBeNull()
    expect(bindingTooltipBox).not.toBeNull()
    expect(bindingTooltipBox!.x).toBeGreaterThanOrEqual(splitActionBox!.x)
    expect(bindingTooltipBox!.y).toBeGreaterThanOrEqual(splitActionBox!.y)
    expect(bindingTooltipBox!.x + bindingTooltipBox!.width).toBeLessThanOrEqual(splitActionBox!.x + splitActionBox!.width)
    expect(bindingTooltipBox!.y + bindingTooltipBox!.height).toBeLessThanOrEqual(splitActionBox!.y + splitActionBox!.height)
    if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/tmux-tooltip-desktop.png` })
    await splitAction.click()
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
