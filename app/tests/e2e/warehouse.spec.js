// Warehouse page (card A3) on the fixture scene wh-sdg-fixture-01. Page state is read through warehouse/page.js's
// e2e hooks (project, probe) imported by the same URL the shell used, so it is the mounted instance.
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { mockApi, mockAsk, hazardZone } = require('./mockapi');

const LIVE = process.env.SVA_E2E_LIVE === '1';
const KEY = 'wh-sdg-fixture-01';
const OUT = path.resolve(__dirname, '..', '..', '..', 'fixtures', 'sva-out');
const MF = JSON.parse(fs.readFileSync(path.join(OUT, KEY, 'manifest.json'), 'utf8'));
const HEALTH = { ok: true, mode: 'dev', libs_ready: true, index_rows: { clips: 6 }, assets_ok: true };
const FRAME_MS = 1000 / MF.fps;

const mod = (page, file, expr) =>
  page.evaluate(async ([f, e]) => {
    const k = await import(new URL(f, document.baseURI).href);
    return new Function('k', `return (${e})`)(k);
  }, [file, expr]);
const clock = (page, expr) => mod(page, 'kernel/clock.js', `k.clock.${expr}`);
const probe = (page) => mod(page, 'warehouse/page.js', 'k.probe()');
const project = (page, pts) => mod(page, 'warehouse/page.js', `k.project(${JSON.stringify(pts)})`);
const frameNo = (src) => src && src.match(/\/(\d{5})\.jpg$/)[1];

// Per-test action store (mockapi's is shared by every test in the worker), same responses as mockapi.
function mockActions(page) {
  const store = { actions: [], zones: [], posts: [] };
  if (LIVE) return store;
  page.route(/\/api\/actions(\?|$)/, (r) => {
    const k = new URL(r.request().url()).searchParams.get('scene_key');
    return r.fulfill({ json: { actions: store.actions.filter((a) => a.scene_key === k), zones: store.zones.filter((z) => z.scene_key === k) } });
  });
  page.route(/\/api\/actions\/trigger$/, (r) => {
    const req = r.request().postDataJSON();
    store.posts.push(req);
    const hit = store.actions.find((a) => a.scene_key === req.scene_key && a.event_id === req.event_id);
    if (hit) return r.fulfill({ json: { ...hit, existing: true } });
    const n = store.actions.length + 1;
    const a = { ...req, action_id: `a${n}`, review_task_id: `RT-${n}`, zone_id: `Z${n}`, delivered: true };
    store.actions.push(a);
    if (req.event_id && req.actions.includes('hazard_zone')) store.zones.push(hazardZone(req, n));
    return r.fulfill({ json: { action_id: a.action_id, delivered: true, review_task_id: a.review_task_id, zone_id: a.zone_id, existing: false } });
  });
  return store;
}

// mockapi answers assets without Range; a <video> served that way cannot seek, so mp4 gets 206 partial responses here.
async function mockVideoRange(page) {
  if (LIVE) return;
  await page.route(/\/api\/assets\/.+\.mp4$/, (r) => {
    const buf = fs.readFileSync(path.join(OUT, new URL(r.request().url()).pathname.replace(/^.*?\/api\/assets\//, '')));
    const m = (r.request().headers().range || 'bytes=0-').match(/bytes=(\d+)-(\d*)/);
    const a = +m[1], b = m[2] ? Math.min(+m[2], buf.length - 1) : buf.length - 1;
    r.fulfill({ status: 206, headers: { 'content-type': 'video/mp4', 'accept-ranges': 'bytes', 'content-range': `bytes ${a}-${b}/${buf.length}` }, body: buf.subarray(a, b + 1) });
  });
}

// OpenCV pinhole projection of world points with K and cam2world (OpenGL camera: negate y, z), in source pixels.
function pinhole(cam, X) {
  const M = cam.cam2world, [fx, fy, cx, cy] = cam.K;
  const C = [M[12], M[13], M[14]], col = (i) => [M[4 * i], M[4 * i + 1], M[4 * i + 2]];
  return X.map((p) => {
    const d = [p[0] - C[0], p[1] - C[1], p[2] - C[2]];
    const [x, y, z] = [0, 1, 2].map((i) => col(i).reduce((s, v, k) => s + v * d[k], 0));
    return [cx + (fx * x) / -z, cy + (fy * -y) / -z];
  });
}

let errors;
test.beforeEach(async ({ page }) => {
  errors = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(`${m.text()} @ ${m.location().url}`));
  page.on('pageerror', (e) => errors.push(String(e)));
  await mockApi(page);
  if (!LIVE) await page.route(/\/health$/, (r) => r.fulfill({ json: HEALTH }));
});
test.afterEach(() => expect(errors).toEqual([]));

async function open(page) {
  await page.goto(`#/warehouse/${KEY}`);
  await expect(page.locator('.wh')).toHaveAttribute('data-scene', KEY);
}

test('default state: no <video>, three frustum bases carry the current second’s thumbnail and change with the clock', async ({ page }) => {
  mockActions(page);
  await open(page);
  await expect(page.locator('body')).toHaveAttribute('data-view', 'default');
  await expect(page.locator('#ask-input')).toBeVisible();
  await expect(page.locator('.wh-pin')).toContainText('Worth a look');
  const thumbs = async () => Object.values((await probe(page)).thumbs).map(frameNo);
  await expect.poll(thumbs).toEqual(['00000', '00000', '00000']);
  await clock(page, 'seek(3500)');
  await expect.poll(thumbs).toEqual(['00003', '00003', '00003']);
  // eye_00 has no video in [6000, 7000): its base turns grey.
  await clock(page, 'seek(6200)');
  await expect.poll(async () => (await probe(page)).thumbs).toEqual({ ceiling_00: expect.stringMatching(/00006\.jpg$/), ceiling_04: expect.stringMatching(/00006\.jpg$/), eye_00: null });
  expect(await page.locator('video').count()).toBe(0);
});

test('people and the forklift follow the master clock; a single-view frame draws only the root, tagged single view', async ({ page }) => {
  mockActions(page);
  await open(page);
  await clock(page, 'seek(1000)');
  const a = (await probe(page)).entities;
  await clock(page, 'seek(3000)');
  const b = (await probe(page)).entities;
  expect(Object.keys(a).sort()).toEqual(['F1', 'P1', 'P2']);
  for (const id of ['P1', 'P2', 'F1']) expect(Math.hypot(b[id][0] - a[id][0], b[id][1] - a[id][1])).toBeGreaterThan(0.5);
  expect(b.F1[0]).toBeCloseTo(2.6, 5);
  expect((await probe(page)).single).toEqual([]);
  await clock(page, 'seek(2100)');
  expect((await probe(page)).single).toEqual(['P2']);
  await expect(page.locator('.lbl-ent', { hasText: 'P2' })).toContainText('single view');
  expect((await probe(page)).blind_cells).toBeGreaterThan(0);
});

test('wh-main replay: hits → three synced <video> within one frame (mediaTime), model primitives, Esc back to default', async ({ page }) => {
  mockActions(page);
  await mockVideoRange(page);
  await mockAsk(page, 'wh-main');
  await open(page);
  await page.locator('.preset').first().click();
  await expect(page.locator('body')).toHaveAttribute('data-view', 'hit');
  // the moment's cameras are the ones the agent used
  await expect(page.locator('.wh-rcam.used')).toHaveCount(3);
  await page.locator('#tabs [data-tab="videos"]').click();
  await expect(page.locator('#panel video')).toHaveCount(3);
  expect(await clock(page, 't')).toBe(4600);
  const presented = () => page.$$eval('#panel video', (vs) => vs.map((v) => (v.dataset.sceneMs === undefined ? null : +v.dataset.sceneMs)));
  await expect.poll(async () => {
    const ms = await presented();
    return ms.every((x) => x !== null && Math.abs(x - 4600) <= FRAME_MS) && Math.max(...ms) - Math.min(...ms) <= FRAME_MS;
  }, { timeout: 10_000 }).toBe(true);
  await expect(page.locator('.wh-vcell[data-cam="ceiling_04"] figcaption')).toContainText('+120 ms');
  expect((await probe(page)).highlighted).toEqual(['P1', 'F1']);
  expect((await probe(page)).dimmed).toEqual(['P2']);
  await expect(page.locator('.lbl-ent.dim')).toHaveText('P2');
  await expect(page.locator('.lbl-model')).toHaveCount(2);
  await expect(page.locator('.lbl-model').filter({ hasText: 'predicts continue' })).toHaveCount(1);
  await expect(page.locator('.lbl-model').filter({ hasText: 'shelf · depth checked' })).toHaveCount(1);
  await expect(page.locator('#timeline .lane-model .tl-seg')).toHaveCount(2);

  // Esc closes the Videos tab, then leaves the answer
  await page.keyboard.press('Escape');
  await expect(page.locator('video')).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(page.locator('body')).toHaveAttribute('data-view', 'default');
  await expect(page.locator('.wh-rcam.used')).toHaveCount(0);
  await expect(page.locator('video')).toHaveCount(0);
  await expect(page.locator('.lbl-model')).toHaveCount(0);
  expect((await probe(page)).dimmed).toEqual([]);
});

test('an agent highlight that arrives while the scene is still loading is applied once it has loaded', async ({ page }) => {
  test.skip(LIVE, 'needs a held scene request');
  mockActions(page);
  await mockAsk(page, 'wh-main');
  let release;
  const held = new Promise((r) => (release = r));
  // Only the page fetches entities, so the shell finishes the answer while the scene is still loading.
  await page.route(new RegExp(`/api/scene/${KEY}/entities$`), async (r) => {
    await held;
    await r.fallback();
  });
  await page.goto(`#/warehouse/${KEY}`);
  await expect(page.locator('.wh')).toHaveCount(1);
  await page.locator('.preset').first().click();
  await expect(page.locator('#ask-status')).toContainText('Done');
  release();
  await expect(page.locator('.wh')).toHaveAttribute('data-scene', KEY);
  await expect.poll(async () => (await probe(page)).highlighted).toEqual(['P1', 'F1']);
});

test('a measured distance: both actors at full strength, a floor line between them labelled value ±sigma within the moment; a new question removes it', async ({ page }) => {
  test.skip(LIVE, 'needs the wh-dodge replay');
  mockActions(page);
  await mockAsk(page, 'wh-dodge');
  await open(page);
  await page.locator('.preset').first().click();
  await expect(page.locator('#ask-status')).toContainText('Done');
  expect((await probe(page)).highlighted).toEqual(['P1', 'F1']);
  expect((await probe(page)).dimmed).toEqual(['P2']);
  const label = page.locator('.lbl-measure');
  await expect(label).toBeVisible();
  await expect(label.locator('.wh-mval')).toHaveText('1.55 m ±0.12');
  // hovering its evidence lights the line
  const line = page.locator('.lbl-mline');
  await expect(line).not.toHaveClass(/\bhot\b/);
  await page.locator('.ans-num', { hasText: '1.55' }).hover();
  await expect(line).toHaveClass(/\bhot\b/);
  await page.mouse.move(0, 0);
  await expect(line).not.toHaveClass(/\bhot\b/);
  // clicking it enters Evidence mode: the label adds what was measured and when, the source dock shows its camera
  await page.locator('.ans-num', { hasText: '1.55' }).click();
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await expect(label.locator('.wh-msub')).toBeVisible();
  await expect(label.locator('.wh-msub')).toHaveText('ground distance · 00:04.6');
  await expect(page.locator('.wh-dock-head')).toContainText('ceiling_04 · 00:04.6');
  await page.keyboard.press('Escape');
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'explore');
  await expect(label.locator('.wh-msub')).toBeHidden();
  const ends = async () => {
    const { measure, entities } = await probe(page);
    return measure && [measure, [entities.P1, entities.F1]];
  };
  let [drawn, actors] = await ends();
  expect(drawn).toEqual(actors);
  // the moment is [4600, 5600)
  await clock(page, 'seek(5300)');
  [drawn, actors] = await ends();
  expect(drawn).toEqual(actors);
  await clock(page, 'seek(6000)');
  expect(await ends()).toBe(null);
  await expect(label).toBeHidden();
  await clock(page, 'seek(4600)');
  await expect(label).toBeVisible();

  await mockAsk(page, 'wh-refuse');
  await page.locator('#ask-input').fill('Who is the person in eye_00?');
  await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-view', 'abstained');
  expect(await ends()).toBe(null);
  await expect(label).toHaveCount(0);
});

test('default scene: points.bin draws as block massing; the raw cloud is a layer that is off until toggled', async ({ page }) => {
  mockActions(page);
  await open(page);
  await expect.poll(async () => (await probe(page)).blocks).toBeGreaterThan(0);
  expect((await probe(page)).cloud).toBe(false);
  await page.locator('.wh-layers-btn').click();
  const box = page.locator('.wh-layers-pop input[data-layer="cloud"]');
  await expect(box).not.toBeChecked();
  await box.check();
  expect((await probe(page)).cloud).toBe(true);
  await box.uncheck();
  expect((await probe(page)).cloud).toBe(false);
});

test('Layers popover replaces the legend: it lists the sources, a toggled layer applies at once and survives a reload', async ({ page }) => {
  mockActions(page);
  await open(page);
  await expect(page.locator('.wh-legend')).toHaveCount(0);
  const pop = page.locator('.wh-layers-pop');
  await expect(pop).toBeHidden();
  await page.locator('.wh-layers-btn').click();
  await expect(pop).toBeVisible();
  await expect(pop.locator('.src')).toHaveText(['Measured', 'Map', 'Model', 'Human']);
  await expect(pop.locator('input[data-layer]')).toHaveCount(6);
  const lift = pop.locator('input[data-layer="forklift"]');
  await expect(lift).toBeChecked();
  await clock(page, 'seek(1000)');
  const f1 = page.locator('.lbl-ent', { hasText: 'F1' });
  await expect(f1).toBeVisible();
  await lift.uncheck();
  await expect(f1).toBeHidden();
  // a click elsewhere closes it
  await page.locator('#panel').click();
  await expect(pop).toBeHidden();

  await page.reload();
  await expect(page.locator('.wh')).toHaveAttribute('data-scene', KEY);
  await clock(page, 'seek(1000)');
  await expect(page.locator('.lbl-ent', { hasText: 'P1' })).toBeVisible();
  await expect(f1).toBeHidden();
  await page.locator('.wh-layers-btn').click();
  await expect(lift).not.toBeChecked();
  await lift.check();
  await expect(f1).toBeVisible();
});

test('camera rail: a 24 px strip of dots that expands to thumbnails on hover; a click views from that camera', async ({ page }) => {
  mockActions(page);
  await open(page);
  const rail = page.locator('.wh-rail');
  await expect(rail.locator('.wh-rcam')).toHaveCount(3);
  expect((await rail.boundingBox()).width).toBeLessThanOrEqual(25);
  await expect(rail.locator('.wh-rthumb').first()).toBeHidden();
  await rail.hover();
  await expect.poll(async () => (await rail.boundingBox()).width).toBeGreaterThanOrEqual(79);
  await expect(rail.locator('.wh-rthumb img').first()).toBeVisible();
  await expect(rail).toContainText('used by agent');
  await rail.locator('.wh-rcam[data-view="cam:ceiling_04"]').click();
  expect((await probe(page)).view).toBe('cam:ceiling_04');
  await expect(rail.locator('.wh-rcam.on')).toHaveAttribute('data-view', 'cam:ceiling_04');
  await page.mouse.move(600, 300);
  await expect.poll(async () => (await rail.boundingBox()).width).toBeLessThanOrEqual(25);
});

// A ground distance between P1 and F1 seen from ceiling_04 (tracks 7 and 6 there).
const GROUND_EV = {
  eid: 'e_test_ground', scene_key: KEY, cam_id: 'ceiling_04', t0_ms: 4600, t1_ms: 4700, kind: 'measure', method: 'ground_distance', value: 1.55, unit: 'm', sigma: 0.12,
  observation_ids: [{ scene_key: KEY, cam_id: 'ceiling_04', local_track_id: 7, t_ms: 4600 }, { scene_key: KEY, cam_id: 'ceiling_04', local_track_id: 6, t_ms: 4600 }],
};

test('evidence:focus with a ground distance enters Evidence mode: others dimmed, focus chip, source dock of its camera; Esc or the chip x returns to Explore', async ({ page }) => {
  mockActions(page);
  await mockVideoRange(page);
  await open(page);
  await clock(page, 'seek(4600)');
  const dock = page.locator('.wh-dock'), chip = page.locator('.wh-focus');
  await expect(dock).toBeHidden();
  await expect(chip).toBeHidden();
  await expect(page.locator('.wh-mode button[data-mode="evidence"]')).toBeDisabled();
  const focus = () => mod(page, 'kernel/bus.js', `k.bus.emit('evidence:focus', ${JSON.stringify(GROUND_EV)})`);
  await focus();
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  expect(await mod(page, 'kernel/state.js', '[k.state.stageMode, k.state.focusEid]')).toEqual(['evidence', 'e_test_ground']);
  await expect(page.locator('.wh-mode button.on')).toHaveText('Evidence');
  await expect(dock).toBeVisible();
  await expect(dock.locator('.wh-dock-head')).toContainText('Source');
  await expect(dock.locator('.wh-dock-head')).toContainText('ceiling_04 · 00:04.6');
  await expect(dock.locator('video')).toHaveCount(1);
  await expect(chip).toContainText('P1–F1 distance');
  await expect.poll(async () => (await probe(page)).dimmed).toEqual(['P2']);
  await expect(page.locator('.wh-rcam.sel')).toHaveAttribute('data-view', 'cam:ceiling_04');

  await page.keyboard.press('Escape');
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'explore');
  await expect(dock).toBeHidden();
  await expect(chip).toBeHidden();
  expect((await probe(page)).dimmed).toEqual([]);
  await expect(page.locator('.wh-rcam.sel')).toHaveCount(0);

  // the seg goes back to the last focused evidence; the chip's x leaves it
  await page.locator('.wh-mode button[data-mode="evidence"]').click();
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await expect(dock).toBeVisible();
  await chip.locator('.wh-focus-clear').click();
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'explore');
  await expect(dock).toBeHidden();
});

test('Videos tab: primary camera 214 px with the others in two 105 px columns, No video hatching, Pin to stage opens a closable PiP', async ({ page }) => {
  test.skip(LIVE, 'needs the wh-main replay');
  mockActions(page);
  await mockVideoRange(page);
  await mockAsk(page, 'wh-main');
  await open(page);
  await page.locator('.preset').first().click();
  await expect(page.locator('#ask-status')).toContainText('Done');
  await page.locator('#tabs [data-tab="videos"]').click();
  const pane = page.locator('#tab-videos');
  const primary = pane.locator('.wh-vcell.primary');
  await expect(primary).toHaveAttribute('data-cam', 'ceiling_00');
  expect((await primary.locator('.wh-vframe').boundingBox()).height).toBeCloseTo(214, 0);
  await expect(primary.locator('figcaption')).toContainText('primary');
  const others = pane.locator('.wh-vgrid .wh-vcell');
  await expect(others).toHaveCount(2);
  for (const b of await others.locator('.wh-vframe').all()) expect((await b.boundingBox()).height).toBeCloseTo(105, 0);
  const [a, b] = await Promise.all([0, 1].map((i) => others.nth(i).boundingBox()));
  expect(Math.abs(a.y - b.y)).toBeLessThan(1);
  await clock(page, 'seek(6200)');
  const eye = pane.locator('.wh-vcell[data-cam="eye_00"]');
  await expect(eye).toHaveClass(/\bnov\b/);
  await expect(eye.locator('.wh-nov')).toHaveText('No video at 00:06.2');

  await pane.locator('.wh-vcell[data-cam="ceiling_04"] .wh-pip-btn').click();
  const pip = page.locator('.wh-pip');
  await expect(pip.locator('video')).toHaveCount(1);
  await expect(pip).toContainText('ceiling_04');
  await pip.locator('.wh-pip-close').click();
  await expect(pip).toHaveCount(0);
});

test('with the agent camera taken over, agent focus and setView commands leave the view as it is', async ({ page }) => {
  mockActions(page);
  await open(page);
  await mod(page, 'warehouse/page.js', "k.applyUI({ cmd: 'setView', args: { view: 'top' } })");
  expect((await probe(page)).view).toBe('top');
  await mod(page, 'kernel/state.js', 'k.state.agentCamera = false');
  await mod(page, 'warehouse/page.js', "k.applyUI({ cmd: 'setView', args: { view: 'cam:eye_00' } })");
  await mod(page, 'warehouse/page.js', "k.applyUI({ cmd: 'focus', args: { cam_id: 'eye_00' } })");
  expect((await probe(page)).view).toBe('top');
});

test('several predictions of one actor: one Model label at the end of the latest arrow, clear of other labels and the person ring', async ({ page }) => {
  test.skip(LIVE, 'needs injected model results');
  mockActions(page);
  await mockAsk(page, 'wh-main');
  const cosmos = JSON.parse(fs.readFileSync(path.join(OUT, KEY, MF.assets.cosmos), 'utf8')).records;
  const c2 = cosmos.find((r) => r.task === 'C2');
  const shifted = (id, dt, [dx, dy]) => ({ ...c2, record_id: id, t_ms: c2.t_ms + dt, payload: { ...c2.payload, ghost: c2.payload.ghost.map(([t, x, y]) => [t + dt, x + dx, y + dy]) } });
  // The latest prediction ends on P1 (at about [0.1, -1.5] at 4.6 s), inside its ring.
  const extra = [shifted('c_c2_early', -500, [0.4, 0]), shifted('c_c2_late', 500, [0.3, 1])];
  await page.route(/\/api\/model-results\?/, (r) => r.fulfill({ json: [...cosmos, ...extra] }));
  await open(page);
  await page.locator('.preset').first().click();
  await expect(page.locator('body')).toHaveAttribute('data-view', 'hit');
  const pred = page.locator('.lbl-model', { hasText: 'predicts' });
  await expect(pred).toHaveCount(1);
  await expect(pred).toHaveText(/^Model\s*F1 predicts continue · 2 s$/);
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  const p1 = (await probe(page)).entities.P1;
  const ring = await project(page, Array.from({ length: 16 }, (_, i) => [p1[0] + 0.76 * Math.cos((i * Math.PI) / 8), p1[1] + 0.76 * Math.sin((i * Math.PI) / 8), 0.012]));
  const ringBox = { left: Math.min(...ring.map((p) => p[0])), right: Math.max(...ring.map((p) => p[0])), top: Math.min(...ring.map((p) => p[1])), bottom: Math.max(...ring.map((p) => p[1])) };
  const boxes = await page.$$eval('.wh-lbl', (els) => els.filter((e) => e.getClientRects().length).map((e) => {
    const r = e.getBoundingClientRect();
    return { model: e.classList.contains('lbl-model'), text: e.textContent, left: r.left, right: r.right, top: r.top, bottom: r.bottom };
  }));
  const hit = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
  const model = boxes.filter((b) => b.model);
  for (const m of model) {
    expect(hit(m, ringBox)).toBe(false);
    for (const b of boxes) if (b !== m) expect(hit(m, b), `${m.text} | ${b.text}`).toBe(false);
  }
});

test('cam:ceiling_04 view: ground points project within 5 px of the K + cam2world pinhole', async ({ page }) => {
  mockActions(page);
  await open(page);
  expect((await probe(page)).floor).toBe(true);
  await page.locator('.wh-rcam[data-view="cam:ceiling_04"]').click();
  expect((await probe(page)).view).toBe('cam:ceiling_04');
  // the camera frame shows through: no drawn floor or blocks in front of it
  expect((await probe(page)).floor).toBe(false);
  const cam = MF.cams.find((c) => c.id === 'ceiling_04');
  const X = [[0, 0, 0], [2, 1, 0], [-1, -2, 0]];
  const uv = pinhole(cam, X);
  for (const [u, v] of uv) expect(u > 0 && u < cam.w && v > 0 && v < cam.h).toBe(true);
  const r = await page.locator('.wh-canvas').boundingBox();
  expect(Math.abs(r.width / r.height - cam.w / cam.h)).toBeLessThan(0.01);
  const got = await project(page, X);
  uv.forEach(([u, v], i) => {
    expect(Math.hypot(got[i][0] - (r.x + (u * r.width) / cam.w), got[i][1] - (r.y + (v * r.height) / cam.h))).toBeLessThan(5);
  });
});

test('clicking an object outlines it everywhere and offers Ask about this with the object as selection', async ({ page }) => {
  mockActions(page);
  await open(page);
  await clock(page, 'seek(1000)');
  // meshes move in the render loop; picking before the next frames raycasts against the old positions
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  const p1 = (await probe(page)).entities.P1;
  const [[x, y]] = await project(page, [[p1[0], p1[1], 0.9]]);
  await page.mouse.click(x, y);
  await expect.poll(async () => (await probe(page)).highlighted).toEqual(['P1']);
  expect(await mod(page, 'kernel/selection.js', '[...k.selection.ids]')).toEqual(['P1']);
  await expect(page.locator('.lbl-ent.on')).toHaveCount(1);
  await page.locator('.wh-ask-this').click();
  await expect(page.locator('#ask-input')).toHaveValue(/P1/);
  await expect(page.locator('#ask-input')).toBeFocused();
});

test('playback crossing a severity-2 event triggers once: pin, seek, review task, red hazard zone; replays do not re-trigger', async ({ page }) => {
  test.skip(LIVE, 'needs a fresh action store');
  const store = mockActions(page);
  await open(page);
  await expect(page.locator('.lbl-hazard')).toHaveCount(0);
  await clock(page, 'seek(4000)');
  const posted = page.waitForRequest((r) => r.url().endsWith('/api/actions/trigger'));
  await clock(page, 'play()');
  expect((await posted).postDataJSON()).toEqual({ scene_key: KEY, event_id: `${KEY}:E1`, auto: true, actions: ['create_review_task', 'hazard_zone', 'webhook'] });
  const toast = page.locator('.wh-toast');
  await expect(toast).toContainText('Review task #RT-1 and hazard zone #Z1 created');
  await expect(toast).toContainText('E1 near_miss · 00:04.3');
  await expect(toast.locator('button')).toHaveText(['View']);
  await expect(page.locator('.lbl-hazard')).toHaveText('Hazard zone #Z1');
  await expect(page.locator('.wh-pin[data-pin="E1"]')).toHaveClass(/\bon\b/);
  await expect(page.locator('#timeline .ev-mark[data-ev="E1"]')).toHaveClass(/\bon\b/);

  await clock(page, 'pause()');
  await clock(page, 'seek(4000)');
  await clock(page, 'play()');
  await expect.poll(() => clock(page, 't')).toBeGreaterThan(5000);
  expect(store.posts).toHaveLength(1);

  await page.reload();
  await expect(page.locator('.wh')).toHaveAttribute('data-scene', KEY);
  await expect(page.locator('.lbl-hazard')).toHaveText('Hazard zone #Z1');
  await clock(page, 'seek(4000)');
  await clock(page, 'play()');
  await expect.poll(() => clock(page, 't')).toBeGreaterThan(5000);
  expect(store.posts).toHaveLength(1);
});

test('drawing a floor zone posts it and keeps only the events inside on the timeline', async ({ page }) => {
  mockActions(page);
  let body = null;
  if (!LIVE) {
    await page.route(/\/api\/scene\/[a-z0-9_-]+\/zone$/, (r) => {
      body = r.request().postDataJSON();
      r.fulfill({ json: { polygon_world: body.polygon_world, cells: [], events_in_zone: [{ event_id: `${KEY}:E1`, scene_key: KEY, type: 'near_miss', t0_ms: 4300, t1_ms: 5200, actor_ids: ['P1', 'F1'], severity: 2, source: 'rule' }] } });
    });
  }
  await open(page);
  await expect(page.locator('#timeline .ev-mark')).toHaveCount(2);
  await page.locator('.wh-zone').click();
  const corners = await project(page, [[-2, -4, 0], [3, -4, 0], [3, 1, 0], [-2, 1, 0]]);
  for (const [x, y] of corners.slice(0, 3)) await page.mouse.click(x, y);
  await page.mouse.dblclick(...corners[3]);
  await expect(page.locator('.wh-zone-chip')).toContainText('1 event in zone');
  await expect(page.locator('#timeline .ev-mark')).toHaveCount(1);
  if (!LIVE) {
    expect(body.polygon_world).toHaveLength(4);
    body.polygon_world.forEach((p, i) => [[-2, -4], [3, -4], [3, 1], [-2, 1]][i].forEach((v, k) => expect(p[k]).toBeCloseTo(v, 1)));
  }
  await page.locator('.wh-zone-clear').click();
  await expect(page.locator('#timeline .ev-mark')).toHaveCount(2);
});

test('a skeleton with null joints still draws: the bones of a missing joint collapse', async ({ page }) => {
  await open(page);
  await clock(page, 'seek(1200)');
  await expect.poll(async () => Object.keys((await probe(page)).entities)).toContain('P1');
  expect((await probe(page)).single).not.toContain('P1');
});

test('a crowd: person labels that would overlap a nearer one are hidden, a selected one always shows', async ({ page }) => {
  test.skip(LIVE, 'needs an injected skeleton');
  const sk = JSON.parse(fs.readFileSync(path.join(OUT, KEY, MF.assets.skeleton), 'utf8'));
  // six copies of P2, 0.15 m apart: their labels are about 40 px wide, so they pile up at the default view
  for (let i = 1; i <= 6; i++) {
    const p = sk.persons.P2;
    sk.persons[`P${10 + i}`] = { ...p, root_world: p.root_world.map((r) => r && [r[0] + 0.15 * i, r[1], r[2]]), xyz: p.xyz.map((f) => f && f.map((j) => j && [j[0] + 0.15 * i, j[1], j[2]])) };
  }
  await page.route(new RegExp(`/api/assets/${KEY}/${MF.assets.skeleton}$`), (r) => r.fulfill({ json: sk }));
  await open(page);
  await clock(page, 'seek(5000)');
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  const boxes = () => page.$$eval('.wh-lbl:not(.lbl-model):not(.lbl-hazard)', (els) => els
    .filter((e) => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' && /^P\d+/.test(e.textContent))
    .map((e) => { const r = e.getBoundingClientRect(); return { text: e.textContent, left: r.left, right: r.right, top: r.top, bottom: r.bottom }; }));
  const hit = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
  const shown = await boxes();
  expect(shown.length).toBeGreaterThan(1);
  for (const a of shown) for (const b of shown) if (a !== b) expect(hit(a, b), `${a.text} | ${b.text}`).toBe(false);
});

test('the clock at the scene end asks for no moment past it', async ({ page }) => {
  const asked = [];
  page.on('request', (r) => { const m = r.url().match(/\/moment\?t=(\d+)/); if (m) asked.push(+m[1]); });
  await open(page);
  // a clock carried over from the fleet replay lands far past this scene and clamps to its end
  await clock(page, 'seek(999999)');
  await page.waitForTimeout(500);
  expect(asked.filter((t) => t >= MF.t1_ms)).toEqual([]);
});
