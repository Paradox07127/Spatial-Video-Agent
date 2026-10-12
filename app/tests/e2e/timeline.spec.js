// Compact timeline (card P1b): ruler, Agent looked and Evidence lanes by default; Events and camera lanes behind one toggle.
const { test, expect } = require('@playwright/test');
const { mockApi, mockAsk } = require('./mockapi');

const LIVE = process.env.SVA_E2E_LIVE === '1';
const HEALTH = { ok: true, mode: 'dev', libs_ready: true, index_rows: { clips: 6, events: 3 }, assets_ok: true };
// wh-main evidence: three measured, one model; the answer's 1.55 m cites DIST.
const DIST = 'e_5d60fc7e6ed93290', CELLS = 'e_1449f20a31b383ff';

const kernel = (page, mod, expr) =>
  page.evaluate(async ([m, e]) => {
    const k = await import(new URL(`kernel/${m}.js`, document.baseURI).href);
    return new Function('k', `return (${e})`)(k);
  }, [mod, expr]);
const height = async (page) => (await page.locator('#timeline').boundingBox()).height;
const lane = (page) => page.locator('#timeline .tl-evidence');
const mark = (page, eid) => lane(page).locator(`.tl-evm[data-eid="${eid}"]`);
// Chrome snaps the 1.5px outline to device pixels, so only its style is compared.
const outline = (loc) => loc.evaluate((e) => getComputedStyle(e).outlineStyle);

let errors, allowed;
test.beforeEach(async ({ page }) => {
  errors = [];
  allowed = /^$/;
  page.on('console', (m) => m.type() === 'error' && errors.push(`${m.text()} @ ${m.location().url}`));
  page.on('pageerror', (e) => errors.push(String(e)));
  await mockApi(page);
  if (!LIVE) await page.route(/\/health$/, (r) => r.fulfill({ json: HEALTH }));
});
test.afterEach(() => expect(errors.filter((e) => !allowed.test(e))).toEqual([]));

async function replay(page) {
  await mockAsk(page, 'wh-main');
  await page.goto('#/test');
  await expect(page.locator('#timeline .lane-agent')).toBeVisible();
  await expect(lane(page)).toBeHidden();
  await page.locator('.preset').first().click();
  await expect(page.locator('#ask-status')).toContainText('Done');
}

test('after a result the Evidence lane shows one mark per evidence, styled by its kind; Esc clears it', async ({ page }) => {
  await replay(page);
  await expect(lane(page)).toBeVisible();
  await expect(lane(page).locator('.tl-label')).toHaveText('Evidence · 4');
  await expect(lane(page).locator('.tl-evm')).toHaveCount(4);
  await expect(lane(page).locator('.tl-evm.k-measure')).toHaveCount(3);
  await expect(lane(page).locator('.tl-evm.k-model')).toHaveCount(1);
  // First Esc exits the completed answer's default Answer mode; the next clears the run.
  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');
  await expect(page.locator('body')).toHaveAttribute('data-view', 'default');
  await expect(lane(page)).toBeHidden();
  await expect(lane(page).locator('.tl-evm')).toHaveCount(0);
});

test('hovering an answer number outlines its evidence mark', async ({ page }) => {
  await replay(page);
  // Answer mode focuses its primary citation; hover can still outline this distance.
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await page.locator(`#answer .ans-num[data-eid="${DIST}"]`).hover();
  await expect.poll(() => outline(mark(page, DIST))).toBe('solid');
  expect(await outline(mark(page, CELLS))).toBe('none');
});

test('clicking an evidence mark focuses that evidence and seeks the clock to it', async ({ page }) => {
  await replay(page);
  await mark(page, CELLS).click();
  await expect.poll(() => kernel(page, 'clock', 'k.clock.t')).toBe(4000);
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await expect.poll(() => outline(mark(page, CELLS))).toBe('solid');
});

test('camera lanes and Events are collapsed behind the Cameras toggle', async ({ page }) => {
  allowed = /404.*\/warehouse\/page\.js$/;
  await page.route(/\/warehouse\/page\.js$/, (r) => r.fulfill({ status: 404, body: '' }));
  await page.goto('#/warehouse/wh-sdg-fixture-01');
  const cams = page.locator('#timeline .lane-coverage');
  await expect(cams).toHaveCount(3);
  await expect(page.locator('#timeline .lane-agent')).toBeVisible();
  for (const c of await cams.all()) await expect(c).toBeHidden();
  await expect(page.locator('#timeline .tl-events')).toBeHidden();
  const toggle = page.locator('#timeline .tl-cams-btn');
  await expect(toggle).toHaveText('Cameras · 3');
  expect(await height(page)).toBe(72);
  await toggle.click();
  for (const c of await cams.all()) await expect(c).toBeVisible();
  await expect(page.locator('#timeline .tl-events')).toBeVisible();
  expect(await kernel(page, 'state', 'k.state.cameraLanesOpen')).toBe(true);
  await toggle.click();
  await expect(cams.first()).toBeHidden();
  expect(await kernel(page, 'state', 'k.state.cameraLanesOpen')).toBe(false);
});

test('the timeline is 72 px high, 84 px while the Evidence lane is shown', async ({ page }) => {
  await replay(page);
  await expect.poll(() => height(page)).toBe(84);
  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');
  await expect(lane(page)).toBeHidden();
  expect(await height(page)).toBe(72);
});

test('long Fleet ruler labels fit at 486px and redraw on paused desktop/narrow resize without moving evidence or brush scope', async ({ page }) => {
  await mockAsk(page, 'fl-where');
  await page.goto('#/fleet');
  await expect(page.locator('.fl-car')).toHaveCount(2);
  await page.locator('#ask-input').fill('Did a pedestrian cross near Queen and Spadina?');
  await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  // The answer's own ruler zoom lands first.
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await kernel(page, 'clock', '(k.clock.pause(), k.clock.setRange(0, 36000000), k.clock.seek(3000))');
  const ruler = page.locator('#timeline .tl-ruler .tl-track');
  const labels = () => ruler.evaluate((el) => {
    const r = el.getBoundingClientRect(), boxes = [...el.querySelectorAll('.tick')].map((tick) => {
      const b = tick.getBoundingClientRect(); return { left: b.left, right: b.right };
    });
    return { count: boxes.length, readable: boxes.length >= 2 && boxes.every((b, i) => b.left >= r.left - 1 && b.right <= r.right + 1 && (!i || b.left >= boxes[i - 1].right + 8)) };
  });
  await expect.poll(async () => (await labels()).readable).toBe(true);
  const desktopCount = (await labels()).count;
  const evidence = page.locator('.tl-evm[data-eid="e_da97463cc6587a96"]');
  const evidencePercent = await evidence.evaluate((e) => e.style.left);
  await page.setViewportSize({ width: 486, height: 900 });
  await expect.poll(async () => (await labels()).readable).toBe(true);
  const narrowCount = (await labels()).count;
  expect(narrowCount).toBeLessThan(desktopCount);
  expect(await kernel(page, 'clock', 'k.clock.playing')).toBe(false);
  expect(await kernel(page, 'clock', 'k.clock.t')).toBe(3000);
  expect(await evidence.evaluate((e) => e.style.left)).toBe(evidencePercent);

  // At narrow widths the fleet timeline keeps the ruler, Speed and Evidence lanes.
  const track = await page.locator('#timeline [data-lane="speed"] .tl-track').boundingBox();
  await page.mouse.move(track.x + track.width * .2, track.y + track.height / 2); await page.mouse.down();
  await page.mouse.move(track.x + track.width * .6, track.y + track.height / 2); await page.mouse.up();
  const brush = page.locator('.tl-brush');
  await expect(brush).toBeVisible();
  const scope = await brush.evaluate((e) => [e.style.left, e.style.width]);
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect.poll(async () => (await labels()).count).toBe(desktopCount);
  await expect.poll(async () => (await labels()).readable).toBe(true);
  await expect(brush).toBeVisible();
  expect(await brush.evaluate((e) => [e.style.left, e.style.width])).toEqual(scope);
  expect(await evidence.evaluate((e) => e.style.left)).toBe(evidencePercent);
  expect(await kernel(page, 'clock', 'k.clock.t')).toBe(3000);
});
