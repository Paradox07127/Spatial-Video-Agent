// Street page (L1 street front end, part A) on the fixture scene st-fixture_cam_1-clip01: one 2D camera, its chunk video
// slaved to the clock, detection boxes, the L1 chip and the caps line; routes, ⌘K and the shell's scene_key dispatch.
const { test, expect } = require('@playwright/test');
const { ASKS, mockApi, mockAsk } = require('./mockapi');

const LIVE = process.env.SVA_E2E_LIVE === '1';
const HEALTH = { ok: true, mode: 'dev', libs_ready: true, index_rows: { clips: 2 }, assets_ok: true };
const KEY = 'st-fixture_cam_1-clip01', CAM = 'fixture_cam-1';

const mod = (page, file, expr) =>
  page.evaluate(async ([f, e]) => {
    const k = await import(new URL(f, document.baseURI).href);
    return new Function('k', `return (${e})`)(k);
  }, [file, expr]);
const clock = (page, expr) => mod(page, 'kernel/clock.js', `k.clock.${expr}`);
const probe = (page) => mod(page, 'street/page.js', 'k.probe()');
// app.js is loaded with a version query; importing the same URL gives the running shell.
const shellRun = (page, expr) =>
  page.evaluate(async (e) => new Function('run', `return (${e})`)((await import(document.querySelector('script[src*="app.js"]').src)).currentRun()), expr);
const sse = (events) => events.map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');

let errors;
test.beforeEach(async ({ page }) => {
  errors = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(`${m.text()} @ ${m.location().url}`));
  page.on('pageerror', (e) => errors.push(String(e)));
  await mockApi(page);
  if (!LIVE) await page.route(/\/health$/, (r) => r.fulfill({ json: HEALTH }));
});
test.afterEach(() => expect(errors).toEqual([]));

test('#/street/<key> mounts the camera video, detection boxes, the L1 chip, the caps line and one coverage lane', async ({ page }) => {
  await page.goto(`#/street/${KEY}`);
  const video = page.locator('#stage video');
  await expect(video).toHaveCount(1);
  await expect(video).toHaveAttribute('src', new RegExp(`api/street/${KEY}/chunk/${CAM}/0$`));
  await expect.poll(() => video.evaluate((v) => v.readyState)).toBeGreaterThanOrEqual(2);
  await clock(page, 'seek(1000)');
  await expect.poll(async () => (await probe(page)).boxes).toBeGreaterThanOrEqual(1);
  expect(await probe(page)).toMatchObject({ scene_key: KEY, t_ms: 1000 });
  await expect(page.locator('.st-chip')).toHaveText('L1 · 2D video');
  const caps = await page.evaluate(async () => (await (await fetch('api/workspaces')).json()).find((w) => w.id === 'street').scenes[0].caps);
  await expect(page.locator('.st-caps')).toHaveText(/^Can't measure here: /);
  await expect(page.locator('.st-caps')).toContainText(caps.distance.text);
  await expect(page.locator(`#timeline [data-lane="cam:${CAM}"] .tl-seg`)).toHaveCount(1);
  expect(await clock(page, 'range')).toEqual({ t0: 0, t1: 10000 });
  // L1 has no track ids: object commands are ignored, seek works.
  expect(await mod(page, 'street/page.js', "k.applyUI({ cmd: 'highlight', args: { ids: ['P1'] } })")).toEqual({ ignored: true, reason: 'no_track_ids' });
  expect(await mod(page, 'street/page.js', "k.applyUI({ cmd: 'seek', args: { t_ms: 2500 } })")).toEqual({ t_ms: 2500 });
});

test('#/street/<key>@<t_ms> seeks there after load', async ({ page }) => {
  await page.goto(`#/street/${KEY}@4000`);
  await expect(page.locator('.st-chip')).toBeVisible();
  await expect.poll(() => clock(page, 't')).toBe(4000);
  await expect.poll(() => page.locator('#stage video').evaluate((v) => v.currentTime)).toBeCloseTo(4, 1);
});

test('⌘K lists the street scene and opens it; the source tag and the home @ list show it with the camera icon', async ({ page }) => {
  await page.goto('#/test');
  await page.locator('#cmdk-btn').click();
  await page.keyboard.type(KEY);
  await expect(page.locator('#cmdk li').first()).toContainText('Street fixture camera 1 (clip01)');
  await expect(page.locator('#cmdk li').first().locator('use')).toHaveAttribute('href', 'icons.svg#camera');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(new RegExp(`#/street/${KEY}$`));
  await expect(page.locator('#source-tag')).toContainText(KEY);
  await expect(page.locator('#source-tag use').first()).toHaveAttribute('href', 'icons.svg#camera');
  await expect(page.locator('#presets .preset')).toHaveText(['When does a pedestrian cross the street?', 'How many people walk past in this clip?', 'When is the street busiest in this clip?']);

  await page.goto('#/');
  await page.locator('#home-input').fill('Who walks past @st-');
  const row = page.locator('.home-at-row[data-ws="street"]');
  await expect(row).toHaveCount(1);
  await expect(row.locator('.home-at-id')).toHaveText(KEY);
  await expect(row.locator('use')).toHaveAttribute('href', 'icons.svg#camera');
});

test('street distance ask: the run finishes, the street seeks are applied, hits show as time bands', async ({ page }) => {
  await mockAsk(page, 'corpus-street-distance');
  await page.goto(`#/street/${KEY}`);
  await expect(page.locator('.st-chip')).toBeVisible();
  await page.locator('#ask-input').fill('How far was the pedestrian from the car on the street?');
  await page.keyboard.press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  await expect(page.locator('#ask-status')).toContainText('Done');
  await expect(page.locator('.st-band')).toHaveCount(2);
  await expect.poll(() => clock(page, 't')).toBe(5000);
  expect(await shellRun(page, 'run.skippedUi')).toBe(0);
});

test('ui frames and hits for another scene are not applied to the street page', async ({ page }) => {
  const ask = ASKS.find((a) => a.ask_id === 'corpus-street-distance');
  const last = ask.events.findLastIndex((f) => f.event === 'ui');
  const run_id = ask.events[0].data.run_id, other = 'wh-sdg-fixture-01';
  const anchor = ASKS.find((a) => a.ask_id === 'corpus-anchor').events.find((f) => f.event === 'hits');
  const events = [
    ...ask.events.slice(0, last + 1).map((f) => (f.event === 'hits' ? { ...f, data: { ...f.data, moments: anchor.data.moments } } : f)),
    { event: 'ui', data: { run_id, cmd: 'seek', args: { t_ms: 9000, scene_key: other }, scene_key: other } },
    ...ask.events.slice(last + 1),
  ];
  await page.route(/\/api\/agent\/ask$/, (r) => r.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse(events) }));
  await page.goto(`#/street/${KEY}`);
  await expect(page.locator('.st-chip')).toBeVisible();
  await page.locator('#ask-input').fill('How far was the pedestrian from the car on the street?');
  await page.keyboard.press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  // corpus-anchor's hits span three scenes; only the two street moments reach the page.
  await expect(page.locator('.st-band')).toHaveCount(2);
  await expect.poll(() => clock(page, 't')).toBe(5000);
  expect(await shellRun(page, 'run.skippedUi')).toBe(1);
});
