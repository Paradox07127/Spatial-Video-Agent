// E2E test suite for redesigned answer panel (Batch 1: rows 1, 3, 4, 5, 8, 9, 10).
const { test, expect } = require('@playwright/test');
const { ASKS, mockApi, mockAsk } = require('./mockapi');

const LIVE = process.env.SVA_E2E_LIVE === '1';
const HEALTH = { ok: true, mode: 'dev', libs_ready: true, index_rows: { clips: 6, events: 3 }, assets_ok: true };

const width = async (page, sel) => (await page.locator(sel).boundingBox()).width;

const makeEv = (overrides) => ({
  eid: 'e_default00000001',
  scene_key: 'wh-sdg-fixture-01',
  cam_id: 'ceiling_04',
  t0_ms: 1000,
  t1_ms: 2000,
  observation_ids: [],
  analysis_rev: 'fx-0001',
  kind: 'measure',
  method: 'ground_distance',
  value: null,
  unit: null,
  sigma: null,
  artifact_ref: null,
  ...overrides,
});

let errors, allowed;
test.beforeEach(async ({ page }) => {
  errors = [];
  allowed = /^$/;
  page.on('console', (m) => m.type() === 'error' && errors.push(`${m.text()} @ ${m.location().url}`));
  page.on('pageerror', (e) => errors.push(String(e)));
  await mockApi(page);
  if (!LIVE) await page.route(/\/health$/, (r) => r.fulfill({ json: HEALTH }));
});
test.afterEach(() => expect(errors.filter((e) => !allowed.test(e))).toEqual([]));

async function replay(page, askId) {
  await mockAsk(page, askId);
  await page.goto('#/test');
  await page.locator('.preset').first().click();
}

async function replayFrames(page, list) {
  const body = list.map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');
  await page.route(/\/api\/agent\/ask$/, (r) => r.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body }));
  await page.goto('#/test');
  await page.locator('.preset').first().click();
}

async function askWithHeldStream(page, q, headFrames) {
  const runId = 'r_hold_' + Math.random().toString(36).slice(2);
  const headWithRunId = headFrames.map((f) => ({ ...f, data: { ...f.data, run_id: runId } }));
  const tailFrames = [
    { event: 'status', data: { run_id: runId, final: 'SUPPORTED', stop_reason: 'enough_evidence' } },
    { event: 'done', data: { run_id: runId } }
  ];
  const headStr = headWithRunId.map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');
  const tailStr = tailFrames.map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');

  await page.evaluate(([head, tail]) => {
    window.__origFetch ??= window.fetch;
    const fetch0 = window.__origFetch;
    const gate = new Promise((resolve) => (window.__releaseAsk = resolve));
    window.fetch = (url, init) => {
      if (!String(url).endsWith('api/agent/ask')) return fetch0(url, init);
      const enc = new TextEncoder();
      const body = new ReadableStream({
        async start(c) {
          c.enqueue(enc.encode(head));
          await gate;
          c.enqueue(enc.encode(tail));
          c.close();
        }
      });
      return Promise.resolve(new Response(body, { headers: { 'content-type': 'text/event-stream' } }));
    };
  }, [headStr, tailStr]);

  await page.locator('#ask-input').fill(q);
  await page.locator('#ask-input').press('Enter');
}

test('row 1: panel width defaults to 40% (576px at 1440px), drag and arrow keys resize clamped 1/3-2/3, persists in localStorage', async ({ page }) => {
  await page.goto('#/test');
  const resizer = page.locator('#panel-resizer');

  // 1. Default width is 40% of 1440 = 576px
  expect(Math.round(await width(page, '#panel'))).toBe(576);

  // 2. Resizer is focusable and Left/Right arrows resize it
  await resizer.focus();
  await page.keyboard.press('ArrowLeft'); // expands panel by 16px -> 592
  expect(Math.round(await width(page, '#panel'))).toBe(592);
  await page.keyboard.press('ArrowRight'); // shrinks panel by 16px -> 576
  expect(Math.round(await width(page, '#panel'))).toBe(576);

  // 3. Drag resize and clamping: 1/3 (480px) to 2/3 (960px) of 1440px window
  const box = await resizer.boundingBox();
  // Drag left to x = 100 (clamp at 2/3 = 960px)
  await page.mouse.move(box.x + box.width / 2, box.y + 100);
  await page.mouse.down();
  await page.mouse.move(100, box.y + 100);
  await page.mouse.up();
  expect(Math.round(await width(page, '#panel'))).toBe(960);

  // Drag right to x = 1300 (clamp at 1/3 = 480px)
  const box2 = await resizer.boundingBox();
  await page.mouse.move(box2.x + box2.width / 2, box2.y + 100);
  await page.mouse.down();
  await page.mouse.move(1300, box2.y + 100);
  await page.mouse.up();
  expect(Math.round(await width(page, '#panel'))).toBe(480);

  // 4. Persistence in localStorage: sva.panelWidth
  const stored = await page.evaluate(() => localStorage.getItem('sva.panelWidth'));
  expect(stored).toBe('480');

  // Reload page and check width is restored from localStorage
  await page.goto('#/test');
  expect(Math.round(await width(page, '#panel'))).toBe(480);

  // 5. Window resize clamps saved width so it does not exceed 2/3 of a narrower window
  await page.setViewportSize({ width: 600, height: 900 });
  await expect.poll(async () => Math.round(await width(page, '#panel'))).toBe(400); // 2/3 of 600
  await page.setViewportSize({ width: 1440, height: 900 });
});

test('row 3: verdict cards group by moment with verdict border color, predicate chips (✓, ✗, ?, —) and collapsed counts', async ({ page }) => {
  const frames = [
    { event: 'step', data: { run_id: 'r_vcard', step_id: 'r_vcard.s0', i: 0, state: 'measure', elapsed_ms: 10, summary: 'measure', exec_state: 'live' } },
    { event: 'tool_call', data: {
      run_id: 'r_vcard', step_id: 'r_vcard.s0', call_id: 'call_1', name: 'measure',
      args: { scene_key: 'wh-sdg-fixture-01', a: { entity_id: 'P1' }, b: { entity_id: 'F1' } }
    } },
    { event: 'tool_result', data: {
      run_id: 'r_vcard', step_id: 'r_vcard.s0', call_id: 'call_1', ok: true,
      envelope: { input_range: { scene_key: 'wh-sdg-fixture-01', t0_ms: 0, t1_ms: 5000 } },
      evidence: [
        makeEv({ eid: 'e_supp', method: 'ground_distance', value: 1.5, unit: 'm', sigma: 0.12 }),
        makeEv({ eid: 'e_unres', cam_id: 'ceiling_00', method: 'visibility', value: null }),
      ]
    } },
    { event: 'claim', data: {
      run_id: 'r_vcard',
      claim_id: 'c1',
      moment_id: 'm1',
      text: 'Pedestrian and forklift interaction',
      aggregate: 'CONTRADICTED',
      before: 'UNRESOLVED',
      after: 'CONTRADICTED',
      examined_eids: [],
      available_eids: [],
      explain: null,
      predicates: [
        { predicate_id: 'p1', subject_refs: [{ entity_id: 'P1' }], operator: 'exists', value: 'person', unit: null, status: 'supported', eids: ['e_supp'] },
        { predicate_id: 'p2', subject_refs: [{ entity_id: 'P1' }], operator: 'exists', value: 'person', unit: null, status: 'supported', eids: ['e_supp'] },
        { predicate_id: 'p3', subject_refs: [{ entity_id: 'F1' }], operator: 'lt', value: 2.0, unit: 'm', status: 'contradicted', eids: [] },
        { predicate_id: 'p4', subject_refs: [{ entity_id: 'P1' }], operator: 'visibility', value: 'visible', unit: null, status: 'unknown', eids: ['e_unres'] },
        { predicate_id: 'p5', subject_refs: [{ entity_id: 'P1' }], operator: 'speed', value: 5.0, unit: 'mps', status: 'unknown', eids: [] },
      ]
    } },
    { event: 'answer', data: { run_id: 'r_vcard', sentence_id: 'a1', text: 'Interaction observed.', claim_ids: ['c1'], cites: ['e_supp'], measure_refs: [{ eid: 'e_supp', field: 'value' }] } },
    { event: 'status', data: { run_id: 'r_vcard', final: 'CONTRADICTED', stop_reason: 'enough_evidence' } },
    { event: 'done', data: { run_id: 'r_vcard' } },
  ];

  await replayFrames(page, frames);
  const card = page.locator('#claims .verdict-card');
  await expect(card).toHaveCount(1);
  await expect(card).toHaveClass(/v-contradicted/);

  // Supported chip with collapsed count (p1 and p2 are identical: P1 exists person)
  const suppChip = card.locator('.chip-supported');
  await expect(suppChip).toHaveCount(1);
  await expect(suppChip.locator('.chip-sym')).toHaveText('✓');
  await expect(suppChip.locator('.chip-count')).toHaveText('×2');

  // Contradicted chip
  const contraChip = card.locator('.chip-contradicted');
  await expect(contraChip).toHaveCount(1);
  await expect(contraChip.locator('.chip-sym')).toHaveText('✗');

  // Checked but unresolved (?) chip
  const unresChip = card.locator('.chip-unresolved');
  await expect(unresChip).toHaveCount(1);
  await expect(unresChip.locator('.chip-sym')).toHaveText('?');

  // Not checked (—) chip
  const notCheckedChip = card.locator('.chip-not-checked');
  await expect(notCheckedChip).toHaveCount(1);
  await expect(notCheckedChip.locator('.chip-sym')).toHaveText('—');
});

test('row 4: "Measured" source badge appears with measure_refs and evidence level badge is shown on head and card', async ({ page }) => {
  const framesA = [
    { event: 'step', data: { run_id: 'r_lvl', step_id: 's0', i: 0, state: 'measure', elapsed_ms: 10, summary: 'measure', exec_state: 'live' } },
    { event: 'tool_call', data: {
      run_id: 'r_lvl', step_id: 's0', call_id: 'call_m', name: 'measure',
      args: { scene_key: 'wh-sdg-fixture-01', a: { entity_id: 'P1' }, b: { entity_id: 'F1' } }
    } },
    { event: 'tool_result', data: {
      run_id: 'r_lvl', step_id: 's0', call_id: 'call_m', ok: true,
      envelope: { input_range: { scene_key: 'wh-sdg-fixture-01', t0_ms: 0, t1_ms: 5000 } },
      evidence: [
        makeEv({ eid: 'e_m', method: 'ground_distance', value: 2.1, unit: 'm', sigma: 0.1 }),
      ]
    } },
    { event: 'claim', data: {
      run_id: 'r_lvl', claim_id: 'c1', text: 'Distance check', aggregate: 'SUPPORTED', before: 'UNRESOLVED', after: 'SUPPORTED',
      evidence_level: 'L2', examined_eids: [], available_eids: [], explain: null,
      predicates: [{ predicate_id: 'p1', subject_refs: [{ entity_id: 'P1' }], operator: 'lt', value: 3.0, unit: 'm', status: 'supported', eids: ['e_m'] }]
    } },
    { event: 'answer', data: { run_id: 'r_lvl', sentence_id: 'a1', text: 'Distance is 2.1 m.', claim_ids: ['c1'], cites: ['e_m'], measure_refs: [{ eid: 'e_m', field: 'value' }], evidence_level: 'L2' } },
    { event: 'status', data: { run_id: 'r_lvl', final: 'SUPPORTED', stop_reason: 'enough_evidence' } },
    { event: 'done', data: { run_id: 'r_lvl' } },
  ];
  await replayFrames(page, framesA);
  await expect(page.locator('#answer .ans-src .src-measure')).toBeVisible();
  await expect(page.locator('#answer .ans-head .level-l2')).toHaveText('L2');
  await expect(page.locator('#claims .verdict-card .level-l2')).toHaveText('L2');
});

test('row 4: "Measured" source badge is omitted when measure_refs is empty', async ({ page }) => {
  const framesB = [
    { event: 'step', data: { run_id: 'r_nomeas', step_id: 's0', i: 0, state: 'measure', elapsed_ms: 10, summary: 'measure', exec_state: 'live' } },
    { event: 'tool_call', data: {
      run_id: 'r_nomeas', step_id: 's0', call_id: 'call_m2', name: 'measure',
      args: { scene_key: 'wh-sdg-fixture-01', a: { entity_id: 'P1' }, b: { entity_id: 'F1' } }
    } },
    { event: 'tool_result', data: {
      run_id: 'r_nomeas', step_id: 's0', call_id: 'call_m2', ok: true,
      envelope: { input_range: { scene_key: 'wh-sdg-fixture-01', t0_ms: 0, t1_ms: 5000 } },
      evidence: [
        makeEv({ eid: 'e_m2', method: 'ground_distance', value: 2.1, unit: 'm', sigma: 0.1 }),
      ]
    } },
    { event: 'claim', data: {
      run_id: 'r_nomeas', claim_id: 'c1', text: 'Distance check', aggregate: 'SUPPORTED', before: 'UNRESOLVED', after: 'SUPPORTED',
      examined_eids: [], available_eids: [], explain: null,
      predicates: [{ predicate_id: 'p1', subject_refs: [{ entity_id: 'P1' }], operator: 'lt', value: 3.0, unit: 'm', status: 'supported', eids: ['e_m2'] }]
    } },
    { event: 'answer', data: { run_id: 'r_nomeas', sentence_id: 'a1', text: 'Sentence without measure refs.', claim_ids: ['c1'], cites: ['e_m2'], measure_refs: [] } },
    { event: 'status', data: { run_id: 'r_nomeas', final: 'SUPPORTED', stop_reason: 'enough_evidence' } },
    { event: 'done', data: { run_id: 'r_nomeas' } },
  ];
  await replayFrames(page, framesB);
  await expect(page.locator('#answer .ans-src .src-measure')).toHaveCount(0);
});

test('row 5: "Not measured" line is absent when everything was measured', async ({ page }) => {
  await replay(page, 'wh-main');
  await expect(page.locator('#answer .ans-not-measured')).toHaveCount(0);
});

test('row 5: "Not measured" line directly under answer sentence distinguishes not checked vs checked unresolved', async ({ page }) => {
  const framesAbstain = [
    { event: 'step', data: { run_id: 'r_abs', step_id: 's0', i: 0, state: 'inspect', elapsed_ms: 10, summary: 'inspect', exec_state: 'live' } },
    { event: 'tool_call', data: {
      run_id: 'r_abs', step_id: 's0', call_id: 'call_vis', name: 'visibility',
      args: { scene_key: 'wh-sdg-fixture-01', target: 'P1', cam: 'ceiling_00' }
    } },
    { event: 'tool_result', data: {
      run_id: 'r_abs', step_id: 's0', call_id: 'call_vis', ok: true,
      envelope: { input_range: { scene_key: 'wh-sdg-fixture-01', t0_ms: 0, t1_ms: 5000 } },
      evidence: [
        makeEv({ eid: 'e_vis', cam_id: 'ceiling_00', method: 'floor_cell_visibility', value: { cell: [16, 7], state: 'occluded' } }),
      ]
    } },
    { event: 'claim', data: {
      run_id: 'r_abs', claim_id: 'c1', text: 'Partial measurement', aggregate: 'SUPPORTED', before: 'UNRESOLVED', after: 'SUPPORTED',
      examined_eids: [], available_eids: [],
      explain: { abstain: { reason: 'no_geometry', text: 'single camera, no depth' } },
      predicates: [
        { predicate_id: 'p1', subject_refs: [{ entity_id: 'P1' }], operator: 'distance', value: 2.0, unit: 'm', status: 'unknown', eids: [] },
        { predicate_id: 'p2', subject_refs: [{ entity_id: 'P1' }], operator: 'speed', value: 10.0, unit: 'km/h', status: 'unknown', eids: ['e_vis'] },
        { predicate_id: 'p3', subject_refs: [{ entity_id: 'P1' }], operator: 'detected', value: 'person', unit: null, status: 'unknown', eids: [] },
      ]
    } },
    { event: 'answer', data: { run_id: 'r_abs', sentence_id: 'a1', text: 'The person was detected.', claim_ids: ['c1'], cites: [], measure_refs: [] } },
    { event: 'status', data: { run_id: 'r_abs', final: 'SUPPORTED', stop_reason: 'enough_evidence' } },
    { event: 'done', data: { run_id: 'r_abs' } },
  ];
  await replayFrames(page, framesAbstain);
  const notMeasured = page.locator('#answer .ans-not-measured');
  await expect(notMeasured).toBeVisible();
  await expect(notMeasured).toContainText('Not measured:');
  await expect(notMeasured).toContainText('distance — single camera, no depth');
  await expect(notMeasured).toContainText('speed — checked but unresolved');
  await expect(notMeasured).toContainText('presence — not checked');

  // Verify first screen budget at 1440x900: verdict, cited sentence, and not measured line fit without page scroll
  await expect(page.locator('#answer .ans-head')).toBeVisible();
  await expect(page.locator('#answer .ans-text')).toBeVisible();
  await expect(notMeasured).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight)).toBe(true);
});

test('row 8: model results in box titled "Model description · not measured", time formatted as "model says ≈...", keeps checked by measure', async ({ page }) => {
  await replay(page, 'wh-main');
  const mp = page.locator('#modelpanel');
  await expect(mp.locator('.mp-box')).toBeVisible();
  await expect(mp.locator('.mp-header')).toHaveText('Model description · not measured');
  await expect(mp.locator('.mp-summary')).toContainText('model says ≈00:04');
  await expect(mp.locator('.mp-summary')).not.toContainText('located');
  await expect(mp.locator('.mp-summary')).not.toContainText('measured');
  await expect(mp.locator('.mp-sub')).toContainText('Checked by measure: yes · describes only, no numbers');
});

test('row 9: completed run collapses steps into single line "N steps · X.X s · Reasoning ›" that opens the reasoning page', async ({ page }) => {
  await replay(page, 'wh-main');
  const reasoning = page.locator('#answer .ans-run');
  await expect(reasoning).toBeVisible();
  await expect(reasoning).toHaveText(/^\d+ steps · \d+\.\d+ s · Reasoning ›$/);
  await page.locator('#answer .ans-meta').click();
  await expect(page).toHaveURL(/#\/run\//);
});

test('row 10: route change clears #ask-input', async ({ page }) => {
  await page.goto('#/test');
  await page.locator('#ask-input').fill('A pending question');
  expect(await page.locator('#ask-input').inputValue()).toBe('A pending question');
  // Change scene / workspace
  await page.evaluate(() => { location.hash = '#/warehouse/wh-sdg-fixture-01'; });
  await expect(page.locator('#ask-input')).toHaveValue('');
});

test('row 10: answer slot placeholder reflects question type while waiting', async ({ page }) => {
  await page.goto('#/test');

  const makeStream = (summary, operator, unit = null) => [
    { event: 'step', data: { step_id: 's0', i: 0, state: 'parse', elapsed_ms: 10, summary, exec_state: 'live' } },
    { event: 'claim', data: {
      claim_id: 'c1', text: 'Holding', aggregate: 'UNRESOLVED', before: null, after: 'UNRESOLVED', examined_eids: [], available_eids: [],
      predicates: [{ predicate_id: 'p1', subject_refs: [{ entity_id: 'P1' }], operator, value: 1, unit, status: 'unknown', eids: [] }]
    } }
  ];

  // Distance
  await askWithHeldStream(page, 'How far was the forklift?', makeStream('Template find: distance', 'lt', 'm'));
  await expect(page.locator('#answer .ans-slot .muted')).toHaveText('Appears here once the distance is measured.');
  await page.evaluate(() => window.__releaseAsk());
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');

  // Speeding claim (operator gt, unit km/h)
  await askWithHeldStream(page, 'Was the vehicle speeding?', makeStream('Template speed: speeding check', 'gt', 'km/h'));
  await expect(page.locator('#answer .ans-slot .muted')).toHaveText('Appears here once the speed is measured.');
  await page.evaluate(() => window.__releaseAsk());
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');

  // Count
  await askWithHeldStream(page, 'How many workers in the aisle?', makeStream('Template count: workers in area', 'count'));
  await expect(page.locator('#answer .ans-slot .muted')).toHaveText('Appears here once objects are counted.');
  await page.evaluate(() => window.__releaseAsk());
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');

  // Timing
  await askWithHeldStream(page, 'When did the worker dodge?', makeStream('Template when: worker dodges', 'timing'));
  await expect(page.locator('#answer .ans-slot .muted')).toHaveText('Appears here once the timing is located.');
  await page.evaluate(() => window.__releaseAsk());
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');

  // Speed
  await askWithHeldStream(page, 'What was the vehicle speed?', makeStream('Template speed: forklift speed', 'speed', 'm/s'));
  await expect(page.locator('#answer .ans-slot .muted')).toHaveText('Appears here once the speed is measured.');
  await page.evaluate(() => window.__releaseAsk());
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');

  // Other / inspect
  await askWithHeldStream(page, 'Did the alarm trigger?', makeStream('Template inspect: check condition', 'exists'));
  await expect(page.locator('#answer .ans-slot .muted')).toHaveText('Appears here once evidence is verified.');
  await page.evaluate(() => window.__releaseAsk());
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
});
