// Warehouse layout W-A on the fixture scene: stage header, the 3D stage full width on top, source video and evidence cells
// below, the narrow layouts, and the page's evidence detail, swap, clip and zone drawing.
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { mockApi, mockAsk } = require('./mockapi');

const KEY = 'wh-sdg-fixture-01';
const OUT = path.resolve(__dirname, '..', '..', '..', 'fixtures', 'sva-out');
const MF = JSON.parse(fs.readFileSync(path.join(OUT, KEY, 'manifest.json'), 'utf8'));
const SHOTS = path.resolve(__dirname, '..', '..', '..', '.e2e', 'v4-warehouse');

const mod = (page, file, expr) =>
  page.evaluate(async ([f, e]) => {
    const k = await import(new URL(f, document.baseURI).href);
    return new Function('k', `return (${e})`)(k);
  }, [file, expr]);
const clock = (page, expr) => mod(page, 'kernel/clock.js', `k.clock.${expr}`);
const probe = (page) => mod(page, 'warehouse/page.js', 'k.probe()');
const project = (page, pts) => mod(page, 'warehouse/page.js', `k.project(${JSON.stringify(pts)})`);
const focus = (page, e) => mod(page, 'kernel/bus.js', `k.bus.emit('evidence:focus', ${JSON.stringify(e)})`);
const frames = (page) => page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));

// A ground distance between P1 and F1 seen from ceiling_04 (tracks 7 and 6 there).
const GROUND_EV = {
  eid: 'e_test_ground', scene_key: KEY, cam_id: 'ceiling_04', t0_ms: 4600, t1_ms: 4700, kind: 'measure', method: 'ground_distance', value: 1.55, unit: 'm', sigma: 0.12,
  observation_ids: [{ scene_key: KEY, cam_id: 'ceiling_04', local_track_id: 7, t_ms: 4600 }, { scene_key: KEY, cam_id: 'ceiling_04', local_track_id: 6, t_ms: 4600 }],
};

let errors;
test.beforeEach(async ({ page }) => {
  errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await mockApi(page);
});
test.afterEach(() => expect(errors).toEqual([]));

async function open(page) {
  await page.goto(`#/warehouse/${KEY}`);
  await expect(page.locator('.wh')).toHaveAttribute('data-scene', KEY);
}

// Elements drawn over the 3D canvas at a grid of its points: anything on top that is neither the canvas nor an in-scene label.
const covering = (page) => page.evaluate(() => {
  const c = document.querySelector('.wh-canvas').getBoundingClientRect(), out = new Set();
  for (let i = 1; i < 12; i++) for (let j = 1; j < 8; j++) {
    const top = document.elementFromPoint(c.left + (c.width * i) / 12, c.top + (c.height * j) / 8);
    if (top && !top.matches('.wh-canvas') && !top.closest('.wh-labels')) out.add(top.className || top.tagName);
  }
  return [...out];
});

const box = (page, sel) => page.locator(sel).boundingBox();

test('1440x900: stage header with the toggle and facts, the 3D stage full width on top, source video and evidence cells below', async ({ page }) => {
  await open(page);
  await clock(page, 'seek(1000)');
  const head = page.locator('.wh-head');
  await expect(head.locator('.stage-controls button')).toHaveText(['Answer', 'Explore']);
  await expect(head.locator('.wh-scene-chip')).toHaveText(`${KEY}L3SDG`);
  await expect(head.locator('.wh-fact-time')).toHaveText('00:01.0');
  await expect(head.locator('.wh-fact-cams')).toHaveText('3 cams ok');
  await expect(head.locator('.wh-views button')).toHaveText(['Free', 'Top']);
  await expect(head.locator('.wh-layers-btn')).toBeVisible();
  await expect(head.locator('.wh-zone')).toBeVisible();
  // eye_00 has no video in [6000, 7000)
  await clock(page, 'seek(6200)');
  await expect(head.locator('.wh-fact-cams')).toHaveText('2/3 cams ok');
  await expect(page.locator('.wh-hud, .wh-bar, .wh-sv-open')).toHaveCount(0);

  const stage = await box(page, '#stage'), h = await box(page, '.wh-head'), top = await box(page, '.wh-top-slot');
  const left = await box(page, '.wh-spatial-left'), right = await box(page, '.wh-spatial-right'), canvas = await box(page, '.wh-canvas');
  expect(h.height).toBeGreaterThanOrEqual(32);
  expect(h.height).toBeLessThanOrEqual(40);
  expect(top.width).toBeCloseTo(stage.width, 0);
  expect(top.y).toBeCloseTo(h.y + h.height, 0);
  expect(top.height).toBeGreaterThan(1.7 * left.height);
  // the canvas takes the slot but the 24 px camera rail
  expect(canvas.width).toBeGreaterThanOrEqual(top.width - 25);
  expect(left.y).toBeGreaterThanOrEqual(top.y + top.height);
  expect(right.y).toBeCloseTo(left.y, 0);
  expect(left.height).toBeGreaterThanOrEqual(180);
  expect(left.x + left.width).toBeLessThanOrEqual(right.x);
  expect(right.width).toBeGreaterThan(left.width);
  expect(left.y + left.height).toBeLessThanOrEqual(stage.y + stage.height + 1);
  await expect(page.locator('.wh-spatial-left .wh-stage-video video')).toHaveCount(1);
  await expect(page.locator('.wh-evidence-tabs button')).toHaveText(['Measure', 'Cameras', 'Visibility']);
  await expect(page.locator('.wh-spatial-tabs')).toBeHidden();
  await expect(page.locator('.wh-mobile-seg')).toBeHidden();
});

test('nothing but in-scene labels lies over the 3D canvas, in Explore and in Answer mode', async ({ page }) => {
  await open(page);
  await clock(page, 'seek(4600)');
  await frames(page);
  expect(await covering(page)).toEqual([]);
  await focus(page, GROUND_EV);
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await frames(page);
  expect(await covering(page)).toEqual([]);
});

test('Answer mode: the source video shows the cited camera, the evidence cell the distance detail, the header the measurement', async ({ page }) => {
  await mockAsk(page, 'wh-main');
  await open(page);
  await page.locator('.preset').first().click();
  await expect(page.locator('#ask-status')).toContainText('Done');
  // the answer focuses its ground distance (ceiling_04) although the hit's primary camera is ceiling_00
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  const dock = page.locator('.wh-spatial-left .wh-stage-video');
  await expect(dock.locator('.wh-vcell')).toHaveAttribute('data-cam', 'ceiling_04');
  await expect(dock.locator('.wh-sv-head')).toContainText('Source· ceiling_04 · 00:04.6');
  await expect(dock.locator('.wh-sv-tag')).toHaveText('1.55 m ±0.12');
  await expect(page.locator('.wh-head .wh-focus')).toContainText('P1–F1 distance1.55 m ±0.12Measured');
  await expect(page.locator('.wh-head .wh-fact-measure')).toHaveText('1.55 m ±0.12');
  const cell = page.locator('.wh-spatial-right');
  await expect(cell.locator('.wh-evidence-tabs')).toBeHidden();
  // each actor's track sample nearest the sample time
  const tracks = JSON.parse(fs.readFileSync(path.join(OUT, KEY, MF.assets.tracks), 'utf8')).tracks;
  const [P1, F1] = ['P1', 'F1'].map((id) => tracks.find((t) => t.entity_id === id).pts.reduce((b, p) => (Math.abs(p[0] - 4600) < Math.abs(b[0] - 4600) ? p : b)));
  const dl = cell.locator('.wh-measure-detail');
  await expect(dl.locator('dt')).toHaveText(['P1 ground point', 'F1 ground point', 'Sample time', 'Sigma', 'Method', 'Camera']);
  await expect(dl.locator('dd')).toHaveText([
    `(${P1[1].toFixed(2)}, ${P1[2].toFixed(2)}) m`, `(${F1[1].toFixed(2)}, ${F1[2].toFixed(2)}) m`, '00:04.6', '±0.12 m', 'ground_distance_tri', 'ceiling_04',
  ]);
  await expect(cell.locator('.wh-pane-head')).toContainText('P1–F1 distance');
  // the layout holds in Answer mode: the page fills the stage, the 3D stage stays on top
  expect(await box(page, '.wh')).toEqual(await box(page, '#stage'));
  expect((await box(page, '.wh-top-slot')).height).toBeGreaterThan(1.7 * (await box(page, '.wh-spatial-left')).height);
});

test('Answer mode on a blind-spot evidence lists which cameras see the floor cell and which do not', async ({ page }) => {
  await open(page);
  await focus(page, { eid: 'e_blind', scene_key: KEY, cam_id: 'ceiling_00', t0_ms: 4000, t1_ms: 5000, observation_ids: [], kind: 'measure', method: 'floor_cell_visibility', value: { cell: [16, 7], state: 'occluded' }, unit: null, sigma: null });
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  const pane = page.locator('.wh-pane-measure');
  await expect(pane.locator('.wh-pane-head')).toContainText('Floor cell (16, 7)');
  await expect(pane.locator('.wh-cam-states li')).toHaveText(['ceiling_00occluded', 'ceiling_04sees it', 'eye_00sees it']);
  await expect(pane.locator('.wh-cam-states li.seen')).toHaveCount(2);
  await expect(page.locator('.wh-head .wh-fact-measure')).toHaveCount(0);
});

test('Swap with 3D puts the source video in the top slot and the 3D stage in the cell, and back', async ({ page }) => {
  await open(page);
  await page.locator('.wh-swap-btn').click();
  await expect(page.locator('.wh-top-slot .wh-stage-video video')).toHaveCount(1);
  await expect(page.locator('.wh-spatial-left .wh-canvas')).toBeVisible();
  const cell = await box(page, '.wh-spatial-left');
  await expect.poll(async () => (await box(page, '.wh-canvas')).height).toBeCloseTo(cell.height, 0);
  // the stage keeps working in the cell: a click still picks
  await clock(page, 'seek(1000)');
  await frames(page);
  const p1 = (await probe(page)).entities.P1;
  const [[x, y]] = await project(page, [[p1[0], p1[1], 0.9]]);
  await page.mouse.click(x, y);
  await expect.poll(async () => (await probe(page)).highlighted).toEqual(['P1']);
  await page.locator('.wh-swap-btn').click();
  await expect(page.locator('.wh-top-slot .wh-canvas')).toBeVisible();
  await expect(page.locator('.wh-spatial-left .wh-stage-video video')).toHaveCount(1);
});

test('the clip plays from 2.5 s before the cited evidence and stops 2.5 s after it', async ({ page }) => {
  await open(page);
  await focus(page, GROUND_EV);
  await page.locator('.wh-clip').click();
  expect(await clock(page, 'playing')).toBe(true);
  expect(await clock(page, 't')).toBeLessThan(2400);
  await clock(page, 'seek(7000)');
  await expect.poll(() => clock(page, 'playing')).toBe(false);
  expect(await clock(page, 't')).toBeGreaterThanOrEqual(7200);
  expect(await clock(page, 't')).toBeLessThan(7600);
});

test('Explore tabs: Cameras is a synchronized grid of every camera, Visibility the blind cells and the cell under the pointer', async ({ page }) => {
  await open(page);
  await clock(page, 'seek(4000)');
  await page.locator('.wh-evidence-tabs button', { hasText: 'Cameras' }).click();
  const grid = page.locator('.wh-pane-cameras');
  await expect(grid.locator('.wh-vcell')).toHaveCount(MF.cams.length);
  const cells = await grid.locator('.wh-vcell').evaluateAll((els) => els.map((e) => e.getBoundingClientRect()).map((r) => [Math.round(r.x), Math.round(r.y), Math.round(r.width)]));
  expect(cells[0][1]).toBe(cells[1][1]);
  expect(cells[2][0]).toBe(cells[0][0]);
  expect(cells[2][1]).toBeGreaterThan(cells[0][1]);
  await expect(page.locator('video')).toHaveCount(1 + MF.cams.length);

  await page.locator('.wh-evidence-tabs button', { hasText: 'Visibility' }).click();
  await expect(page.locator('video')).toHaveCount(1);
  const vis = page.locator('.wh-pane-visibility');
  await expect(vis.locator('.wh-pane-head')).toContainText(`${(await probe(page)).blind_cells} floor cells seen by no camera`);
  await expect(vis).toContainText('Point at the floor');
  // the centre of cell (16, 7): origin + (ix + 0.5) · cell_m
  const { origin: [x0, y0], cell_m } = MF.ground;
  const [[x, y]] = await project(page, [[x0 + 16.5 * cell_m, y0 + 7.5 * cell_m, 0]]);
  await page.mouse.move(x, y);
  await expect(vis).toContainText('Cell under the pointer (16, 7)');
  await expect(vis.locator('.wh-cam-states li')).toHaveText(['ceiling_00occluded', 'ceiling_04sees it', 'eye_00sees it']);
});

test('a 404 point cloud says Point cloud unavailable and the rest of the stage works', async ({ page }) => {
  await page.route(new RegExp(`/api/assets/${KEY}/${MF.assets.points}$`), (r) => r.fulfill({ status: 404, contentType: 'text/html', body: '<html>Not found</html>' }));
  await open(page);
  await expect(page.locator('.wh-head .wh-fact-warn')).toHaveText('Point cloud unavailable');
  await page.locator('.wh-layers-btn').click();
  await expect(page.locator('.wh-layers-pop input[data-layer="cloud"]')).toBeDisabled();
  expect((await probe(page)).blocks).toBe(0);
  await clock(page, 'seek(3000)');
  expect(Object.keys((await probe(page)).entities).sort()).toEqual(['F1', 'P1', 'P2']);
});

test('a measure without sigma shows ±σ n/a on the floor label, in the header and in the evidence cell', async ({ page }) => {
  await open(page);
  const e = { ...GROUND_EV, eid: 'e_no_sigma', sigma: null };
  await mod(page, 'warehouse/page.js', `k.showMeasure(${JSON.stringify(e)}, ['P1', 'F1'], { t0_ms: 4600, t1_ms: 5600 })`);
  await clock(page, 'seek(4600)');
  await expect(page.locator('.lbl-measure .wh-mval')).toHaveText('1.55 m ±σ n/a');
  await focus(page, e);
  await expect(page.locator('.wh-head .wh-fact-measure')).toHaveText('1.55 m ±σ n/a');
  await expect(page.locator('.wh-measure-detail dd').nth(3)).toHaveText('±σ n/a');
});

test('a ground distance without observations focuses the pair named in its request', async ({ page }) => {
  await open(page);
  await clock(page, 'seek(4600)');
  await focus(page, { ...GROUND_EV, eid: 'e_params', method: 'ground_distance_tri', observation_ids: [], params: { a: { scene_key: KEY, entity_id: 'P1' }, b: { scene_key: KEY, entity_id: 'F1' } } });
  await expect(page.locator('.wh-focus')).toContainText('P1–F1 distance');
  await expect.poll(async () => (await probe(page)).dimmed).toEqual(['P2']);
});

test('a hit whose primary camera is empty shows the first camera with video', async ({ page }) => {
  await open(page);
  await mod(page, 'warehouse/page.js', `k.applyHits([{ moment_id: 'm', scene_key: '${KEY}', primary_cam: '', cams: [], t0_ms: 1000, t1_ms: 2000, actor_refs: [], evidence_ids: [] }])`);
  await expect(page.locator('.wh-stage-video .wh-vcell')).toHaveAttribute('data-cam', 'ceiling_00');
  await expect(page.locator('.wh-sv-tabs button')).toHaveText(['ceiling_00']);
});

test('Draw zone: Esc cancels drawing and stays on the page; a click off the floor adds no point', async ({ page }) => {
  let body = null;
  await page.route(/\/api\/scene\/[a-z0-9_-]+\/zone$/, (r) => {
    body = r.request().postDataJSON();
    r.fulfill({ json: { polygon_world: body.polygon_world, cells: [], events_in_zone: [] } });
  });
  await open(page);
  const btn = page.locator('.wh-zone');
  await btn.click();
  await expect(btn).toHaveClass(/\bon\b/);
  await page.keyboard.press('Escape');
  await expect(btn).not.toHaveClass(/\bon\b/);
  expect(new URL(page.url()).hash).toBe(`#/warehouse/${KEY}`);

  // a canvas point whose ray meets the floor plane beyond the floor's edge: off the projected floor quad
  const { origin: [x0, y0], nx, ny, cell_m } = MF.ground, x1 = x0 + nx * cell_m, y1 = y0 + ny * cell_m;
  const quad = await project(page, [[x0, y0, 0], [x1, y0, 0], [x1, y1, 0], [x0, y1, 0]]);
  const c = await box(page, '.wh-canvas');
  const inside = ([px, py]) => quad.every(([ax, ay], i) => {
    const [bx, by] = quad[(i + 1) % 4];
    return Math.sign((bx - ax) * (py - ay) - (by - ay) * (px - ax)) === Math.sign((quad[1][0] - quad[0][0]) * (quad[2][1] - quad[0][1]) - (quad[1][1] - quad[0][1]) * (quad[2][0] - quad[0][0]));
  });
  const off = [[c.x + 30, c.y + 6], [c.x + c.width - 6, c.y + 6], [c.x + c.width / 2, c.y + 6]].find((p) => !inside(p));
  expect(off).toBeTruthy();
  await btn.click();
  await page.mouse.click(...off);
  const corners = await project(page, [[-2, -4, 0], [3, -4, 0], [3, 1, 0], [-2, 1, 0]]);
  for (const [x, y] of corners.slice(0, 3)) await page.mouse.click(x, y);
  await page.mouse.dblclick(...corners[3]);
  await expect(page.locator('.wh-zone-chip')).toHaveText('No events in zone');
  expect(body.polygon_world).toHaveLength(4);
});

test('a zone the server rejects is rolled back with a message', async ({ page }) => {
  await page.route(/\/api\/scene\/[a-z0-9_-]+\/zone$/, (r) => r.fulfill({ status: 500, body: 'boom' }));
  await open(page);
  await page.evaluate(async () => {
    const { bus } = await import(new URL('kernel/bus.js', document.baseURI).href);
    window.__zones = [];
    bus.on('context:zone', (z) => window.__zones.push(z && 'polygon'));
  });
  await page.locator('.wh-zone').click();
  const corners = await project(page, [[-2, -4, 0], [3, -4, 0], [3, 1, 0], [-2, 1, 0]]);
  for (const [x, y] of corners.slice(0, 3)) await page.mouse.click(x, y);
  await page.mouse.dblclick(...corners[3]);
  await expect(page.locator('.wh-toast')).toContainText('Zone not saved');
  expect(await page.evaluate(() => window.__zones)).toEqual(['polygon', null]);
  await expect(page.locator('.wh-zone-chip')).toBeHidden();
});

test('1024x768: the spatial row is one cell with Source video | Evidence | Cameras | Visibility tabs', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  await open(page);
  const tabs = page.locator('.wh-spatial-tabs button');
  await expect(tabs).toHaveText(['Source video', 'Evidence', 'Cameras', 'Visibility']);
  await expect(page.locator('.wh-stage-video')).toBeVisible();
  await expect(page.locator('.wh-spatial-right')).toBeHidden();
  await tabs.nth(1).click();
  await expect(page.locator('.wh-stage-video')).toBeHidden();
  await expect(page.locator('.wh-pane-measure')).toBeVisible();
  await tabs.nth(3).click();
  await expect(page.locator('.wh-pane-visibility')).toBeVisible();
  const h = await box(page, '.wh-head'), top = await box(page, '.wh-top-slot'), row = await box(page, '.wh-spatial');
  expect(h.height).toBeLessThanOrEqual(72);
  expect(top.y).toBeGreaterThanOrEqual(h.y + h.height - 1);
  expect(row.y).toBeGreaterThanOrEqual(top.y + top.height - 1);
  expect(await covering(page)).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  // with the focus chip the header still takes two rows
  await focus(page, GROUND_EV);
  await expect(page.locator('.wh-focus')).toBeVisible();
  expect((await box(page, '.wh-head')).height).toBeLessThanOrEqual(72);
});

test('390x844: no horizontal scroll, nothing overlaps, and 3D | Video | Evidence switches the slot under the header', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const seg = page.locator('.wh-mobile-seg button');
  await expect(seg).toHaveText(['3D', 'Video', 'Evidence']);
  const h = await box(page, '.wh-head'), top = await box(page, '.wh-top-slot'), s = await box(page, '.wh-mobile-seg'), stage = await box(page, '#stage');
  expect(top.y).toBeGreaterThanOrEqual(h.y + h.height - 1);
  expect(s.y).toBeGreaterThanOrEqual(top.y + top.height - 1);
  expect(s.y + s.height).toBeLessThanOrEqual(stage.y + stage.height + 1);
  expect(h.height).toBeLessThanOrEqual(72);
  expect(top.height).toBeGreaterThanOrEqual(150);
  expect(await covering(page)).toEqual([]);
  await seg.nth(1).click();
  await expect(page.locator('.wh-top-slot')).toBeHidden();
  const v = await box(page, '.wh-stage-video');
  expect(v.y).toBeGreaterThanOrEqual(h.y + h.height - 1);
  expect(v.y + v.height).toBeLessThanOrEqual(s.y + 1);
  await expect(page.locator('.wh-spatial-right')).toBeHidden();
  await seg.nth(2).click();
  await expect(page.locator('.wh-stage-video')).toBeHidden();
  await expect(page.locator('.wh-pane-measure')).toBeVisible();
  await seg.nth(0).click();
  await expect(page.locator('.wh-canvas')).toBeVisible();
  await focus(page, GROUND_EV);
  await expect(page.locator('.wh-focus')).toBeVisible();
  expect((await box(page, '.wh-head')).height).toBeLessThanOrEqual(72);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('screenshots: 1440x900 Explore and after a fixture answer, 1024x768, 390x844', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'root', 'one set of screenshots');
  await mockAsk(page, 'wh-main');
  await open(page);
  await clock(page, 'seek(4600)');
  await frames(page);
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, 'warehouse-1440-explore.png') });
  await page.locator('.preset').first().click();
  await expect(page.locator('#ask-status')).toContainText('Done');
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await page.waitForTimeout(1200);
  await page.screenshot({ path: path.join(SHOTS, 'warehouse-1440-answer.png') });
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(SHOTS, 'warehouse-1024.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(SHOTS, 'warehouse-390.png') });
});
