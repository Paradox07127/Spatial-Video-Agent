// Home page (#/, redesign v2-1a): background stage of the last workspace, @ source picker, refusal card.
const { test, expect } = require('@playwright/test');
const { mockApi } = require('./mockapi');

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
  await expect(page.locator('.home-brand')).toHaveText('SVASpatial Video Agent');
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

test('a last warehouse route draws that scene behind the home page and names it top-right', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('sva.lastRoute', '#/warehouse/wh-sdg-fixture-01'));
  await page.goto('#/');
  await expect(page.locator('.home-scene')).toHaveText('Warehouse · wh-sdg-fixture-01');
  await expect(page.locator('.home-bg canvas')).toHaveCount(1);
});
