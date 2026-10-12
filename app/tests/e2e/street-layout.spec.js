// Street page layout S-A on the fixture scene: a stage header with the Answer/Explore toggle and the L1 facts, the video full
// stage width on top, the moments as buttons under it, and a spatial row of Activity (per-second counts) and Evidence.
const { test, expect } = require('@playwright/test');
const { ASKS, mockApi, mockAsk } = require('./mockapi');

const KEY = 'st-fixture_cam_1-clip01', CAM = 'fixture_cam-1';
// e_58bcc3f872f35946: the yolo_coco_2d evidence of [0, 5000) cited by corpus-street-distance.
const COUNT_EID = 'e_58bcc3f872f35946';

const mod = (page, file, expr) =>
  page.evaluate(async ([f, e]) => {
    const k = await import(new URL(f, document.baseURI).href);
    return new Function('k', `return (${e})`)(k);
  }, [file, expr]);
const clock = (page, expr) => mod(page, 'kernel/clock.js', `k.clock.${expr}`);
const probe = (page) => mod(page, 'street/page.js', 'k.probe()');
const box = (page, sel) => page.locator(sel).first().boundingBox();
const sse = (events) => events.map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');

let errors;
test.beforeEach(async ({ page }) => {
  errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await mockApi(page);
});
test.afterEach(() => expect(errors).toEqual([]));

test('header: the Answer/Explore toggle, scene chip with L1, scene time, L1 · 2D video and a Can\'t measure popover', async ({ page }) => {
  await page.goto(`#/street/${KEY}@2500`);
  const head = page.locator('#stage .st-head');
  await expect(head.locator('.stage-controls [data-mode="evidence"]')).toHaveText('Answer');
  await expect(head.locator('.st-scene')).toContainText(KEY);
  await expect(head.locator('.st-scene .level')).toHaveText('L1');
  await expect(head.locator('.st-time')).toHaveText('00:02.5');
  await expect(head.locator('.st-chip')).toHaveText('L1 · 2D video');
  expect((await head.boundingBox()).height).toBeLessThanOrEqual(40);
  const caps = await page.evaluate(async () => (await (await fetch('api/workspaces')).json()).find((w) => w.id === 'street').scenes[0].caps);
  const limits = head.locator('.st-limits');
  await expect(limits).toHaveText("Can't measure");
  await expect(page.locator('.st-caps')).toBeHidden();
  await limits.click();
  await expect(page.locator('.st-caps')).toBeVisible();
  await expect(page.locator('.st-caps')).toContainText(caps.distance.text);
  await expect(page.locator('.st-caps')).toContainText(caps.tracks.text);
  await limits.click();
  await expect(page.locator('.st-caps')).toBeHidden();
  // The moved toggle still drives the shell, and leaving the page gives it back.
  await head.locator('.stage-controls [data-mode="evidence"]').click();
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await page.goto('#/warehouse/wh-sdg-fixture-01');
  await expect(page.locator('.stage-wrap > .stage-controls')).toHaveCount(1);
  await expect(page.locator('#stage .stage-controls')).toHaveCount(0);
});

test('1440x900: the video is full stage width and top-aligned, Activity and Evidence sit side by side below it', async ({ page }) => {
  await page.goto(`#/street/${KEY}`);
  await expect(page.locator('.st-vbox')).toHaveAttribute('data-media-state', 'ready');
  const stage = await box(page, '#stage'), head = await box(page, '.st-head'), top = await box(page, '.st-top'), vbox = await box(page, '.st-vbox');
  const act = await box(page, '.st-activity'), ev = await box(page, '.st-evidence');
  expect(Math.abs(top.x - stage.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(top.width - stage.width)).toBeLessThanOrEqual(1);
  expect(Math.abs(top.y - (head.y + head.height))).toBeLessThanOrEqual(1);
  expect(Math.abs(vbox.y - top.y)).toBeLessThanOrEqual(1);
  expect(Math.abs(vbox.width - stage.width)).toBeLessThanOrEqual(1);
  expect(act.y).toBeGreaterThanOrEqual(vbox.y + vbox.height);
  expect(Math.abs(ev.y - act.y)).toBeLessThanOrEqual(1);
  expect(ev.x).toBeGreaterThanOrEqual(act.x + act.width - 1);
  expect(act.height).toBeGreaterThanOrEqual(180);
  expect(act.y + act.height).toBeLessThanOrEqual(stage.y + stage.height + 1);
  await expect(page.locator('.st-spatial-tabs')).toBeHidden();
});

test('Activity: per-second person and vehicle counts drawn over the clip; hover shows the count, click seeks', async ({ page }) => {
  await page.goto(`#/street/${KEY}`);
  await expect(page.locator('.st-vbox')).toHaveAttribute('data-media-state', 'ready');
  // The fixture: one person for the whole clip, one car in [2000, 8000) (max boxes of a kind in one frame).
  await expect.poll(async () => (await probe(page)).activity).toEqual({ person: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1], vehicle: [0, 0, 1, 1, 1, 1, 1, 1, 0, 0] });
  const chart = page.locator('.st-activity canvas');
  const inked = () => chart.evaluate((c) => c.getContext('2d').getImageData(0, 0, c.width, c.height).data.some((x, i) => i % 4 === 3 && x > 0));
  await expect.poll(inked).toBe(true);
  const b = await chart.boundingBox();
  await page.mouse.move(b.x + b.width * 0.45, b.y + b.height / 2);
  await expect(page.locator('.st-act-tip')).toHaveText(/^00:04\.5 · 1 person · 1 vehicle$/);
  await page.mouse.click(b.x + b.width * 0.75, b.y + b.height / 2);
  await expect.poll(() => clock(page, 't')).toBeGreaterThanOrEqual(7400);
  expect(await clock(page, 't')).toBeLessThanOrEqual(7600);
});

test('Answer mode draws only the cited classes, shows the cited count fact and evidence card; the boxes control overrides', async ({ page }) => {
  // The cited 2D count names only persons, so the car in [2000, 8000) is not a cited class.
  const ask = structuredClone(ASKS.find((a) => a.ask_id === 'corpus-street-distance'));
  for (const f of ask.events) for (const e of f.event === 'tool_result' ? f.data.evidence : []) {
    if (e.eid === COUNT_EID) e.value = { ...e.value, counts: { person: 1 }, classes: ['person'] };
  }
  await page.route(/\/api\/agent\/ask$/, (r) => r.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sse(ask.events) }));
  await page.goto(`#/street/${KEY}`);
  await page.locator('#ask-input').fill('When does a person cross the street?');
  await page.keyboard.press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  await page.locator('#tabs [data-tab="evidence"]').click();
  await page.locator(`#tab-evidence .ev[data-eid="${COUNT_EID}"]`).click();
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await expect(page.locator('.st-head .st-fact')).toContainText('Peak 1 person');
  await expect(page.locator('.st-head .st-fact')).toContainText('Measured');
  const card = page.locator('.st-evidence .st-card');
  await expect(card).toContainText('2D count');
  await expect(card).toContainText('person 1');
  await expect(card).toContainText('50 frames');
  await expect(card).toContainText(/conf 0\.\d\d/);
  await expect(page.locator('.st-evidence [role="tablist"]')).toBeHidden();
  await clock(page, 'seek(2500)');
  const boxes = async () => (await probe(page)).boxes;
  await expect.poll(boxes).toBe(1);
  const ctl = page.locator('.st-boxes');
  await ctl.locator('[data-boxes="all"]').click();
  await expect.poll(boxes).toBe(2);
  await ctl.locator('[data-boxes="off"]').click();
  await expect.poll(boxes).toBe(0);
  await ctl.locator('[data-boxes="cited"]').click();
  await expect.poll(boxes).toBe(1);
  // The boxes control is a view setting: Answer mode stays.
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  // Explore shows every class again, and the evidence tabs.
  await page.locator('.st-head .stage-controls [data-mode="explore"]').click();
  await expect.poll(boxes).toBe(2);
  await expect(page.locator('.st-head .st-fact')).toBeHidden();
  await expect(page.locator('.st-evidence [role="tablist"] [role="tab"]')).toHaveText(['Detections', 'Captions · Model', 'Limits']);
});

test('Explore evidence tabs: detections of the frame, model captions marked unverified, limits; a caption seeks', async ({ page }) => {
  await page.goto(`#/street/${KEY}@2500`);
  const ev = page.locator('.st-evidence');
  await expect(ev.locator('.st-pane:visible')).toContainText('car 0.');
  await expect(ev.locator('.st-pane:visible')).toContainText('person 0.');
  await ev.locator('[role="tab"]', { hasText: 'Captions · Model' }).click();
  const caption = ev.locator('.st-seg').nth(1);
  await expect(caption).toContainText('car and person in view');
  await expect(caption).toContainText('Model');
  await expect(caption).toContainText('unverified');
  await caption.click();
  await expect.poll(() => clock(page, 't')).toBe(5000);
  await ev.locator('[role="tab"]', { hasText: 'Limits' }).click();
  await expect(ev.locator('.st-pane:visible')).toContainText('no track ids');
});

test('moment marks are buttons with a 24 px hit area; Enter and click seek to the moment', async ({ page }) => {
  await mockAsk(page, 'corpus-street-distance');
  await page.goto(`#/street/${KEY}`);
  await page.locator('#ask-input').fill('How far was the pedestrian from the car on the street?');
  await page.keyboard.press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  const bands = page.locator('button.st-band');
  await expect(bands).toHaveCount(2);
  for (const b of await bands.all()) expect((await b.boundingBox()).height).toBeGreaterThanOrEqual(24);
  await expect.poll(() => clock(page, 't')).toBe(5000);
  await bands.first().focus();
  await page.keyboard.press('Enter');
  await expect.poll(() => clock(page, 't')).toBe(0);
  await bands.last().click();
  await expect.poll(() => clock(page, 't')).toBe(5000);
});

test('the overlay follows the displayed video size after a resize', async ({ page }) => {
  await page.goto(`#/street/${KEY}@1000`);
  await expect(page.locator('.st-vbox')).toHaveAttribute('data-media-state', 'ready');
  await expect.poll(async () => (await probe(page)).boxes).toBeGreaterThanOrEqual(1);
  await page.setViewportSize({ width: 1100, height: 760 });
  // The person box at 1000 ms: x1 = 110, y 245–325 in 640x360 source pixels.
  const fit = () => page.evaluate(() => {
    const c = document.querySelector('.st-ovl'), a = c.getBoundingClientRect(), b = document.querySelector('#stage video').getBoundingClientRect();
    const k = c.width / 640, g = c.getContext('2d').getImageData(Math.round(110 * k) - 2, Math.round(285 * k), 5, 1).data;
    return { off: Math.abs(a.x - b.x) + Math.abs(a.y - b.y) + Math.abs(a.width - b.width) + Math.abs(a.height - b.height),
      backing: Math.abs(c.width - Math.round(a.width * devicePixelRatio)), edge: g.some((x, i) => i % 4 === 3 && x > 0) };
  });
  await expect.poll(fit).toEqual({ off: 0, backing: 0, edge: true });
});

test('1024x768: the spatial row is one cell with tabs [Activity | Evidence]', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto(`#/street/${KEY}`);
  const tabs = page.locator('.st-spatial-tabs [role="tab"]');
  await expect(tabs).toHaveText(['Activity', 'Evidence']);
  await expect(page.locator('.st-activity')).toBeVisible();
  await expect(page.locator('.st-evidence')).toBeHidden();
  await tabs.last().click();
  await expect(page.locator('.st-evidence')).toBeVisible();
  await expect(page.locator('.st-activity')).toBeHidden();
});

test('390x844: no horizontal scroll, video full width, segmented control under it, the limits text whole in its popover', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`#/street/${KEY}`);
  await expect(page.locator('.st-vbox')).toHaveAttribute('data-media-state', 'ready');
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  expect(await page.locator('.st-root').evaluate((r) => r.scrollWidth - r.clientWidth)).toBeLessThanOrEqual(0);
  const vbox = await box(page, '.st-vbox'), tabs = await box(page, '.st-spatial-tabs');
  expect(vbox.width).toBeGreaterThanOrEqual(389);
  expect(tabs.y).toBeGreaterThanOrEqual(vbox.y + vbox.height);
  await page.locator('.st-limits').click();
  const pop = page.locator('.st-caps');
  await expect(pop).toBeVisible();
  const p = await pop.boundingBox();
  expect(p.x).toBeGreaterThanOrEqual(0);
  expect(p.x + p.width).toBeLessThanOrEqual(390);
  expect(await pop.evaluate((e) => e.scrollWidth <= e.clientWidth && e.scrollHeight <= e.clientHeight)).toBe(true);
});
