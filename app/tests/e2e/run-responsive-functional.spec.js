const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { mockApi } = require('./mockapi');

const FIXTURE = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../../fixtures/runs/r_fixturewh0001.json'), 'utf8'));
const QUESTION = 'When did the worker start to dodge the forklift in the warehouse, and what evidence supports the measured distance across both camera views?';
const ANSWER = 'The worker started to dodge at 4.6 s, 1.55 m from the forklift. The second camera supports the distance measurement.';
const RUN = {
  ...FIXTURE, q: QUESTION, weave_url: 'https://wandb.ai/example/project/r/call/responsive-fixture',
  steps: FIXTURE.steps.map((s) => s.name === 'answer' ? { ...s, output: { text: ANSWER, cites: [FIXTURE.evidence.at(-1).eid] } } : s),
};

test.beforeEach(async ({ page }) => {
  test.skip(process.env.SVA_E2E_LIVE === '1', 'uses browser API mocks; does not call models');
  await mockApi(page);
  await page.route(/\/api\/agent\/runs(?:\/r_fixturewh0001)?$/, (route) => route.fulfill({
    json: new URL(route.request().url()).pathname.endsWith('/runs') ? [RUN] : RUN,
  }));
});

async function fits(page, selector) {
  const widths = await page.locator(selector).evaluateAll((els) => els.map((el) => {
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, scroll: el.scrollWidth, width: el.clientWidth };
  }));
  expect(widths.length).toBeGreaterThan(0);
  for (const box of widths) {
    expect(box.left).toBeGreaterThanOrEqual(0);
    expect(box.right).toBeLessThanOrEqual(page.viewportSize().width + 1);
    expect(box.scroll).toBeLessThanOrEqual(box.width + 1);
  }
}

for (const width of [486, 768, 1440]) {
  test(`reasoning remains readable and controls work at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('#/test');
    await page.evaluate(() => { location.hash = '#/run/r_fixturewh0001'; });
    const rp = page.locator('#runpage');
    await expect(rp.locator('.rp-q')).toHaveText(QUESTION);
    await expect(rp.locator('.rp-answer')).toContainText(ANSWER);
    await fits(page, '#runpage, #runpage .rp-main, #runpage .rp-q, #runpage .rp-answer, #runpage .rp-top, #runpage .rp-nav');
    await page.screenshot({ path: testInfo.outputPath('reasoning-top.png') });
    const layout = await rp.evaluate((el) => ({
      main: el.querySelector('.rp-main').getBoundingClientRect().toJSON(),
      nav: el.querySelector('.rp-nav').getBoundingClientRect().toJSON(),
    }));
    if (width === 1440) {
      expect(layout.nav.left).toBe(80);
      expect(layout.main.left).toBe(320);
      expect(layout.main.width).toBe(920);
    } else {
      expect(layout.nav.bottom).toBeLessThanOrEqual(layout.main.top + 1);
      expect(layout.main.width).toBeGreaterThan(width - 60);
    }

    await rp.locator('[data-go="steps"]').click();
    const first = rp.locator('.trace-step').first();
    await first.locator('.trace-row').click();
    await expect(first.locator('.trace-detail')).toBeVisible();
    await fits(page, '#runpage .trace-row, #runpage .trace-detail, #runpage .td-kv');
    if (width === 486) await fits(page, '#runpage .ts-note');
    await first.locator('.td-links > details').filter({ has: page.locator('summary', { hasText: 'Response' }) }).locator('summary').first().click();
    await fits(page, '#runpage .trace-detail');

    await rp.locator('[data-go="evidence"]').click();
    const row = rp.locator('.rp-evrow').last();
    await row.locator('.rp-raw-btn').click();
    await expect(row.locator('.rp-raw')).toBeVisible();
    await expect(row.locator('.rp-raw')).toContainText('ground_distance_tri');
    await expect(row.locator('.rp-evval')).toHaveText('1.55 m ±0.12');
    await fits(page, '#runpage .rp-evrow, #runpage .rp-raw');
    if (width === 486) await fits(page, '#runpage .rp-evlabel, #runpage .rp-evwhere, #runpage .rp-evval');
    if (width === 486) {
      await row.locator('.rp-raw').scrollIntoViewIfNeeded();
      await page.screenshot({ path: testInfo.outputPath('reasoning-evidence.png') });
    }

    await rp.locator('.rp-top a[href="#/runs"]').click();
    await expect(rp.locator('.rp-title')).toHaveText('Recent runs');
    await fits(page, '#runpage .rp-col, #runpage .rp-list a, #runpage .rp-lmeta');
    if (width === 486) {
      await expect(rp.locator('.rp-lq')).toHaveText(QUESTION);
      await fits(page, '#runpage .rp-lq');
    }
    await page.screenshot({ path: testInfo.outputPath('recent-runs.png') });
    await rp.locator('.rp-list a').click();
    await expect(rp.locator('.rp-q')).toHaveText(QUESTION);
  });
}
