const { test, expect } = require('@playwright/test');
const { ASKS, mockApi, mockAskFrames, mockSceneAlias, fixtureWorkspaces } = require('./mockapi');
const fs = require('fs');
const path = require('path');
const T1 = 'fl-pie-fixture-01', T3 = 'fl-pie-fixture-03';
const out = path.resolve(__dirname, '../../../fixtures/sva-out');
const mod = (page, rel, expr) => page.evaluate(async ([r, e]) => new Function('m', `return (${e})`)(await import(new URL(r, document.baseURI).href)), [rel, expr]);

test('three fleet candidates keep the cited trip active; selecting another synchronizes frame, lane and follow-up', async ({ page }) => {
  await mockApi(page);
  const third = await mockSceneAlias(page, T3, T1);
  const ws = fixtureWorkspaces(); ws.find(w => w.id === 'fleet').scenes.push(third);
  await page.route(/\/api\/workspaces$/, r => r.fulfill({ json: ws }));
  const gj = JSON.parse(fs.readFileSync(path.join(out, T1, 'trip.geojson')));
  gj.features[0].properties.replay.trip_id = T3;
  await page.route(new RegExp(`/api/assets/${T3}/trip.geojson$`), r => r.fulfill({ json: gj }));
  await page.route(/\/api\/fleet\/trips$/, r => r.fulfill({ json: [T1, T3, 'fl-pie-fixture-02'].map(trip_id => ({ trip_id, title: trip_id, geojson_url: `api/assets/${trip_id}/trip.geojson`, full: trip_id !== 'fl-pie-fixture-02' })) }));
  const frames = structuredClone(ASKS.find(a => a.ask_id === 'fl-where').events);
  const hit = frames.find(f => f.event === 'hits').data;
  const first = hit.moments[0];
  hit.moments.push({ ...first, scene_key: 'fl-pie-fixture-02', moment_id: 'm2', t0_ms: 5000, t1_ms: 8000 },
    { ...first, scene_key: T3, moment_id: 'm3', t0_ms: 7000, t1_ms: 9000, thumb: first.thumb.replace(T1, T3) });
  await mockAskFrames(page, frames);
  await page.goto('#/fleet');
  await expect(page.locator('.fl-car')).toHaveCount(3);
  await page.locator('#ask-input').fill('Was this car speeding?'); await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  await expect(page).toHaveURL(new RegExp(`#/fleet/${T1}@3000$`));
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.trip.id')).toBe(T1);
  await expect(page.locator('#keyframe .kf-at')).toContainText('00:03.0');
  await expect(page.locator('#followup .fu-ctx')).toContainText('00:03.0');
  await expect(page.locator('#source-tag')).toContainText(T1);
  await expect(page.locator(`#timeline [data-lane="trip:${T1}"]`)).toBeVisible();
  await page.locator('.p-moments-head .p-more').click(); await page.locator('.moment').nth(2).click();
  await expect(page).toHaveURL(new RegExp(`#/fleet/${T3}@7000$`));
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.trip.id')).toBe(T3);
  await expect(page.locator('#keyframe .kf-at')).toContainText('00:07.0');
  await expect(page.locator('#followup .fu-ctx')).toContainText('00:07.0');
  await expect(page.locator('#source-tag')).toContainText(T3);
  const request = page.waitForRequest(/\/api\/agent\/ask$/);
  await page.locator('#followup-input').fill('What happened then?'); await page.locator('#followup-input').press('Enter');
  expect((await request).postDataJSON().context).toMatchObject({ scene_key: T3, t_ms: 7000 });
});

test('answer first: level, missing conditions, merged verdict card and removable follow-up constraints', async ({ page }) => {
  await mockApi(page);
  const frames = structuredClone(ASKS.find(a => a.ask_id === 'wh-main').events);
  const claim = frames.find(f => f.event === 'claim' && f.data.after === 'SUPPORTED');
  claim.data.predicates.push({ predicate_id: 'missing', subject_refs: [], operator: 'attribute:red', value: true, unit: null, status: 'unknown', eids: [] });
  claim.data.explain = { not_measured: ['attribute:red'] };
  frames.splice(frames.indexOf(claim) + 1, 0, { event: 'claim', data: { ...structuredClone(claim.data), claim_id: 'same-moment' } });
  await mockAskFrames(page, frames);
  await page.goto('#/warehouse/wh-sdg-fixture-01');
  await expect(page.locator('.wh')).toBeVisible();
  await page.locator('#ask-input').fill('Was a red worker close to the forklift?'); await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await expect(page.locator('#answer .evidence-level')).toHaveText('L3');
  await expect(page.locator('.not-measured')).toContainText('attribute:red — not checked');
  await expect(page.locator('.verdict-card')).toHaveCount(1);
  await expect(page.locator('.condition-chip').filter({ hasText: 'attribute:red' })).toHaveCount(1);
  await expect(page.locator('#followup [data-context="time"]')).toBeVisible();
  await mod(page, 'kernel/selection.js', "m.selection.set(['P2', 'F1'], {source:'user'})");
  await page.locator('#followup [data-context="time"]').click();
  await page.locator('#followup [data-context="selection"]').click();
  const request = page.waitForRequest(/\/api\/agent\/ask$/);
  await page.locator('#followup-input').fill('What was checked?'); await page.locator('#followup-input').press('Enter');
  const context = (await request).postDataJSON().context;
  expect(context).not.toHaveProperty('t_ms'); expect(context).not.toHaveProperty('selection');
  await page.evaluate(() => location.hash = '#/street/st-fixture_cam_1-clip01');
  await expect(page.locator('#ask-input')).toHaveValue('');
});

test('40 percent panel resizes within confirmed bounds and narrow workspace stacks', async ({ page }) => {
  await mockApi(page); await page.goto('#/street/st-fixture_cam_1-clip01');
  const ratio = () => page.locator('#panel').evaluate(el => el.getBoundingClientRect().width / el.closest('main').getBoundingClientRect().width);
  await expect.poll(ratio).toBeCloseTo(0.4, 2);
  const handle = await page.locator('.panel-resize').boundingBox();
  await page.mouse.move(handle.x + 2, handle.y + 50); await page.mouse.down(); await page.mouse.move(720, handle.y + 50); await page.mouse.up();
  await expect.poll(ratio).toBeCloseTo(0.5, 2);
  await page.locator('.panel-resize').focus(); await page.keyboard.press('Home');
  await page.locator('.panel-resize').focus(); await page.keyboard.press('ArrowLeft');
  await expect.poll(ratio).toBeCloseTo(592 / 1440, 3);
  for (let i = 0; i < 30; i++) await page.keyboard.press('ArrowLeft');
  await expect.poll(ratio).toBeCloseTo(2/3, 2);
  await page.keyboard.press('Home'); await expect.poll(ratio).toBeCloseTo(0.4, 2);
  await page.setViewportSize({ width: 486, height: 900 });
  const stage = await page.locator('.stage-wrap').boundingBox(), panel = await page.locator('#panel').boundingBox();
  expect(stage.width).toBeGreaterThan(400); expect(stage.height).toBeGreaterThan(200);
  expect(panel.y).toBeGreaterThanOrEqual(stage.y + stage.height - 1);
});

test('a count without a numeric measure_ref is not labelled Measured; model timing stays approximate', async ({ page }) => {
  await mockApi(page);
  const frames = structuredClone(ASKS.find(a => a.ask_id === 'fl-where').events);
  frames.find(f => f.event === 'answer').data.measure_refs = [];
  await mockAskFrames(page, frames);
  await page.goto('#/fleet'); await expect(page.locator('.fl-car')).toHaveCount(2);
  await page.locator('#ask-input').fill('How many pedestrians?'); await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  await expect(page.locator('#answer .src-measure')).toHaveCount(0);
  await expect(page.locator('#modelpanel')).toContainText('model says ≈');
  await expect(page.locator('#modelpanel')).toContainText('not measured');
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await expect(page.locator('.fl-cols .fl-col')).toHaveCount(0);
  await page.locator('.stage-controls [data-mode="explore"]').click();
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'explore');
});

test('Fleet annotations use presented mediaTime while the master clock is ahead', async ({ page }) => {
  await mockApi(page);
  await page.addInitScript(() => { window.__frames = []; HTMLVideoElement.prototype.requestVideoFrameCallback = function (fn) { window.__frames.push(fn); return window.__frames.length; }; });
  await page.goto(`#/fleet/${T1}@6000`);
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix?.frame?.t_ms')).toBe(6000);
  await expect.poll(() => page.locator('.fl-vid video').evaluate(v => v.readyState >= 2 && !v.seeking)).toBe(true);
  await page.evaluate(() => window.__frames.at(-1)(0, { mediaTime: 3 }));
  await expect.poll(() => mod(page, 'fleet/page.js', 'm.view.ix.presented')).toBe(3000);
  expect(await mod(page, 'kernel/clock.js', 'm.clock.t')).toBe(6000);
  expect(await mod(page, 'fleet/page.js', 'm.view.ix.frame.t_ms')).toBe(6000);
});

test('timeline brush and drawn zone reach the request as explicit removable scope', async ({ page }) => {
  await mockApi(page); await mockAskFrames(page, structuredClone(ASKS.find(a => a.ask_id === 'wh-main').events));
  await page.goto('#/warehouse/wh-sdg-fixture-01'); await expect(page.locator('.wh')).toBeVisible();
  await page.locator('#ask-input').fill('How close did the forklift come?'); await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  const track = await page.locator('[data-lane="agent"] .tl-track').boundingBox();
  await page.mouse.move(track.x + track.width * .2, track.y + track.height / 2); await page.mouse.down();
  await page.mouse.move(track.x + track.width * .6, track.y + track.height / 2); await page.mouse.up();
  await expect(page.locator('#followup [data-context="range"]')).toBeVisible();
  const zone = [[0,0],[3,0],[3,3],[0,3]];
  await page.evaluate(async zone => (await import(new URL('kernel/bus.js', document.baseURI).href)).bus.emit('context:zone', zone), zone);
  await expect(page.locator('#followup [data-context="zone"]')).toBeVisible();
  const request = page.waitForRequest(/\/api\/agent\/ask$/);
  await page.locator('#followup-input').fill('What about this area?'); await page.locator('#followup-input').press('Enter');
  const context = (await request).postDataJSON().context;
  expect(context).not.toHaveProperty('t_ms'); expect(context.t_range[0]).toBeCloseTo(2000, -1); expect(context.t_range[1]).toBeCloseTo(6000, -1); expect(context.zone).toEqual(zone);
});

test('degraded model dependencies are visible without claiming inference readiness', async ({ page }) => {
  await mockApi(page); await page.route(/\/api\/dependencies$/, r => r.fulfill({ json: { degraded: true, services: { vlm: { configured: true, status: 'unavailable' } }, check: 'tcp_reachability', inference_verified: false } }));
  await page.goto('#/'); await expect(page.locator('#dependency-status')).toHaveText('Live models unavailable');
  await expect(page.locator('#dependency-status')).toHaveAttribute('title', /connection check only/);
});

test('a cited Fleet facade stays visible in Answer mode without becoming an actor constraint', async ({ page }) => {
  await mockApi(page);
  const frames = structuredClone(ASKS.find(a => a.ask_id === 'fl-where').events).filter(f => f.event !== 'claim');
  const evidence = frames.find(f => f.event === 'tool_result').data.evidence[0];
  Object.assign(evidence, { t0_ms: 3000, t1_ms: 3001, method: 'osm_facade', value: { bid: 'w1001', name: 'Fixture Block A' } });
  frames.find(f => f.event === 'hits').data.moments[0].actor_refs = [];
  Object.assign(frames.find(f => f.event === 'answer').data, { text: 'The building on the right is Fixture Block A.', measure_refs: [], cites: [evidence.eid] });
  await mockAskFrames(page, frames); await page.goto('#/fleet'); await expect(page.locator('.fl-car')).toHaveCount(2);
  await page.locator('#ask-input').fill('What is the building on the right?'); await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  await expect(page.locator('.fl-cols .fl-col')).toHaveCount(1);
  await expect(page.locator('.fl-cols .fl-col')).toHaveAttribute('data-bid', 'w1001');
  expect(await mod(page, 'kernel/selection.js', '[...m.selection.ids]')).not.toContain('w1001');
  await page.locator('.stage-controls [data-mode="explore"]').click();
  await expect(page.locator('.fl-cols .fl-col')).toHaveCount(2);
});
