import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * Deterministic real-browser UI contract. Control Plane, provider, and
 * terminal responses are isolated at the browser boundary, so this test is
 * not evidence of a live B/OpenCode/provider deployment.
 */
const TERM_ID = 'term-e2e-0001'
const PROFILE_ID = '11111111-1111-4111-8111-111111111111'
const BINDING_ID = '22222222-2222-4222-8222-222222222222'
const CONVERSATION_ID = '33333333-3333-4333-8333-333333333333'
const APPROVAL_ID = '44444444-4444-4444-8444-444444444444'
const RUN_ID = '55555555-5555-4555-8555-555555555555'
const MESSAGE_ID = '66666666-6666-4666-8666-666666666666'
const TERMINAL_ID = '77777777-7777-4777-8777-777777777777'
const STREAM_ID = '88888888-8888-4888-8888-888888888888'
const ADMIN_CREDENTIAL = 'fixture-root-credential-private'
const PROVIDER_CREDENTIAL = 'fixture-provider-credential-private'
const MCP_CREDENTIAL = 'fixture-mcp-credential-private'
const PRIVATE_PROMPT = '请在当前允许的终端中执行 echo 1，并告诉我结果。'
const DISCLOSURE_TERMS = 'fixture-only 完整保留条款，不得写入浏览器持久化表面'

type SetupState = 'unconfigured' | 'activating' | 'ready'

const profile = {
  profile_id: PROFILE_ID,
  display_name: 'Terminal Agent Profile',
  backend_kind: 'opencode',
  provider_id: 'fixture-provider',
  model_id: 'fixture-model',
}

function disclosure(configured: boolean) {
  return {
    binding_id: configured ? BINDING_ID : null,
    disclosure_fingerprint: 'd'.repeat(64),
    provider_id: 'fixture-provider',
    model_id: 'fixture-model',
    endpoint_origin: 'https://provider.invalid',
    region: 'fixture-region',
    retention_terms: DISCLOSURE_TERMS,
    retention_version: 'fixture-retention-v1',
    no_training: true,
    policy_version: 'fixture-policy-v1',
    credential_source: 'FIXTURE_PROVIDER_API_KEY',
    accepted: configured,
    accepted_at: configured ? '2030-01-01T00:00:00Z' : null,
    accepted_auth_epoch: configured ? 1 : null,
  }
}

function setupResponse(state: SetupState) {
  const configured = state !== 'unconfigured'
  return {
    state,
    term_id: TERM_ID,
    binding_id: configured ? BINDING_ID : null,
    profile: configured ? profile : null,
    profiles: configured ? [profile] : [],
    token: { installed: configured, expires_at: null },
    runtime: configured
      ? {
          readiness: state === 'ready' ? 'ready' : 'starting',
          reason_code: null,
          config_revision: 1,
          applied_revision: state === 'ready' ? 1 : null,
          runtime_epoch: 1,
          provider_readiness: 'configured_unverified',
          observed_runtime_ref: state === 'ready' ? 'fixture-runtime' : null,
          observed_capability_ref: state === 'ready' ? 'fixture-capability' : null,
          provider_reason_code: null,
        }
      : null,
    pane_policy: configured
      ? { binding_id: BINDING_ID, pane_ids: ['%0'], topology_revision: 7 }
      : null,
    disclosure: disclosure(configured),
    topology_revision: 7,
    reason_code: null,
  }
}

const conversation = {
  conversation_id: CONVERSATION_ID,
  binding_id: BINDING_ID,
  title: 'Agent echo 1',
  status: 'active',
  created_at: '2030-01-01T00:00:00Z',
  updated_at: '2030-01-01T00:00:00Z',
}

const pendingApproval = {
  approval_id: APPROVAL_ID,
  binding_id: BINDING_ID,
  conversation_id: CONVERSATION_ID,
  run_id: RUN_ID,
  tool_call_id: 'fixture-tool-call-1',
  canonical_hash: 'a'.repeat(64),
  state: 'pending',
  expires_at: '2030-01-01T00:05:00Z',
  decided_at: null,
  decision: null,
  auth_epoch: 1,
  created_at: '2030-01-01T00:00:00Z',
  pane_id: '%0',
  operation: 'pane.send_keys',
  intent_summary: '在允许的 %0 窗格执行一条受审批命令',
}

function topology() {
  return {
    instance_id: TERM_ID,
    topology: {
      session_id: '$0',
      session_name: 'Mock terminal',
      revision: 7,
      windows: [
        {
          window_id: '@0',
          index: 0,
          name: 'main',
          active: true,
          panes: [
            {
              pane_id: '%0',
              window_id: '@0',
              index: 0,
              title: 'allowed-shell',
              width: 60,
              height: 30,
              left: 0,
              top: 0,
              current_command: 'bash',
              active: true,
              dead: false,
            },
            {
              pane_id: '%1',
              window_id: '@0',
              index: 1,
              title: 'not-selected',
              width: 60,
              height: 30,
              left: 60,
              top: 0,
              current_command: 'bash',
              active: false,
              dead: false,
            },
          ],
        },
      ],
    },
  }
}

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

function agentFrame(cursor: string, event: Record<string, unknown>): string {
  return `event: agent_event\ndata: ${JSON.stringify({ cursor, event })}\n\n`
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

function error(code: string, message: string) {
  return { error: { code, message, request_id: 'fixture-request-id' } }
}

async function mockProductFlow(page: Page, options: { seedConversation?: boolean } = {}) {
  const permissionReady = deferred()
  const completionReady = deferred()
  const traffic = {
    terminalConnections: 0,
    agentStreamConnections: 0,
    browserLogins: 0,
    setupRequests: [] as Array<Record<string, unknown>>,
    conversationCreates: 0,
    approvalDecisionAttempts: 0,
    approvalDecisionSuccesses: 0,
    terminalOutputFrames: [] as Buffer[],
  }
  let authenticated = false
  let setupState: SetupState = 'unconfigured'
  let activatingReadsRemaining = 0
  let conversationCreated = options.seedConversation === true
  let approvalVisible = false
  let sendTerminalOutput: (() => void) | null = null

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname

    if (path === '/api/v1/admin/session' && request.method() === 'GET') {
      return json(route, {
        authenticated,
        expires_at: '2030-01-01T01:00:00Z',
      })
    }
    if (path === '/api/v1/admin/sessions' && request.method() === 'POST') {
      const body = request.postDataJSON() as { admin_token?: string }
      if (body.admin_token !== ADMIN_CREDENTIAL) {
        return json(route, error('authentication_failed', 'Invalid credential.'), 401)
      }
      authenticated = true
      traffic.browserLogins += 1
      return json(
        route,
        { authenticated: true, expires_at: '2030-01-01T01:00:00Z' },
        201,
      )
    }
    if (path === '/api/v1/dashboard') {
      return json(route, {
        metrics: {
          online_terms: 1,
          total_terms: 1,
          active_panes: 2,
          interactions_24h: 0,
          computers: 1,
        },
        computers: [
          {
            installation_id: 'computer-e2e-0001',
            hostname: 'mock-host',
            display_name: 'Mock Computer',
            platform: 'linux',
            client_version: '0.2.0',
            registered_at: '2030-01-01T00:00:00Z',
            last_seen_at: '2030-01-01T00:00:00Z',
            online: true,
            terms: [
              {
                instance_id: TERM_ID,
                name: 'Mock terminal',
                online: true,
                window_count: 1,
                pane_count: 2,
                active_pane_count: 1,
                current_command: 'bash',
                last_seen_at: '2030-01-01T00:00:00Z',
              },
            ],
          },
        ],
      })
    }
    if (path === '/api/v1/computers' && request.method() === 'GET') {
      return json(route, {
        computers: [
          {
            installation_id: 'computer-e2e-0001',
            hostname: 'mock-host',
            display_name: 'Mock Computer',
            platform: 'linux',
            client_version: '0.2.0',
            registered_at: '2030-01-01T00:00:00Z',
            last_seen_at: '2030-01-01T00:00:00Z',
            online: true,
            terms: [
              {
                instance_id: TERM_ID,
                name: 'Mock terminal',
                online: true,
                window_count: 1,
                pane_count: 2,
                active_pane_count: 1,
                current_command: 'bash',
                last_seen_at: '2030-01-01T00:00:00Z',
              },
            ],
          },
        ],
      })
    }
    if (path === '/api/v1/agent/capabilities') {
      return json(route, {
        agent_broker_enabled: true,
        state: 'ready',
        reason_code: null,
        delegated_write_grants_enabled: false,
      })
    }
    if (path === '/api/v1/agent/admin/bindings' && request.method() === 'GET') {
      return json(route, {
        bindings: [
          {
            binding_id: BINDING_ID,
            profile_id: PROFILE_ID,
            term_id: TERM_ID,
            status: 'ready',
            runtime_ref: 'fixture-runtime',
            runtime_epoch: 1,
            capability_ref: 'fixture-capability',
            created_at: '2030-01-01T00:00:00Z',
            updated_at: '2030-01-01T00:00:00Z',
          },
        ],
      })
    }
    if (path === `/api/v1/instances/${TERM_ID}/topology`) {
      return json(route, topology())
    }
    if (path === '/api/v1/agent/admin/setup' && request.method() === 'GET') {
      if (setupState === 'activating' && activatingReadsRemaining === 0) {
        setupState = 'ready'
      }
      const response = setupResponse(setupState)
      if (setupState === 'activating') activatingReadsRemaining -= 1
      return json(route, response)
    }
    if (path === '/api/v1/agent/admin/setup' && request.method() === 'POST') {
      traffic.setupRequests.push(
        request.postDataJSON() as Record<string, unknown>,
      )
      setupState = 'activating'
      activatingReadsRemaining = 1
      return json(route, setupResponse('activating'))
    }
    if (
      path === '/api/v1/agent/conversations'
      && request.method() === 'GET'
    ) {
      return json(route, {
        conversations: conversationCreated ? [conversation] : [],
      })
    }
    if (
      path === '/api/v1/agent/conversations'
      && request.method() === 'POST'
    ) {
      conversationCreated = true
      traffic.conversationCreates += 1
      return json(route, conversation, 201)
    }
    if (
      path === `/api/v1/agent/conversations/${CONVERSATION_ID}`
      && request.method() === 'GET'
    ) {
      return json(route, {
        ...conversation,
        binding: {
          binding_id: BINDING_ID,
          profile_id: PROFILE_ID,
          term_id: TERM_ID,
          status: 'active',
        },
      })
    }
    if (
      path === `/api/v1/agent/conversations/${CONVERSATION_ID}/messages`
      && request.method() === 'GET'
    ) {
      return json(route, { messages: [] })
    }
    if (
      path === `/api/v1/agent/conversations/${CONVERSATION_ID}/events`
      && request.method() === 'GET'
    ) {
      return json(route, {
        events: [],
        next_cursor: Number(url.searchParams.get('since') ?? 0),
      })
    }
    if (
      path === `/api/v1/agent/conversations/${CONVERSATION_ID}/messages`
      && request.method() === 'POST'
    ) {
      approvalVisible = true
      permissionReady.resolve()
      return json(
        route,
        {
          message_id: 'fixture-user-message',
          conversation_id: CONVERSATION_ID,
          admission_seq: 1,
          idempotency_key: 'fixture-message-idempotency',
          delivery_state: 'accepted',
          submission_state: 'accepted',
        },
        202,
      )
    }
    if (
      path === '/api/v1/agent/approvals'
      && request.method() === 'GET'
    ) {
      return json(route, {
        approvals: approvalVisible ? [pendingApproval] : [],
      })
    }
    if (
      path === `/api/v1/agent/approvals/${APPROVAL_ID}`
      && request.method() === 'GET'
    ) {
      return json(route, {
        ...pendingApproval,
        binding: {
          binding_id: BINDING_ID,
          profile_id: PROFILE_ID,
          term_id: TERM_ID,
          status: 'active',
        },
      })
    }
    if (
      path === `/api/v1/agent/approvals/${APPROVAL_ID}/decide`
      && request.method() === 'POST'
    ) {
      traffic.approvalDecisionAttempts += 1
      if (traffic.approvalDecisionAttempts === 1) {
        return json(
          route,
          error(
            'approval_reauthentication_required',
            'Fresh authentication is required.',
          ),
          428,
        )
      }
      approvalVisible = false
      traffic.approvalDecisionSuccesses += 1
      sendTerminalOutput?.()
      completionReady.resolve()
      return json(route, {
        ...pendingApproval,
        state: 'approved',
        decided_at: '2030-01-01T00:00:10Z',
        decision: 'approve',
        binding: {
          binding_id: BINDING_ID,
          profile_id: PROFILE_ID,
          term_id: TERM_ID,
          status: 'active',
        },
      })
    }
    return json(route, {})
  })

  await page.route('**/api/v1/agent/stream**', async (route) => {
    traffic.agentStreamConnections += 1
    if (traffic.agentStreamConnections === 1) {
      await permissionReady.promise
      const body = [
        agentFrame('1-1', {
          type: 'RUN_STARTED',
          threadId: CONVERSATION_ID,
          runId: RUN_ID,
        }),
        agentFrame('1-2', {
          type: 'TOOL_CALL_START',
          toolCallId: 'fixture-tool-call-1',
          toolCallName: 'termflow_terminal_write',
        }),
        agentFrame('1-3', {
          type: 'CUSTOM',
          name: 'termflow.permission_requested',
          value: {
            approval_request_id: APPROVAL_ID,
            tool_name: 'termflow_terminal_write',
            expires_at: pendingApproval.expires_at,
          },
        }),
      ].join('')
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body,
      })
    }
    if (traffic.agentStreamConnections === 2) {
      await completionReady.promise
      const body = [
        agentFrame('1-4', {
          type: 'TOOL_CALL_RESULT',
          messageId: 'fixture-tool-result',
          toolCallId: 'fixture-tool-call-1',
          content: JSON.stringify({ status: 'success', truncated: false }),
        }),
        agentFrame('1-5', {
          type: 'TEXT_MESSAGE_CHUNK',
          messageId: MESSAGE_ID,
          role: 'assistant',
          delta: 'Agent 已执行，终端结果为 1。',
        }),
        agentFrame('1-6', {
          type: 'TEXT_MESSAGE_END',
          messageId: MESSAGE_ID,
        }),
        agentFrame('1-7', {
          type: 'RUN_FINISHED',
          threadId: CONVERSATION_ID,
          runId: RUN_ID,
        }),
      ].join('')
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body,
      })
    }
    return route.fulfill({ status: 204 })
  })

  await page.routeWebSocket(
    /\/api\/v1\/terms\/[^/]+\/terminal(?:\?.*)?$/,
    (socket) => {
      traffic.terminalConnections += 1
      socket.onMessage(() => undefined)
      socket.send(
        JSON.stringify({
          type: 'terminal.ready',
          terminal_id: TERMINAL_ID,
          stream_id: STREAM_ID,
          rows: 30,
          cols: 120,
        }),
      )
      sendTerminalOutput = () => {
        const output = Buffer.from('\r\n1\r\n')
        traffic.terminalOutputFrames.push(output)
        socket.send(output)
      }
    },
  )

  return traffic
}

test(
  'configures the terminal sidecar, reauthenticates one approval, and keeps one terminal',
  async ({ page }) => {
    const consoleMessages: string[] = []
    const pageErrors: string[] = []
    const requestUrls: string[] = []
    page.on('console', (message) => consoleMessages.push(message.text()))
    page.on('pageerror', (cause) => pageErrors.push(cause.message))
    page.on('request', (request) => requestUrls.push(request.url()))
    const traffic = await mockProductFlow(page)

    await page.goto(`/terms/${TERM_ID}?keep=yes&secret=must-drop`)
    await expect(page.getByRole('heading', { name: '登录' })).toBeVisible()
    await page.getByLabel('管理员令牌').fill(ADMIN_CREDENTIAL)
    await page.getByRole('button', { name: '登录', exact: true }).click()

    await expect(page).toHaveURL(new RegExp(`/terms/${TERM_ID}$`))
    await expect(page.locator('.terminal-view')).toBeVisible()
    await expect(page.locator('[data-action="toggle-agent"]')).toHaveAttribute(
      'aria-label', /未设置/,
    )
    await page.locator('[data-action="toggle-agent"]').click()
    await expect(
      page.locator('[data-agent-panel-state="unconfigured"]'),
    ).toBeVisible()
    await expect(page.getByText(DISCLOSURE_TERMS, { exact: false })).toBeVisible()

    await page.locator('input[name="profileDisplayName"]').fill(
      'Terminal Agent Profile',
    )
    await expect(page.locator('input[name="paneIds"][value="%0"]')).toBeChecked()
    await expect(page.locator('input[name="paneIds"][value="%1"]')).toBeChecked()
    await page.locator('input[name="paneIds"][value="%1"]').uncheck()
    await expect(
      page.locator('input[name="paneIds"][value="%1"]'),
    ).not.toBeChecked()
    await page.locator('input[name="accepted"]').check()
    await page.getByRole('button', { name: '启用 Agent' }).click()
    await expect
      .poll(() => traffic.setupRequests.length)
      .toBe(1)
    expect(traffic.setupRequests[0]?.pane_ids).toEqual(['%0'])
    expect(traffic.setupRequests[0]?.topology_revision).toBe(7)
    expect(traffic.setupRequests[0]?.accepted).toBe(true)

    await expect(
      page.locator('[data-agent-panel-state="activating"]'),
    ).toBeVisible()
    await page.getByRole('button', { name: '刷新状态' }).click()
    await expect(page.locator('[data-agent-panel-state="ready"]')).toBeVisible()
    await expect(page.locator('[data-action="toggle-agent"]')).toHaveAttribute(
      'aria-label', /已就绪/,
    )

    const overlayMode = await page.evaluate(() =>
      matchMedia('(max-width: 47.99rem), (pointer: coarse)').matches,
    )
    if (overlayMode) {
      await expect(
        page.locator('[data-terminal-interaction-host]'),
      ).toHaveAttribute('inert', '')
      expect(
        await page.locator('.terminal-host').evaluate(
          (element) => element.closest('[inert]') !== null,
        ),
      ).toBe(true)
      expect(
        await page.locator('.mobile-keybar').evaluate(
          (element) => element.closest('[inert]') !== null,
        ),
      ).toBe(true)
      const mobileGeometry = await page.locator('#terminal-agent-panel').evaluate((element) => {
        const panel = element.getBoundingClientRect()
        const workspace = element.parentElement!.getBoundingClientRect()
        return {
          panel: { left: panel.left, top: panel.top, right: panel.right, bottom: panel.bottom },
          workspace: { left: workspace.left, top: workspace.top, right: workspace.right, bottom: workspace.bottom },
        }
      })
      expect(mobileGeometry.panel).toEqual(mobileGeometry.workspace)
      await expect(page.locator('[data-action="drag-agent"]')).toHaveCount(0)
      await expect(page.locator('[data-agent-resize-handle]')).toHaveCount(0)
    } else {
      const dimensions = await page
        .locator('#terminal-agent-panel')
        .evaluate((element) => {
          const panel = element.getBoundingClientRect()
          const workspace = element.parentElement!.getBoundingClientRect()
          return {
            width: panel.width,
            rem: Number.parseFloat(
              getComputedStyle(document.documentElement).fontSize,
            ),
            panel: {
              left: panel.left,
              top: panel.top,
              right: panel.right,
              bottom: panel.bottom,
            },
            workspace: {
              left: workspace.left,
              top: workspace.top,
              right: workspace.right,
              bottom: workspace.bottom,
            },
          }
        })
      expect(dimensions.width).toBeGreaterThanOrEqual(dimensions.rem * 26 - 1)
      expect(dimensions.width).toBeLessThanOrEqual(dimensions.rem * 30 + 1)
      expect(dimensions.panel.left).toBeGreaterThan(dimensions.workspace.left)
      expect(dimensions.panel.top).toBeGreaterThan(dimensions.workspace.top)
      expect(dimensions.panel.right).toBeLessThan(dimensions.workspace.right)
      expect(dimensions.panel.bottom).toBeLessThan(dimensions.workspace.bottom)

      // The desktop panel is an actual floating surface, not a static drawer:
      // drag its open-source-icon handle, then resize from the south-east
      // edge and assert both geometry changes and workspace containment.
      const panel = page.locator('#terminal-agent-panel')
      const beforeDrag = await panel.boundingBox()
      const dragHandle = await page.locator('[data-action="drag-agent"]').boundingBox()
      expect(beforeDrag).not.toBeNull()
      expect(dragHandle).not.toBeNull()
      await page.mouse.move(dragHandle!.x + dragHandle!.width / 2, dragHandle!.y + dragHandle!.height / 2)
      await page.mouse.down()
      await page.mouse.move(dragHandle!.x + dragHandle!.width / 2 - 120, dragHandle!.y + dragHandle!.height / 2 + 48)
      await page.mouse.up()
      await expect.poll(async () => (await panel.boundingBox())?.x ?? beforeDrag!.x).toBeLessThan(beforeDrag!.x - 20)
      await expect.poll(async () => (await panel.boundingBox())?.y ?? beforeDrag!.y).toBeGreaterThan(beforeDrag!.y + 20)

      const beforeResize = await panel.boundingBox()
      const resizeHandle = await page.locator('[data-agent-resize-handle="se"]').boundingBox()
      expect(beforeResize).not.toBeNull()
      expect(resizeHandle).not.toBeNull()
      await page.mouse.move(resizeHandle!.x + resizeHandle!.width / 2, resizeHandle!.y + resizeHandle!.height / 2)
      await page.mouse.down()
      await page.mouse.move(resizeHandle!.x + resizeHandle!.width / 2 + 72, resizeHandle!.y + resizeHandle!.height / 2 + 52)
      await page.mouse.up()
      await expect.poll(async () => (await panel.boundingBox())?.width ?? beforeResize!.width).toBeGreaterThan(beforeResize!.width + 20)
      await expect.poll(async () => (await panel.boundingBox())?.height ?? beforeResize!.height).toBeGreaterThan(beforeResize!.height + 20)
      const afterResize = await panel.boundingBox()
      expect(afterResize).not.toBeNull()
      expect(afterResize!.x).toBeGreaterThan(dimensions.workspace.left)
      expect(afterResize!.y).toBeGreaterThan(dimensions.workspace.top)
      expect(afterResize!.x + afterResize!.width).toBeLessThan(dimensions.workspace.right)
      expect(afterResize!.y + afterResize!.height).toBeLessThan(dimensions.workspace.bottom)
    }

    await page.getByRole('button', { name: '新建会话' }).click()
    await expect
      .poll(() => traffic.conversationCreates)
      .toBe(1)
    await expect(page).toHaveURL(
      new RegExp(`/terms/${TERM_ID}\\?agent=${CONVERSATION_ID}$`),
    )
    await expect(page.locator('[data-agent-panel-title]')).toHaveText(
      'Agent echo 1',
    )
    const screenshotDir = process.env.TERMFLOW_E2E_SCREENSHOT_DIR
    if (screenshotDir) {
      await page.screenshot({
        path: `${screenshotDir}/terminal-agent-floating-${test.info().project.name}.png`,
      })
    }

    const approvalPrompt = page.locator('[data-agent-approval-prompt]')
    await expect(approvalPrompt).toBeHidden()
    await page.locator('[data-agent-composer-input]').fill(PRIVATE_PROMPT)
    await page.locator('[data-action="send-message"]').click()
    await expect(page.locator('[data-agent-message-list]')).toContainText(
      PRIVATE_PROMPT,
    )
    const approvalCard = page.locator(
      `[data-agent-approval-card][data-agent-approval-id="${APPROVAL_ID}"]`,
    )
    await expect(approvalCard).toBeVisible()
    await expect(approvalPrompt).toBeVisible()

    await approvalCard.locator('[data-action="focus-approval"]').click()
    const approveButton = page.locator(
      `[data-agent-approval-item][data-agent-approval-id="${APPROVAL_ID}"] [data-action="approve-approval"]`,
    )
    await expect(approveButton).toBeFocused()
    await approveButton.click()
    await expect(page.getByRole('alertdialog')).toHaveCount(1)
    await page.locator('[data-action="approve-confirm"]').click()

    const reauth = page.getByRole('dialog', { name: '重新验证身份' })
    await expect(reauth).toBeVisible()
    await expect(page.getByRole('alertdialog')).toHaveCount(1)
    await reauth.getByLabel('Root 凭据').fill(ADMIN_CREDENTIAL)
    await reauth.getByRole('button', { name: '验证' }).click()
    await expect(reauth).toBeHidden()
    await expect(page.getByRole('alertdialog')).toHaveCount(0)
    await expect(approvalPrompt).toBeHidden()
    await expect(page.locator('[data-agent-message-list]')).toContainText(
      'Agent 已执行，终端结果为 1。',
    )
    await expect
      .poll(() =>
        Buffer.concat(traffic.terminalOutputFrames)
          .toString('utf8')
          .replaceAll('\r', ''),
      )
      .toContain('\n1\n')

    expect(traffic.browserLogins).toBe(2)
    expect(traffic.approvalDecisionAttempts).toBe(2)
    expect(traffic.approvalDecisionSuccesses).toBe(1)
    expect(traffic.terminalOutputFrames).toHaveLength(1)
    expect(traffic.terminalConnections).toBe(1)

    await page.locator('[data-action="close-agent"]').click()
    await expect(page).toHaveURL(new RegExp(`/terms/${TERM_ID}$`))
    await expect(page.locator('#terminal-agent-panel')).toBeHidden()
    await expect(page.locator('[data-action="toggle-agent"]')).toBeFocused()
    await expect(
      page.locator('[data-terminal-interaction-host]'),
    ).not.toHaveAttribute('inert', '')
    expect(traffic.terminalConnections).toBe(1)

    const storage = await page.evaluate(() =>
      JSON.stringify({
        local: Object.entries(localStorage),
        session: Object.entries(sessionStorage),
      }),
    )
    const exposedSurfaces = [
      page.url(),
      requestUrls.join('\n'),
      consoleMessages.join('\n'),
      storage,
    ].join('\n')
    for (const privateValue of [
      PRIVATE_PROMPT,
      ADMIN_CREDENTIAL,
      PROVIDER_CREDENTIAL,
      MCP_CREDENTIAL,
      DISCLOSURE_TERMS,
    ]) {
      expect(exposedSurfaces).not.toContain(privateValue)
    }
    expect(exposedSurfaces).not.toMatch(/(?:^|[\r\n])1(?:$|[\r\n])/)
    expect(pageErrors).toEqual([])
  },
)

test('renders the Agent directory as a Term-facing table in the product shell', async ({ page }, testInfo) => {
  await mockProductFlow(page, { seedConversation: true })

  await page.goto('/agent')
  await expect(page.getByRole('heading', { name: '登录' })).toBeVisible()
  await page.getByLabel('管理员令牌').fill(ADMIN_CREDENTIAL)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).toHaveURL(/\/agent$/)

  await expect(page.getByRole('heading', { name: 'Agent 控制台', level: 1 })).toBeVisible()
  await expect(page.locator('[data-agent-create-title]')).toHaveCount(0)
  await expect(page.locator('[data-action="create-conversation"]')).toHaveCount(0)

  const table = page.locator('[data-agent-conversation-table]')
  await expect(table).toBeVisible()
  if (testInfo.project.name.startsWith('mobile')) {
    // Mobile intentionally changes the table header into per-card labels so
    // the four fields remain readable in the narrow viewport.
    for (const label of ['会话名称', 'Term', '工作状态', '操作']) {
      await expect(table.locator(`[role="cell"][data-label="${label}"]`)).toHaveCount(1)
    }
    // Actions are a separate bottom row on a narrow card. Keeping them under
    // the status field preserves the reading order and gives the icon group a
    // full-width touch target instead of pinning it beside the title.
    if (testInfo.project.name === 'mobile-portrait') {
      const row = table.locator('[role="row"][data-agent-conversation-id]').first()
      const rowBox = await row.boundingBox()
      const statusBox = await row.locator('[role="cell"][data-label="工作状态"]').boundingBox()
      const actionsBox = await row.locator('[role="cell"][data-label="操作"]').boundingBox()
      expect(rowBox).not.toBeNull()
      expect(statusBox).not.toBeNull()
      expect(actionsBox).not.toBeNull()
      expect(actionsBox!.y).toBeGreaterThan(statusBox!.y + statusBox!.height - 1)
      expect(actionsBox!.x).toBeGreaterThanOrEqual(rowBox!.x - 1)
      expect(actionsBox!.x + actionsBox!.width).toBeLessThanOrEqual(rowBox!.x + rowBox!.width + 1)
    }
  } else {
    await expect(table.getByRole('columnheader')).toHaveText(['会话名称', 'Term', '工作状态', '操作'])
  }
  if (testInfo.project.name === 'mobile-landscape') {
    const operationCell = table.locator('[role="row"][data-agent-conversation-id]').first().locator('[role="cell"][data-label="操作"]')
    const operationBox = await operationCell.boundingBox()
    const viewport = page.viewportSize()
    expect(operationBox).not.toBeNull()
    expect(viewport).not.toBeNull()
    expect(operationBox!.x + operationBox!.width).toBeLessThanOrEqual(viewport!.width + 1)
    for (const button of await operationCell.locator('a, button').all()) {
      const buttonBox = await button.boundingBox()
      expect(buttonBox).not.toBeNull()
      expect(buttonBox!.x + buttonBox!.width).toBeLessThanOrEqual(viewport!.width + 1)
    }
  }
  await expect(table).toContainText('Mock terminal')
  await expect(table).toContainText('运行中')
  await expect(page.getByText('Delegated Write Grants')).toHaveCount(0)
  await expect(page.getByText('选择 Binding')).toHaveCount(0)
  await expect(table.locator('[data-action="open-conversation"] svg')).toBeVisible()
  await expect(table.locator('[data-action="rename-conversation"] svg')).toBeVisible()
  await expect(table.locator('[data-action="delete-conversation"] svg')).toBeVisible()

  if (testInfo.project.name.startsWith('mobile')) {
    // Portrait uses the bottom navigation; landscape (844px wide) keeps the
    // side navigation. Assert the navigation that is actually rendered.
    const links = page.locator('.side-nav:visible a, .mobile-nav:visible a')
    await expect(links).toHaveCount(4)
    for (let index = 0; index < 4; index += 1) {
      await expect(links.nth(index).locator('svg')).toBeVisible()
      if (testInfo.project.name === 'mobile-portrait') {
        await expect(links.nth(index).locator('.nav-label')).toHaveCSS('display', 'none')
      }
    }
  }

  const screenshotDir = process.env.TERMFLOW_E2E_SCREENSHOT_DIR
  if (screenshotDir) {
    await page.screenshot({
      path: `${screenshotDir}/agent-directory-${testInfo.project.name}.png`,
    })
  }
})
