import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
// The npm Chromium bundle includes the shared libraries missing in minimal CI images.
process.env.AWS_EXECUTION_ENV = 'AWS_Lambda_nodejs22.x';
const chromium = require('@sparticuz/chromium');
const LambdaFS = require('@sparticuz/chromium/build/lambdafs.js').default;
await LambdaFS.inflate('node_modules/@sparticuz/chromium/bin/al2023.tar.br');
const { chromium: playwright } = require('playwright-core');
const base = process.env.NDM_TEST_URL || 'http://127.0.0.1:3000';
await mkdir('.local', { recursive: true });
const browser = await playwright.launch({ executablePath: await chromium.executablePath(), args: chromium.args, headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const check = (condition, message) => { assert.ok(condition, message); console.log('✓', message); };

try {
  await page.goto(base, { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);
  await page.getByRole('heading', { name: 'Downloads.' }).waitFor();
  check(await page.locator('.download-row').count() > 0, 'dashboard renders real daemon transfers');
  await page.screenshot({ path: '.local/ui-desktop.png', fullPage: true });

  await page.getByLabel('Search downloads').fill('Sketchbook');
  check(await page.locator('.download-row').count() === 1, 'search filters the queue');
  await page.getByLabel('Clear search').click();
  await page.getByRole('tab', { name: /^Completed/ }).click();
  check(await page.locator('.download-row.completed').count() > 0, 'completed tab filters by status');
  await page.getByRole('tab', { name: /^All downloads/ }).click();

  await page.getByRole('button', { name: /New download/ }).click();
  let dialog = page.getByRole('dialog');
  await dialog.getByPlaceholder('https://example.com/something-good.zip').fill('https://open.spotify.com/track/example');
  check(await dialog.getByRole('link', { name: /Open Spotify/ }).isVisible(), 'Spotify links offer the supported offline route');
  check(await dialog.getByRole('button', { name: 'Start download' }).isDisabled(), 'Spotify ripping is not offered');
  await dialog.getByPlaceholder('https://example.com/something-good.zip').fill(`${base}/demo/textures.zip?copy=ui-smoke`);
  await dialog.getByPlaceholder('Let the server name it, or make it your own').fill('UI smoke sample.zip');
  await dialog.getByLabel('Add to queue without starting').check();
  await dialog.getByRole('button', { name: 'Add to queue' }).click();
  await page.locator('.download-row').filter({ hasText: 'UI smoke sample.zip' }).waitFor();
  check(true, 'new download form creates a persistent queue entry');
  const smokeRow = page.locator('.download-row').filter({ hasText: 'UI smoke sample.zip' });
  await smokeRow.getByRole('button', { name: /^Resume/ }).click();
  await smokeRow.getByRole('button', { name: /^Pause/ }).waitFor({ timeout: 10000 });
  await page.waitForTimeout(700);
  await smokeRow.getByRole('button', { name: /^Pause/ }).click();
  await smokeRow.getByRole('button', { name: /^Resume/ }).waitFor();
  check(true, 'pause and resume operate a real transfer');
  await page.getByLabel('Details for UI smoke sample.zip').click();
  await page.getByRole('button', { name: 'Remove from downloads' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Remove downloads', exact: true }).click();
  await page.waitForTimeout(400);
  check(await page.locator('.download-row').filter({ hasText: 'UI smoke sample.zip' }).count() === 0, 'confirmed removal updates the queue');

  const sample = page.locator('.download-row').filter({ hasText: 'Sketchbook' });
  if (await sample.getByRole('button', { name: /^Resume/ }).count()) {
    await sample.getByRole('button', { name: /^Resume/ }).click();
    await sample.getByRole('button', { name: /^Pause/ }).waitFor({ timeout: 10000 });
    await page.waitForTimeout(1200);
    await sample.getByRole('button', { name: /^Pause/ }).click();
    await sample.getByRole('button', { name: /^Resume/ }).waitFor();
    check(true, 'pause and resume controls operate the real engine');
  }

  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('tab', { name: 'Appearance' }).click();
  await page.getByRole('button', { name: 'Jade', exact: true }).click();
  await page.waitForTimeout(300);
  check(await page.evaluate(() => document.documentElement.style.getPropertyValue('--accent')) === '#169c83', 'custom accent is applied and persisted');
  await page.getByRole('button', { name: 'Dark', exact: true }).click();
  await page.waitForTimeout(300);
  check(await page.evaluate(() => document.documentElement.dataset.theme) === 'dark', 'dark mode works');
  await page.screenshot({ path: '.local/ui-dark.png', fullPage: true });
  await page.getByRole('button', { name: 'Light', exact: true }).click();
  await page.waitForTimeout(700);
  await page.getByRole('button', { name: 'Iris', exact: true }).click();
  await page.waitForTimeout(700);
  const restored = await (await page.request.get(`${base}/api/state`)).json();
  check(restored.settings.theme === 'light' && restored.settings.accent === '#7856e8', 'default appearance restored after testing');

  await page.getByRole('button', { name: 'Media grabber', exact: true }).click();
  await page.getByRole('button', { name: 'Spotify', exact: true }).click();
  check(await page.getByRole('link', { name: /Open in Spotify/ }).isVisible(), 'media page handles Spotify honestly');
  await page.getByLabel('Enable automatic media capture').click();
  await page.waitForTimeout(250);
  check(await page.getByLabel('Enable automatic media capture').getAttribute('aria-checked') === 'true', 'capture is explicitly opt-in');
  await page.getByLabel('Enable automatic media capture').click();
  await page.screenshot({ path: '.local/ui-media.png', fullPage: true });

  await page.getByRole('button', { name: 'Offline music', exact: true }).click();
  await page.locator('.track-row').first().waitFor();
  check(await page.locator('.track-row').count() >= 2, 'offline shelf lists real local audio');
  await page.locator('.track-play').first().click();
  await page.locator('.music-player audio').waitFor();
  check((await page.locator('.music-player audio').getAttribute('src')).startsWith('/api/library/'), 'music player streams a local library file');

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base, { waitUntil: 'networkidle' });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), 'mobile layout has no horizontal overflow');
  await page.getByLabel('Open navigation').click();
  await page.getByRole('button', { name: 'Media grabber', exact: true }).click();
  check(await page.getByRole('heading', { name: 'Media grabber.' }).isVisible(), 'mobile navigation works');
  await page.getByLabel('Open navigation').click();
  await page.getByRole('button', { name: /^Downloads/ }).click();
  await page.screenshot({ path: '.local/ui-mobile.png', fullPage: true });
  check(errors.length === 0, `no browser runtime errors: ${errors.join('; ')}`);
  console.log('All browser smoke checks passed.');
} finally {
  await browser.close();
}
