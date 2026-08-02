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

test('uses env-authoritative relay settings and the complete authenticator lifecycle', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'The stateful security trajectory runs once on desktop')
  test.setTimeout(90_000)
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })

  await loginWithAdminToken(page)
  await page.locator('.side-nav a[href="/settings"]').click()
  await expect(page.getByRole('heading', { name: '设置', level: 1 })).toBeVisible()
  await expect(page.getByText('主题在客户端本地保存；认证和客户端授权由当前 B 管理。')).toHaveCount(0)

  const appearance = page.locator('section.settings-panel').filter({ has: page.getByRole('heading', { name: '界面主题' }) })
  const themeOptions = appearance.getByRole('radio')
  await expect(themeOptions).toHaveCount(3)
  const widths = await themeOptions.evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().width))
  expect(Math.max(...widths) - Math.min(...widths)).toBeLessThan(1)
  await appearance.getByRole('radio', { name: '云端钴蓝' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'cloud-cobalt')

  const server = page.locator('section.settings-panel').filter({ has: page.getByRole('heading', { name: '中继服务器' }) })
  await expect(server.locator('.eyebrow')).toHaveText('Server')
  await expect(server.getByRole('heading', { name: '服务网址' })).toBeVisible()
  await expect(server.locator('[data-server-issuer]')).toHaveText(baseUrl)
  await server.getByRole('button', { name: '显示服务网址二维码' }).click()
  const qrDialog = page.getByRole('dialog', { name: '服务网址二维码' })
  const qrImage = qrDialog.getByRole('img', { name: '服务网址二维码' })
  await expect(qrImage).toBeVisible()
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
  await expect(page.locator('[data-guide-step]')).toHaveCount(5)
  await page.getByLabel('管理员 Token').fill(adminToken)
  await page.locator('[data-action="begin-totp-setup"]').getByRole('button', { name: '继续' }).click()
  const setupKey = (await page.locator('[data-setup-key]').textContent())?.trim() ?? ''
  expect(setupKey).not.toBe('')
  await expect(page.getByRole('img', { name: '验证器设置二维码' })).toBeVisible()
  if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/settings-totp-setup.png`, fullPage: true })

  const setupCounter = currentCounter() - 1
  await page.getByLabel('验证器生成的第一个 6 位验证码').fill(totpForCounter(setupKey, setupCounter))
  await page.getByRole('button', { name: '确认绑定' }).click()
  await expect(page.getByRole('heading', { name: '验证器已绑定' })).toBeVisible()
  const protectionSwitch = page.getByRole('switch', { name: '启用双重认证登录' })
  await expect(protectionSwitch).toHaveAttribute('aria-checked', 'false')

  const enableCounter = currentCounter()
  await protectionSwitch.click()
  const enableDialog = page.getByRole('dialog', { name: '启用双重认证登录' })
  await enableDialog.getByLabel('管理员 Token').fill(adminToken)
  await enableDialog.getByLabel('当前验证码').fill(totpForCounter(setupKey, enableCounter))
  await enableDialog.getByRole('button', { name: '确认', exact: true }).click()
  await expect(protectionSwitch).toHaveAttribute('aria-checked', 'true')
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
