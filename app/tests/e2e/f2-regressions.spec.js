// Page regressions use fixture API responses and held requests to make races deterministic.
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { ASKS, mockApi, mockAsk } = require('./mockapi');

const FX = path.resolve(__dirname, '../../../fixtures');
const WH = 'wh-sdg-fixture-01', FL = 'fl-pie-fixture-01', ST = 'st-fixture_cam_1-clip01';
const read = (rel) => JSON.parse(fs.readFileSync(path.join(FX, rel), 'utf8'));
const RUN = read('runs/r_fixturewh0001.json');
const MF = read(`sva-out/${FL}/manifest.json`), GEO = read(`sva-out/${FL}/trip.geojson`), EGO = read(`sva-out/${FL}/ego/00000.json`);
const frames = (id) => structuredClone(ASKS.find((a) => a.ask_id === id).events);
const sse = (events) => events.map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');
const mod = (page, file, expr) => page.evaluate(async ([f, e]) => new Function('m', `return (${e})`)(await import(new URL(f, document.baseURI).href)), [file, expr]);
const clock = (page, expr) => mod(page, 'kernel/clock.js', `m.clock.${expr}`);
const seek = (page, t) => clock(page, `seek(${t})`);
const nextFrames = (page) => page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
const ink = (page, selector) => page.locator(selector).evaluate((c) => c.getContext('2d').getImageData(0, 0, c.width, c.height).data.filter((v, i) => i % 4 === 3 && v > 0).length);

let errors;
test.beforeEach(async ({ page }) => {
  test.skip(process.env.SVA_E2E_LIVE === '1', 'requires scripted fixture API responses');
  errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await mockApi(page);
  await page.route(/\/health$/, (r) => r.fulfill({ json: { mode: 'dev', index_rows: {} } }));
  // Keep map tests independent of tile assets; overview already has a blank-style fallback.
  await page.route(/\/api\/assets\/_map\/style\.json$/, (r) => r.fulfill({ status: 404, body: '' }));
  await page.route(/\/api\/assets\/.+\.mp4$/, (r) => {
    const file = path.join(FX, 'sva-out', new URL(r.request().url()).pathname.replace(/^.*?\/api\/assets\//, ''));
    const data = fs.readFileSync(file), range = (r.request().headers().range ?? 'bytes=0-').match(/bytes=(\d+)-(\d*)/);
    const a = +range[1], b = range[2] ? Math.min(+range[2], data.length - 1) : data.length - 1;
    return r.fulfill({ status: 206, headers: { 'content-type': 'video/mp4', 'accept-ranges': 'bytes', 'content-range': `bytes ${a}-${b}/${data.length}` }, body: data.subarray(a, b + 1) });
  });
  await page.route(/\/api\/actions\?/, (r) => r.fulfill({ json: { actions: [], zones: [] } }));
});
test.afterEach(() => expect(errors).toEqual([]));

async function ask(page, events) {
  await page.route(/\/api\/agent\/ask$/, (r) => r.fulfill({ contentType: 'text/event-stream', body: sse(events) }));
  await page.locator('#ask-input').fill('Inspect this evidence');
  await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
}

test('measure before hits draws immediately and reconciles the hit range', async ({ page }) => {
  const events = frames('wh-dodge'), hit = events.find((f) => f.event === 'hits');
  const ordered = events.filter((f) => f !== hit), i = ordered.findIndex((f) => f.event === 'tool_result' && f.data.evidence.some((e) => e.method === 'ground_distance'));
  ordered.splice(i + 1, 0, hit);
  await page.goto(`#/warehouse/${WH}`);
  await expect(page.locator('.wh')).toHaveAttribute('data-scene', WH);
  await ask(page, ordered);
  await expect(page.locator('.lbl-measure .wh-mval')).toHaveText('1.55 m ±0.12');
  await seek(page, 5300);
  await expect(page.locator('.lbl-measure')).toBeVisible();
  await seek(page, 6000);
  await expect(page.locator('.lbl-measure')).toBeHidden();
});

test('persisted array answers render every level sentence and abstention', async ({ page }) => {
  const answers = [{ text: 'L3: measured evidence.', cites: [RUN.evidence[0].eid] }, { text: 'L1: no geometry to measure distance.', cites: [], evidence_level: 'L1' }];
  const run = { ...RUN, steps: RUN.steps.map((s) => s.name === 'answer' ? { ...s, output: answers } : s) };
  await page.route(/\/api\/agent\/runs\/r_fixturewh0001$/, (r) => r.fulfill({ json: run }));
  await page.goto('#/test');
  await page.evaluate(() => { location.hash = '#/run/r_fixturewh0001'; });
  await expect(page.locator('.rp-sent')).toHaveCount(2);
  await expect(page.locator('.rp-answer')).toContainText(answers[0].text);
  await expect(page.locator('.rp-answer')).toContainText(answers[1].text);
  await expect(page.locator('.rp-sent').first().locator('.cite')).toHaveCount(1);
});

for (const [workspace, key, t] of [['warehouse', WH, 4600], ['fleet', FL, 2000], ['street', ST, 4000]]) {
  test(`trace activates ${workspace} evidence from a warehouse-context corpus run at its time`, async ({ page }) => {
    const step = { ...RUN.steps[4], i: 0, evidence_ids: [], output: { input_range: { scene_key: key, t0_ms: t, t1_ms: t + 100 } } };
    const evidence = workspace === 'street' ? [{ ...RUN.evidence[0], scene_key: key, t0_ms: t }] : [];
    if (workspace === 'street') Object.assign(step, { input: {}, output: {}, evidence_ids: [evidence[0].eid] });
    const run = { ...RUN, context: { workspace: 'warehouse', scene_key: WH, scope: 'corpus' }, steps: [step], claims: [], evidence };
    await page.route(/\/api\/agent\/runs\/r_fixturewh0001$/, (r) => r.fulfill({ json: run }));
    await page.goto('#/test');
    await page.evaluate(() => { location.hash = '#/run/r_fixturewh0001'; });
    await page.locator('.trace-row').click();
    const link = page.locator('.td-3d');
    await expect(link).toHaveAttribute('href', workspace === 'warehouse' ? `?t_ms=${t}#/warehouse/${key}` : `#/${workspace}/${key}@${t}`);
    await link.click();
    await expect(page.locator('#runpage')).toBeHidden();
    await expect.poll(() => clock(page, 't')).toBe(t);
    if (workspace === 'warehouse') await expect(page.locator('.wh')).toHaveAttribute('data-scene', key);
    if (workspace === 'fleet') await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.trip.id')).toBe(key);
    if (workspace === 'street') await expect.poll(() => mod(page, 'street/page.js', 'm.probe().scene_key')).toBe(key);
  });
}

for (const target of ['run', 'list', 'close']) {
  test(`late cached source run cannot overwrite a newer ${target}`, async ({ page }) => {
    let release;
    const held = new Promise((r) => { release = r; });
    await page.route(/\/api\/agent\/runs\/r_slow$/, (r) => r.fulfill({ json: { ...RUN, run_id: 'r_slow', q: 'Old question', source_run_id: 'r_source' } }));
    await page.route(/\/api\/agent\/runs\/r_source$/, async (r) => { await held; await r.fulfill({ json: RUN }); });
    await page.route(/\/api\/agent\/runs\/r_fast$/, (r) => r.fulfill({ json: { ...RUN, run_id: 'r_fast', q: 'New question' } }));
    await page.route(/\/api\/agent\/runs$/, (r) => r.fulfill({ json: [] }));
    await page.goto('#/test');
    const source = page.waitForRequest(/\/api\/agent\/runs\/r_source$/);
    await page.evaluate(() => { location.hash = '#/run/r_slow'; });
    await source;
    await page.evaluate((t) => { location.hash = t === 'run' ? '#/run/r_fast' : t === 'list' ? '#/runs' : '#/test'; }, target);
    if (target === 'run') await expect(page.locator('.rp-q')).toHaveText('New question');
    if (target === 'list') await expect(page.locator('.rp-title')).toHaveText('Recent runs');
    if (target === 'close') await expect(page.locator('#runpage')).toBeHidden();
    const response = page.waitForResponse(/\/api\/agent\/runs\/r_source$/);
    release();
    await response;
    await nextFrames(page);
    await expect(page.locator('.rp-q', { hasText: 'Old question' })).toHaveCount(0);
    if (target === 'run') await expect(page.locator('.rp-q')).toHaveText('New question');
    if (target === 'list') await expect(page.locator('.rp-title')).toHaveText('Recent runs');
  });
}

test('failed watch action leaves its pin uncommitted and retries on later playback', async ({ page }) => {
  let release, posts = 0;
  const held = new Promise((r) => { release = r; });
  await page.route(/\/api\/actions\/trigger$/, async (r) => {
    posts++;
    if (posts === 1) { await held; return r.fulfill({ status: 503, body: '' }); }
    return r.fulfill({ json: { review_task_id: 'RT-retry', zone_id: 'Z-retry', delivered: true } });
  });
  await page.goto(`#/warehouse/${WH}`);
  await expect(page.locator('.wh')).toHaveAttribute('data-scene', WH);
  await seek(page, 4200);
  await clock(page, 'play()');
  await seek(page, 4400);
  await clock(page, 'pause()');
  await expect.poll(() => posts).toBe(1);
  const pin = page.locator('.wh-pin[data-pin="E1"]');
  await expect(pin).not.toHaveClass(/\bon\b/);
  release();
  await expect(page.locator('.wh-toast')).toContainText('Action unavailable for E1');
  await seek(page, 4200);
  await clock(page, 'play()');
  await seek(page, 4400);
  await clock(page, 'pause()');
  await expect.poll(() => posts).toBe(2);
  await expect(pin).toHaveClass(/\bon\b/);
  await expect(page.locator('.wh-toast')).toContainText('RT-retry');
});

test('fleet late earlier chunk cannot overwrite cached later actors and GPS', async ({ page }) => {
  const mf = { ...MF, t1_ms: 30000 }, gj = structuredClone(GEO);
  gj.features[0].properties.replay.coverage_intervals = [[0, 30000]];
  let release;
  const held = new Promise((r) => { release = r; });
  await page.route(new RegExp(`/api/scene/${FL}/manifest$`), (r) => r.fulfill({ json: mf }));
  await page.route(new RegExp(`/api/assets/${FL}/trip.geojson$`), (r) => r.fulfill({ json: gj }));
  await page.route(new RegExp(`/api/assets/${FL}/ego/0000[12].json$`), async (r) => {
    const n = Number(r.request().url().match(/(\d{5})\.json$/)[1]);
    if (n === 1) await held;
    return r.fulfill({ json: { frames: EGO.frames.map((f) => ({ ...f, t_ms: f.t_ms + n * 10000 })) } });
  });
  await page.route(new RegExp(`/api/assets/${FL}/boxes/dashcam/0000[12].json$`), (r) => r.fulfill({ json: { rows: [] } }));
  await page.goto(`#/fleet/${FL}@3000`);
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.frame?.t_ms')).toBe(3000);
  await seek(page, 21000);
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix.frame.t_ms')).toBe(21000);
  const request = page.waitForRequest(new RegExp(`/api/assets/${FL}/ego/00001.json$`));
  await seek(page, 11000);
  await request;
  await seek(page, 22000);
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix.frame.t_ms')).toBe(22000);
  const facts = await page.locator('.fl-facts').textContent();
  const response = page.waitForResponse(new RegExp(`/api/assets/${FL}/ego/00001.json$`));
  release();
  await response;
  await nextFrames(page);
  expect(await mod(page, 'fleet/page.js', 'm.view.ix.frame.t_ms')).toBe(22000);
  await expect(page.locator('.fl-facts')).toHaveText(facts);
});

test('street boxes do not cross sampled empty frames before or after a car', async ({ page }) => {
  await page.route(new RegExp(`/api/street/${ST}/detections\\?`), (r) => r.fulfill({ json: { rows: [[2000, 'car', 10, 10, 100, 100, 0.9]], gaps: [] } }));
  await page.goto(`#/street/${ST}`);
  await expect(page.locator('.st-root')).toBeVisible();
  await expect.poll(() => mod(page, 'street/page.js', 'm.probe().scene_key')).toBe(ST);
  await seek(page, 2000);
  await expect.poll(() => mod(page, 'street/page.js', 'm.probe().boxes')).toBe(1);
  for (const t of [1900, 2100]) {
    await seek(page, t);
    await nextFrames(page);
    expect(await mod(page, 'street/page.js', 'm.probe().boxes')).toBe(0);
    expect(await ink(page, '.st-ovl')).toBe(0);
  }
});

test('new fleet video inherits an already playing clock and pauses with it', async ({ page }) => {
  await page.goto(`#/fleet/${FL}@3000`);
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.frame?.t_ms')).toBe(3000);
  await clock(page, 'play()');
  await mod(page, 'fleet/page.js', `m.applyHits([{ scene_key: '${FL}', t0_ms: 3500, t1_ms: 4500, actor_refs: [] }])`);
  await expect.poll(() => page.locator('.fl-vid video').evaluate((v) => !v.paused && v.readyState >= 2)).toBe(true);
  await clock(page, 'pause()');
  await expect.poll(() => page.locator('.fl-vid video').evaluate((v) => v.paused)).toBe(true);
});

test('exclusive scene endpoint stays valid for follow-up context and map', async ({ page }) => {
  await mockAsk(page, 'wh-main');
  await page.goto('#/test');
  await page.locator('#presets .preset').first().click();
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  await seek(page, 10000);
  expect(await clock(page, 't')).toBe(9999);
  expect(await clock(page, 'map(10000).t_ms')).toBe(9999);
  const request = page.waitForRequest(/\/api\/agent\/ask$/);
  await page.locator('#followup-input').fill('What happened here?');
  await page.locator('#followup-input').press('Enter');
  expect((await request).postDataJSON().context.t_ms).toBe(9999);
});

test('pause and immediate resume leave only one playback loop', async ({ page }) => {
  await page.goto('#/test');
  await expect.poll(() => clock(page, 'range.t1')).toBe(10000);
  const result = await page.evaluate(async () => {
    const { clock } = await import(new URL('kernel/clock.js', document.baseURI).href);
    const raf = window.requestAnimationFrame, pending = [];
    window.requestAnimationFrame = (fn) => { pending.push(fn); return pending.length; };
    try {
      clock.seek(1000);
      clock.play();
      clock.pause();
      clock.play();
      for (const fn of pending.splice(0)) fn(performance.now() + 16);
      const result = { loops: pending.length, advanced: clock.t - 1000 };
      clock.pause();
      return result;
    } finally { window.requestAnimationFrame = raf; }
  });
  expect(result.loops).toBe(1);
  expect(result.advanced).toBeLessThan(25);
});

test('warehouse transient box read clears overlays and retries on the next tick', async ({ page }) => {
  let failed = true, requests = 0;
  await page.route(new RegExp(`/api/assets/${WH}/boxes/ceiling_00/00000.json$`), (r) => {
    requests++;
    return failed ? r.fulfill({ status: 503, body: '' }) : r.fallback();
  });
  await page.goto(`#/warehouse/${WH}`);
  await expect(page.locator('.wh')).toHaveAttribute('data-scene', WH);
  await expect(page.locator('.wh-stage-video .wh-nov')).toHaveText('Detections unavailable');
  expect(await ink(page, '.wh-stage-video canvas')).toBe(0);
  failed = false;
  await seek(page, 1000);
  await expect.poll(() => requests).toBeGreaterThan(1);
  await expect.poll(() => ink(page, '.wh-stage-video canvas')).toBeGreaterThan(0);
  await expect(page.locator('.wh-stage-video .wh-nov')).toBeHidden();
});

test('warehouse documented missing box chunk is cached as empty without rejection', async ({ page }) => {
  let requests = 0;
  await page.route(new RegExp(`/api/assets/${WH}/boxes/ceiling_00/00000.json$`), (r) => { requests++; return r.fulfill({ status: 404, body: '' }); });
  await page.goto(`#/warehouse/${WH}`);
  await expect(page.locator('.wh')).toHaveAttribute('data-scene', WH);
  await seek(page, 1000);
  await nextFrames(page);
  expect(requests).toBe(1);
  expect(await ink(page, '.wh-stage-video canvas')).toBe(0);
  await expect(page.locator('.wh-stage-video .wh-nov')).toBeHidden();
});

test('street rejected detection promise is evicted and next tick recovers', async ({ page }) => {
  let failed = true, requests = 0;
  await page.route(new RegExp(`/api/street/${ST}/detections\\?`), (r) => {
    requests++;
    return failed ? r.fulfill({ status: 503, body: '' }) : r.fulfill({ json: { rows: [[1000, 'car', 10, 10, 100, 100, 0.9]], gaps: [] } });
  });
  await page.goto(`#/street/${ST}`);
  await expect(page.locator('.st-chip')).toContainText('Detections unavailable');
  expect(await ink(page, '.st-ovl')).toBe(0);
  failed = false;
  await seek(page, 1000);
  await expect.poll(() => requests).toBeGreaterThan(1);
  await expect.poll(() => mod(page, 'street/page.js', 'm.probe().boxes')).toBe(1);
  await expect(page.locator('.st-chip')).toHaveText('L1 · 2D video');
});

for (const kind of ['ego', 'boxes/dashcam']) {
  test(`fleet rejected ${kind} chunk is evicted and next tick recovers`, async ({ page }) => {
    let failed = true, requests = 0;
    await page.route(new RegExp(`/api/assets/${FL}/${kind}/00000.json$`), (r) => {
      requests++;
      return failed ? r.fulfill({ status: 503, body: '' }) : r.fallback();
    });
    await page.goto(`#/fleet/${FL}@3000`);
    await expect(page.locator('.fl-facts')).toHaveText('Detections unavailable');
    expect(await ink(page, '.fl-ovl')).toBe(0);
    failed = false;
    await seek(page, 3100);
    await expect.poll(() => requests).toBeGreaterThan(1);
    await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.frame?.t_ms')).toBe(3100);
    await expect(page.locator('.fl-facts')).toContainText('GPS');
    await expect.poll(() => ink(page, '.fl-ovl')).toBeGreaterThan(0);
  });
}

for (const scenario of ['empty workspace', 'no fixes', 'event before first fix']) {
  test(`fleet mounts ${scenario} without invalid bounds or unlocated markers`, async ({ page }) => {
    if (scenario === 'empty workspace') await page.route(/\/api\/fleet\/trips$/, (r) => r.fulfill({ json: [] }));
    else {
      const gj = structuredClone(GEO);
      if (scenario === 'no fixes') {
        gj.features = gj.features.slice(0, 1);
        gj.features[0].geometry.coordinates = [];
      } else gj.features = [gj.features[0], ...gj.features.slice(1).filter((f) => f.properties.t_ms >= 5000)];
      if (scenario === 'event before first fix') await page.route(new RegExp(`/api/assets/${FL}/${MF.assets.events}$`), (r) => r.fulfill({ json: { events: read(`sva-out/${FL}/${MF.assets.events}`).events.filter((e) => e.t0_ms < 5000) } }));
      await page.route(/\/api\/fleet\/trips$/, (r) => r.fulfill({ json: [{ trip_id: FL, geojson_url: `api/assets/${FL}/trip.geojson`, title: MF.title }] }));
      await page.route(new RegExp(`/api/assets/${FL}/trip.geojson$`), (r) => r.fulfill({ json: gj }));
    }
    await page.goto('#/fleet');
    await expect.poll(() => mod(page, 'fleet/page.js', '!!m.view.overview')).toBe(true);
    expect(await mod(page, 'fleet/page.js', 'm.view.overview.camera().center.every(Number.isFinite)')).toBe(true);
    expect(await mod(page, 'kernel/clock.js', 'Number.isFinite(m.clock.range.t0) && Number.isFinite(m.clock.range.t1)')).toBe(true);
    await expect(page.locator('.fl-pin, .fl-cluster')).toHaveCount(0);
    await expect(page.locator('.fl-ix')).toHaveCount(0);
    await expect(page.locator('.fl-cap')).toHaveText('GPS unavailable');
    if (scenario === 'no fixes') {
      await mod(page, 'fleet/page.js', `m.applyHits([{ scene_key: '${FL}', t0_ms: 3000, t1_ms: 4000 }])`);
      await mod(page, 'fleet/page.js', `m.loadScene('${FL}')`);
      await expect(page.locator('.fl-ix')).toHaveCount(0);
    }
  });
}

test('agent building highlight opens its card and focuses the map facade', async ({ page }) => {
  const events = frames('fl-where');
  const status = events.findIndex((f) => f.event === 'status');
  events.splice(status, 0, { event: 'ui', data: { run_id: events[0].data.run_id, scene_key: FL, cmd: 'highlight', args: { ids: ['w1001', 'P1'] } } });
  await page.goto(`#/fleet/${FL}@3000`);
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.frame?.t_ms')).toBe(3000);
  await ask(page, events);
  await expect(page.locator('.fl-card')).toBeVisible();
  await expect(page.locator('.fl-card')).toHaveAttribute('data-bid', 'w1001');
  expect(await mod(page, 'fleet/page.js', "m.view.overview.map.getFeatureState({ source: 'fl-bld', id: 'w1001' }).focus")).toBe(true);
  expect(await mod(page, 'kernel/selection.js', '[...m.selection.ids]')).toEqual(['P1']);
});

test('trip hit excludes its boundary chunk and boundary frame', async ({ page }) => {
  const ego = structuredClone(EGO), last = structuredClone(EGO.frames.at(-1));
  last.t_ms = 10000;
  last.ego.sigma_m = 999;
  ego.frames.push(last);
  await page.route(new RegExp(`/api/assets/${FL}/ego/00000.json$`), (r) => r.fulfill({ json: ego }));
  await page.route(new RegExp(`/api/assets/${FL}/ego/00001.json$`), (r) => r.fulfill({ json: { frames: [] } }));
  const requested = [];
  page.on('request', (r) => { if (/\/ego\/00001.json$/.test(r.url())) requested.push(r.url()); });
  await page.goto(`#/fleet/${FL}@3000`);
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.frame?.t_ms')).toBe(3000);
  await mod(page, 'fleet/page.js', `m.applyHits([{ scene_key: '${FL}', t0_ms: 3000, t1_ms: 10000, actor_refs: [] }])`);
  await mod(page, 'kernel/bus.js', "m.bus.emit('panel:tab', 'trip')");
  await expect(page.locator('#tab-trip .tt-head')).toContainText(FL);
  expect(requested).toEqual([]);
  await expect(page.locator('#tab-trip .tt-gps')).not.toContainText('999');
});

test('failed fleet stylesheet settles mounting and queued commands', async ({ page }) => {
  await page.route(/\/fleet\/fleet.css$/, (r) => r.abort());
  await page.goto(`#/fleet/${FL}@3000`);
  await expect.poll(() => mod(page, 'fleet/page.js', '!!m.view.overview'), { timeout: 10000 }).toBe(true);
  await mod(page, 'fleet/page.js', "m.applyUI({ cmd: 'highlight', args: { ids: ['P1'] } })");
  expect(await mod(page, 'kernel/selection.js', '[...m.selection.ids]')).toEqual(['P1']);
});

// ---- F2: one trip-time timeline, zoom, telemetry lanes, evidence focus, keyboard, key frame, takeover, SSE
// The shell module by the URL the page loaded (the app adds a version query).
const view = (page) => page.evaluate(async () => (await import(document.querySelector('script[src*="app.js"]').src)).timeline.view);
// The fixture trip made 60 s long, so a moment ±10 s is less than the whole trip.
async function longTrip(page) {
  const gj = structuredClone(GEO);
  gj.features[0].properties.replay.coverage_intervals = [[0, 60000]];
  await page.route(new RegExp(`/api/scene/${FL}/manifest$`), (r) => r.fulfill({ json: { ...MF, t1_ms: 60000 } }));
  await page.route(new RegExp(`/api/assets/${FL}/trip.geojson$`), (r) => r.fulfill({ json: gj }));
}
async function askFleet(page, events = frames('fl-where')) {
  await page.goto(`#/fleet/${FL}@3000`);
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.trip.id')).toBe(FL);
  await ask(page, events);
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await page.evaluate(() => document.activeElement.blur());
}

test('fleet ruler shows trip time and spans only the open trip; the overview keeps the fleet replay clock', async ({ page }) => {
  // Trip time 0 is replay wall time 20 s, at double speed.
  const gj = structuredClone(GEO);
  Object.assign(gj.features[0].properties.replay, { replay_start_ms: 20000, rate: 2 });
  await page.route(new RegExp(`/api/assets/${FL}/trip.geojson$`), (r) => r.fulfill({ json: gj }));
  await page.goto(`#/fleet/${FL}@3000`);
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.trip.id')).toBe(FL);
  expect(await clock(page, 'range')).toEqual({ t0: 0, t1: 10000 });
  expect(await clock(page, 't')).toBe(3000);
  await expect(page.locator('#timeline .tl-now')).toHaveText('00:03.0');
  await expect(page.locator('.fl-fact-time')).toHaveText('00:03.0');
  await expect(page.locator('#timeline .tl-clock')).toHaveText('Trip time');
  const ticks = await page.locator('#timeline .tl-ruler .tick').allTextContents();
  expect(ticks.length).toBeGreaterThan(1);
  for (const t of ticks) expect(t < '00:10.0').toBe(true);
  // The other trip folds into a faded toggle; Answer mode hides it.
  await expect(page.locator(`#timeline [data-lane="trip:${FL}"] .tl-label`)).toHaveText('GPS · fl-pie-fixture-01');
  await expect(page.locator('#timeline [data-lane="trip:fl-pie-fixture-02"]')).toBeHidden();
  await expect(page.locator('#timeline .tl-others')).toBeVisible();
  await page.locator('.stage-controls [data-mode="evidence"]').click();
  await expect(page.locator('#timeline .tl-others')).toBeHidden();
  await page.locator('.stage-controls [data-mode="explore"]').click();
  await expect(page.locator('#timeline .tl-others')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('.fl-root')).not.toHaveClass(/\bix\b/);
  expect(await clock(page, 'range')).toEqual({ t0: 0, t1: 60000 });
  expect(await clock(page, 't')).toBe(21500);
  await expect(page.locator('#timeline .tl-clock')).toHaveText('Fleet replay clock');
  await expect(page.locator('.fl-sim')).toHaveText('Fleet replay clock');
});

test('after a fleet answer the ruler shows the moment ±10 s; Ctrl+wheel, −, +, Undo, Reset and Zoom to range zoom it', async ({ page }) => {
  await longTrip(page);
  await askFleet(page);
  // The moment is 3.0–8.0 s; ±10 s starts at the trip start.
  await expect.poll(() => view(page)).toEqual({ t0: 0, t1: 25000 });
  await expect(page.locator('#timeline .tl-overview')).toBeVisible();
  const ruler = await page.locator('#timeline .tl-ruler .tl-track').boundingBox();
  await page.mouse.move(ruler.x + ruler.width * 0.4, ruler.y + ruler.height / 2);
  await page.keyboard.down('Control');
  await page.mouse.wheel(0, -100);
  await page.keyboard.up('Control');
  // The time under the pointer (10 s) stays in place.
  await expect.poll(() => view(page)).toEqual({ t0: 2000, t1: 22000 });
  // − and + keep the playhead (3 s) in place.
  await page.locator('#timeline [data-zoom="in"]').click();
  expect(await view(page)).toEqual({ t0: 2500, t1: 12500 });
  await page.locator('#timeline [data-zoom="out"]').click();
  expect(await view(page)).toEqual({ t0: 2000, t1: 22000 });
  await page.locator('#timeline [data-zoom="undo"]').click();
  expect(await view(page)).toEqual({ t0: 2500, t1: 12500 });
  await page.locator('#timeline [data-zoom="reset"]').click();
  expect(await view(page)).toEqual({ t0: 0, t1: 60000 });
  await expect(page.locator('#timeline [data-zoom="reset"]')).toBeDisabled();
  // A brushed range offers Zoom to range next to its follow-up chip.
  const lane = await page.locator('#timeline [data-lane="speed"] .tl-track').boundingBox();
  await page.mouse.move(lane.x + lane.width * 0.5, lane.y + lane.height / 2); await page.mouse.down();
  await page.mouse.move(lane.x + lane.width * 0.75, lane.y + lane.height / 2); await page.mouse.up();
  await page.locator('#followup [data-zoom-range]').click();
  await expect.poll(async () => { const v = await view(page); return [Math.round(v.t0 / 1000), Math.round(v.t1 / 1000)]; }).toEqual([30, 45]);
});

test('the Speed lane draws OBD speed against the map limit and names both sources; lanes show values on hover and seek on click', async ({ page }) => {
  await page.goto(`#/fleet/${FL}@3000`);
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.trip.id')).toBe(FL);
  const lane = page.locator('#timeline .lane-speed');
  await expect(lane).toBeVisible();
  await expect(lane.locator('.tl-label')).toHaveText('Speed · OBD km/hLimit · Map');
  await expect.poll(() => ink(page, '#timeline .lane-speed canvas')).toBeGreaterThan(0);
  const box = await lane.locator('.tl-track').boundingBox();
  await page.mouse.move(box.x + box.width * 0.1, box.y + box.height / 2);
  await expect(page.locator('#timeline .tl-tip')).toHaveText(/^00:01\.0 · \d+ km\/h · OBD · limit 40 km\/h · Map$/);
  await expect(page.locator('#timeline .tl-ghost')).toBeVisible();
  expect(await page.locator(`#timeline [data-lane="trip:${FL}"] .s-last_known`).count()).toBeGreaterThan(0);
  await expect(page.locator(`#timeline .tl-events [data-ev="${FL}:E1"]`)).toHaveText('PC2');
  await expect(page.locator(`#timeline .tl-events [data-ev="${FL}:E1"]`)).toBeVisible();
  await page.locator(`#timeline [data-lane="trip:${FL}"] .tl-track`).click({ position: { x: box.width * 0.5, y: 5 } });
  await expect.poll(() => clock(page, 't')).toBe(5000);
});

test('clicking a speed evidence row switches the fleet evidence cell to Speed, zooms the ruler and enters Answer mode', async ({ page }) => {
  await longTrip(page);
  const events = frames('fl-where');
  const speed = { eid: 'e_speed', scene_key: FL, cam_id: null, t0_ms: 20000, t1_ms: 21000, observation_ids: [], analysis_rev: 'fx-0001', kind: 'measure', method: 'obd_speed', value: 48.1, unit: 'km/h', sigma: null, artifact_ref: null };
  events.find((f) => f.event === 'tool_result').data.evidence.push(speed);
  events.find((f) => f.event === 'answer').data.cites.push(speed.eid);
  await askFleet(page, events);
  await page.locator('.stage-controls [data-mode="explore"]').click();
  await page.locator('#tabs [data-tab="evidence"]').click();
  await page.locator('#tab-evidence .ev[data-eid="e_speed"] .ev-label').click();
  await expect.poll(() => clock(page, 't')).toBe(20000);
  await expect(page.locator('.fl-evidence-tabs button[data-tab="speed"]')).toHaveClass(/\bon\b/);
  await expect(page.locator('.fl-speed-pane')).toBeVisible();
  await expect.poll(() => view(page)).toEqual({ t0: 15000, t1: 26000 });
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await expect(page.locator('.tl-evm[data-eid="e_speed"]')).toHaveClass(/\bon\b/);
});

test('global keys: Space plays and pauses, arrows and , . step, ? opens the key sheet; text fields keep their keys', async ({ page }) => {
  await page.goto('#/test');
  await seek(page, 5000);
  await page.keyboard.press('Space');
  expect(await clock(page, 'playing')).toBe(true);
  await page.keyboard.press('Space');
  expect(await clock(page, 'playing')).toBe(false);
  await seek(page, 5000);
  await page.keyboard.press('ArrowRight');
  expect(await clock(page, 't')).toBe(6000);
  await page.keyboard.press('Shift+ArrowLeft');
  expect(await clock(page, 't')).toBe(1000);
  await page.keyboard.press('.');
  expect(await clock(page, 't')).toBe(1033);
  await page.keyboard.press(',');
  expect(await clock(page, 't')).toBe(1000);
  await page.keyboard.press('?');
  await expect(page.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#keysheet')).toBeHidden();
  await page.locator('#ask-input').press('Space');
  expect(await clock(page, 'playing')).toBe(false);
});

test('] and [ step through the Evidence marks, Shift+] to the next trip event, Z zooms to the evidence and 0 resets', async ({ page }) => {
  await longTrip(page);
  await askFleet(page);
  await seek(page, 1000);
  await page.keyboard.press(']');
  await expect.poll(() => clock(page, 't')).toBe(3000);
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await page.keyboard.press('[');
  await expect.poll(() => clock(page, 't')).toBe(0);
  await page.keyboard.press('Shift+BracketRight');
  expect(await clock(page, 't')).toBe(3000);
  await expect(page.locator(`#timeline .ev-mark.on[data-ev="${FL}:E1"]`)).toHaveCount(1);
  await page.keyboard.press('Shift+BracketRight');
  expect(await clock(page, 't')).toBe(5000);
  await page.keyboard.press('0');
  expect(await view(page)).toEqual({ t0: 0, t1: 60000 });
  // The focused evidence is the place name over the whole fixture window 0–10 s.
  await page.keyboard.press('z');
  expect(await view(page)).toEqual({ t0: 0, t1: 20000 });
});

test('the key frame is a 64 px strip while the cursor is at the cited moment, the full frame when it is more than 1 s away', async ({ page }) => {
  await askFleet(page);
  expect(await clock(page, 't')).toBe(3000);
  const kf = page.locator('#keyframe .keyframe');
  await expect(kf).toHaveClass(/\bkf-strip\b/);
  await expect(page.locator('#keyframe .kf-back')).toHaveText('Back to 00:03.0');
  expect(Math.round((await kf.boundingBox()).height)).toBe(64);
  await seek(page, 3900);
  await expect(kf).toHaveClass(/\bkf-strip\b/);
  await seek(page, 4500);
  await expect(kf).not.toHaveClass(/\bkf-strip\b/);
  await expect(page.locator('#keyframe .kf-back')).toHaveCount(0);
  expect((await kf.boundingBox()).height).toBeGreaterThan(64);
  await page.locator('.stage-controls [data-mode="explore"]').click();
  await seek(page, 3500);
  await page.locator('#keyframe .kf-back').click();
  await expect.poll(() => clock(page, 't')).toBe(3000);
});

test('stage controls keep Answer mode; a press on the map takes over', async ({ page }) => {
  await askFleet(page);
  for (const control of ['.fl-boxes-btn', '.fl-evidence-tabs [data-tab="speed"]', '.fl-swap-btn', '.fl-swap-btn', '.fl-facts', '.fl-trip-chip']) {
    await page.locator(control).click();
    await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  }
  await page.locator('.fl-map canvas.maplibregl-canvas').dispatchEvent('pointerdown');
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'explore');
});

test('sse.js joins two data: lines with "\\n" and strips one leading space each', async ({ page }) => {
  await page.goto('#/test');
  expect(await mod(page, 'kernel/sse.js', "m.sseFrame('event: step\\ndata: {\"a\":\\ndata:  1}\\nid: 7')")).toEqual({ event: 'step', data: '{"a":\n 1}', id: 7 });
});

test('a throwing frame handler does not report a connection failure', async ({ page }) => {
  await page.goto('#/test');
  const seen = await page.evaluate(async () => {
    const { askSSE } = await import(new URL('kernel/sse.js', document.baseURI).href);
    const fetch0 = window.fetch, seen = [];
    window.fetch = async () => new Response('event: step\ndata: {"run_id":"r"}\n\nevent: status\ndata: {"run_id":"r","final":"SUPPORTED"}\n\n', { headers: { 'content-type': 'text/event-stream' } });
    try {
      await new Promise((resolve) => {
        askSSE({}, {
          step: () => { throw new Error('handler bug'); },
          status: (d) => seen.push(d.final),
          error: (e) => { seen.push(`error: ${e.message}`); resolve(); },
          comparison_error: (e) => { seen.push(`comparison_error: ${e.message}`); resolve(); },
        });
        setTimeout(resolve, 300);
      });
      return seen;
    } finally { window.fetch = fetch0; }
  });
  expect(seen).toEqual(['SUPPORTED']);
  // The handler's exception is still reported as a page error.
  await expect.poll(() => errors).toEqual(['handler bug']);
  errors = [];
});
