import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('../apps/clients/web/node_modules/playwright');
import { createServer } from 'http';
import { readFile } from 'fs/promises';
import { join, extname } from 'path';
import { existsSync, mkdirSync } from 'fs';

const distDir = join(process.cwd(), 'apps/clients/web/dist');
const mimeTypes = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
};

const server = createServer(async (req, res) => {
  let url = (req.url || '/').split('?')[0];
  let filePath = join(distDir, url);
  if (url === '/' || !extname(url)) {
    filePath = join(distDir, 'index.html');
  }
  try {
    const data = await readFile(filePath);
    const ext = extname(filePath);
    res.writeHead(200, { 'Content-Type': mimeTypes[ext] || 'application/octet-stream' });
    res.end(data);
  } catch (e) {
    try {
      const indexData = await readFile(join(distDir, 'index.html'));
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end(indexData);
    } catch {
      res.writeHead(404);
      res.end('Not found');
    }
  }
});

await new Promise((resolve) => server.listen(4173, resolve));
console.log('Static server listening on http://localhost:4173');

const outputDir = join(process.cwd(), 'artifacts/screenshots');
if (!existsSync(outputDir)) {
  mkdirSync(outputDir, { recursive: true });
}

const executablePath = '/home/mcocdaa/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
const browser = await chromium.launch({
  executablePath: existsSync(executablePath) ? executablePath : undefined,
  args: ['--no-sandbox', '--disable-setuid-sandbox'],
});

try {
  // Desktop 1280x900
  const desktopContext = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 2,
  });
  const desktopPage = await desktopContext.newPage();
  await desktopPage.goto('http://localhost:4173/agent-preview');
  await desktopPage.waitForLoadState('networkidle');
  await desktopPage.waitForTimeout(800);

  // Expand git_diff and system_log to showcase diff syntax highlighting & intelligent fold
  const gitDiffSummary = desktopPage.locator('summary:has-text("git_diff")');
  if (await gitDiffSummary.isVisible()) await gitDiffSummary.click();
  const systemLogSummary = desktopPage.locator('summary:has-text("system_log")');
  if (await systemLogSummary.isVisible()) await systemLogSummary.click();
  await desktopPage.waitForTimeout(500);

  await desktopPage.screenshot({
    path: join(outputDir, 'termflow-desktop-agent-chat.png'),
    fullPage: true,
  });
  console.log('Saved termflow-desktop-agent-chat.png');

  // Switch to Tokyo Night theme
  const tokyoNightBtn = desktopPage.locator('[role="radio"][aria-label="Tokyo Night"]');
  if (await tokyoNightBtn.isVisible()) {
    await tokyoNightBtn.click();
    await desktopPage.waitForTimeout(500);
    await desktopPage.screenshot({
      path: join(outputDir, 'termflow-desktop-tokyo-night.png'),
      fullPage: true,
    });
    console.log('Saved termflow-desktop-tokyo-night.png');
  }

  // Mobile 375x812
  const mobileContext = await browser.newContext({
    viewport: { width: 375, height: 812 },
    deviceScaleFactor: 2,
    hasTouch: true,
    isMobile: true,
  });
  const mobilePage = await mobileContext.newPage();
  await mobilePage.goto('http://localhost:4173/agent-preview');
  await mobilePage.waitForLoadState('networkidle');
  await mobilePage.waitForTimeout(800);

  const mobileGitDiff = mobilePage.locator('summary:has-text("git_diff")');
  if (await mobileGitDiff.isVisible()) await mobileGitDiff.click();
  await mobilePage.waitForTimeout(500);

  await mobilePage.screenshot({
    path: join(outputDir, 'termflow-mobile-agent-chat.png'),
  });
  console.log('Saved termflow-mobile-agent-chat.png');

  // Scroll down to MobileKeyBar and capture it
  const mobileBarSection = mobilePage.locator('.x-preview__mobile-bar-wrap');
  if (await mobileBarSection.isVisible()) {
    await mobileBarSection.scrollIntoViewIfNeeded();
    await mobilePage.waitForTimeout(300);
    await mobilePage.screenshot({
      path: join(outputDir, 'termflow-mobile-keybar.png'),
    });
    console.log('Saved termflow-mobile-keybar.png');
  }
} catch (err) {
  console.error('Screenshot error:', err);
  throw err;
} finally {
  await browser.close();
  server.close();
}
