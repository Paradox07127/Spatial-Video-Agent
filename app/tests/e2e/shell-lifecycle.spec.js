// F1 regression report: each defect's test records the pre-fix failing assertion and the implemented change.
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { ASKS, mockApi, mockAsk, mockAskHeld, mockAskFrames, mockSceneAlias, fixtureWorkspaces } = require('./mockapi');

const WH = 'wh-sdg-fixture-01', OTHER = 'wh-second', ST = 'st-fixture_cam_1-clip01', OTHER_ST = 'st-second';
const FL = 'fl-pie-fixture-01', GPS = 'fl-pie-fixture-02';
const DIST = 'e_5d60fc7e6ed93290';
const main = () => structuredClone(ASKS.find((a) => a.ask_id === 'wh-main').events);
const frame = (event, data) => ({ event, data: { run_id: 'r_fixturewh0001', ...data } });
const status = (stop_reason = 'enough_evidence', final = 'SUPPORTED') => frame('status', { final, stop_reason });
const step = () => main()[0];
const moment = (key = WH) => ({ ...main().find((f) => f.event === 'hits').data.moments[0], scene_key: key });
const models = () => main().find((f) => f.event === 'model_result').data.results;
const mod = (page, rel, expr) => page.evaluate(async ([r, e]) => new Function('m', `return (${e})`)(await import(new URL(r, document.baseURI).href)), [rel, expr]);
const shell = (page, expr) => page.evaluate(async (e) => new Function('r', `return (${e})`)((await import(document.querySelector('script[src*="app.js"]').src)).currentRun()), expr);
const seek = (page, t) => mod(page, 'kernel/clock.js', `m.clock.seek(${t})`);
const ask = async (page, text = 'What happened here?') => {
  await page.locator('#ask-input').fill(text);
  await page.locator('#ask-input').press('Enter');
};
const routeTo = (page, hash) => page.evaluate((h) => { location.hash = h; }, hash);
const readyWH = (page, key = WH) => expect(page.locator('.wh')).toHaveAttribute('data-scene', key);
const readyST = async (page, key = ST) => {
  await expect(page.locator('.st-root')).toBeVisible();
  await expect.poll(() => mod(page, 'street/page.js', 'm.probe().scene_key')).toBe(key);
};
const encode = (frames) => frames.map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');

async function aliases(page) {
  const ws = fixtureWorkspaces();
  ws.find((w) => w.id === 'warehouse').scenes.push(await mockSceneAlias(page, OTHER, WH));
  ws.find((w) => w.id === 'street').scenes.push(await mockSceneAlias(page, OTHER_ST, ST));
  await page.route(/\/api\/workspaces$/, (r) => r.fulfill({ json: ws }));
}

// A read failure is released only after the head has visibly finalized, avoiding buffered-stream timing races.
async function failingStream(page, frames) {
  await page.addInitScript((head) => {
    const original = window.fetch;
    window.fetch = (url, init) => {
      if (!String(url).endsWith('api/agent/ask')) return original(url, init);
      const body = new ReadableStream({ start(c) {
        c.enqueue(new TextEncoder().encode(head));
        window.__failStream = () => c.error(new Error('comparison socket lost'));
      } });
      return Promise.resolve(new Response(body, { headers: { 'content-type': 'text/event-stream' } }));
    };
  }, encode(frames));
}

let errors;
test.beforeEach(async ({ page }) => {
  test.skip(process.env.SVA_E2E_LIVE === '1', 'uses scripted API and transport failures');
  errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await mockApi(page);
  await page.route(/\/api\/assets\/_map\//, (r) => r.continue());
  await page.route(/\/health$/, (r) => r.fulfill({ json: { ok: true, mode: 'dev', index_rows: {} } }));
});
test.afterEach(() => expect(errors).toEqual([]));

// discarded-stop-reason: before, the status contains "Done" for both failures and no continuation guidance.
// Change: distinguish backend error, budget exhaustion, abstention and completion, retaining partial answers.
for (const [reason, final, message] of [
  ['error', 'UNRESOLVED', 'Investigation failed'],
  ['budget_exhausted', 'UNRESOLVED', 'Budget exhausted'],
  ['abstained', 'NO_MATCH', 'Abstained'],
]) {
  test(`discarded-stop-reason: ${reason} has its own outcome and guidance`, async ({ page }) => {
    await mockAskFrames(page, [step(), frame('answer', { text: 'Partial investigation.', cites: [], measure_refs: [] }), status(reason, final)]);
    await page.goto('#/test');
    await ask(page);
    await expect(page.locator('#ask-status')).toContainText(message);
    if (reason !== 'abstained') await expect(page.locator('#ask-status')).not.toContainText('Done');
    if (reason !== 'abstained') await expect(page.locator('#answer .ans-instead')).toContainText(reason === 'error' ? 'retry' : 'continue');
    await expect(page.locator('#answer')).toContainText('Partial investigation.');
  });
}

// post-final-answer-invalidation: before, #answer loses Supported and becomes Agent connection failed.
// Change: SSE dispatches post-status failures separately; the shell reports comparison failure in Steps.
test('post-final-answer-invalidation: transport failure preserves the finalized answer', async ({ page }) => {
  await failingStream(page, main().filter((f) => f.event !== 'done'));
  await page.goto('#/test');
  await ask(page);
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  const answer = await page.locator('#answer').textContent();
  await page.evaluate(() => window.__failStream());
  await expect(page.locator('#ask-status')).toContainText('Comparison connection failed');
  await expect(page.locator('#answer')).toHaveText(answer);
  await page.locator('#tabs [data-tab="steps"]').click();
  await expect(page.locator('#tab-steps')).toContainText('Comparison connection failed');
});

// hidden-home-run-feedback: before, .nr-stop and #ask-status are hidden, including after HTTP 503 / incomplete EOF.
// Change: move the existing narration/status into home output and settle the page hold on failure or Stop.
test('hidden-home-run-feedback: waiting, Stop and errors remain visible on home', async ({ page }) => {
  await mockAskHeld(page, 'wh-main', 'hits');
  await page.goto('#/');
  await page.locator('#home-input').fill('Search the footage');
  await page.locator('#home-input').press('Enter');
  await expect(page.locator('#ask-status')).toBeVisible();
  await expect(page.locator('.nr-stop')).toBeVisible();
  await page.locator('.nr-stop').click();
  await expect(page.locator('#ask-status')).toHaveText('Stopped. Ask again to continue.');
  await page.evaluate(() => window.__releaseAsk());
  await expect(page).toHaveURL(/#\/$/);
  await expect(page.locator('#moments')).toBeEmpty();
});
for (const failure of ['http', 'incomplete']) {
  test(`hidden-home-run-feedback: ${failure} failure is visible and a new ask can mount`, async ({ page }) => {
    if (failure === 'http') await page.route(/\/api\/agent\/ask$/, (r) => r.fulfill({ status: 503, body: '' }));
    else await mockAskFrames(page, [step()]);
    await page.goto('#/');
    await page.locator('#home-input').fill('Search every clip');
    await page.locator('#home-input').press('Enter');
    await expect(page.locator('#ask-status')).toBeVisible();
    await expect(page.locator('#ask-status')).toContainText('Agent connection failed');
    await expect(page.locator('body')).toHaveAttribute('data-run', 'failed');
    await mockAsk(page, 'wh-main');
    await page.locator('.home-chips .preset').first().click();
    await readyWH(page);
    await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  });
}

// workspace-only-state-reset: before, ordinary warehouse scene changes retain answer, stream and P1 selection.
// Change: reset on scene/page identity changes; only explicit evidence navigation preserves the run.
test('workspace-only-state-reset: palette scene changes clear the run and scene-local selection', async ({ page }) => {
  await aliases(page);
  await mockAsk(page, 'wh-main');
  await page.goto(`#/warehouse/${WH}`);
  await readyWH(page);
  await ask(page);
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  await mod(page, 'kernel/selection.js', "m.selection.set(['P1'], { source: 'user' })");
  await page.locator('#cmdk-btn').click();
  await page.locator('.cmdk-input').fill(OTHER);
  await page.locator('.cmdk-input').press('Enter');
  await readyWH(page, OTHER);
  await expect(page.locator('#answer')).toBeEmpty();
  expect(await shell(page, 'r')).toBeNull();
  expect(await mod(page, 'kernel/selection.js', '[...m.selection.ids]')).toEqual([]);
});

// unguarded-navigation-completion: before, late reads overwrite a newer route or call loadScene before mount.
// Change: generation checks, route-local contexts/hosts and serialized module mounts discard superseded completions.
for (const boundary of ['manifest', 'entities']) {
  test(`unguarded-navigation-completion: superseded ${boundary} cannot win over a newer scene`, async ({ page }) => {
    await aliases(page);
    const mf = JSON.parse(fs.readFileSync(path.resolve(__dirname, `../../../fixtures/sva-out/${WH}/manifest.json`), 'utf8'));
    await page.route(new RegExp(`/api/scene/${OTHER}/manifest$`), (r) => r.fulfill({ json: { ...mf, scene_key: OTHER, t1_ms: 8000 } }));
    let release, started;
    const held = new Promise((r) => (release = r)), seen = new Promise((r) => (started = r));
    await page.route(new RegExp(`/api/scene/${WH}/${boundary}$`), async (r) => { started(); await held; await r.fallback(); });
    await page.goto(`#/warehouse/${WH}`);
    await seen;
    await routeTo(page, `#/warehouse/${OTHER}`);
    release();
    await readyWH(page, OTHER);
    await expect(page.locator('#source-tag')).toContainText(OTHER);
    await expect(page.locator('.wh')).toHaveCount(1);
    expect(await mod(page, 'kernel/clock.js', 'm.clock.range.t1')).toBe(8000);
    await expect(page.locator('link[href$="warehouse.css"]')).toHaveCount(1);
    // A second workspace switch during a street read must not retain any late warehouse DOM or subscriptions.
    await routeTo(page, `#/street/${ST}`);
    await readyST(page);
    await expect(page.locator('.wh')).toHaveCount(0);
    await expect(page.locator('#timeline .lane-coverage')).toHaveCount(1);
  });
}

test('unguarded-navigation-completion: a late warehouse mount cannot replace a newer workspace', async ({ page }) => {
  let release, started;
  const held = new Promise((r) => (release = r)), seen = new Promise((r) => (started = r));
  await page.route(new RegExp(`/api/scene/${WH}/manifest$`), async (r) => { started(); await held; await r.fallback(); });
  await page.goto(`#/warehouse/${WH}`);
  await seen;
  await routeTo(page, '#/test');
  release();
  await expect(page.locator('#stage')).toContainText('kernel test page');
  await expect(page.locator('.wh')).toHaveCount(0);
  await expect(page.locator('body')).toHaveAttribute('data-ws', 'warehouse');
});

// unsettled-page-load-failure: before, timeline/mount rejection escapes and Retry is absent; held commands never settle.
// Change: catch the full load, clean partial resources, settle null, and remount from an existing-style Retry button.
for (const boundary of ['manifest', 'entities']) {
  test(`unsettled-page-load-failure: ${boundary} failure is retryable and drops queued commands`, async ({ page }) => {
    let fail = true;
    await page.route(new RegExp(`/api/scene/${WH}/${boundary}$`), (r) => fail ? r.fulfill({ status: 503, body: '' }) : r.fallback());
    await mockAsk(page, 'wh-main');
    await page.goto('#/');
    await page.locator('.home-chips .preset').first().click();
    await expect(page.locator('#stage .page-retry')).toBeVisible();
    await expect(page.locator('#stage')).toContainText('503');
    fail = false;
    await page.locator('.page-retry').click();
    await readyWH(page);
    await ask(page, 'Retry the investigation');
    await expect(page.locator('#answer .verdict')).toHaveText('Supported');
    await expect.poll(() => mod(page, 'warehouse/page.js', 'm.probe().camera.pitch')).toBeCloseTo(60, 0);
    await expect(page.locator('link[href$="warehouse.css"]')).toHaveCount(1);
  });
}

// seek-without-evidence-activation: before, the URL/scene stays WH and time is sought in WH for foreign evidence.
// Change: central activation mounts the owning workspace, retains the run, applies the hit and replays that scene's queue.
for (const control of ['hit', 'citation', 'keyframe']) {
  test(`seek-without-evidence-activation: ${control} opens its evidence scene before seeking`, async ({ page }) => {
    await aliases(page);
    const m = { ...moment(OTHER_ST), primary_cam: 'fixture_cam-1', cams: ['fixture_cam-1'], actor_refs: [], thumb: null };
    const e = { ...main().findLast((f) => f.event === 'tool_result').data.evidence[0], scene_key: OTHER_ST, eid: DIST, method: 'caption_embed1_rrf', kind: 'model' };
    const call = frame('tool_call', { call_id: 'c1', name: 'search', why: 'Locate evidence', args: {} });
    const result = frame('tool_result', { call_id: 'c1', evidence: [e], ok: true, envelope: { input_range: { t0_ms: 4000, t1_ms: 6000 }, elapsed_ms: 1, exec_state: 'cached' } });
    const moments = control === 'hit' ? [moment(), m] : [m, moment()];
    await mockAskFrames(page, [step(), call, result, frame('hits', { moments, auto_open: null }),
      frame('ui', { scene_key: OTHER_ST, cmd: 'seek', args: { scene_key: OTHER_ST, t_ms: 9000 } }),
      frame('answer', { text: 'At 4.6 seconds.', cites: [DIST], measure_refs: [{ eid: DIST, field: 't0_ms' }] }), status()]);
    await page.goto(`#/warehouse/${WH}`);
    await readyWH(page);
    await ask(page);
    await expect(page.locator('#answer .verdict')).toHaveText('Supported');
    if (control === 'hit') { await page.locator('.p-more').click(); await page.locator('.moment[data-i="1"]').click(); }
    else if (control === 'citation') { await page.locator('#tabs [data-tab="evidence"]').click(); await page.locator(`#tab-evidence .ev[data-eid="${DIST}"]`).click(); }
    else await page.locator('.kf-play').click();
    await expect(page).toHaveURL(new RegExp(`#/street/${OTHER_ST}$`));
    await readyST(page, OTHER_ST);
    await expect(page.locator('#answer .verdict')).toHaveText('Supported');
    expect(await shell(page, 'r.id')).toBe('r_fixturewh0001');
    await expect(page.locator('.st-band')).toHaveCount(1);
    if (control !== 'keyframe') await expect.poll(() => mod(page, 'kernel/clock.js', 'm.clock.t')).toBe(e.t0_ms);
    else await expect.poll(() => mod(page, 'kernel/clock.js', 'm.clock.playing')).toBe(true);
  });
}

test('seek-without-evidence-activation: auto_open selects the actual source from home', async ({ page }) => {
  await aliases(page);
  await mockAskFrames(page, [step(), frame('hits', { moments: [moment(OTHER)], auto_open: OTHER }), status()]);
  await page.goto('#/');
  await page.locator('.home-chips .preset').first().click();
  await expect(page).toHaveURL(new RegExp(`#/warehouse/${OTHER}$`));
  await readyWH(page, OTHER);
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
});

test('seek-without-evidence-activation: GPS-only hit and car clear the previous dashcam detail', async ({ page }) => {
  await mockAskFrames(page, [step(), frame('hits', { moments: [moment(FL), { ...moment(GPS), primary_cam: null, cams: [], actor_refs: [], thumb: null }], auto_open: null }), status()]);
  await page.goto('#/fleet');
  await expect(page.locator('.fl-ix')).toBeVisible();
  await ask(page);
  await page.locator('.p-more').click();
  await page.locator('.moment[data-i="1"]').click();
  await expect(page).toHaveURL(new RegExp(`#/fleet/${GPS}@`));
  await expect(page.locator('.fl-car')).toHaveCount(2);
  await expect(page.locator('.fl-ix')).toHaveCount(0);
  await routeTo(page, `#/fleet/${FL}@3000`);
  await expect(page.locator('.fl-ix')).toBeVisible();
  await page.locator('.fl-map').click();
  await mod(page, 'fleet/page.js', "m.view.overview.map.jumpTo({ center: [-79.398, 43.6488], zoom: 13 })");
  await page.locator(`.fl-car[data-trip="${GPS}"]`).dispatchEvent('click');
  await expect(page.locator(`.fl-car[data-trip="${GPS}"]`)).toHaveClass(/\bsel\b/);
  await expect(page.locator('.fl-car')).toHaveCount(2);
  await expect(page.locator('.fl-ix')).toHaveCount(0);
});

test('seek-without-evidence-activation: a GPS-only event closes the other trip detail', async ({ page }) => {
  const dir = path.resolve(__dirname, `../../../fixtures/sva-out/${GPS}`);
  const mf = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
  const points = JSON.parse(fs.readFileSync(path.join(dir, 'trip.geojson'), 'utf8')).features.slice(1).map((f) => f.properties);
  const event = { id: 'GPS-E1', type: 'gps_pass', t0_ms: 3000, t1_ms: 3500, severity: 2, actor_ids: [], params: {} };
  await page.route(new RegExp(`/api/scene/${GPS}/manifest$`), (r) => r.fulfill({ json: { ...mf, assets: { ...mf.assets, events: 'events.json' } } }));
  await page.route(new RegExp(`/api/assets/${GPS}/events.json$`), (r) => r.fulfill({ json: { events: [event] } }));
  await page.goto('#/fleet');
  await expect(page.locator('.fl-ix')).toBeVisible();
  await page.locator('.fl-map').click();
  const p = points.findLast((p) => p.t_ms <= 3000) ?? points[0];
  await mod(page, 'fleet/page.js', `m.view.overview.map.jumpTo({ center: [${p.lon}, ${p.lat}], zoom: 18 })`);
  const pin = page.locator(`.fl-pin[title^="${GPS} GPS-E1 "]`);
  await expect(pin).toHaveCount(1);
  await pin.dispatchEvent('click');
  await expect(page.locator(`.fl-car[data-trip="${GPS}"]`)).toHaveClass(/\bsel\b/);
  expect(await mod(page, 'kernel/clock.js', `m.clock.toWall('${GPS}', 3000) === m.clock.t`)).toBe(true);
  await expect(page.locator('.fl-car')).toHaveCount(2);
  await expect(page.locator('.fl-ix')).toHaveCount(0);
});

// lost-model-provenance: before, foreign envelope records appear in #modelpanel and earlier records are lost.
// Change: retain scene -> record_id maps and dispatch/render only the active scene's records.
test('lost-model-provenance: envelope routing retains every batch without displaying another scene', async ({ page }) => {
  await aliases(page);
  const c1 = models().find((r) => r.task === 'C1'), c2 = models().find((r) => r.task === 'C2');
  const foreign = { ...c1, record_id: 'foreign', payload: { ...c1.payload, label: 'unrelated' } };
  await mockAskFrames(page, [step(), frame('model_result', { scene_key: WH, results: [c1] }),
    frame('model_result', { scene_key: WH, results: [c2] }), frame('model_result', { scene_key: OTHER, results: [foreign] }), status()]);
  await page.goto(`#/warehouse/${WH}`);
  await readyWH(page);
  await ask(page);
  await expect(page.locator('#modelpanel')).toContainText('Situation yielding');
  await expect(page.locator('#modelpanel')).toContainText('predicts continue');
  await expect(page.locator('#modelpanel')).not.toContainText('unrelated');
  expect(await shell(page, '[...r.models].map(([key, rs]) => [key, [...rs.keys()]])')).toEqual([[WH, [c1.record_id, c2.record_id]], [OTHER, ['foreign']]]);
});

// stale-asynchronous-page-effects: before, a hit's detached model read applies to a new run with view=hit.
// Change: serialized commands wait for mounted readiness, pass ownership, and guard page API completions by run generation.
test('stale-asynchronous-page-effects: an old hit model read cannot contaminate a newer run', async ({ page }) => {
  let release, requested;
  const held = new Promise((r) => (release = r)), seen = new Promise((r) => (requested = r));
  const c2 = models().find((r) => r.task === 'C2');
  const old = { ...c2, record_id: 'stale-record', payload: { ...c2.payload, actions: ['stale-action'] } };
  let reads = 0;
  await page.route(/\/api\/model-results\?/, async (r) => {
    if (reads++ > 0) return r.fulfill({ json: [] });
    requested();
    await held;
    await r.fulfill({ json: [old], headers: { 'x-old': '1' } });
  });
  await mockAskFrames(page, [step(), frame('hits', { moments: [moment()] }), status()]);
  await page.goto(`#/warehouse/${WH}`);
  await readyWH(page);
  await ask(page, 'First run');
  await seen;
  await mockAskFrames(page, [step(), frame('hits', { moments: [moment()], auto_open: null }), status()]);
  await ask(page, 'Second run');
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  const finished = page.waitForResponse((r) => r.headers()['x-old'] === '1');
  release();
  await finished;
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  await expect(page.locator('.lbl-model', { hasText: 'stale-action' })).toHaveCount(0);
});

test('stale-asynchronous-page-effects: async page commands complete in arrival order', async ({ page }) => {
  let release, started;
  const held = new Promise((r) => (release = r)), seen = new Promise((r) => (started = r));
  await page.route(/\/api\/model-results\?/, async (r) => { started(); await held; await r.fulfill({ json: [] }); });
  // Use the existing test page interface with a held API boundary to make command ordering observable.
  await page.route(/\/kernel\/_testpage\.js$/, (r) => r.fulfill({ contentType: 'text/javascript', body: `
    let ctx;
    export function mount(root, c) { ctx = c; window.__effects = []; root.textContent = 'Ordering test'; return () => {}; }
    export async function applyHits(ms, owns = () => true) {
      await ctx.api.get('api/model-results?scene_key=${WH}&t_ms=4000');
      if (owns()) window.__effects.push('hit');
    }
    export function applyUI() { window.__effects.push('seek'); }
  ` }));
  await mockAskFrames(page, [step(), frame('hits', { moments: [moment()] }), frame('ui', { cmd: 'seek', args: { t_ms: 4000 } }), status()]);
  await page.goto('#/test');
  await expect(page.locator('#stage')).toHaveText('Ordering test');
  await ask(page);
  await seen;
  expect(await page.evaluate(() => window.__effects)).toEqual([]);
  release();
  await expect.poll(() => page.evaluate(() => window.__effects)).toEqual(['hit', 'seek']);
});

// cross-page-citation-bubbling: before, an older run's same eid switches the hidden workspace into evidence mode.
// Change: listen for workspace citations only inside #panel.
test('cross-page-citation-bubbling: old reasoning cites do not seek the retained workspace', async ({ page }) => {
  const saved = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../../fixtures/runs/r_fixturewh0001.json'), 'utf8'));
  const old = { ...saved, run_id: 'r_older', steps: saved.steps.map((s) => s.name === 'answer'
    ? { ...s, output: { text: 'An older answer.', cites: [DIST] } } : s) };
  await page.route(/\/api\/agent\/runs\/r_older$/, (r) => r.fulfill({ json: old }));
  await mockAsk(page, 'wh-main');
  await page.goto('#/test');
  await ask(page);
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  await seek(page, 1000);
  await routeTo(page, '#/run/r_older');
  await expect(page.locator('#runpage .rp-q')).toBeVisible();
  await page.locator('#runpage .rp-sent .cite').click();
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'explore');
  expect(await mod(page, 'kernel/clock.js', 'm.clock.t')).toBe(1000);
  await expect(page.locator(`#runpage .rp-evrow[data-eid="${DIST}"]`)).toHaveClass(/flash/);
});

// unbounded-pinned-answer-height: before, the answer has no overflow and clips the tabs/follow-up at 600px height.
// Change: cap the existing answer at 40vh and give it its own vertical scroll.
test('unbounded-pinned-answer-height: long answers scroll while tabs and follow-up stay reachable', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 });
  const text = 'The investigation checked another interval. '.repeat(180) + 'Final interval reached.';
  await mockAskFrames(page, [step(), frame('answer', { text, cites: [], measure_refs: [] }), status()]);
  await page.goto('#/test');
  await ask(page);
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  expect(await page.locator('#answer').evaluate((el) => el.scrollHeight > el.clientHeight && getComputedStyle(el).overflowY === 'auto')).toBe(true);
  await page.locator('#answer').evaluate((el) => { el.scrollTop = el.scrollHeight; });
  await expect(page.locator('#tabs')).toBeInViewport();
  await expect(page.locator('#followup-input')).toBeInViewport();
  expect(await page.locator('#answer').evaluate((el) => el.scrollTop > 0)).toBe(true);
});

// omitted-street-discovery: before, the real workspace API returns only warehouse/fleet (browser mocks hid it).
// Change: add the real street group with tags/levels/caps; retain the palette's street route.
test('omitted-street-discovery: real workspace API includes street and palette opens its route', async ({ page }) => {
  const response = await page.request.get('api/workspaces');
  expect(response.ok()).toBe(true);
  expect((await response.json()).map((w) => w.id)).toEqual(['warehouse', 'fleet', 'street']);
  await page.goto('#/');
  await page.locator('#cmdk-btn').click();
  await page.locator('.cmdk-input').fill(ST);
  await page.locator('.cmdk-input').press('Enter');
  await expect(page).toHaveURL(new RegExp(`#/street/${ST}$`));
  await readyST(page);
  await expect(page.locator('.st-caps')).toContainText('no geometry');
});

// omitted-corpus-source-context: before, the typed question's context picks the remembered scene instead of corpus.
// Change: only unpicked home text sends scope=corpus; source picks and preset chips retain their workspace context.
test('omitted-corpus-source-context: empty home source searches the corpus, picks and presets stay scoped', async ({ page }) => {
  const sent = [];
  page.on('request', (r) => r.url().endsWith('/api/agent/ask') && sent.push(r.postDataJSON().context));
  await page.addInitScript(() => localStorage.setItem('sva.lastRoute', '#/fleet/fl-pie-fixture-01@0'));
  await page.route(/\/api\/agent\/ask$/, () => {});
  await page.goto('#/');
  await page.locator('#home-input').fill('Search every clip across sites');
  await page.locator('#home-input').press('Enter');
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0]).toEqual({ scope: 'corpus' });
  await page.locator('.nr-stop').click();
  await page.locator('#home-input').fill('Show the street @st-');
  await expect(page.locator('.home-at')).toBeVisible();
  await page.locator('#home-input').press('Enter');
  await page.locator('#home-input').press('Enter');
  await expect.poll(() => sent.length).toBe(2);
  expect(sent[1]).toEqual({ workspace: 'street', scene_key: ST });
  await page.locator('.nr-stop').click();
  await page.locator('.home-chips .preset[data-ws="fleet"]').click();
  await expect.poll(() => sent.length).toBe(3);
  expect(sent[2]).toEqual({ workspace: 'fleet', scene_key: null });
});

// case-sensitive-palette-filter: before, lowercase warehouse misses the capitalized title and wh-prefixed key.
// Change: normalize query, labels and hints, preserving the original text of the Ask action.
test('case-sensitive-palette-filter: lowercase query finds a capitalized scene title', async ({ page }) => {
  const ws = fixtureWorkspaces();
  ws[0].scenes[0].title = 'Warehouse fixture';
  await page.route(/\/api\/workspaces$/, (r) => r.fulfill({ json: ws }));
  await page.goto('#/test');
  await page.locator('#cmdk-btn').click();
  await page.locator('.cmdk-input').fill('warehouse');
  await expect(page.locator('#cmdk li').first()).toContainText('Warehouse fixture');
  await expect(page.locator('#cmdk li').last()).toContainText('Ask the agent: warehouse');
});

// unrestored-palette-focus: before, Escape leaves focus in a hidden palette/body instead of the Ask input.
// Change: remember the invoking element and restore it if still connected.
test('unrestored-palette-focus: Escape returns focus to the invoking Ask input', async ({ page }) => {
  await page.goto('#/test');
  await page.locator('#ask-input').fill('Continue here');
  await page.locator('#ask-input').press('Control+k');
  await expect(page.locator('.cmdk-input')).toBeFocused();
  await page.locator('.cmdk-input').press('Escape');
  await expect(page.locator('#ask-input')).toBeFocused();
  await page.keyboard.type(' please');
  await expect(page.locator('#ask-input')).toHaveValue('Continue here please');
});

// unscrolled-keyboard-selection: before, the active row falls below the list viewport as ArrowDown advances.
// Change: scroll each rendered active row with block:nearest in both pickers.
for (const picker of ['palette', 'home']) {
  test(`unscrolled-keyboard-selection: ${picker} keeps the selected row in the list viewport`, async ({ page }) => {
    const ws = fixtureWorkspaces(), first = ws[0].scenes[0];
    ws[0].scenes = Array.from({ length: 35 }, (_, i) => ({ ...first, scene_key: `wh-row-${i}`, title: `Warehouse row ${i}` }));
    await page.route(/\/api\/workspaces$/, (r) => r.fulfill({ json: ws }));
    await page.goto('#/');
    if (picker === 'palette') await page.locator('#cmdk-btn').click();
    else await page.locator('#home-input').fill('@');
    const list = page.locator(picker === 'palette' ? '#cmdk ul' : '.home-at');
    await expect(list).toBeVisible();
    const input = page.locator(picker === 'palette' ? '.cmdk-input' : '#home-input');
    for (let i = 0; i < 20; i++) await input.press('ArrowDown');
    expect(await list.evaluate((el) => {
      const active = el.querySelector('.on').getBoundingClientRect(), box = el.getBoundingClientRect();
      return active.top >= box.top && active.bottom <= box.bottom;
    })).toBe(true);
    expect(await list.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
  });
}

// stale-source-picker-completion: before, resolving workspaces after blur opens the dismissed @ dropdown.
// Change: invalidate generation on blur/disposal and verify the focused input after the await.
test('stale-source-picker-completion: a slow source list cannot reopen after blur', async ({ page }) => {
  let release, started;
  const held = new Promise((r) => (release = r)), seen = new Promise((r) => (started = r));
  await page.route(/\/api\/workspaces$/, async (r) => { started(); await held; await r.fallback(); });
  await page.goto('#/');
  await page.locator('#home-input').fill('@');
  await seen;
  await page.locator('#home-input').evaluate((el) => el.blur());
  const finished = page.waitForResponse((r) => r.url().endsWith('/api/workspaces'));
  release();
  await (await finished).finished();
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  await expect(page.locator('.home-at')).toBeHidden();
  await page.locator('#home-input').fill('@wh');
  await expect(page.locator('.home-at')).toBeVisible();
  await page.locator('#home-input').press('Escape');
  await expect(page.locator('.home-at')).toBeHidden();
});
