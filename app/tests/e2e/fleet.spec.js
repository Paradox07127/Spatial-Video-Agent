// Fleet page (card A4, board v2-4a): overview map under the replay clock, GPS gap styling, fl-where hit -> 2×2 result layout,
// building card, place card, Trip tab, Esc.
// The map loads only the local _map assets (fixtures/sva-out/_map), as on a site without internet.
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { mockApi, mockAsk } = require('./mockapi');

const LIVE = process.env.SVA_E2E_LIVE === '1';
const HEALTH = { ok: true, mode: 'dev', libs_ready: true, index_rows: { clips: 6, events: 3 }, assets_ok: true };
const OUT = path.resolve(__dirname, '..', '..', '..', 'fixtures', 'sva-out');
const replayOf = (key) => JSON.parse(fs.readFileSync(path.join(OUT, key, 'trip.geojson'), 'utf8')).features[0].properties.replay;
const T1 = 'fl-pie-fixture-01', T2 = 'fl-pie-fixture-02';

// Evaluates expr with m = the module at rel (same URL as the shell imported, so the same instance).
const mod = (page, rel, expr) =>
  page.evaluate(async ([r, e]) => new Function('m', `return (${e})`)(await import(new URL(r, document.baseURI).href)), [rel, expr]);
const seek = (page, t) => mod(page, 'kernel/clock.js', `m.clock.seek(${t})`);
const camera = (page) => mod(page, 'fleet/page.js', 'm.view.overview.camera()');
const car = (page, id) => page.locator(`.fl-car[data-trip="${id}"]`);
const box = async (page, sel) => page.locator(sel).boundingBox();
const big = (page) => mod(page, 'fleet/page.js', 'm.view.big');
// Esc from the default dashcam view: the full overview map.
async function toOverview(page) {
  await page.keyboard.press('Escape');
  await expect(page.locator('.fl-root')).not.toHaveClass(/\bix\b/);
}

// index.html's web fonts are not the map's (another card owns that file).
const FONTS = /^fonts\.(googleapis|gstatic)\.com$/;
let errors, external, pmtiles;
test.beforeEach(async ({ page }) => {
  errors = [];
  external = [];
  pmtiles = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(`${m.text()} @ ${m.location().url}`));
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('request', (r) => {
    const u = new URL(r.url());
    if (/^https?:$/.test(u.protocol) && u.hostname !== '127.0.0.1' && !FONTS.test(u.hostname)) external.push(r.url());
  });
  page.on('response', (r) => /\/_map\/\w+\.pmtiles$/.test(r.url()) && pmtiles.push(r.status()));
  await mockApi(page);
  // PMTiles reads byte ranges, which the mock does not serve: the map assets come from the app's asset route.
  if (!LIVE) await page.route(/\/api\/assets\/_map\//, (r) => r.continue());
  if (!LIVE) await page.route(/\/health$/, (r) => r.fulfill({ json: HEALTH }));
  await page.goto('#/fleet');
  await expect(page.locator('.fl-car')).toHaveCount(2);
});
test.afterEach(() => {
  expect(errors).toEqual([]);
  expect(external).toEqual([]);
});

test('offline map: basemap and 3D Massing tiles are read from the local PMTiles with HTTP Range', async ({ page }) => {
  await toOverview(page);
  const count = (source, layer) => mod(page, 'fleet/page.js', `m.view.overview.map.querySourceFeatures('${source}', { sourceLayer: '${layer}' }).length`);
  await mod(page, 'fleet/page.js', 'm.view.overview.map.jumpTo({ center: [-79.398, 43.6488], zoom: 16 })');
  await expect.poll(() => count('openmaptiles', 'transportation')).toBeGreaterThan(0);
  await expect.poll(() => count('massing', 'buildings')).toBeGreaterThan(0);
  await expect.poll(() => mod(page, 'fleet/page.js', "m.view.overview.map.queryRenderedFeatures({ layers: ['fl-massing'] }).length")).toBeGreaterThan(0);
  expect(pmtiles.length).toBeGreaterThan(0);
  expect(pmtiles.every((s) => s === 206)).toBe(true);
});

test('default state: dashcam view of the trip with video and the most events, boxes drawn, map a small inset', async ({ page }) => {
  await expect(page.locator('body')).toHaveAttribute('data-view', 'default');
  await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
  expect(await mod(page, 'fleet/page.js', 'm.view.ix.trip.id')).toBe(T1);
  expect(await big(page)).toBe('video');
  // E1, the trip's first event, starts at 3.0 s, where the mock has boxes.
  expect(await mod(page, 'kernel/clock.js', 'm.clock.t')).toBe(3000);
  const stage = await box(page, '#stage'), vid = await box(page, '.fl-vid'), map = await box(page, '.fl-map');
  const head = await box(page, '.fl-head');
  expect(Math.round(head.y)).toBe(Math.round(stage.y));
  expect(head.height).toBeGreaterThanOrEqual(32);
  expect(Math.round(vid.x)).toBe(Math.round(stage.x));
  expect(Math.round(vid.width)).toBe(Math.round(stage.width));
  expect(Math.round(vid.y)).toBe(Math.round(head.y + head.height));
  expect(Math.round(map.y)).toBeGreaterThanOrEqual(Math.round(vid.y + vid.height));
  await expect(page.locator('.fl-legend .lg-detected')).toBeVisible();
  const inked = () => page.locator('.fl-ovl').evaluate((c) => c.getContext('2d').getImageData(0, 0, c.width, c.height).data.filter((v, i) => i % 4 === 3 && v > 0).length);
  expect(await inked()).toBeGreaterThan(0);
  const video = page.locator('.fl-vid video');
  await mod(page, 'kernel/clock.js', 'm.clock.play()');
  await expect.poll(() => video.evaluate((v) => !v.paused && v.readyState >= 2)).toBe(true);
  await mod(page, 'kernel/clock.js', 'm.clock.pause()');
  // Outside the trip's coverage the video panel says so instead of a frozen frame.
  await seek(page, 20000);
  await expect(page.locator('.fl-novid')).toContainText('No video at this time');
  await expect(page.locator('.fl-novid')).toBeVisible();
  await expect(page.locator('.fl-vbox')).toBeHidden();
  await seek(page, 3000);
  await expect(page.locator('.fl-novid')).toBeHidden();
});

test('layout swap: swap with video puts map in top slot and dashcam in left spatial slot; swap again restores', async ({ page }) => {
  const stage = await box(page, '#stage');
  expect(await big(page)).toBe('video');
  const vid = await box(page, '.fl-vid'), map = await box(page, '.fl-map');
  expect(Math.round(vid.width)).toBe(Math.round(stage.width));
  expect(map.width).toBeLessThan(stage.width);

  // Click swap button in map toolbar
  await page.locator('.fl-swap-btn').click();
  await expect.poll(() => big(page)).toBe('map');
  const mapSwapped = await box(page, '.fl-map'), vidSwapped = await box(page, '.fl-vid');
  expect(Math.round(mapSwapped.width)).toBe(Math.round(stage.width));
  expect(vidSwapped.width).toBeLessThan(stage.width);

  // Swap back
  await page.locator('.fl-swap-btn').click();
  await expect.poll(() => big(page)).toBe('video');
  const mapRestored = await box(page, '.fl-map'), vidRestored = await box(page, '.fl-vid');
  expect(Math.round(vidRestored.width)).toBe(Math.round(stage.width));
  expect(mapRestored.width).toBeLessThan(stage.width);
});

test('on the big map a click on another trip car with video opens that trip dashcam view at the clock time', async ({ page }) => {
  // Trip 02 gets trip 01's dashcam and its media.
  const mf1 = JSON.parse(fs.readFileSync(path.join(OUT, T1, 'manifest.json'), 'utf8'));
  const mf2 = JSON.parse(fs.readFileSync(path.join(OUT, T2, 'manifest.json'), 'utf8'));
  await page.route(new RegExp(`/api/scene/${T2}/manifest$`), (r) => r.fulfill({ json: { ...mf2, cams: mf1.cams, assets: { ...mf2.assets, ego_dir: 'ego', boxes_dir: 'boxes' } } }));
  await page.route(new RegExp(`/api/assets/${T2}/(video|ego|boxes)/`), (r) => {
    const rel = new URL(r.request().url()).pathname.split(`/${T2}/`)[1].replace(/\d{5}\.json$/, '00000.json');
    return r.fulfill({ path: path.join(OUT, T1, rel) });
  });
  await page.reload();
  await expect(page.locator('.fl-car')).toHaveCount(2);
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.trip.id')).toBe(T1);
  await page.locator('.fl-swap-btn').click();
  await expect.poll(() => big(page)).toBe('map');
  const p = JSON.parse(fs.readFileSync(path.join(OUT, T2, 'trip.geojson'), 'utf8')).features.slice(1).map((f) => f.properties).find((q) => q.t_ms === 3000);
  await mod(page, 'fleet/page.js', `m.view.overview.map.jumpTo({ center: [${p.lon}, ${p.lat}] })`);
  await car(page, T2).locator('.fl-tag').click();
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix.trip.id')).toBe(T2);
  expect(await mod(page, 'fleet/page.js', 'm.view.ix.moment.t0_ms')).toBe(3000);
  expect(await big(page)).toBe('video');
});

test('Esc closes the default dashcam view to the full overview map; a click on a car with video opens it again', async ({ page }) => {
  await toOverview(page);
  await expect(page.locator('.fl-ix')).toHaveCount(0);
  expect(await page.locator('video').count()).toBe(0);
  expect(await box(page, '.fl-map')).toEqual(await box(page, '#stage'));
  expect(await page.evaluate(() => location.hash)).toBe('#/fleet');
  await car(page, T1).locator('.fl-tag').click();
  await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
  expect(await mod(page, 'fleet/page.js', 'm.view.ix.trip.id')).toBe(T1);
  expect(await big(page)).toBe('video');
});

test('overview: full-bleed map, replay clock label, trip lanes, event pins, thumbnail chip', async ({ page }) => {
  await toOverview(page);
  expect(await box(page, '.fl-map')).toEqual(await box(page, '#stage'));
  await expect(page.locator('.fl-map canvas.maplibregl-canvas')).toBeVisible();
  await expect(page.locator('.fl-sim')).toHaveAttribute('title', 'Simulated replay clock: trips were not filmed at the same time');
  await expect(page.locator('#timeline .lane-trip')).toHaveCount(2);
  await expect(page.locator('#timeline .ev-mark')).toHaveCount(2);
  // Every event is on the map, as its own pin or counted in a cluster.
  await expect.poll(() => page.evaluate(() => document.querySelectorAll('.fl-pin').length + [...document.querySelectorAll('.fl-cluster')].reduce((s, c) => s + +c.textContent, 0))).toBe(2);
  await seek(page, 2400);
  await expect(car(page, T1).locator('.fl-chip img')).toHaveAttribute('src', /fl-pie-fixture-01\/thumbs\/dashcam\/00002\.jpg$/);
  await expect(car(page, T2).locator('.fl-chip')).toHaveCount(0);
});

test('crowded event pins merge into a counted cluster; a cluster click zooms in to the pins; a pin click selects its event', async ({ page }) => {
  await toOverview(page);
  await mod(page, 'fleet/page.js', 'm.view.overview.map.jumpTo({ zoom: 10 })');
  const cl = page.locator('.fl-cluster');
  await expect(cl).toHaveText('2');
  await expect(cl).toHaveClass(/sev-2/);
  await expect(page.locator('.fl-pin')).toHaveCount(0);
  await cl.click();
  await expect(page.locator('.fl-pin')).toHaveCount(2);
  await expect(cl).toHaveCount(0);
  expect((await camera(page)).zoom).toBeGreaterThan(10);
  await page.locator('.fl-pin.sev-2').click();
  await expect(car(page, T1)).toHaveClass(/sel/);
  await expect(page.locator('#timeline .ev-mark.on')).toHaveCount(1);
});

test('a cluster on top of a car arrow still leaves the trip tag to select the car', async ({ page }) => {
  await toOverview(page);
  await mod(page, 'fleet/page.js', 'm.view.overview.map.jumpTo({ zoom: 10 })');
  await expect(page.locator('.fl-cluster')).toHaveCount(1);
  await car(page, T2).locator('.fl-tag').click();
  await expect(car(page, T2)).toHaveClass(/sel/);
});

test('clock.map uses each trip ReplayMapping', async ({ page }) => {
  const maps = [replayOf(T1), replayOf(T2)];
  const expected = (wall) => {
    for (const m of maps) {
      const t = m.source_start_ms + (wall - m.replay_start_ms) * m.rate;
      if (m.coverage_intervals.some(([a, b]) => t >= a && t < b)) return { trip_id: m.trip_id, t_ms: Math.round(t) };
    }
    return null;
  };
  expect(await mod(page, 'kernel/clock.js', 'm.clock.domain')).toEqual({ kind: 'fleet', replay: maps });
  expect(await mod(page, 'kernel/clock.js', 'm.clock.range')).toEqual({ t0: 0, t1: 60000 });
  for (const wall of [0, 5500, 9999, 10000, 30000, 59999, 60000]) {
    expect(await mod(page, 'kernel/clock.js', `m.clock.map(${wall})`)).toEqual(expected(wall));
  }
  expect(await mod(page, 'kernel/clock.js', 'm.clock.map(30000)')).toEqual({ trip_id: T2, t_ms: 30000 });
});

test('cars move with the replay clock; only the selected trip draws a trail; a trip out of coverage hides its car', async ({ page }) => {
  await toOverview(page);
  const trail = () => mod(page, 'fleet/page.js', "m.view.overview.map.queryRenderedFeatures({ layers: ['fl-trail'] }).length");
  await seek(page, 1000);
  const a = await box(page, `.fl-car[data-trip="${T2}"] .fl-arrow`);
  await seek(page, 9000);
  const b = await box(page, `.fl-car[data-trip="${T2}"] .fl-arrow`);
  expect(Math.hypot(b.x - a.x, b.y - a.y)).toBeGreaterThan(20);
  expect(await trail()).toBe(0);
  await car(page, T2).locator('.fl-arrow').click();
  await expect(car(page, T2)).toHaveClass(/sel/);
  await expect.poll(trail).toBeGreaterThan(0);
  await seek(page, 20000);
  await expect(car(page, T1).locator('.fl-arrow')).toBeHidden();
  await expect(car(page, T2).locator('.fl-arrow')).toBeVisible();
});

test('each trip has its own colour on route, gap, car arrow and lane; the selected trip is wider, the others fade', async ({ page }) => {
  await toOverview(page);
  // Rendered route pieces with their evaluated paint, one entry per trip and layer.
  const routes = () => mod(page, 'fleet/page.js', `Object.fromEntries(m.view.overview.map.queryRenderedFeatures({ layers: ['fl-route', 'fl-route-gap'] })
    .map((f) => [f.properties.trip + ' ' + f.layer.id, ['line-color', 'line-width', 'line-opacity'].map((k) => JSON.stringify(f.layer.paint[k]))]))`);
  const rgb = (loc) => loc.evaluate((e) => getComputedStyle(e).fill);
  await seek(page, 1000);
  let r;
  await expect.poll(async () => Object.keys((r = await routes())).sort()).toEqual([`${T1} fl-route`, `${T1} fl-route-gap`, `${T2} fl-route`]);
  expect(r[`${T1} fl-route`][0]).not.toBe(r[`${T2} fl-route`][0]);
  expect(r[`${T1} fl-route-gap`][0]).toBe(r[`${T1} fl-route`][0]);
  expect(await rgb(car(page, T1).locator('.fl-arrow'))).not.toBe(await rgb(car(page, T2).locator('.fl-arrow')));
  const lane = (id) => page.locator(`#timeline [data-lane="trip:${id}"] .s-ok`).first().evaluate((e) => getComputedStyle(e).backgroundColor);
  expect(await lane(T1)).not.toBe(await lane(T2));

  await car(page, T2).locator('.fl-tag').click();
  await expect.poll(async () => (r = await routes())[`${T2} fl-route`][1]).not.toBe(r[`${T1} fl-route`][1]);
  const [sel, other] = [r[`${T2} fl-route`], r[`${T1} fl-route`]];
  expect(+sel[1]).toBeGreaterThan(+other[1]);
  expect(+sel[2]).toBe(1);
  expect(+other[2]).toBeLessThan(1);
  expect(+r[`${T1} fl-route-gap`][2]).toBeLessThan(1);
});

test('hits that arrive before the trips are loaded are applied once they are', async ({ page }) => {
  let release;
  const held = new Promise((r) => (release = r));
  await page.route(/\/api\/fleet\/trips$/, async (r) => {
    await held;
    await r.fallback();
  });
  await page.reload();
  await mockAsk(page, 'fl-where');
  await page.locator('#ask-input').fill('Near Queen and Spadina, did a pedestrian cross in front of the car?');
  await page.keyboard.press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-view', 'hit');
  expect(await page.locator('.fl-car').count()).toBe(0);
  release();
  await expect(page.locator('.fl-hit')).toHaveCount(1);
  await expect(page.locator('.fl-ix')).toHaveCount(1);
  await expect(car(page, T1)).toHaveClass(/sel/);
});

test('inside the GPS gap the car is drawn last_known with its age', async ({ page }) => {
  await seek(page, 3000);
  await expect(car(page, T1)).not.toHaveClass(/stale/);
  await seek(page, 5500);
  await expect(car(page, T1)).toHaveClass(/stale/);
  await expect(car(page, T1).locator('.fl-age')).toHaveText('last fix 1.5 s ago');
  await seek(page, 7200);
  await expect(car(page, T1)).not.toHaveClass(/stale/);
});

// Asks the fl-where question and waits for the result layout.
async function askWhere(page) {
  await mockAsk(page, 'fl-where');
  await page.locator('#ask-input').fill('Near Queen and Spadina, did a pedestrian cross in front of the car?');
  await page.keyboard.press('Enter');
  await expect(page.locator('.fl-ix')).toHaveCount(1);
  // The cited onset (3 s) now owns the answer; these detail checks explicitly explore the later 6 s frame.
  await expect.poll(() => mod(page, 'kernel/clock.js', 'm.clock.t')).toBe(3000);
  await page.locator('.stage-controls [data-mode="explore"]').click();
  await seek(page, 6000);
}

test('fl-where: hits open the result layout at once; building card; Esc restores the overview camera', async ({ page }) => {
  await toOverview(page);
  await mockAsk(page, 'fl-where');
  const cam0 = await camera(page);
  await page.evaluate(() => {
    window.__t = {};
    new MutationObserver(() => {
      const now = performance.now();
      if (document.body.dataset.view === 'hit') window.__t.hit ??= now;
      if (document.querySelector('.fl-ix')) window.__t.ix ??= now;
    }).observe(document.body, { attributes: true, childList: true, subtree: true });
  });
  await page.locator('#ask-input').fill('Near Queen and Spadina, did a pedestrian cross in front of the car?');
  await page.keyboard.press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-view', 'hit');
  await expect(page.locator('.fl-ix')).toHaveCount(1);
  // No intermediate overview state: the grid opens with the hit, the map is not flown.
  const t = await page.evaluate(() => window.__t);
  expect(t.ix - t.hit).toBeLessThan(300);
  expect(await mod(page, 'fleet/page.js', 'm.view.overview.map.isMoving()')).toBe(false);

  await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
  const stage = await box(page, '#stage'), W = Math.round(stage.width);
  const [head, dash, map, evCell] = await Promise.all(['.fl-head', '.fl-vid', '.fl-map', '.fl-evidence-cell'].map((s) => box(page, s)));
  expect(Math.round(head.y)).toBe(Math.round(stage.y));
  expect(Math.round(dash.width)).toBe(W);
  expect(Math.round(dash.y)).toBe(Math.round(head.y + head.height));
  expect(Math.round(map.y)).toBeGreaterThanOrEqual(Math.round(dash.y + dash.height));
  expect(Math.round(evCell.y)).toBe(Math.round(map.y));
  await expect(page.locator('.fl-3d canvas')).toBeVisible();
  await expect(page.locator('.fl-vid video')).toHaveCount(1);
  await expect(page.locator('.fl-map canvas.maplibregl-canvas')).toBeVisible();
  await expect(page.locator('.fl-map .fl-cap')).toHaveText('Dashed ring = GPS uncertainty');
  await expect(page.locator('.fl-facts')).toHaveCSS('height', '32px');
  await expect(page.locator('.fl-badge')).toHaveText('Monocular range: indicative only');
  await expect(page.locator('.fl-views button')).toHaveText(['Chase', 'Top', 'Dashcam']);
  expect(await mod(page, 'kernel/clock.js', 'm.clock.t')).toBe(3000);
  await page.locator('.stage-controls [data-mode="explore"]').click();
  await seek(page, 6000);
  await expect(page.locator('.fl-gnss')).toHaveText('GNSS ±7.0 m · last fix');
  await expect(page.locator('.fl-al')).toContainText(['P1 approx']);

  await seek(page, 2000);
  await page.locator('.fl-col', { hasText: 'Fixture Block A' }).click();
  const card = page.locator('.fl-card');
  await expect(card).toBeVisible();
  await expect(card.locator('.fl-bhead')).toContainText('Fixture Block A');
  await expect(card).toContainText('18 m · 5 floors');
  await expect(card).toContainText('Fixture Cafe');
  await expect(card.locator('.fl-sign')).toContainText('Sign does not match the map');
  await expect(card.locator('.fl-sign')).toContainText('FIXTURE CAFE');
  expect(await mod(page, 'fleet/page.js', "m.view.overview.map.getFeatureState({ source: 'fl-bld', id: 'w1001' })")).toEqual({ focus: true });

  await page.locator('.fl-undist').click();
  await expect(page.locator('.fl-remap')).toBeVisible();
  // The view buttons take clicks in the 3D view.
  await page.locator('.fl-evidence-tabs button[data-tab="3d"]').click();
  await page.locator('.fl-views [data-v="top"]').click();
  await expect(page.locator('.fl-views [data-v="top"]')).toHaveClass(/on/);

  await page.keyboard.press('Escape');
  await expect(page.locator('body')).toHaveAttribute('data-view', 'default');
  await expect(page.locator('.fl-ix')).toHaveCount(0);
  expect(await page.locator('video').count()).toBe(0);
  await expect(page.locator('.fl-card')).toBeHidden();
  await expect(page.locator('.fl-map')).toBeVisible();
  expect(await box(page, '.fl-map')).toEqual(await box(page, '#stage'));
  await expect(page.locator('.fl-gps')).toHaveCount(0);
  await expect.poll(async () => {
    const c = await camera(page);
    return Math.max(Math.abs(c.center[0] - cam0.center[0]), Math.abs(c.center[1] - cam0.center[1]), Math.abs(c.zoom - cam0.zoom) / 1e3, Math.abs(c.pitch - cam0.pitch) / 1e3, Math.abs(c.bearing - cam0.bearing) / 1e3);
  }).toBeLessThan(1e-7);
});

test('intersection view: agent labels do not overlap, the one nearest the ego wins, a selected agent is always labelled', async ({ page }) => {
  test.skip(LIVE, 'needs injected agents');
  const ego = JSON.parse(fs.readFileSync(path.join(OUT, T1, 'ego', '00000.json'), 'utf8'));
  // Cars 5 cm apart straight ahead behind P1 (x = 9 m): their labels stack on one spot at any canvas size; V11 is nearest.
  const queue = Array.from({ length: 6 }, (_, i) => ({ entity_id: `V${11 + i}`, x: 14 + 0.05 * i, y: 0, yaw: 0, cls: 'car', dims: [4.5, 1.8, 1.5], sigma_m: 3 }));
  for (const f of ego.frames) f.vehs = [...f.vehs, ...queue];
  await page.route(/\/api\/assets\/fl-pie-fixture-01\/ego\/00000\.json$/, (r) => r.fulfill({ json: ego }));
  // The default dashcam view has already loaded the chunk without the queue.
  await page.reload();
  await askWhere(page);
  await expect(page.locator('.fl-al', { hasText: 'V16' })).toHaveCount(1);
  const shown = () => page.$$eval('.fl-al', (els) => els.filter((e) => e.checkVisibility({ visibilityProperty: true })).map((e) => {
    const r = e.getBoundingClientRect();
    return { id: e.querySelector('b').textContent, left: r.left, right: r.right, top: r.top, bottom: r.bottom };
  }));
  const overlapping = (bs) => bs.filter((a, i) => bs.some((b, j) => j > i && a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom));
  await expect.poll(async () => overlapping(await shown()).length).toBe(0);
  const ids = (await shown()).map((b) => b.id);
  expect(ids).toContain('P1');
  expect(ids.length).toBeLessThan(7);
  expect(ids).not.toContain('V16');
  await mod(page, 'kernel/selection.js', "m.selection.set(['V16'], { source: 'user' })");
  await expect(page.locator('.fl-al', { hasText: 'V16' })).toBeVisible();
});

test('building card: a markup string as the height or floors from the API is shown as text, not run', async ({ page }) => {
  test.skip(LIVE, 'needs an injected building');
  const xss = (id) => `<img src=x id=${id} onerror="window.__xss=1">`;
  const b = JSON.parse(fs.readFileSync(path.join(OUT, T1, 'buildings.json'), 'utf8')).buildings.w1001;
  await page.route(/\/api\/fleet\/building\/w1001$/, (r) => r.fulfill({ json: { ...b, bid: 'w1001', facade_note: null, height_m: xss('xss-h'), floors: xss('xss-f') } }));
  await askWhere(page);
  await seek(page, 2000);
  await page.locator('.fl-col', { hasText: 'Fixture Block A' }).click();
  const card = page.locator('.fl-card');
  await expect(card.locator('.fl-bhead')).toContainText('Fixture Block A');
  await expect(card).toContainText(`${xss('xss-h')} m · ${xss('xss-f')} floors`);
  await expect(page.locator('#xss-h, #xss-f')).toHaveCount(0);
  expect(await page.evaluate(() => window.__xss)).toBeUndefined();
});

test('result map: the car sits in a dashed GPS ring of radius sigma_m in pixels at the current zoom', async ({ page }) => {
  await askWhere(page);
  // Ring diameter (px) expected for sigma (m) at the car's latitude and the map's zoom; MapLibre's world is 512 · 2^zoom px.
  const expected = (sigma) => mod(page, 'fleet/page.js', `2 * ${sigma} / (40075016.686 * Math.cos(43.6487888 * Math.PI / 180) / (512 * 2 ** m.view.overview.map.getZoom()))`);
  const ring = page.locator('.fl-gps i');
  await expect(ring).toHaveCSS('border-top-style', 'dashed');
  const close = async (sigma) => Math.abs((await ring.boundingBox()).width - (await expected(sigma)));
  // The ego frame at 6.0 s has sigma_m 7.0, the one at 2.0 s 3.0.
  await expect.poll(() => close(7)).toBeLessThan(1);
  await mod(page, 'fleet/page.js', 'm.view.overview.map.jumpTo({ zoom: m.view.overview.map.getZoom() + 1 })');
  await expect.poll(() => close(7)).toBeLessThan(1);
  expect((await ring.boundingBox()).width).toBeGreaterThan(20);
  await seek(page, 2000);
  await expect.poll(() => close(3)).toBeLessThan(1);
});

test('place card: the place under the car from the map, side and distance, the C5 reading with its map check', async ({ page }) => {
  await askWhere(page);
  const card = page.locator('.fl-place');
  await expect(card).toContainText('PLACE');
  await expect(card.locator('.fl-place-name')).toHaveText('Queen St W & Spadina Ave');
  // The junction is 23 m straight ahead of the car at 6.0 s; it has no footprint in the map data.
  await expect(card.locator('.fl-place-meta')).toHaveText('Ahead · 23 m');
  await expect(card.locator('.fl-model')).toContainText('Brick facade with a cafe sign at street level.');
  await expect(card.locator('.fl-model')).toContainText('Cosmos C5 · checked by map: conflict');
});

test('Trip tab: renders into #tab-trip on panel:tab with the trip, GPS quality over the crossing and other passes', async ({ page }) => {
  await askWhere(page);
  await page.locator('#tabs [data-tab="trip"]').click();
  const tab = page.locator('#tab-trip');
  await expect(tab).toBeVisible();
  await expect(tab.locator('.tt-head')).toContainText(T1);
  await expect(tab.locator('.tt-head')).toContainText('PIE fixture trip 01');
  // The crossing (3.0–8.0 s) runs through the GPS gap; the largest ego sigma in it is 8.8 m.
  const gps = tab.locator('.tt-gps');
  await expect(gps).toHaveClass(/warn/);
  await expect(gps).toContainText('GPS used a last-known fix during this interval');
  await expect(gps).toContainText('±8.8 m');
  const other = tab.locator('.tt-other');
  await expect(other.locator('summary')).toContainText('Other passes near Queen St W & Spadina Ave');
  await expect(other.locator('summary .mono')).toHaveText('1');
  await expect(other.locator('summary')).toContainText('1 GPS only');
  expect(await other.evaluate((e) => e.open)).toBe(false);
  await expect(tab.locator('.tt-facts')).toContainText('10.0 s');
});

test('going back from a trip deep link to #/fleet keeps the trip lanes', async ({ page }) => {
  await page.goto(`#/fleet/${T1}@2000`);
  await expect(page.locator('#timeline .lane-trip')).toHaveCount(2);
  await page.goto('#/fleet');
  await expect(page.locator('#timeline .lane-trip')).toHaveCount(2);
});

test('the place presets asked from a chip run against all trips; the speeding preset and typed questions keep the trip', async ({ page }) => {
  test.skip(LIVE, 'needs mocked asks');
  const { sseBody } = require('./mockapi');
  const TRIP = `#/fleet/${T1}@0`, ROM = 'Near the ROM, did a pedestrian cross in front of the car?';
  const sent = [];
  page.on('request', (r) => r.url().endsWith('/api/agent/ask') && sent.push(r.postDataJSON().context));
  const scope = async (n) => {
    await expect.poll(() => sent.length).toBe(n);
    return [sent[n - 1].workspace, sent[n - 1].scene_key ?? null];
  };
  // Esc ends the shown run, so the preset chips come back
  const onTrip = async () => {
    if (sent.length) {
      if (await page.locator('body').getAttribute('data-stage') === 'evidence') await page.keyboard.press('Escape');
      await page.keyboard.press('Escape');
    }
    await expect(page.locator('body')).toHaveAttribute('data-view', 'default');
    await page.goto(TRIP);
    await expect(page.locator('#source-tag')).toHaveText(T1);
    await expect(page.locator('.fl-car')).toHaveCount(2);
  };
  const answered = (v = 'Supported') => expect(page.locator('#answer .verdict')).toHaveText(v);
  await mockAsk(page, 'fl-where');
  // workspace chips on a trip
  await onTrip();
  await page.locator('#presets .preset').nth(0).click();
  expect(await scope(1)).toEqual(['fleet', null]);
  // Search was global; the answer now activates its cited trip and time.
  await expect(page).toHaveURL(new RegExp(`#/fleet/${T1}@3000$`));
  await expect(page.locator('#source-tag')).toHaveText(T1);
  await answered();
  await onTrip();
  await page.locator('#presets .preset').nth(2).click();
  expect(await scope(2)).toEqual(['fleet', T1]);
  await answered();
  await page.locator('#ask-input').fill(ROM);
  await page.keyboard.press('Enter');
  expect(await scope(3)).toEqual(['fleet', T1]);
  await answered();
  // an abstain suggestion
  await onTrip();
  await page.route(/\/api\/agent\/ask$/, (r) => r.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: sseBody('wh-nomatch') }));
  await page.locator('#ask-input').fill('Did a cyclist pass the car?');
  await page.keyboard.press('Enter');
  expect(await scope(4)).toEqual(['fleet', T1]);
  await answered('No match');
  await page.locator('#answer .ans-instead .preset').nth(1).click();
  expect(await scope(5)).toEqual(['fleet', null]);
  await expect(page).toHaveURL(/#\/fleet$/);
});

test('⌘K lists each trip and jumps to it', async ({ page }) => {
  await page.locator('#cmdk-btn').click();
  await page.keyboard.type('fixture-02');
  await expect(page.locator('#cmdk li').first()).toContainText('fixture-02');
  await page.keyboard.press('Enter');
  await expect.poll(() => page.evaluate(() => location.hash)).toMatch(new RegExp(`^#/fleet/${T2}@\\d+$`));
});

test('Layout A: video full-width on top with map + evidence cell below at 1440x900', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
  const stage = await box(page, '#stage');
  const head = await box(page, '.fl-head');
  const vid = await box(page, '.fl-vid');
  const map = await box(page, '.fl-map');
  const ev = await box(page, '.fl-evidence-cell');
  expect(Math.round(head.y)).toBe(Math.round(stage.y));
  expect(head.height).toBeGreaterThanOrEqual(32);
  expect(Math.round(vid.x)).toBe(Math.round(stage.x));
  expect(Math.round(vid.width)).toBe(Math.round(stage.width));
  expect(Math.round(vid.y)).toBe(Math.round(head.y + head.height));
  expect(Math.round(map.y)).toBeGreaterThanOrEqual(Math.round(vid.y + vid.height));
  expect(Math.round(ev.y)).toBe(Math.round(map.y));
  expect(map.width).toBeGreaterThanOrEqual(300);
  expect(ev.width).toBeGreaterThanOrEqual(300);
});

test('Layout A: evidence cell switches to Speed for a speed answer and to 3D for a distance answer', async ({ page }) => {
  await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
  await expect(page.locator('.fl-evidence-tabs button[data-tab="3d"]')).toHaveClass(/on/);
  await expect(page.locator('.fl-3d-pane')).toBeVisible();

  // Speed answer switches to Speed chart
  await mod(page, 'fleet/page.js', `m.applyHits([{ scene_key: '${T1}', t0_ms: 3000, t1_ms: 5000, why: ['Speed limit check'], evidence_ids: ['e_speed_1'] }])`);
  await expect(page.locator('.fl-evidence-tabs button[data-tab="speed"]')).toHaveClass(/on/);
  await expect(page.locator('.fl-speed-pane')).toBeVisible();
  await expect(page.locator('.fl-3d-pane')).toBeHidden();

  // Distance answer switches back to 3D ego view
  await mod(page, 'fleet/page.js', `m.applyUI({ cmd: 'setView', args: { view: '3d' } })`);
  await expect(page.locator('.fl-evidence-tabs button[data-tab="3d"]')).toHaveClass(/on/);
  await expect(page.locator('.fl-3d-pane')).toBeVisible();
});

test('Layout A: map follows the car during playback', async ({ page }) => {
  await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
  await expect(page.locator('.fl-follow-btn')).toHaveClass(/on/);
  await seek(page, 2000);
  const center1 = await camera(page);
  await seek(page, 5000);
  const center2 = await camera(page);
  expect(Math.hypot(center2.center[0] - center1.center[0], center2.center[1] - center1.center[1])).toBeGreaterThan(0);
});

test('Layout A: no element overlaps the video', async ({ page }) => {
  await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
  const vidBox = await box(page, '.fl-vid');
  const stageControls = await box(page, '.fl-head .stage-controls');
  const facts = await box(page, '.fl-facts');
  const map = await box(page, '.fl-map');
  const ev = await box(page, '.fl-evidence-cell');

  expect(stageControls.y + stageControls.height).toBeLessThanOrEqual(vidBox.y + 1);
  expect(facts.y + facts.height).toBeLessThanOrEqual(vidBox.y + 1);
  expect(map.y).toBeGreaterThanOrEqual(vidBox.y + vidBox.height - 1);
  expect(ev.y).toBeGreaterThanOrEqual(vidBox.y + vidBox.height - 1);
});

test('Layout A: 390x844 layout shows video, segmented control, and panel without horizontal scroll', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.fl-root')).toHaveClass(/\bix\b/);
  await expect(page.locator('.fl-vid')).toBeVisible();
  await expect(page.locator('.fl-spatial-tabs')).toBeVisible();

  const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(scrollWidth).toBeLessThanOrEqual(390);

  await page.locator('.fl-spatial-tabs button[data-tab="3d"]').click();
  await expect(page.locator('.fl-3d-pane')).toBeVisible();
  await page.locator('.fl-spatial-tabs button[data-tab="speed"]').click();
  await expect(page.locator('.fl-speed-pane')).toBeVisible();
});

