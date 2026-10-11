const { test, expect } = require('@playwright/test');
const { mockApi } = require('./mockapi');

const T1 = 'fl-pie-fixture-01';

test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await page.route(/\/api\/assets\/_map\//, (r) => r.continue());
});

test('narrow layout shows one spatial cell per tab and a wide window shows both again', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 800 });
  await page.goto(`#/fleet/${T1}@3000`);
  await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
  const left = page.locator('.fl-spatial-left'), right = page.locator('.fl-spatial-right');
  await expect(left).toBeVisible();
  await expect(right).toBeHidden();
  await page.locator('.fl-spatial-tabs button[data-tab="speed"]').click();
  await expect(right).toBeVisible();
  await expect(left).toBeHidden();
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(left).toBeVisible();
  await expect(right).toBeVisible();
});
