import { expect, test, type Page } from '@playwright/test'

const adminToken = process.env.TERMFLOW_E2E_ADMIN_TOKEN ?? ''
const baseUrl = process.env.TERMFLOW_E2E_BASE_URL ?? ''
const termId = process.env.TERMFLOW_E2E_TERM_ID ?? ''

test.skip(
  !adminToken || !baseUrl || !termId,
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

test('smokes the Agent chat: capability gate, conversation creation, fail-closed composer', async ({ page }, testInfo) => {
  const consoleErrors: string[] = []
  const pageErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await login(page)

  // The smoke branches on the unauthenticated capability endpoint so a
  // fixture with the broker disabled exercises the hidden navigation and
  // the placeholder route instead of the management flow.
  const capabilitiesResponse = await page.request.get('/api/v1/agent/capabilities')
  expect(capabilitiesResponse.ok()).toBe(true)
  const capabilities = await capabilitiesResponse.json() as { agent_broker_enabled: boolean }

  if (!capabilities.agent_broker_enabled) {
    await expect(page.locator('.side-nav a[href="/agent"]')).toHaveCount(0)
    await expect(page.locator('.mobile-nav a[href="/agent"]')).toHaveCount(0)
    await page.goto('/agent')
    await expect(page.getByRole('heading', { name: 'Agent 控制台' })).toBeVisible()
    await expect(page.locator('[data-agent-disabled]')).toContainText('Agent Broker 未启用')
    expect(consoleErrors).toEqual([])
    expect(pageErrors).toEqual([])
    return
  }

  // Capability open: the Agent entry renders in both navigation landmarks
  // (role selectors only match the visible one, so `.first()` targets the
  // active navigation for the current viewport project).
  await expect(page.locator('.side-nav a[href="/agent"]')).toHaveCount(1)
  await expect(page.locator('.mobile-nav a[href="/agent"]')).toHaveCount(1)
  await page.getByRole('link', { name: 'Agent 控制台' }).first().click()
  await expect(page.getByRole('heading', { name: 'Agent 控制台' })).toBeVisible()
  await expect(page.locator('[data-agent-disabled]')).toHaveCount(0)
  // Spec smoke gate: /agent renders without console errors.
  expect(consoleErrors).toEqual([])

  // Fixture a profile + binding over the admin API. The binding's runtime is
  // never activated, so the composer must fail closed on submit (spec §10
  // risk 6 / M4.5 fail-closed pipeline mapping).
  const profile = await page.request.post('/api/v1/agent/admin/profiles', {
    headers: { Authorization: `Bearer ${adminToken}` },
    data: { display_name: `browser-profile-${testInfo.project.name}`, backend_kind: 'opencode', config: '{"model": "default"}' },
  })
  expect(profile.status()).toBe(201)
  const profileId = (await profile.json() as { profile_id: string }).profile_id
  const binding = await page.request.post('/api/v1/agent/admin/bindings', {
    headers: { Authorization: `Bearer ${adminToken}` },
    data: { profile_id: profileId, term_id: termId },
  })
  expect(binding.status()).toBe(201)
  const bindingId = (await binding.json() as { binding_id: string }).binding_id

  await page.reload()
  const bindingOption = page.locator(`[data-agent-binding-id="${bindingId}"]`)
  await expect(bindingOption).toBeVisible()
  await bindingOption.click()

  await page.locator('[data-agent-create-title]').fill(`smoke-${testInfo.project.name}`)
  const created = page.waitForResponse((response) =>
    response.request().method() === 'POST'
    && response.url().endsWith('/api/v1/agent/conversations')
    && response.status() === 201,
  )
  await page.locator('[data-action="create-conversation"]').click()
  const createdResponse = await created
  const conversationId = (await createdResponse.json() as { conversation_id: string }).conversation_id

  const conversationRow = page.locator(`[data-agent-conversation-id="${conversationId}"]`)
  await expect(conversationRow).toBeVisible()
  await conversationRow.locator('.agent-conversation-row__open').click()
  await expect(page).toHaveURL(new RegExp(`/agent/${conversationId}$`))

  // Without a runtime the composer is reachable, but submitting greys it
  // out with the unavailable hint (503 binding_runtime_unavailable).
  await expect(page.locator('[data-agent-composer-input]')).toBeEnabled()
  await page.locator('[data-agent-composer-input]').fill('烟雾测试')
  const rejected = page.waitForResponse((response) =>
    response.request().method() === 'POST'
    && response.url().endsWith(`/api/v1/agent/conversations/${conversationId}/messages`)
    && response.status() === 503,
  )
  await page.locator('[data-action="send-message"]').click()
  await rejected
  await expect(page.locator('[data-agent-composer-unavailable]')).toContainText('后端运行时未就绪')
  await expect(page.locator('[data-agent-composer-input]')).toBeDisabled()

  // The deliberate 503 surfaces as a browser resource-load console message,
  // so only runtime exceptions are asserted at the end of the flow.
  expect(pageErrors).toEqual([])
})
