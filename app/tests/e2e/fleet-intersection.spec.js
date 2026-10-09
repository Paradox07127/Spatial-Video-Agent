// Intersection view buildings: a footprint hole stays open and a MultiPolygon building is one pickable building. Picks are
// probed in the top view, whose canvas centre looks straight down at ego (12, 0).
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { mockApi } = require('./mockapi');

const LIVE = process.env.SVA_E2E_LIVE === '1';
const HEALTH = { ok: true, mode: 'dev', libs_ready: true, index_rows: { clips: 6, events: 3 }, assets_ok: true };
const OUT = path.resolve(__dirname, '..', '..', '..', 'fixtures', 'sva-out');
const CAM = JSON.parse(fs.readFileSync(path.join(OUT, 'fl-pie-fixture-01', 'manifest.json'), 'utf8')).cams[0];

let errors;
test.beforeEach(async ({ page }) => {
  errors = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(`${m.text()} @ ${m.location().url}`));
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.route(/tiles\.openfreemap\.org/, (r) => r.abort());
  await mockApi(page);
  // PMTiles reads byte ranges, which the mock does not serve: the map assets come from the app's asset route.
  if (!LIVE) await page.route(/\/api\/assets\/_map\//, (r) => r.continue());
  if (!LIVE) await page.route(/\/health$/, (r) => r.fulfill({ json: HEALTH }));
  await page.goto('#/fleet');
  await expect(page.locator('.fl-car')).toHaveCount(2);
});
test.afterEach(() => expect(errors.filter((e) => !/net::ERR_FAILED @ https:\/\/tiles\.openfreemap\.org\//.test(e))).toEqual([]));

// Mounts the view on w1 (a block with a courtyard) and w2 (a MultiPolygon: a solid part and a part with a courtyard),
// rectangles given in east/north metres from the origin. window.__pickAt(e, n) = the bid picked at the canvas centre with
// the ego at (e, n) facing east, i.e. the bid at world (e + 12, n), or null.
async function mount(page) {
  await page.evaluate(async (cam) => {
    const { createIntersection } = await import(new URL('fleet/intersection.js', document.baseURI).href);
    const O = [-79.398, 43.649], M = (Math.PI / 180) * 6378137, k = Math.cos((O[1] * Math.PI) / 180);
    const ll = ([e, n]) => [O[0] + e / (M * k), O[1] + n / M];
    const rect = (e0, e1, n0, n1) => [[e0, n0], [e1, n0], [e1, n1], [e0, n1], [e0, n0]].map(ll);
    const b = (geom) => ({ name: null, height_m: 10, pois: [], geom });
    const buildings = {
      w1: b({ type: 'Polygon', coordinates: [rect(4, 40, -10, 10), rect(8, 16, -4, 4)] }),
      w2: b({ type: 'MultiPolygon', coordinates: [[rect(4, 14, 30, 40)], [rect(20, 40, 30, 50), rect(26, 34, 36, 44)]] }),
    };
    const el = Object.assign(document.createElement('div'), { className: 'fl-3d' });
    el.style.cssText = 'position:fixed;left:0;top:0;width:600px;height:400px;z-index:99';
    document.body.append(el);
    let picked = null;
    const ix = createIntersection(el, { cam, origin: O, buildings, onPick: (bid) => (picked = bid) });
    ix.setView('top');
    const canvas = el.querySelector('canvas');
    window.__pickAt = async (e, n) => {
      const [lon, lat] = ll([e, n]);
      ix.setFrame({ ego: { lon, lat, heading: 90, sigma_m: 1, state: 'observed' }, peds: [], vehs: [] }, new Set());
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      const r = canvas.getBoundingClientRect(), at = { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
      picked = null;
      canvas.dispatchEvent(new PointerEvent('pointerdown', at));
      canvas.dispatchEvent(new PointerEvent('pointerup', at));
      return picked;
    };
  }, CAM);
}
const pickAt = (page, e, n) => page.evaluate(([a, b]) => window.__pickAt(a, b), [e, n]);

test('a footprint hole is open: the courtyard picks nothing, the block around it picks the building', async ({ page }) => {
  await mount(page);
  expect(await pickAt(page, 0, 0)).toBeNull();
  expect(await pickAt(page, 18, 0)).toBe('w1');
});

test('a MultiPolygon building: both parts pick it, the courtyard of one part picks nothing', async ({ page }) => {
  await mount(page);
  expect(await pickAt(page, -3, 35)).toBe('w2');
  expect(await pickAt(page, 18, 40)).toBeNull();
  expect(await pickAt(page, 21, 35)).toBe('w2');
});
