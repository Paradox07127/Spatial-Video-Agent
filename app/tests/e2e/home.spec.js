// Home page (#/, redesign v2-1a): background stage of the last workspace, @ source picker, refusal card.
const { test, expect } = require('@playwright/test');
const { mockApi, mockAskFrames, ASKS } = require('./mockapi');
const fs = require('fs');
const path = require('path');

const E2E_DIR = path.resolve(__dirname, '../../../.e2e/v4-home');
const LIVE = process.env.SVA_E2E_LIVE === '1';
const HEALTH = { ok: true, mode: 'dev', libs_ready: true, index_rows: { clips: 6, events: 3 }, assets_ok: true };

let errors;
test.beforeEach(async ({ page }) => {
  errors = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(`${m.text()} @ ${m.location().url}`));
  page.on('pageerror', (e) => errors.push(String(e)));
  await mockApi(page);
  if (!LIVE) await page.route(/\/health$/, (r) => r.fulfill({ json: HEALTH }));
});
test.afterEach(() => expect(errors).toEqual([]));

test('#/ renders the home page in place: brand, headline, input, chips and principle line', async ({ page }) => {
  await page.goto('#/');
  await expect(page.locator('.home-brand')).toHaveText('Spatial Video Agent');
  await expect(page.locator('.home-title')).toHaveText('What happened, where, and how far apart?');
  await expect(page.locator('#home-input')).toBeVisible();
  await expect(page.locator('.home-chips .preset')).toHaveCount(3);
  await expect(page.locator('.home-note')).toHaveText('Numbers come from measurement. Questions about identity, intent or blame are declined.');
  await expect(page).toHaveURL(/#\/$/);
});

test('typing @ lists scenes and trips in one list; arrows and Enter pick one into a chip in the input', async ({ page }) => {
  await page.goto('#/');
  const input = page.locator('#home-input');
  await input.pressSequentially('Was anyone near the dock @');
  const list = page.locator('.home-at');
  await expect(list).toBeVisible();
  await expect(list.locator('.home-at-row[data-ws="warehouse"]')).not.toHaveCount(0);
  await expect(list.locator('.home-at-row[data-ws="fleet"]')).not.toHaveCount(0);
  await expect(list.locator('.home-at-row').last()).toHaveText('Leave empty and the agent picks the scene');
  await expect(list.locator('.home-at-row.on')).toHaveCount(1);
  await expect(list.locator('.home-at-row').first()).toHaveClass(/\bon\b/);
  await page.keyboard.press('ArrowDown');
  await expect(list.locator('.home-at-row').nth(1)).toHaveClass(/\bon\b/);
  const id = await list.locator('.home-at-row.on .home-at-id').textContent();
  await page.keyboard.press('Enter');
  await expect(list).toBeHidden();
  await expect(page.locator('.home-src')).toBeVisible();
  await expect(page.locator('.home-src')).toContainText(id);
  await expect(input).toHaveValue('Was anyone near the dock ');
  await expect(page).toHaveURL(/#\/$/);
});

test('Escape closes the @ list and keeps the home page', async ({ page }) => {
  await page.goto('#/');
  await page.locator('#home-input').pressSequentially('@');
  await expect(page.locator('.home-at')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('.home-at')).toBeHidden();
  await expect(page.locator('.home-src')).toBeHidden();
  await expect(page.locator('#home-input')).toBeVisible();
  await expect(page).toHaveURL(/#\/$/);
});

test('with no last route the background is the dark paper only, without a canvas or errors', async ({ page }) => {
  await page.goto('#/');
  await expect(page.locator('#home-input')).toBeVisible();
  await expect(page.locator('.home-bg canvas')).toHaveCount(0);
  await expect(page.locator('.home-scene')).toBeHidden();
});



test('gallery lists every fixture scene with level chip and opens it on click', async ({ page }) => {
  await page.goto('#/');
  const cards = page.locator('.home-card');
  await expect(cards).toHaveCount(4);
  await expect(cards.filter({ hasText: 'L3' })).toHaveCount(1);
  await expect(cards.filter({ hasText: 'L2' })).toHaveCount(2);
  await expect(cards.filter({ hasText: 'L1' })).toHaveCount(1);
  await cards.filter({ hasText: 'wh-sdg-fixture-01' }).first().click();
  await expect(page).toHaveURL(/#\/warehouse\/wh-sdg-fixture-01$/);
});

test('the @ button sets the source', async ({ page }) => {
  await page.goto('#/');
  const card = page.locator('.home-card').filter({ hasText: 'fl-pie-fixture-01' }).first();
  await card.locator('.home-card-at').click();
  await expect(page.locator('.home-src')).toBeVisible();
  await expect(page.locator('.home-src')).toContainText('fl-pie-fixture-01');
});

test('source filter restricts the @ picker', async ({ page }) => {
  await page.goto('#/');
  await page.locator('.home-filter[data-filter="fleet"]').click();
  const input = page.locator('#home-input');
  await input.pressSequentially('@');
  const list = page.locator('.home-at');
  await expect(list).toBeVisible();
  await expect(list.locator('.home-at-row[data-ws="fleet"]')).not.toHaveCount(0);
  await expect(list.locator('.home-at-row[data-ws="warehouse"]')).toHaveCount(0);
  await expect(list.locator('.home-at-row[data-ws="street"]')).toHaveCount(0);
});

test('recent investigations list opens a run', async ({ page }) => {
  const session = {
    session_id: 's_recent',
    title: 'Recent investigation',
    created: '2026-10-11T12:00:00Z',
    updated: '2026-10-11T12:05:00Z',
    turns: [{
      turn_id: 't_01',
      q: 'How close did the forklift come?',
      status: 'done',
      final: 'SUPPORTED',
      run_id: 'r_fixturewh0001',
      created: '2026-10-11T12:00:00Z',
    }],
  };
  await page.route(/\/api\/agent\/sessions(?:\/[^/?]+)?$/, (r) => {
    return r.fulfill({ json: new URL(r.request().url()).pathname.endsWith('/sessions') ? { sessions: [session] } : session });
  });
  await page.goto('#/');
  await page.locator('.home-tab[data-tab="recent"]').click();
  const row = page.locator('.home-run-row').first();
  await expect(row).toBeVisible();
  await expect(row).toContainText('How close did the forklift come?');
  await expect(row).toContainText('SUPPORTED');
  await row.click();
  await expect(page).toHaveURL(/#\/run\/r_fixturewh0001$/);
});

test('no request for a points file or a scene manifest on the home page', async ({ page }) => {
  let pointsRequested = false, manifests = 0;
  page.on('request', (req) => {
    if (req.url().includes('points')) pointsRequested = true;
    if (/\/api\/scene\/[^/]+\/manifest/.test(req.url())) manifests += 1;
  });
  await page.addInitScript(() => localStorage.setItem('sva.lastRoute', '#/warehouse/wh-sdg-fixture-01'));
  await page.goto('#/');
  await expect(page.locator('.home-card').first()).toBeVisible();
  await page.waitForTimeout(600);
  expect(pointsRequested).toBe(false);
  expect(manifests).toBe(0);
  await expect(page.locator(`.home-card[data-key="wh-sdg-fixture-01"] .home-card-cams`)).toHaveText('3 cams');
});

test('390x844 the question box is fully inside the viewport and there is no horizontal scroll', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('#/');
  const form = page.locator('#home-form');
  await expect(form).toBeVisible();
  const box = await form.boundingBox();
  expect(box).not.toBeNull();
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(844);
  const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(scrollWidth).toBeLessThanOrEqual(390);
});

test('capture layout H-A screenshots for 1440 Explore, 1440 Answer, 1024, and 390', async ({ page }) => {
  fs.mkdirSync(E2E_DIR, { recursive: true });

  // 1. 1440x900 Explore
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('#/');
  await expect(page.locator('.home-card')).toHaveCount(4);
  await page.screenshot({ path: path.join(E2E_DIR, 'home-1440-explore.png') });

  // 2. 1440x900 after a fixture answer (candidates panel visible on home)
  const baseFrames = structuredClone(ASKS.find((a) => a.ask_id === 'wh-main').events);
  const frames = baseFrames.map((f) => (f.event === 'hits' ? { ...f, data: { ...f.data, auto_open: null } } : f));
  await mockAskFrames(page, frames);
  await page.locator('#home-input').fill('Find moments across scenes');
  await page.locator('#home-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-candidates', 'true');
  await expect(page.locator('#panel')).toBeVisible();
  await page.screenshot({ path: path.join(E2E_DIR, 'home-1440-answer.png') });

  // 3. 1024x768
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto('#/');
  await page.reload();
  await expect(page.locator('.home-card')).toHaveCount(4);
  await page.screenshot({ path: path.join(E2E_DIR, 'home-1024.png') });

  // 4. 390x844
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('#/');
  await page.reload();
  await expect(page.locator('#home-form')).toBeVisible();
  await expect(page.locator('.home-card')).toHaveCount(4);
  await page.screenshot({ path: path.join(E2E_DIR, 'home-390.png') });
});
