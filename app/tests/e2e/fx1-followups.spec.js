// Follow-ups to the layout-A batch: clock domain of the agent lane and evidence marks, cross-trip evidence, the street
// Activity chart and peak answer, fleet timeline lanes in the viewport, one SVA mark on home.
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { mockApi, mockAsk, mockAskFrames, ASKS } = require('./mockapi');

const FX = path.resolve(__dirname, '..', '..', '..', 'fixtures');
const T1 = 'fl-pie-fixture-01', T2 = 'fl-pie-fixture-02', ST = 'st-fixture_cam_1-clip01';
const mod = (page, rel, expr) =>
  page.evaluate(async ([r, e]) => new Function('m', `return (${e})`)(await import(new URL(r, document.baseURI).href)), [rel, expr]);
const shell = (page, expr) =>
  page.evaluate(async (e) => new Function('m', `return (${e})`)(await import(document.querySelector('script[src*="app.js"]').src)), expr);

let errors;
test.beforeEach(async ({ page }) => {
  errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await mockApi(page);
  await page.route(/\/api\/assets\/_map\//, (r) => r.continue());
});
test.afterEach(() => expect(errors).toEqual([]));

async function openFleet(page) {
  await page.goto('#/fleet');
  await expect(page.locator('.fl-car')).toHaveCount(2);
}

async function askWhere(page) {
  await page.locator('#ask-input').fill('Near Queen and Spadina, did a pedestrian cross in front of the car?');
  await page.keyboard.press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
}

// Left edge of the first agent lane segment and of an evidence mark, as drawn and as expected from scene time now.
const positions = (page, eid, laneT, markT) => page.evaluate(async ([T, eid, laneT, markT]) => {
  const { clock } = await import(new URL('kernel/clock.js', document.baseURI).href);
  const { timeline } = await import(document.querySelector('script[src*="app.js"]').src);
  const v = timeline.view, pct = (t) => Math.round(1000 * (100 * (t - v.t0)) / (v.t1 - v.t0)) / 1000;
  const left = (el) => Math.round(1000 * parseFloat(el.style.left)) / 1000;
  return {
    lane: [left(document.querySelector('#timeline .lane-agent .tl-seg')), pct(clock.toWall(T, laneT))],
    mark: [left(document.querySelector(`#timeline .tl-evm[data-eid="${eid}"]`)), pct(clock.toWall(T, markT))],
  };
}, [T1, eid, laneT, markT]);

test('agent lane and evidence marks re-convert after a clock domain switch (overview to trip and back)', async ({ page }) => {
  const route = JSON.parse(fs.readFileSync(path.join(FX, 'sva-out', T1, 'trip.geojson'), 'utf8'));
  Object.assign(route.features[0].properties.replay, { replay_start_ms: 20000 });
  await page.route(new RegExp(`/api/assets/${T1}/trip.geojson$`), (r) => r.fulfill({ json: route }));
  await openFleet(page);
  await page.keyboard.press('Escape');
  await expect(page.locator('.fl-root')).not.toHaveClass(/\bix\b/);
  expect(await mod(page, 'kernel/clock.js', 'm.clock.domain.kind')).toBe('fleet');
  await mockAsk(page, 'fl-where');
  await askWhere(page);
  await expect(page.locator('.fl-ix')).toHaveCount(1);
  expect(await mod(page, 'kernel/clock.js', 'm.clock.domain.kind')).toBe('scene');
  const eid = 'e_da97463cc6587a96';
  let p = await positions(page, eid, 0, 3000);
  expect(p.lane[0]).toBe(p.lane[1]);
  expect(p.mark[0]).toBe(p.mark[1]);
  expect(p.lane[1]).toBe(0);

  // Back on the fleet replay clock: the same scene times sit 20 s into the replay.
  await mod(page, 'fleet/page.js', `(async () => {
    const { clock } = await import(new URL('kernel/clock.js', document.baseURI).href);
    clock.bind({ kind: 'fleet', replay: m.view.trips.map((t) => t.replay) });
    clock.setRange(0, 60000, 25000);
  })()`);
  p = await positions(page, eid, 0, 3000);
  expect(p.lane[0]).toBe(p.lane[1]);
  expect(p.mark[0]).toBe(p.mark[1]);
  expect(p.lane[1]).toBeCloseTo(100 / 3, 2);
});

test('cross-trip evidence that no moment covers opens its trip and lands at its time', async ({ page }) => {
  const where = ASKS.find((a) => a.ask_id === 'fl-where').events;
  const run_id = where[0].data.run_id, eid = 'e_t2_pass';
  const evidence = { eid, scene_key: T2, cam_id: null, t0_ms: 20000, t1_ms: 21000, observation_ids: [], analysis_rev: 'fx-0001', kind: 'map', method: 'gps_pass',
    value: { place: 'Fixture corner' }, unit: null, sigma: null, artifact_ref: null };
  const extra = { event: 'tool_result', data: { run_id, step_id: 's_t2', call_id: 'c_t2', ok: true,
    envelope: { input_range: { scene_key: T2, cam_id: null, t0_ms: 20000, t1_ms: 21000 }, elapsed_ms: 1, exec_state: 'precomputed' }, evidence: [evidence] } };
  const at = where.findIndex((f) => f.event === 'hits');
  await mockAskFrames(page, [...where.slice(0, at), extra, ...where.slice(at)]);
  await openFleet(page);
  await askWhere(page);
  await expect(page.locator('.fl-ix')).toHaveCount(1);
  await page.locator('#tabs [data-tab="evidence"]').click();
  await page.locator('.ev-rest summary').click();
  await page.locator(`.ev[data-eid="${eid}"]`).click();
  await expect.poll(() => page.evaluate(() => location.hash)).toBe(`#/fleet/${T2}@20000`);
  await expect.poll(() => mod(page, 'kernel/clock.js', 'm.clock.range.t1')).toBe(60000);
  await expect.poll(() => mod(page, 'kernel/clock.js', 'm.clock.t')).toBe(20000);
  await expect(page.locator(`.fl-car[data-trip="${T2}"]`)).toHaveClass(/sel/);
});

// A 12-minute street clip: one frame per second, a 13-person peak at 136 s and 2 persons at 137 s.
const persons = (s) => (s === 136 ? 13 : s === 137 ? 2 : s % 5);
async function longStreet(page) {
  const mf = JSON.parse(fs.readFileSync(path.join(FX, 'l1', ST, 'manifest.json'), 'utf8'));
  await page.route(new RegExp(`/api/scene/${ST}/manifest$`), (r) => r.fulfill({ json: { ...mf, t1_ms: 720000 } }));
  await page.route(new RegExp(`/api/street/${ST}/detections\\?`), (r) => {
    const u = new URL(r.request().url()), t0 = +u.searchParams.get('t0'), t1 = Math.min(+u.searchParams.get('t1'), 720000), rows = [];
    for (let t = Math.ceil(t0 / 1000) * 1000; t < t1; t += 1000) {
      for (let k = 0; k < persons(t / 1000); k++) rows.push([t, 'person', 10 * k, 10, 10 * k + 8, 30, 0.9]);
      if ((t / 1000) % 7 === 0) rows.push([t, 'car', 200, 50, 260, 90, 0.8]);
    }
    return r.fulfill({ json: { cam_id: u.searchParams.get('cam'), rows, source: 'fixture', gaps: [] } });
  });
}

test('street Activity at 1440x900 draws bars binned to its width; hover shows the per-second value', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await longStreet(page);
  await page.goto(`#/street/${ST}`);
  await expect(page.locator('.st-root')).toBeVisible();
  await expect.poll(() => mod(page, 'street/page.js', 'm.probe().activity?.person.filter((n) => n != null).length')).toBe(720);
  const chart = page.locator('.st-act canvas');
  const box = await chart.boundingBox();
  const bins = await mod(page, 'street/page.js', 'm.probe().bins');
  expect(bins.length).toBeGreaterThan(60);
  expect(bins.length).toBeLessThanOrEqual(Math.floor(box.width / 2));
  // Each bar is the most persons in one frame of its seconds.
  const peak = bins.find((b) => b.t0_ms <= 136000 && 136000 < b.t1_ms);
  expect(peak.person).toBe(13);
  for (const b of bins) {
    const own = Array.from({ length: (b.t1_ms - b.t0_ms) / 1000 }, (_, i) => persons(b.t0_ms / 1000 + i));
    expect(b.person).toBe(Math.max(...own));
  }
  await page.mouse.move(box.x + box.width * (137.5 / 720), box.y + box.height / 2);
  await expect(page.locator('.st-act-tip')).toHaveText('02:17.5 · 2 persons · 0 vehicles');
  await page.mouse.move(box.x + box.width * (136.5 / 720), box.y + box.height / 2);
  await expect(page.locator('.st-act-tip')).toHaveText('02:16.5 · 13 persons · 0 vehicles');
});

test('street: a peak answer opens at the peak time and marks it on the Activity chart', async ({ page }) => {
  const eid = 'e_street_peak', base = { run_id: 'r_street_peak' };
  const evidence = { eid, scene_key: ST, cam_id: 'fixture_cam-1', t0_ms: 0, t1_ms: 10000, observation_ids: [], analysis_rev: 'fx-0001', kind: 'measure', method: 'yolo_people_peak',
    value: { count: 5, t_s: 6.4, t0_s: 0, t1_s: 10, coverage: 1 }, unit: null, sigma: null, artifact_ref: null };
  await mockAskFrames(page, [
    { event: 'step', data: { ...base, step_id: 's0', i: 0, state: 'measure', elapsed_ms: 1, summary: 'Measure peak', exec_state: 'precomputed' } },
    { event: 'tool_call', data: { ...base, step_id: 's0', call_id: 'c0', name: 'people_statistic', args: { scene_key: ST }, why: 'Find peak' } },
    { event: 'tool_result', data: { ...base, step_id: 's0', call_id: 'c0', ok: true, envelope: { input_range: { scene_key: ST, t0_ms: 0, t1_ms: 10000 }, elapsed_ms: 1, exec_state: 'precomputed' }, evidence: [evidence] } },
    { event: 'hits', data: { ...base, query_id: 'q0', moments: [{ moment_id: 'm_peak', scene_key: ST, t0_ms: 0, t1_ms: 10000, cams: ['fixture_cam-1'], primary_cam: 'fixture_cam-1', actor_refs: [], evidence_ids: [eid], why: ['Peak'], score: 1, thumb: null }] } },
    { event: 'answer', data: { ...base, sentence_id: 'a0', text: 'Measured peak of 5 simultaneous person detections at 6.4 s across 0–10 s.', claim_ids: [], cites: [eid],
      measure_refs: ['count', 't_s', 't0_s', 't1_s'].map((f) => ({ eid, field: `value.${f}` })) } },
    { event: 'status', data: { ...base, final: 'SUPPORTED', stop_reason: 'enough_evidence' } },
    { event: 'done', data: base },
  ]);
  await page.goto(`#/street/${ST}`);
  await expect(page.locator('.st-root')).toBeVisible();
  await page.locator('#ask-input').fill('When is the street busiest in this clip?');
  await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  await expect.poll(() => mod(page, 'kernel/clock.js', 'm.clock.t')).toBe(6400);
  await expect.poll(() => mod(page, 'street/page.js', 'm.probe().peak_ms')).toBe(6400);
});

// Every timeline row on screen ends at least 4 px above the viewport edge and inside the lane list that scrolls it;
// others are folded away.
const clippedRows = (page) => page.evaluate(() => [...document.querySelectorAll('#timeline .tl-row')].filter((row) => row.checkVisibility()).flatMap((row) => {
  const r = row.getBoundingClientRect(), box = row.closest('.tl-lanes, .tl-cams')?.getBoundingClientRect();
  return r.bottom > innerHeight - 4 + 0.5 || (box && r.bottom > box.bottom + 0.5) ? [row.dataset.lane ?? row.className] : [];
}));

test('fleet trip after an answer: every timeline lane is inside the viewport at 1440x900, folded or whole at 1024x768', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openFleet(page);
  await mockAsk(page, 'fl-where');
  await askWhere(page);
  await expect(page.locator('.fl-ix')).toHaveCount(1);
  for (const sel of ['.tl-ruler', '.lane-agent', '.tl-evidence', '.lane-speed', `[data-lane="trip:${T1}"]`, '.tl-events']) {
    await expect(page.locator(`#timeline ${sel}`)).toBeVisible();
  }
  expect(await clippedRows(page)).toEqual([]);
  await page.setViewportSize({ width: 1024, height: 768 });
  for (const sel of ['.tl-ruler', '.lane-speed', '.tl-evidence']) await expect(page.locator(`#timeline ${sel}`)).toBeVisible();
  await expect.poll(() => clippedRows(page)).toEqual([]);
});

test('home shows the SVA mark once and the product name once', async ({ page }) => {
  await page.goto('#/');
  await expect(page.locator('.home-head')).toBeVisible();
  const marks = await page.evaluate(() => [...document.querySelectorAll('body *')].filter((el) => el.checkVisibility() && [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim() === 'SVA')).length);
  expect(marks).toBe(1);
  expect((await page.locator('body').innerText()).split('Spatial Video Agent').length - 1).toBeLessThanOrEqual(1);
  await expect(page.locator('#home-summary')).toBeVisible();
  await expect(page.locator('.home-filters')).toBeVisible();
});
