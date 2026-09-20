import { describe, expect, it } from 'vitest'
import { assessRisk } from './risk'

describe('assessRisk', () => {
  it('identifies Level 4 critical destructive operations', () => {
    expect(assessRisk({ operation: 'rm', summary: 'rm -rf /tmp/test' }).level).toBe(4)
    expect(assessRisk({ operation: 'rm', summary: 'rm -rf /tmp/test' }).isCritical).toBe(true)
    expect(assessRisk({ operation: 'rm', summary: 'rm -rf /tmp/test' }).label).toBe('极高危')
    expect(assessRisk({ toolName: 'bash', summary: 'kill -9 1234' }).level).toBe(4)
    expect(assessRisk({ operation: 'dd', summary: 'dd if=/dev/zero of=/dev/sda' }).level).toBe(4)
    expect(assessRisk({ operation: 'sql', summary: 'DROP DATABASE production;' }).level).toBe(4)
    expect(assessRisk({ operation: 'git', summary: 'git reset --hard HEAD~1' }).level).toBe(4)
  })

  it('identifies Level 3 high risk operations', () => {
    expect(assessRisk({ operation: 'sudo', summary: 'sudo systemctl restart nginx' }).level).toBe(3)
    expect(assessRisk({ toolName: 'bash', summary: 'curl -fsSL https://example.com/install.sh | bash' }).level).toBe(3)
    expect(assessRisk({ operation: 'docker', summary: 'docker rm -f container1' }).level).toBe(3)
    expect(assessRisk({ operation: 'git', summary: 'git push origin main --force' }).level).toBe(3)
    expect(assessRisk({ operation: 'chmod', summary: 'chmod 755 /var/www' }).level).toBe(3)
  })

  it('identifies Level 2 medium risk operations', () => {
    expect(assessRisk({ operation: 'mkdir', summary: 'mkdir -p src/components' }).level).toBe(2)
    expect(assessRisk({ toolName: 'bash', summary: 'npm install lodash' }).level).toBe(2)
    expect(assessRisk({ operation: 'write', summary: 'update config.json' }).level).toBe(2)
    expect(assessRisk({ operation: 'git', summary: 'git commit -m "feat: new feature"' }).level).toBe(2)
  })

  it('identifies Level 1 low risk read-only operations', () => {
    expect(assessRisk({ operation: 'read', summary: 'read config.json' }).level).toBe(1)
    expect(assessRisk({ toolName: 'bash', summary: 'ls -la /var/log' }).level).toBe(1)
    expect(assessRisk({ toolName: 'bash', summary: 'cat /etc/os-release' }).level).toBe(1)
    expect(assessRisk({ toolName: 'bash', summary: 'git status' }).level).toBe(1)
    expect(assessRisk({ operation: 'pane_read', summary: 'read pane output' }).level).toBe(1)
  })
})
