import { expect, test, type Locator, type Page } from '@playwright/test'

const adminToken = process.env.TERMFLOW_E2E_ADMIN_TOKEN ?? ''
const termId = process.env.TERMFLOW_E2E_TERM_ID ?? ''
const termName = process.env.TERMFLOW_E2E_TERM_NAME ?? ''
const offlineTermIds = JSON.parse(
  process.env.TERMFLOW_E2E_OFFLINE_TERM_IDS ?? '{}',
) as Record<string, string>
test.skip(
  !adminToken || !termId || !termName,
  'TermFlow isolated browser fixture variables are required',
)

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

interface PaneGeometry {
  pane_id: string
  active: boolean
  left: number
  top: number
  width: number
  height: number
}

interface TouchPoint { x: number; y: number; id: number }
interface TouchStep {
  type: 'touchStart' | 'touchMove' | 'touchEnd'
  points: TouchPoint[]
  delayMs?: number
}

async function dispatchTouchSequence(page: Page, steps: TouchStep[]) {
  const session = await page.context().newCDPSession(page)
  try {
    for (const step of steps) {
      await session.send('Input.dispatchTouchEvent', {
        type: step.type,
        touchPoints: step.points.map((point) => ({ x: point.x, y: point.y, id: point.id })),
      })
      if (step.delayMs) await page.waitForTimeout(step.delayMs)
    }
  } finally {
    await session.detach()
  }
}

async function mobilePageGeometry(page: Page) {
  return page.evaluate(() => {
    const html = document.documentElement
    const body = document.body
    const titlebar = document.querySelector<HTMLElement>('.terminal-titlebar')!
    const frame = document.querySelector<HTMLElement>('.terminal-frame')!
    const keybarShell = document.querySelector<HTMLElement>('.mobile-keybar-shell')!
    const keybar = document.querySelector<HTMLElement>('.mobile-keybar')!
    const rectangle = (element: HTMLElement) => {
      const box = element.getBoundingClientRect()
      return { left: box.left, top: box.top, right: box.right, bottom: box.bottom }
    }
    return {
      windowX: window.scrollX,
      windowY: window.scrollY,
      htmlLeft: html.scrollLeft,
      htmlTop: html.scrollTop,
      bodyLeft: body.scrollLeft,
      bodyTop: body.scrollTop,
      visualLeft: window.visualViewport?.offsetLeft ?? 0,
      visualTop: window.visualViewport?.offsetTop ?? 0,
      titlebar: rectangle(titlebar),
      frame: rectangle(frame),
      keybarShell: rectangle(keybarShell),
      keybar: rectangle(keybar),
      keybarScrollLeft: keybar.scrollLeft,
    }
  })
}

async function expectInsideVisualViewport(locator: Locator, page: Page) {
  const box = await locator.boundingBox()
  expect(box).not.toBeNull()
  const viewport = await page.evaluate(() => ({
    left: window.visualViewport?.offsetLeft ?? 0,
    top: window.visualViewport?.offsetTop ?? 0,
    width: window.visualViewport?.width ?? window.innerWidth,
    height: window.visualViewport?.height ?? window.innerHeight,
  }))
  expect(box!.x).toBeGreaterThanOrEqual(viewport.left - 1)
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.left + viewport.width + 1)
  expect(box!.y).toBeGreaterThanOrEqual(viewport.top - 1)
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.top + viewport.height + 1)
}

const buttonVisualStyle = (trigger: Locator) => trigger.evaluate((element) => {
  const style = getComputedStyle(element)
  return {
    backgroundColor: style.backgroundColor,
    color: style.color,
    borderColor: style.borderColor,
  }
})

async function panesForTerm(page: Page): Promise<PaneGeometry[]> {
  const response = await page.request.get(`/api/v1/instances/${termId}/topology`)
  expect(response.ok()).toBe(true)
  const body = await response.json() as { topology: { windows: Array<{ panes: PaneGeometry[] }> } }
  return body.topology.windows.flatMap((window) => window.panes)
}

async function ensureTwoPanes(page: Page): Promise<[PaneGeometry, PaneGeometry]> {
  let panes = (await panesForTerm(page)).toSorted((left, right) => left.left - right.left)
  if (panes.length === 1) {
    await page.getByRole('button', { name: 'tmux 操作' }).click()
    await page.getByRole('menuitem', { name: /左右切分 Pane/ }).click()
    await expect.poll(async () => (await panesForTerm(page)).length).toBe(2)
    panes = (await panesForTerm(page)).toSorted((left, right) => left.left - right.left)
  }
  expect(panes).toHaveLength(2)
  return [panes[0]!, panes[1]!]
}

async function selectLeftPaneWithKeyboard(page: Page): Promise<[PaneGeometry, PaneGeometry]> {
  const panes = (await panesForTerm(page)).toSorted((left, right) => left.left - right.left)
  expect(panes).toHaveLength(2)
  const [leftPane, rightPane] = panes
  await page.locator('.terminal-host textarea').focus()
  await page.keyboard.press('Control+b')
  await page.keyboard.press('ArrowLeft')
  await expect.poll(async () => (await panesForTerm(page)).find((pane) => pane.active)?.pane_id).toBe(leftPane.pane_id)
  return [leftPane, rightPane]
}

async function terminalPoint(page: Page, column: number, row: number) {
  const screen = page.locator('.xterm-screen')
  const box = await screen.boundingBox()
  expect(box).not.toBeNull()
  const panes = await panesForTerm(page)
  const cols = Math.max(...panes.map((candidate) => candidate.left + candidate.width))
  const rows = Math.max(...panes.map((candidate) => candidate.top + candidate.height)) + 1
  return {
    x: box!.x + (column + 0.5) / cols * box!.width,
    y: box!.y + (row + 0.5) / rows * box!.height,
    col: Math.floor(column) + 1,
    row: Math.floor(row) + 1,
  }
}

async function clickPaneCenter(page: Page, pane: PaneGeometry) {
  const column = Math.floor(pane.left + pane.width / 2)
  const row = Math.floor(pane.top + pane.height / 2)
  const point = await terminalPoint(page, column, row)
  await page.mouse.click(point.x, point.y)
  return point
}

async function expectWordSelection(
  page: Page,
  terminalOutputFrames: Buffer[],
  pane: PaneGeometry,
  suffix: string,
  exitCopyMode = false,
) {
  const firstWord = `LEFT${suffix}`
  const secondWord = `RIGHT${suffix}`
  await page.locator('.terminal-host textarea').focus()
  if (exitCopyMode) await page.keyboard.press('q')
  await page.keyboard.type(String.raw`printf '\033[2J\033[HLEFT%s    RIGHT%s' ${suffix} ${suffix}`)
  const outputStart = terminalOutputFrames.length
  await page.keyboard.press('Enter')
  await expect.poll(() => Buffer.concat(terminalOutputFrames.slice(outputStart)).toString('utf8')).toContain(secondWord)
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
  await expect(page.locator('.xterm')).toHaveClass(/enable-mouse-events/)
  const secondWordColumn = pane.left + firstWord.length + 4 + Math.floor(secondWord.length / 2)
  const selectionTarget = await terminalPoint(page, secondWordColumn, pane.top)
  await page.keyboard.down('Shift')
  await page.mouse.dblclick(selectionTarget.x, selectionTarget.y)
  await page.keyboard.up('Shift')
  const copied = await page.locator('.xterm').evaluate((element) => {
    const clipboard = new DataTransfer()
    element.dispatchEvent(new ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: clipboard }))
    return clipboard.getData('text/plain')
  })
  expect(copied).toBe(secondWord)
}

test('permanently removes only its disposable offline Term', async ({ page }, testInfo) => {
  const offlineTermId = offlineTermIds[testInfo.project.name]
  expect(offlineTermId, `missing offline fixture for ${testInfo.project.name}`).toBeTruthy()
  await login(page)

  const onlineRow = page.locator(`[data-term-id="${termId}"]`)
  await expect(onlineRow.locator('[data-action="delete-offline-term"]')).toHaveCount(0)

  const offlineName = `offline-${testInfo.project.name}`
  const offlineRow = page.locator(`[data-term-id="${offlineTermId}"]`)
  await expect(offlineRow).toBeVisible()
  const deleteButton = offlineRow.getByRole('button', {
    name: `删除离线 Term：${offlineName}`,
  })
  await expect(deleteButton.locator('svg')).toHaveCount(1)
  await deleteButton.click()

  const dialog = page.getByRole('alertdialog')
  await expect(dialog).toContainText('不会删除本地 tmux Session')
  await expect(dialog).toContainText(`termflow activate ${offlineTermId}`)
  const deleted = page.waitForResponse((response) =>
    response.request().method() === 'DELETE'
    && response.url().endsWith(`/api/v1/terms/${offlineTermId}`)
    && response.status() === 204,
  )
  await dialog.locator('[data-action="confirm-delete-term"]').click()
  await deleted
  await expect(offlineRow).toHaveCount(0)
  await page.reload()
  await expect(page.locator(`[data-term-id="${offlineTermId}"]`)).toHaveCount(0)

  const onlineConflict = await page.request.delete(`/api/v1/terms/${termId}`, {
    headers: { Authorization: `Bearer ${adminToken}` },
  })
  expect(onlineConflict.status()).toBe(409)
  expect(JSON.stringify(await onlineConflict.json())).toContain('instance_online')
})

test('uses the real dashboard, themes, terminal transport, and responsive controls', async ({ page }, testInfo) => {
  const terminalFrames: Buffer[] = []
  const terminalOutputFrames: Buffer[] = []
  page.on('websocket', (socket) => {
    if (!socket.url().includes('/terminal')) return
    socket.on('framesent', ({ payload }) => {
      if (Buffer.isBuffer(payload)) terminalFrames.push(payload)
    })
    socket.on('framereceived', ({ payload }) => {
      if (Buffer.isBuffer(payload)) terminalOutputFrames.push(payload)
    })
  })
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
  if (testInfo.project.name === 'desktop') {
    const termNameBox = await termNameControl.boundingBox()
    const computerNameBox = await computerName.boundingBox()
    expect(termNameBox).not.toBeNull()
    expect(computerNameBox).not.toBeNull()
    const termNameCenter = termNameBox!.y + termNameBox!.height / 2
    const computerNameCenter = computerNameBox!.y + computerNameBox!.height / 2
    expect(Math.abs(termNameCenter - computerNameCenter)).toBeLessThanOrEqual(1)
  } else {
    await expect(computerName).toBeHidden()
    await expect(page.getByRole('button', { name: '显示设置' })).toBeVisible()
    await expect(page.getByRole('button', { name: '聚焦 Pane' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'tmux 操作' })).toBeVisible()
    await expect(page.getByRole('button', { name: '锁定画布' })).toHaveAttribute('aria-pressed', 'false')
    await expect(page.getByRole('button', { name: '快捷操作' })).toHaveCount(0)
    await expect(page.getByLabel('移动端 Tmux 操作')).toHaveCount(0)
  }

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
  const closedDisplayStyle = testInfo.project.name === 'desktop' ? null : await buttonVisualStyle(displayTrigger)
  await displayTrigger.click()
  if (testInfo.project.name === 'desktop') {
    const tmuxTrigger = page.getByRole('button', { name: /tmux 操作/i })
    await tmuxTrigger.click()
    await expect(page.getByRole('menu', { name: '终端显示比例' })).toBeHidden()
    await expect(page.getByRole('menu', { name: /tmux 操作/i })).toBeVisible()
    await displayTrigger.click()
    await expect(page.getByRole('menu', { name: /tmux 操作/i })).toBeHidden()
    await expect(page.getByRole('menu', { name: '终端显示比例' })).toBeVisible()

    await page.locator('.terminal-host textarea').focus()
    await page.keyboard.type('tmux set-option -g mouse off')
    await page.keyboard.press('Enter')
    await expect(page.locator('.xterm')).not.toHaveClass(/enable-mouse-events/)
    await page.getByRole('menuitemradio', { name: '100% 实际字号' }).click()
    const desktopFrame = page.locator('.terminal-frame')
    await expect(desktopFrame).toHaveAttribute('data-display-mode', 'font-100')
    await expect.poll(async () => desktopFrame.evaluate((element) => ({
      horizontal: element.scrollWidth - element.clientWidth,
      vertical: element.scrollHeight - element.clientHeight,
    }))).toEqual(expect.objectContaining({
      horizontal: expect.any(Number),
      vertical: expect.any(Number),
    }))
    const desktopOverflow = await desktopFrame.evaluate((element) => ({
      horizontal: element.scrollWidth - element.clientWidth,
      vertical: element.scrollHeight - element.clientHeight,
    }))
    expect(desktopOverflow.horizontal).toBeGreaterThan(1)
    expect(desktopOverflow.vertical).toBeGreaterThan(1)

    const desktopFrameBox = await desktopFrame.boundingBox()
    expect(desktopFrameBox).not.toBeNull()
    await page.mouse.move(
      desktopFrameBox!.x + desktopFrameBox!.width / 2,
      desktopFrameBox!.y + desktopFrameBox!.height / 2,
    )
    await page.mouse.wheel(180, 180)
    await expect.poll(async () => {
      const offsets = await desktopFrame.evaluate((element) => ({
        left: element.scrollLeft,
        top: element.scrollTop,
      }))
      return offsets.left > 0 && offsets.top > 0
    }).toBe(true)
    const unlockedOffsets = await desktopFrame.evaluate((element) => ({
      left: element.scrollLeft,
      top: element.scrollTop,
    }))
    expect(unlockedOffsets.left).toBeGreaterThan(0)
    expect(unlockedOffsets.top).toBeGreaterThan(0)

    const viewportLock = page.locator('[data-action="toggle-viewport-lock"]')
    await viewportLock.click()
    await expect(viewportLock).toHaveAttribute('aria-pressed', 'true')
    await expect(desktopFrame).toHaveAttribute('data-viewport-lock', 'locked')
    await page.mouse.move(
      desktopFrameBox!.x + desktopFrameBox!.width / 2,
      desktopFrameBox!.y + desktopFrameBox!.height / 2,
    )
    await page.mouse.wheel(180, 180)
    await page.waitForTimeout(100)
    expect(await desktopFrame.evaluate((element) => ({
      left: element.scrollLeft,
      top: element.scrollTop,
    }))).toEqual(unlockedOffsets)

    await viewportLock.click()
    await expect(viewportLock).toHaveAttribute('aria-pressed', 'false')
    await page.mouse.move(
      desktopFrameBox!.x + desktopFrameBox!.width / 2,
      desktopFrameBox!.y + desktopFrameBox!.height / 2,
    )
    await page.mouse.wheel(-120, -120)
    await expect.poll(() => desktopFrame.evaluate((element) => ({
      left: element.scrollLeft,
      top: element.scrollTop,
    }))).not.toEqual(unlockedOffsets)

    await page.locator('.terminal-host textarea').focus()
    await page.keyboard.type('tmux set-option -g mouse on')
    await page.keyboard.press('Enter')
    await expect(page.locator('.xterm')).toHaveClass(/enable-mouse-events/)
    await displayTrigger.click()
  } else {
    const displayMenu = page.getByRole('menu', { name: '终端显示比例' })
    await expect(displayTrigger).toHaveAttribute('aria-expanded', 'true')
    await expect(displayMenu).toBeVisible()
    await expectInsideVisualViewport(displayMenu, page)
    expect(await buttonVisualStyle(displayTrigger)).not.toEqual(closedDisplayStyle)
    await displayTrigger.click()
    await expect(displayTrigger).toHaveAttribute('aria-expanded', 'false')
    await expect(displayMenu).toBeHidden()
    await expect.poll(() => buttonVisualStyle(displayTrigger)).toEqual(closedDisplayStyle)
    await displayTrigger.click()
  }
  await page.getByRole('menuitemradio', { name: '50%' }).click()
  await expect(page.locator('.terminal-frame')).toHaveAttribute('data-display-mode', 'scale-50')
  if (closedDisplayStyle) {
    await expect(displayTrigger).toHaveAttribute('aria-expanded', 'false')
    await expect.poll(() => buttonVisualStyle(displayTrigger)).toEqual(closedDisplayStyle)
  }

  let mobilePanes: [PaneGeometry, PaneGeometry] | null = null
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
    const [, rightPane] = await selectLeftPaneWithKeyboard(page)
    await clickPaneCenter(page, rightPane)
    await expect.poll(async () => (await panesForTerm(page)).find((pane) => pane.active)?.pane_id).toBe(rightPane.pane_id)
    await expectWordSelection(page, terminalOutputFrames, rightPane, 'FIFTY')
  } else {
    mobilePanes = await ensureTwoPanes(page)
    const tmuxTrigger = page.getByRole('button', { name: 'tmux 操作' })
    const closedTmuxStyle = await buttonVisualStyle(tmuxTrigger)
    await tmuxTrigger.click()
    await expect(tmuxTrigger).toHaveAttribute('aria-expanded', 'true')
    await expectInsideVisualViewport(page.getByRole('menu', { name: /tmux 操作/i }), page)
    expect(await buttonVisualStyle(tmuxTrigger)).not.toEqual(closedTmuxStyle)
    await expect(page.getByRole('menuitem', { name: '选择左侧 Pane' })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: '选择右侧 Pane' })).toBeVisible()
    await tmuxTrigger.click()
    await expect(tmuxTrigger).toHaveAttribute('aria-expanded', 'false')
    await expect(page.getByRole('menu', { name: /tmux 操作/i })).toBeHidden()
    await expect.poll(() => buttonVisualStyle(tmuxTrigger)).toEqual(closedTmuxStyle)
    await tmuxTrigger.click()
    await page.getByRole('menuitem', { name: '关闭 Pane' }).click()
    const closePaneDialog = page.getByRole('alertdialog', { name: '关闭 Pane？' })
    await expect(closePaneDialog).toBeVisible()
    await closePaneDialog.getByRole('button', { name: '取消' }).click()
    await expect(closePaneDialog).toBeHidden()
    await expect.poll(() => buttonVisualStyle(tmuxTrigger)).toEqual(closedTmuxStyle)

    await page.getByRole('button', { name: 'Ctrl' }).click()
    await expect(page.getByRole('button', { name: 'Ctrl' })).toHaveAttribute('aria-pressed', 'true')
    await page.getByRole('button', { name: 'Ctrl' }).click()
    await page.getByRole('button', { name: 'Ctrl' }).click()
    await expect(page.getByRole('button', { name: 'Ctrl' })).toHaveAttribute('aria-pressed', 'false')

    await displayTrigger.click()
    await page.getByRole('menuitemradio', { name: '100% 实际字号' }).click()
    const mobileFrame = page.locator('.terminal-frame')
    const mobileGrid = page.locator('.terminal-grid')
    const frameBox = await mobileFrame.boundingBox()
    const gridBox = await mobileGrid.boundingBox()
    expect(frameBox).not.toBeNull()
    expect(gridBox).not.toBeNull()
    const center = { x: frameBox!.x + frameBox!.width / 2, y: frameBox!.y + frameBox!.height / 2 }
    expect(gridBox!.width - frameBox!.width).toBeGreaterThan(1)
    expect(gridBox!.height - frameBox!.height).toBeGreaterThan(1)
    const frameCountBefore = terminalFrames.length
    const transformBefore = await mobileGrid.evaluate((element) => getComputedStyle(element).transform)
    const scaleBefore = Number(await mobileFrame.getAttribute('data-visual-scale'))

    await dispatchTouchSequence(page, [
      { type: 'touchStart', points: [{ id: 1, x: center.x + 40, y: center.y }] },
      { type: 'touchMove', points: [{ id: 1, x: center.x - 40, y: center.y }] },
      { type: 'touchEnd', points: [] },
    ])
    const horizontalTransform = await mobileGrid.evaluate((element) => getComputedStyle(element).transform)
    expect(horizontalTransform).not.toBe(transformBefore)

    await dispatchTouchSequence(page, [
      { type: 'touchStart', points: [{ id: 1, x: center.x, y: center.y + 40 }] },
      { type: 'touchMove', points: [{ id: 1, x: center.x, y: center.y - 40 }] },
      { type: 'touchEnd', points: [] },
    ])
    expect(await mobileGrid.evaluate((element) => getComputedStyle(element).transform)).not.toBe(horizontalTransform)

    await dispatchTouchSequence(page, [
      { type: 'touchStart', points: [
        { id: 1, x: center.x - 30, y: center.y },
        { id: 2, x: center.x + 30, y: center.y },
      ] },
      { type: 'touchMove', points: [
        { id: 1, x: center.x - 70, y: center.y },
        { id: 2, x: center.x + 70, y: center.y },
      ] },
      { type: 'touchEnd', points: [] },
    ])
    expect(Number(await mobileFrame.getAttribute('data-visual-scale'))).toBeGreaterThan(scaleBefore)
    expect(terminalFrames).toHaveLength(frameCountBefore)

    const mobileLock = page.locator('[data-action="toggle-viewport-lock"]')
    await mobileLock.click()
    await expect(mobileLock).toHaveAttribute('aria-pressed', 'true')
    await expect(mobileFrame).toHaveAttribute('data-viewport-lock', 'locked')
    const lockedViewport = {
      transform: await mobileGrid.evaluate((element) => getComputedStyle(element).transform),
      scale: await mobileFrame.getAttribute('data-visual-scale'),
      scroll: await mobileFrame.evaluate((element) => ({
        left: element.scrollLeft,
        top: element.scrollTop,
      })),
    }
    await dispatchTouchSequence(page, [
      { type: 'touchStart', points: [{ id: 3, x: center.x + 30, y: center.y + 30 }] },
      { type: 'touchMove', points: [{ id: 3, x: center.x - 30, y: center.y - 30 }] },
      { type: 'touchEnd', points: [] },
    ])
    await dispatchTouchSequence(page, [
      { type: 'touchStart', points: [
        { id: 4, x: center.x - 25, y: center.y },
        { id: 5, x: center.x + 25, y: center.y },
      ] },
      { type: 'touchMove', points: [
        { id: 4, x: center.x - 55, y: center.y },
        { id: 5, x: center.x + 55, y: center.y },
      ] },
      { type: 'touchEnd', points: [] },
    ])
    expect(await mobileGrid.evaluate((element) => getComputedStyle(element).transform)).toBe(lockedViewport.transform)
    expect(await mobileFrame.getAttribute('data-visual-scale')).toBe(lockedViewport.scale)
    expect(await mobileFrame.evaluate((element) => ({
      left: element.scrollLeft,
      top: element.scrollTop,
    }))).toEqual(lockedViewport.scroll)
    await mobileLock.click()
    await expect(mobileLock).toHaveAttribute('aria-pressed', 'false')
  }

  await displayTrigger.click()
  await page.getByRole('menuitemradio', { name: '适应窗口' }).click()
  await expect(page.locator('.terminal-frame')).toHaveAttribute('data-display-mode', 'fit')
  const layout = await page.evaluate(() => {
    const documentElement = document.documentElement
    const view = document.querySelector<HTMLElement>('.terminal-view')!
    const frame = document.querySelector<HTMLElement>('.terminal-frame')!
    const content = document.querySelector<HTMLElement>('.terminal-viewport-content')!
    const grid = document.querySelector<HTMLElement>('.terminal-grid')!
    const screen = document.querySelector<HTMLElement>('.xterm-screen')!
    const xtermViewport = document.querySelector<HTMLElement>('.xterm-viewport')!
    const titlebar = document.querySelector<HTMLElement>('.terminal-titlebar')!
    const rectangle = (element: HTMLElement) => {
      const box = element.getBoundingClientRect()
      return { left: box.left, top: box.top, right: box.right, bottom: box.bottom }
    }
    return {
      documentOverflow: documentElement.scrollHeight - documentElement.clientHeight,
      viewOverflow: view.scrollHeight - view.clientHeight,
      frameOverflowY: getComputedStyle(frame).overflowY,
      frame: rectangle(frame),
      grid: rectangle(grid),
      screen: rectangle(screen),
      contentHeight: content.getBoundingClientRect().height,
      gridHeight: grid.getBoundingClientRect().height,
      gridTransform: getComputedStyle(grid).transform,
      screenHeight: screen.getBoundingClientRect().height,
      visualScale: frame.dataset.visualScale,
      cellHeight: frame.dataset.cellHeight,
      xtermOverflowY: getComputedStyle(xtermViewport).overflowY,
      titlebarJustify: getComputedStyle(titlebar).justifyContent,
    }
  })
  expect(layout.documentOverflow).toBeLessThanOrEqual(1)
  expect(layout.viewOverflow).toBeLessThanOrEqual(1)
  expect(layout.frameOverflowY).toBe('hidden')
  expect(layout.xtermOverflowY).toBe('hidden')
  expect(layout.titlebarJustify).toBe('flex-start')
  expect(layout.grid.left).toBeGreaterThanOrEqual(layout.frame.left - 1)
  expect(layout.grid.top).toBeGreaterThanOrEqual(layout.frame.top - 1)
  expect(layout.grid.right).toBeLessThanOrEqual(layout.frame.right + 1)
  expect(layout.grid.bottom).toBeLessThanOrEqual(layout.frame.bottom + 1)
  expect(layout.screen.left).toBeGreaterThanOrEqual(layout.frame.left - 1)
  expect(layout.screen.top).toBeGreaterThanOrEqual(layout.frame.top - 1)
  expect(layout.screen.right).toBeLessThanOrEqual(layout.frame.right + 1)
  expect(layout.screen.bottom).toBeLessThanOrEqual(layout.frame.bottom + 1)

  if (testInfo.project.name !== 'desktop') {
    const mobileLayout = await page.evaluate(() => {
      const view = document.querySelector<HTMLElement>('.terminal-view')!
      const frame = document.querySelector<HTMLElement>('.terminal-frame')!
      const keybarShell = document.querySelector<HTMLElement>('.mobile-keybar-shell')!
      const keybar = document.querySelector<HTMLElement>('.mobile-keybar')!
      const frameBox = frame.getBoundingClientRect()
      const keybarShellBox = keybarShell.getBoundingClientRect()
      const keybarBox = keybar.getBoundingClientRect()
      const visualViewport = window.visualViewport
      return {
        viewOverflow: view.scrollHeight - view.clientHeight,
        frameBottom: frameBox.bottom,
        keybarShellTop: keybarShellBox.top,
        keybarShellBottom: keybarShellBox.bottom,
        keybarShellLeft: keybarShellBox.left,
        keybarShellRight: keybarShellBox.right,
        keybarTop: keybarBox.top,
        keybarBottom: keybarBox.bottom,
        keybarLeft: keybarBox.left,
        keybarRight: keybarBox.right,
        keybarShellPosition: getComputedStyle(keybarShell).position,
        viewportLeft: visualViewport?.offsetLeft ?? 0,
        viewportWidth: visualViewport?.width ?? window.innerWidth,
        viewportHeight: visualViewport?.height ?? window.innerHeight,
      }
    })
    expect(mobileLayout.viewOverflow).toBeLessThanOrEqual(1)
    expect(mobileLayout.frameBottom).toBeLessThanOrEqual(mobileLayout.keybarShellTop + 1)
    expect(mobileLayout.keybarShellBottom).toBeGreaterThanOrEqual(mobileLayout.viewportHeight - 1)
    expect(mobileLayout.keybarShellBottom).toBeLessThanOrEqual(mobileLayout.viewportHeight + 1)
    expect(mobileLayout.keybarShellLeft).toBeGreaterThanOrEqual(mobileLayout.viewportLeft - 1)
    expect(mobileLayout.keybarShellRight).toBeLessThanOrEqual(mobileLayout.viewportLeft + mobileLayout.viewportWidth + 1)
    expect(mobileLayout.keybarLeft).toBeGreaterThanOrEqual(mobileLayout.keybarShellLeft - 1)
    expect(mobileLayout.keybarRight).toBeLessThanOrEqual(mobileLayout.keybarShellRight + 1)
    expect(mobileLayout.keybarTop).toBeGreaterThanOrEqual(mobileLayout.keybarShellTop - 1)
    expect(mobileLayout.keybarBottom).toBeLessThanOrEqual(mobileLayout.keybarShellBottom + 1)
    expect(mobileLayout.keybarShellPosition).toBe('static')

    const keybar = page.locator('.mobile-keybar')
    await keybar.evaluate((element) => { element.scrollLeft = 0 })
    const keybarOverflow = await keybar.evaluate((element) => element.scrollWidth - element.clientWidth)
    const keybarBox = await keybar.boundingBox()
    expect(keybarBox).not.toBeNull()
    const keybarCenter = {
      x: keybarBox!.x + keybarBox!.width / 2,
      y: keybarBox!.y + keybarBox!.height / 2,
    }
    const pageBeforeKeybarDrag = await mobilePageGeometry(page)
    await dispatchTouchSequence(page, [
      { type: 'touchStart', points: [{ id: 20, ...keybarCenter }] },
      { type: 'touchMove', points: [{ id: 20, x: keybarCenter.x, y: keybarCenter.y - 70 }] },
      { type: 'touchEnd', points: [] },
    ])
    const pageAfterVerticalKeybarDrag = await mobilePageGeometry(page)
    expect(pageAfterVerticalKeybarDrag).toEqual(pageBeforeKeybarDrag)

    await dispatchTouchSequence(page, [
      { type: 'touchStart', points: [{ id: 21, x: keybarBox!.x + 20, y: keybarCenter.y }] },
      { type: 'touchMove', points: [{ id: 21, x: keybarBox!.x + keybarBox!.width - 20, y: keybarCenter.y }] },
      { type: 'touchEnd', points: [] },
    ])
    expect(await mobilePageGeometry(page)).toEqual(pageAfterVerticalKeybarDrag)

    await dispatchTouchSequence(page, [
      { type: 'touchStart', points: [{ id: 22, x: keybarBox!.x + keybarBox!.width - 20, y: keybarCenter.y }] },
      { type: 'touchMove', points: [{ id: 22, x: keybarBox!.x + 20, y: keybarCenter.y }] },
      { type: 'touchEnd', points: [] },
    ])
    const pageAfterHorizontalKeybarDrag = await mobilePageGeometry(page)
    const { keybarScrollLeft: beforeScroll, ...fixedBeforeHorizontalDrag } = pageAfterVerticalKeybarDrag
    const { keybarScrollLeft: afterScroll, ...fixedAfterHorizontalDrag } = pageAfterHorizontalKeybarDrag
    expect(beforeScroll).toBe(0)
    expect(afterScroll).toBe(pageAfterHorizontalKeybarDrag.keybarScrollLeft)
    if (keybarOverflow > 1) expect(afterScroll).toBeGreaterThan(0)
    else expect(afterScroll).toBe(0)
    expect(fixedAfterHorizontalDrag).toEqual(fixedBeforeHorizontalDrag)
    expect(pageAfterHorizontalKeybarDrag.keybarShell.bottom)
      .toBeGreaterThanOrEqual((await page.evaluate(() => window.visualViewport?.height ?? window.innerHeight)) - 1)

    await keybar.evaluate((element) => { element.scrollLeft = element.scrollWidth })
    const pageAtRightBoundary = await mobilePageGeometry(page)
    await dispatchTouchSequence(page, [
      { type: 'touchStart', points: [{ id: 23, x: keybarBox!.x + keybarBox!.width - 20, y: keybarCenter.y }] },
      { type: 'touchMove', points: [{ id: 23, x: keybarBox!.x + 20, y: keybarCenter.y }] },
      { type: 'touchEnd', points: [] },
    ])
    expect(await mobilePageGeometry(page)).toEqual(pageAtRightBoundary)

    if (!mobilePanes) mobilePanes = await ensureTwoPanes(page)
    const currentPanes = (await panesForTerm(page)).toSorted((left, right) => left.left - right.left)
    const activePane = currentPanes.find((pane) => pane.active)!
    const inactivePane = currentPanes.find((pane) => pane.pane_id !== activePane.pane_id)!
    const lock = page.locator('[data-action="toggle-viewport-lock"]')
    await lock.click()
    await expect(lock).toHaveAttribute('aria-pressed', 'true')
    await expect(page.locator('.terminal-frame')).toHaveAttribute('data-viewport-lock', 'locked')

    const target = await terminalPoint(
      page,
      Math.floor(inactivePane.left + inactivePane.width / 2),
      Math.floor(inactivePane.top + inactivePane.height / 2),
    )
    const frameStart = terminalFrames.length
    await dispatchTouchSequence(page, [
      { type: 'touchStart', points: [{ id: 1, x: target.x, y: target.y }] },
      { type: 'touchEnd', points: [] },
    ])
    await expect.poll(async () => (await panesForTerm(page)).find((pane) => pane.active)?.pane_id).toBe(inactivePane.pane_id)

    await dispatchTouchSequence(page, [
      { type: 'touchStart', points: [{ id: 2, x: target.x, y: target.y }] },
      { type: 'touchMove', points: [{ id: 2, x: target.x + 20, y: target.y }] },
      { type: 'touchEnd', points: [] },
    ])
    await expect.poll(() => Buffer.concat(terminalFrames.slice(frameStart)).toString('latin1')).toMatch(/\x1b\[<0;\d+;\d+M/)
    await expect.poll(() => Buffer.concat(terminalFrames.slice(frameStart)).toString('latin1')).toMatch(/\x1b\[<32;\d+;\d+M/)
    await expect.poll(() => Buffer.concat(terminalFrames.slice(frameStart)).toString('latin1')).toMatch(/\x1b\[<0;\d+;\d+m/)

    const wordSuffix = Date.now().toString(36).slice(-4)
    const firstWord = `LEFTM${wordSuffix}`
    const secondWord = `RIGHTM${wordSuffix}`
    await page.locator('.terminal-host textarea').focus()
    await page.keyboard.type(String.raw`printf '\033[2J\033[H${firstWord}    ${secondWord}'`)
    await page.keyboard.press('Enter')
    await expect(page.locator('.terminal-host')).toContainText(secondWord)
    const currentActivePane = (await panesForTerm(page)).find((pane) => pane.active)!
    const wordStart = await terminalPoint(page, currentActivePane.left + firstWord.length + 4, currentActivePane.top)
    const wordEnd = await terminalPoint(page, currentActivePane.left + firstWord.length + 4 + secondWord.length - 1, currentActivePane.top)
    const selectionFrameStart = terminalFrames.length
    await dispatchTouchSequence(page, [
      { type: 'touchStart', points: [{ id: 3, x: wordStart.x, y: wordStart.y }], delayMs: 550 },
      { type: 'touchMove', points: [{ id: 3, x: wordEnd.x, y: wordEnd.y }] },
      { type: 'touchEnd', points: [] },
    ])
    const copied = await page.locator('.xterm').evaluate((element) => {
      const clipboard = new DataTransfer()
      element.dispatchEvent(new ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: clipboard }))
      return clipboard.getData('text/plain')
    })
    expect(copied).toBe(secondWord)
    expect(terminalFrames).toHaveLength(selectionFrameStart)
  }

  if (testInfo.project.name === 'desktop') {
    const [, rightPane] = await selectLeftPaneWithKeyboard(page)
    const target = await clickPaneCenter(page, rightPane)
    await expect.poll(async () => (await panesForTerm(page)).find((pane) => pane.active)?.pane_id).toBe(rightPane.pane_id)

    const frame = page.locator('.terminal-frame')
    const scrollBefore = await frame.evaluate((element) => ({ left: element.scrollLeft, top: element.scrollTop }))
    const frameStart = terminalFrames.length
    await page.mouse.move(target.x, target.y)
    await page.mouse.wheel(0, -120)
    await expect.poll(() => {
      for (const payload of terminalFrames.slice(frameStart)) {
        const match = /\x1b\[<(?:64|65);(\d+);(\d+)M/.exec(payload.toString('latin1'))
        if (match) return { col: Number(match[1]), row: Number(match[2]) }
      }
      return null
    }).toEqual({ col: target.col, row: target.row })
    expect(await frame.evaluate((element) => ({ left: element.scrollLeft, top: element.scrollTop }))).toEqual(scrollBefore)

    await expectWordSelection(page, terminalOutputFrames, rightPane, 'FIT', true)
  }

  if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/${testInfo.project.name}.png` })
})
