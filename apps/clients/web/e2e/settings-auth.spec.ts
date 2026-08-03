import { createHmac } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

const adminToken = process.env.TERMFLOW_E2E_ADMIN_TOKEN ?? ''
const baseUrl = process.env.TERMFLOW_E2E_BASE_URL ?? ''
const screenshotDir = process.env.TERMFLOW_E2E_SCREENSHOT_DIR ?? ''

test.skip(!adminToken || !baseUrl, 'TermFlow isolated browser fixture variables are required')

function decodeBase32(value: string): Buffer {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
  let bits = ''
  for (const character of value.toUpperCase().replaceAll('=', '')) {
    const index = alphabet.indexOf(character)
    if (index < 0) throw new Error('Invalid base32 setup key')
    bits += index.toString(2).padStart(5, '0')
  }
  const bytes: number[] = []
  for (let offset = 0; offset + 8 <= bits.length; offset += 8) {
    bytes.push(Number.parseInt(bits.slice(offset, offset + 8), 2))
  }
  return Buffer.from(bytes)
}

function totpForCounter(setupKey: string, counter: number): string {
  const counterBytes = Buffer.alloc(8)
  counterBytes.writeBigUInt64BE(BigInt(counter))
  const digest = createHmac('sha1', decodeBase32(setupKey)).update(counterBytes).digest()
  const offset = digest[digest.length - 1]! & 0x0f
  const value = (digest.readUInt32BE(offset) & 0x7fff_ffff) % 1_000_000
  return value.toString().padStart(6, '0')
}

function currentCounter(): number {
  return Math.floor(Date.now() / 30_000)
}

async function waitForCounterAfter(counter: number): Promise<number> {
  await expect.poll(currentCounter, { timeout: 35_000, intervals: [200, 500, 1_000] }).toBeGreaterThan(counter)
  return currentCounter()
}

async function loginWithAdminToken(page: Page) {
  await page.goto('/login')
  await page.getByLabel('管理员令牌').fill(adminToken)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '控制中心' })).toBeVisible()
}

async function expectOnlyMainContentScrolls(page: Page) {
  const geometry = await page.evaluate(async () => {
    const main = document.querySelector<HTMLElement>('main')!
    const header = document.querySelector<HTMLElement>('.app-header')!
    const navigation = document.querySelector<HTMLElement>('.side-nav')!
    const before = {
      beforeHeaderTop: header.getBoundingClientRect().top,
      beforeNavigationTop: navigation.getBoundingClientRect().top,
    }
    const spacer = document.createElement('div')
    spacer.dataset.e2eScrollSpacer = 'true'
    spacer.style.height = '200vh'
    spacer.style.flex = '0 0 auto'
    main.append(spacer)
    main.scrollTop = 160
    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
    const result = {
      windowY: window.scrollY,
      htmlTop: document.documentElement.scrollTop,
      bodyTop: document.body.scrollTop,
      documentHeight: document.documentElement.scrollHeight,
      viewportHeight: document.documentElement.clientHeight,
      mainOverflow: getComputedStyle(main).overflowY,
      mainTop: main.scrollTop,
      headerTop: header.getBoundingClientRect().top,
      navigationTop: navigation.getBoundingClientRect().top,
      ...before,
    }
    spacer.remove()
    main.scrollTop = 0
    return result
  })

  expect(geometry.windowY).toBe(0)
  expect(geometry.htmlTop).toBe(0)
  expect(geometry.bodyTop).toBe(0)
  expect(geometry.documentHeight).toBeLessThanOrEqual(geometry.viewportHeight + 1)
  expect(geometry.mainOverflow).toBe('auto')
  expect(geometry.mainTop).toBeGreaterThan(0)
  expect(geometry.headerTop).toBeCloseTo(geometry.beforeHeaderTop, 1)
  expect(geometry.navigationTop).toBeCloseTo(geometry.beforeNavigationTop, 1)
}

test('uses env-authoritative relay settings and the complete authenticator lifecycle', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'The stateful security trajectory runs once on desktop')
  test.setTimeout(90_000)
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })

  await loginWithAdminToken(page)
  await expectOnlyMainContentScrolls(page)
  await page.locator('.side-nav a[href="/settings"]').click()
  await expect(page.getByRole('heading', { name: '设置', level: 1 })).toBeVisible()
  await expect(page.getByText('主题在客户端本地保存；认证和客户端授权由当前 B 管理。')).toHaveCount(0)

  const appearance = page.locator('section.settings-panel').filter({ has: page.getByRole('heading', { name: '界面主题' }) })
  const themeOptions = appearance.getByRole('radio')
  await expect(themeOptions).toHaveCount(3)
  const widths = await themeOptions.evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().width))
  expect(Math.max(...widths) - Math.min(...widths)).toBeLessThan(1)
  const alignments = await themeOptions.evaluateAll((elements) => elements.map((element) => getComputedStyle(element).justifyContent))
  expect(alignments).toEqual(['center', 'center', 'center'])
  await appearance.getByRole('radio', { name: '云端钴蓝' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'cloud-cobalt')

  const server = page.locator('section.settings-panel').filter({ has: page.getByRole('heading', { name: '中继服务器' }) })
  await expect(server.locator('.eyebrow')).toHaveText('Server')
  await expect(server.locator('[data-server-label]')).toHaveText('服务网址')
  await expect(server.locator('[data-server-issuer]')).toHaveText(baseUrl)
  const serverFieldGeometry = await server.locator('[data-server-field]').evaluate((field) => {
    const label = field.querySelector<HTMLElement>('[data-server-label]')!
    const value = field.querySelector<HTMLElement>('.server-address-row')!
    const labelBox = label.getBoundingClientRect()
    const valueBox = value.getBoundingClientRect()
    return {
      labelFontSize: Number.parseFloat(getComputedStyle(label).fontSize),
      labelToValueGap: valueBox.top - labelBox.bottom,
    }
  })
  expect(serverFieldGeometry.labelFontSize).toBeLessThanOrEqual(16)
  expect(serverFieldGeometry.labelToValueGap).toBeGreaterThanOrEqual(4)
  expect(serverFieldGeometry.labelToValueGap).toBeLessThanOrEqual(12)
  await server.getByRole('button', { name: '显示服务网址二维码' }).click()
  const qrDialog = page.getByRole('dialog', { name: '服务网址二维码' })
  const qrImage = qrDialog.getByRole('img', { name: '服务网址二维码' })
  await expect(qrImage).toBeVisible()
  const qrDialogSpacing = await qrDialog.evaluate((dialog) => {
    const header = dialog.querySelector('header')!.getBoundingClientRect()
    const image = dialog.querySelector('img')!.getBoundingClientRect()
    return { headingToImage: image.top - header.bottom }
  })
  expect(qrDialogSpacing.headingToImage).toBeGreaterThanOrEqual(12)
  await expect(qrDialog.locator('p')).toHaveCount(0)
  const qrEvidence = await qrImage.evaluate((image) => {
    const styles = getComputedStyle(document.documentElement)
    const source = (image as HTMLImageElement).src
    return {
      foreground: styles.getPropertyValue('--color-qr-foreground').trim(),
      background: styles.getPropertyValue('--color-qr-background').trim(),
      svg: decodeURIComponent(source.slice(source.indexOf(',') + 1)),
    }
  })
  expect(qrEvidence.svg).toContain(qrEvidence.foreground)
  expect(qrEvidence.svg).toContain(qrEvidence.background)
  if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/settings-server-qr.png`, fullPage: true })
  await qrDialog.getByRole('button', { name: '关闭二维码' }).click()

  await page.locator('.side-nav a[href="/computers"]').click()
  await page.getByRole('button', { name: '添加电脑' }).click()
  const enrollmentDialog = page.getByRole('dialog', { name: '添加电脑' })
  await enrollmentDialog.getByLabel('电脑名称').fill('设置流程测试电脑')
  const enrollmentCreated = page.waitForResponse((response) =>
    response.request().method() === 'POST'
    && response.url().endsWith('/api/v1/enrollment-tokens')
    && response.ok(),
  )
  await enrollmentDialog.getByRole('button', { name: '创建', exact: true }).click()
  const enrollment = await (await enrollmentCreated).json() as { server_url: string; login_command: string }
  expect(enrollment.server_url).toBe(baseUrl)
  expect(enrollment.login_command).toContain(`termflow login --server ${baseUrl} --code `)
  await expect(enrollmentDialog.locator('.login-command')).toHaveText(enrollment.login_command)
  await enrollmentDialog.getByRole('button', { name: '关闭' }).click()

  await page.locator('.side-nav a[href="/settings"]').click()
  await page.getByRole('button', { name: '激活双重因素认证' }).click()
  await expect(page).toHaveURL(/\/settings\/two-factor-auth$/)
  const activationSteps = page.locator('[data-guide-step]')
  await expect(activationSteps).toHaveCount(3)
  await expect(activationSteps.nth(0)).toHaveAttribute('data-state', 'current')
  await expect(activationSteps.nth(0)).toHaveAttribute('aria-current', 'step')
  await expect(page.getByText('管理员 Token 只用于本次验证，不会保存在客户端。')).toHaveCount(0)
  await expect(page.getByText(/使用你的验证器 App 完成绑定/)).toHaveCount(0)
  const activationGeometry = await page.evaluate(() => {
    const card = document.querySelector<HTMLElement>('.totp-guide-card')!.getBoundingClientRect()
    const form = document.querySelector<HTMLElement>('[data-action="begin-totp-setup"]')!.getBoundingClientRect()
    const back = document.querySelector<HTMLAnchorElement>('a.secondary-button')!.getBoundingClientRect()
    const next = document.querySelector<HTMLButtonElement>('[data-action="begin-totp-setup"] button')!.getBoundingClientRect()
    return {
      centerDelta: Math.abs((form.left + form.width / 2) - (card.left + card.width / 2)),
      backHeight: back.height,
      nextHeight: next.height,
    }
  })
  expect(activationGeometry.centerDelta).toBeLessThan(2)
  expect(activationGeometry.backHeight).toBeGreaterThanOrEqual(44)
  expect(activationGeometry.nextHeight).toBeGreaterThanOrEqual(44)
  await page.getByLabel('管理员 Token').fill(adminToken)
  await page.locator('[data-action="begin-totp-setup"]').getByRole('button', { name: '继续' }).click()
  await expect(activationSteps.nth(0)).toHaveAttribute('data-state', 'complete')
  await expect(activationSteps.nth(1)).toHaveAttribute('data-state', 'current')
  await expect(activationSteps.nth(1)).toHaveAttribute('aria-current', 'step')
  await expect(page.locator('[data-wizard-card-title]')).toHaveText('绑定验证器')
  await expect(page.locator('[data-wizard-progress]')).toHaveText('第 2 步，共 3 步')
  const setupQr = page.getByRole('img', { name: '验证器设置二维码' })
  await expect(setupQr).toBeVisible()
  const bindingGeometry = await page.evaluate(() => {
    const card = document.querySelector<HTMLElement>('.totp-guide-card')!.getBoundingClientRect()
    const layout = document.querySelector<HTMLElement>('[data-totp-bind-layout]')!.getBoundingClientRect()
    const qr = document.querySelector<HTMLElement>('.themed-qr-code')!.getBoundingClientRect()
    return {
      centerDelta: Math.abs((layout.left + layout.width / 2) - (card.left + card.width / 2)),
      qrInset: qr.left - card.left,
    }
  })
  expect(bindingGeometry.centerDelta).toBeLessThan(2)
  expect(bindingGeometry.qrInset).toBeGreaterThan(96)
  const setupQrEvidence = await setupQr.evaluate((image) => {
    const styles = getComputedStyle(document.documentElement)
    const source = (image as HTMLImageElement).src
    return {
      foreground: styles.getPropertyValue('--color-qr-foreground').trim(),
      background: styles.getPropertyValue('--color-qr-background').trim(),
      svg: decodeURIComponent(source.slice(source.indexOf(',') + 1)),
    }
  })
  expect(setupQrEvidence.svg).toContain(setupQrEvidence.foreground)
  expect(setupQrEvidence.svg).toContain(setupQrEvidence.background)
  if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/settings-totp-setup.png`, fullPage: true })
  const setupKeyDisclosure = page.getByRole('button', { name: '无法扫描？使用设置密钥' })
  await expect(setupKeyDisclosure).toHaveAttribute('aria-expanded', 'false')
  await expect(page.locator('[data-setup-key]')).toHaveCount(0)
  const setupLayoutBefore = await page.evaluate(() => {
    const qr = document.querySelector<HTMLElement>('.themed-qr-code')!.getBoundingClientRect()
    const form = document.querySelector<HTMLElement>('.totp-confirm-form')!.getBoundingClientRect()
    return {
      qr: { x: qr.x, y: qr.y, width: qr.width, height: qr.height },
      form: { x: form.x, y: form.y, width: form.width, height: form.height },
    }
  })
  await setupKeyDisclosure.click()
  await expect(setupKeyDisclosure).toHaveAttribute('aria-expanded', 'true')
  const setupKeyDialog = page.getByRole('dialog', { name: '设置密钥' })
  await expect(setupKeyDialog).toBeVisible()
  const setupKeyGeometry = await setupKeyDialog.evaluate((dialog) => {
    const box = dialog.getBoundingClientRect()
    const trigger = document.querySelector<HTMLElement>('[data-action="toggle-setup-key"]')!.getBoundingClientRect()
    return {
      left: box.left,
      right: box.right,
      top: box.top,
      triggerBottom: trigger.bottom,
      viewportWidth: window.innerWidth,
    }
  })
  expect(setupKeyGeometry.left).toBeGreaterThanOrEqual(0)
  expect(setupKeyGeometry.right).toBeLessThanOrEqual(setupKeyGeometry.viewportWidth)
  expect(setupKeyGeometry.top).toBeGreaterThanOrEqual(setupKeyGeometry.triggerBottom)
  const setupLayoutAfter = await page.evaluate(() => {
    const qr = document.querySelector<HTMLElement>('.themed-qr-code')!.getBoundingClientRect()
    const form = document.querySelector<HTMLElement>('.totp-confirm-form')!.getBoundingClientRect()
    return {
      qr: { x: qr.x, y: qr.y, width: qr.width, height: qr.height },
      form: { x: form.x, y: form.y, width: form.width, height: form.height },
    }
  })
  for (const area of ['qr', 'form'] as const) {
    for (const dimension of ['x', 'y', 'width', 'height'] as const) {
      expect(setupLayoutAfter[area][dimension]).toBeCloseTo(setupLayoutBefore[area][dimension], 0)
    }
  }
  if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/settings-totp-key-popover.png`, fullPage: true })
  const setupKey = (await setupKeyDialog.locator('[data-setup-key]').textContent())?.trim() ?? ''
  expect(setupKey).not.toBe('')
  await setupKeyDialog.press('Escape')
  await expect(setupKeyDialog).toHaveCount(0)
  await expect(setupKeyDisclosure).toBeFocused()

  await page.setViewportSize({ width: 390, height: 844 })
  await setupKeyDisclosure.click()
  await expect(setupKeyDialog).toBeVisible()
  const narrowSetupKeyGeometry = await setupKeyDialog.evaluate((dialog) => {
    const box = dialog.getBoundingClientRect()
    return { left: box.left, right: box.right, viewportWidth: window.innerWidth }
  })
  expect(narrowSetupKeyGeometry.left).toBeGreaterThanOrEqual(0)
  expect(narrowSetupKeyGeometry.right).toBeLessThanOrEqual(narrowSetupKeyGeometry.viewportWidth)
  await setupKeyDialog.press('Escape')
  await page.setViewportSize({ width: 1440, height: 900 })

  const setupCounter = currentCounter() - 1
  await page.getByLabel('验证器验证码').fill(totpForCounter(setupKey, setupCounter))
  await page.getByRole('button', { name: '确认绑定' }).click()
  await expect(page.getByText('验证器已绑定', { exact: true })).toBeVisible()
  await expect(activationSteps.nth(0)).toHaveAttribute('data-state', 'complete')
  await expect(activationSteps.nth(1)).toHaveAttribute('data-state', 'complete')
  await expect(activationSteps.nth(2)).toHaveAttribute('data-state', 'current')
  const configuredHeading = page.locator('[data-configured-authenticator-heading]')
  await expect(configuredHeading.getByRole('button', { name: '重新配置' })).toBeVisible()
  const protectionSwitch = page.getByRole('switch', { name: '启用双重认证登录' })
  await expect(protectionSwitch).toHaveAttribute('aria-checked', 'false')
  const protectionLabel = page.locator('[data-totp-protection-label]')
  await expect(protectionLabel.locator('strong')).toHaveText('启用双重认证登录')
  const helpButton = protectionLabel.getByRole('button', { name: '说明启用双重认证登录' })
  await expect(helpButton.locator('svg')).toHaveCount(1)
  await helpButton.focus()
  await expect(protectionLabel.getByRole('tooltip')).toBeVisible()
  const labelGeometry = await protectionLabel.evaluate((label) => {
    const title = label.querySelector('strong')!.getBoundingClientRect()
    const help = label.querySelector('button')!.getBoundingClientRect()
    return { titleTop: title.top, titleBottom: title.bottom, helpTop: help.top, helpBottom: help.bottom }
  })
  expect(labelGeometry.helpTop).toBeLessThan(labelGeometry.titleBottom)
  expect(labelGeometry.helpBottom).toBeGreaterThan(labelGeometry.titleTop)
  const tooltipGeometry = await protectionLabel.locator('xpath=..').evaluate((row) => {
    const rowBox = row.getBoundingClientRect()
    const tooltipBox = row.querySelector<HTMLElement>('[role="tooltip"]')!.getBoundingClientRect()
    return { rowLeft: rowBox.left, rowRight: rowBox.right, tooltipLeft: tooltipBox.left, tooltipRight: tooltipBox.right }
  })
  expect(tooltipGeometry.tooltipLeft).toBeGreaterThanOrEqual(tooltipGeometry.rowLeft)
  expect(tooltipGeometry.tooltipRight).toBeLessThanOrEqual(tooltipGeometry.rowRight)

  const enableCounter = currentCounter()
  await protectionSwitch.click()
  const enableDialog = page.getByRole('dialog', { name: '启用双重认证登录' })
  await enableDialog.getByLabel('管理员 Token').fill(adminToken)
  await enableDialog.getByLabel('当前验证码').fill(totpForCounter(setupKey, enableCounter))
  await enableDialog.getByRole('button', { name: '确认', exact: true }).click()
  await expect(protectionSwitch).toHaveAttribute('aria-checked', 'true')
  await expect(page.locator('[data-guide-step][data-state="complete"]')).toHaveCount(3)
  await expect(page.locator('[data-guide-step][aria-current="step"]')).toHaveCount(0)
  if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/settings-totp-enabled.png`, fullPage: true })

  await page.locator('[data-action="logout"]').click()
  const loginCounter = await waitForCounterAfter(enableCounter)
  await page.getByLabel('管理员令牌').fill(adminToken)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByLabel('双重验证码')).toBeVisible()
  await page.getByLabel('双重验证码').fill(totpForCounter(setupKey, loginCounter))
  await page.getByRole('button', { name: '验证并登录' }).click()
  await expect(page.getByRole('heading', { name: '控制中心' })).toBeVisible()

  await page.locator('.side-nav a[href="/settings"]').click()
  const authenticatorActions = page.locator('[data-authenticator-actions]')
  await expect(authenticatorActions.getByText('验证器已绑定', { exact: true })).toBeVisible()
  await expect(authenticatorActions.getByRole('button', { name: '重新配置' })).toBeVisible()
  const persistedSwitch = page.getByRole('switch', { name: '启用双重认证登录' })
  await expect(persistedSwitch).toHaveAttribute('aria-checked', 'true')
  await persistedSwitch.click()
  const disableDialog = page.getByRole('dialog', { name: '停用双重认证登录' })
  await disableDialog.getByLabel('管理员 Token').fill(adminToken)
  await disableDialog.getByLabel('当前验证码').fill(totpForCounter(setupKey, loginCounter + 1))
  await disableDialog.getByRole('button', { name: '确认', exact: true }).click()
  await expect(persistedSwitch).toHaveAttribute('aria-checked', 'false')
  await page.reload()
  await expect(page.getByText('验证器已绑定', { exact: true })).toBeVisible()
  await expect(page.getByRole('switch', { name: '启用双重认证登录' })).toHaveAttribute('aria-checked', 'false')
  if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/settings-configured-disabled.png`, fullPage: true })

  expect(consoleErrors).toEqual([])
})
