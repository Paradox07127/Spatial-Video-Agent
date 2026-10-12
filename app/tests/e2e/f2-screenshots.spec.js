// F2 screenshots (fixture app): the fleet timeline in trip time with its telemetry lanes, Explore and after an answer.
const path = require('path');
const { test, expect } = require('@playwright/test');
const { mockApi, mockAsk } = require('./mockapi');

const T1 = 'fl-pie-fixture-01';
const OUT = path.resolve(__dirname, '../../../.e2e/v4-f2');

test.describe('F2 screenshots', () => {
  test.skip(process.env.SVA_E2E_LIVE === '1', 'fixture app only');
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
    await page.route(/\/api\/assets\/_map\//, (r) => r.continue());
    await page.route(/\/health$/, (r) => r.fulfill({ json: { ok: true, mode: 'dev', libs_ready: true, index_rows: { clips: 6, events: 3 }, assets_ok: true } }));
  });

  for (const [name, width, height, answer] of [['1440x900-explore', 1440, 900, false], ['1440x900-answer', 1440, 900, true], ['1024x768', 1024, 768, true], ['390x844', 390, 844, true]]) {
    test(`capture ${name}`, async ({ page }) => {
      await page.setViewportSize({ width, height });
      await page.goto(`#/fleet/${T1}@3000`);
      await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
      await expect(page.locator('.fl-vid video')).toHaveCount(1);
      if (answer) {
        await mockAsk(page, 'fl-where');
        await page.locator('#ask-input').fill('Near Queen and Spadina, did a pedestrian cross in front of the car?');
        await page.keyboard.press('Enter');
        await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
      }
      await page.waitForTimeout(1000);
      await page.screenshot({ path: path.join(OUT, `${name}.png`) });
    });
  }
});
