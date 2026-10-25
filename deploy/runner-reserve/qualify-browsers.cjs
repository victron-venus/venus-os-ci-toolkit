#!/usr/bin/env node
'use strict';
// Runs with an explicitly supplied, integrity-pinned Playwright toolchain.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');

async function main() {
  assert.equal(process.platform, 'linux');
  assert.equal(process.arch, 'x64');
  assert.equal(process.getuid(), 1001);
  assert.equal(process.argv.length, 4);
  const root = path.resolve(process.argv[2]);
  const record = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const expected = ['chrome', 'chrome-beta', 'msedge', 'msedge-beta'];
  assert.deepEqual(record.packages.map(p => p.channel).sort(), expected);
  const toolchain = createRequire(path.join(root, 'package.json'));
  assert.equal(toolchain('playwright/package.json').version, '1.64.0');
  const { chromium } = toolchain('playwright');
  const channels = [];
  for (const channel of expected) {
    // Match the shared browser workflow launch contract without extra flags.
    const browser = await chromium.launch({ headless: true, channel, timeout: 30000 });
    try {
      const version = browser.version();
      assert.equal(version, record.packages.find(p => p.channel === channel).version.split('-')[0]);
      const context = await browser.newContext();
      await context.route('**/*', route => route.abort());
      const page = await context.newPage();
      await page.goto('data:text/html,<title>runner-browser-probe</title><p id="value">ready</p>',
        { timeout: 10000 });
      assert.equal(await page.title(), 'runner-browser-probe');
      assert.equal(await page.locator('#value').textContent(), 'ready');
      assert.equal(await page.evaluate(() => 21 * 2), 42);
      await context.close();
      channels.push({ channel, version, local_page_passed: true });
    } finally {
      await browser.close();
    }
  }
  console.log(JSON.stringify({ schema: 1, architecture: 'amd64', uid: process.getuid(),
    playwright_version: '1.64.0', channels, full_branded_qualified: true, gvisor_qualified: false }));
}

main().catch(error => { console.error(error); process.exitCode = 1; });
