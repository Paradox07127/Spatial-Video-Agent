const path = require('path');
const { test, expect } = require('@playwright/test');
const { mockApi, mockAsk } = require('./mockapi');

const T1 = 'fl-pie-fixture-01';

test.describe('F1 screenshots', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
    await page.route(/\/api\/assets\/_map\//, (r) => r.continue());
    await page.route(/\/health$/, (r) => r.fulfill({ json: { ok: true, mode: 'dev', libs_ready: true, index_rows: { clips: 6, events: 3 }, assets_ok: true } }));
  });

  test('capture 1440x900 Explore', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`#/fleet/${T1}@3000`);
    await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
    await expect(page.locator('.fl-vid video')).toHaveCount(1);
    await page.waitForTimeout(1000);
    await page.screenshot({ path: path.resolve(__dirname, '../../../.e2e/v4/1440x900-explore.png') });
  });

  test('capture 1440x900 Answer mode', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('#/fleet');
    await expect(page.locator('.fl-car')).toHaveCount(2);
    await page.keyboard.press('Escape');
    await expect(page.locator('.fl-root')).not.toHaveClass(/\bix\b/);
    await mockAsk(page, 'fl-where');
    await page.locator('#ask-input').fill('Near Queen and Spadina, did a pedestrian cross in front of the car?');
    await page.keyboard.press('Enter');
    await expect(page.locator('body')).toHaveAttribute('data-view', 'hit');
    await expect(page.locator('.fl-ix')).toHaveCount(1);
    await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
    await page.waitForTimeout(1000);
    await page.screenshot({ path: path.resolve(__dirname, '../../../.e2e/v4/1440x900-answer.png') });
  });

  test('capture 1024x768', async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto(`#/fleet/${T1}@3000`);
    await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
    await expect(page.locator('.fl-vid video')).toHaveCount(1);
    await page.waitForTimeout(1000);
    await page.screenshot({ path: path.resolve(__dirname, '../../../.e2e/v4/1024x768.png') });
  });

  test('capture 390x844', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`#/fleet/${T1}@3000`);
    await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
    await expect(page.locator('.fl-vid video')).toHaveCount(1);
    await page.waitForTimeout(1000);
    await page.screenshot({ path: path.resolve(__dirname, '../../../.e2e/v4/390x844.png') });
  });
});
