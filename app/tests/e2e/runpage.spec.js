// Reasoning page layout (board v2-5a): section nav, verdict-changing step, evidence raw, model notes, VSS comparison.
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { mockApi } = require('./mockapi');

const LIVE = process.env.SVA_E2E_LIVE === '1';
const FX = path.resolve(__dirname, '..', '..', '..', 'fixtures');
const RUN = JSON.parse(fs.readFileSync(path.join(FX, 'runs', 'r_fixturewh0001.json'), 'utf8'));
const COSMOS = JSON.parse(fs.readFileSync(path.join(FX, 'sva-out', 'wh-sdg-fixture-01', 'cosmos', 'wh-sdg-fixture-01.json'), 'utf8')).records;
const rec = (task) => COSMOS.find((r) => r.task === task);
const TEXT = 'The worker started to dodge at 4.6 s, 1.55 m (±0.12 m) from the forklift.';
const VSS = { run_id: RUN.run_id, status: 'ok', elapsed_ms: 1200, sql_query: 'SELECT source FROM "vss-collection" LIMIT 5', hits: [
  { rank: 1, source: 's3://b/a.mp4', scene_key: 'wh-sdg-fixture-01', cam_id: 'ceiling_04', t_ms: 4000, similarity: 0.412, caption: 'A worker steps back as a forklift approaches.' },
] };
const step = (i, name, output) => ({ step_id: `${RUN.run_id}.s${i}`, parent_step_id: null, i, state: 'inspect', name, started_ms: 96 + i, elapsed_ms: 4,
  input: {}, output, evidence_ids: [], note: null, exec_state: 'cached', weave_call_id: null, trace_delivery_state: 'local', blob_ref: null });
const answered = (cites) => RUN.steps.map((s) => (s.name === 'answer' ? { ...s, output: { sentence_id: 'a1', text: TEXT, cites } } : s));
const FULL = { ...RUN, steps: [...answered(['e_5d60fc7e6ed93290']),
  step(9, 'vlm.C1', rec('C1')), step(10, 'vlm.C2', rec('C2')),
  step(11, 'vlm.C4', { ...rec('C4'), verification: { status: 'conflict', method: 'onset 0.2 s early', ref: null } }),
  step(12, 'vss.search', VSS)] };

let errors;
test.beforeEach(async ({ page }) => {
  test.skip(LIVE, 'needs mocked runs');
  errors = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(`${m.text()} @ ${m.location().url}`));
  page.on('pageerror', (e) => errors.push(String(e)));
  await mockApi(page);
  await page.route(/\/api\/agent\/runs$/, (r) => r.fulfill({ json: [FULL] }));
});
test.afterEach(() => expect(errors).toEqual([]));

async function open(page, run) {
  await page.route(/\/api\/agent\/runs\/r_fixturewh0001$/, (r) => r.fulfill({ json: run }));
  await page.goto('#/test');
  await page.evaluate(() => (location.hash = '#/run/r_fixturewh0001'));
  await expect(page.locator('#runpage .rp-q')).toHaveText(RUN.q);
  return page.locator('#runpage');
}

test('the section nav lists the sections with counts; clicking one scrolls it into view and marks it', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 });
  const rp = await open(page, FULL);
  const nav = rp.locator('.rp-nav button');
  await expect(nav).toHaveText(['Verdict', 'What changed', 'Steps · 13', 'Evidence · 4', 'Model notes · 3', 'VSS comparison']);
  await expect(nav.nth(0)).toHaveClass(/\bon\b/);
  const ev = rp.locator('section[data-sec="evidence"]');
  await expect(ev).not.toBeInViewport();
  await nav.nth(3).click();
  await expect(ev).toBeInViewport();
  await expect(nav.nth(3)).toHaveClass(/\bon\b/);
  await expect(nav.nth(0)).not.toHaveClass(/\bon\b/);
});

test('the verdict-changing step is expanded inline, the others collapsed; the card shows before → after', async ({ page }) => {
  const rp = await open(page, FULL);
  await expect(rp.locator('.rp-changed .p-label')).toHaveText('WHAT CHANGED THE VERDICT · STEP 8');
  await expect(rp.locator('.rp-changed .verdict')).toHaveText(['UNRESOLVED', 'SUPPORTED']);
  await expect(rp.locator('.rp-changed')).toContainText(RUN.claims[0].text);
  await expect(rp.locator('.rp-changed .t-frame').first()).toContainText('ceiling_00');
  await expect(rp.locator('.trace-detail')).toHaveCount(1);
  const changed = rp.locator('.trace-step[data-i="7"]');
  await expect(changed).toHaveClass(/changed/);
  const detail = changed.locator('.trace-detail');
  await expect(detail.locator('.td-kv')).toContainText('camera');
  await expect(detail.locator('.td-kv')).toContainText('ceiling_04');
  await expect(detail.locator('.td-kv')).toContainText('P1, F1');
  await expect(detail.locator('.td-links > details > summary')).toHaveText(['Request', 'Response']);
  await expect(detail.locator('a.td-3d')).toHaveAttribute('href', '?t_ms=4600#/warehouse/wh-sdg-fixture-01');
  await rp.locator('.trace-step[data-i="4"] .trace-row').click();
  await expect(rp.locator('.trace-step[data-i="4"] .trace-detail')).toHaveCount(1);
});

test('an evidence row has a human label and value; raw shows the aligned key / value pre', async ({ page }) => {
  const rp = await open(page, FULL);
  const row = rp.locator('.rp-evrow[data-eid="e_5d60fc7e6ed93290"]');
  await expect(row.locator('.rp-evlabel')).toHaveText('Distance P1 to F1');
  await expect(row.locator('.rp-evwhere')).toHaveText('ceiling_04 + eye_00 · 00:04.6');
  await expect(row.locator('.rp-evval')).toHaveText('1.55 m ±0.12');
  const pre = row.locator('pre.rp-raw');
  await expect(pre).toBeHidden();
  await row.locator('.rp-raw-btn').click();
  await expect(pre).toBeVisible();
  await expect(pre).toContainText('method  ground_distance_tri');
  await row.locator('.rp-raw-btn').click();
  await expect(pre).toBeHidden();
});

test('model notes say whether a measure checked them, a conflict in danger; VSS comparison opens to its hits and SQL', async ({ page }) => {
  const rp = await open(page, FULL);
  const notes = rp.locator('.rp-note');
  await expect(notes.locator('.rp-note-k')).toHaveText(['Situation C1', 'Next C2', 'Timing C4']);
  await expect(notes.nth(0).locator('.rp-check')).toHaveText('Checked by measure: yes');
  await expect(notes.nth(2).locator('.rp-check')).toHaveText('Conflict · onset 0.2 s early');
  await expect(notes.nth(2).locator('.rp-check')).toHaveClass(/conflict/);
  const vss = rp.locator('details.rp-vss');
  await expect(vss.locator('> summary')).toContainText('done · 1.2 s · 1 hit');
  await expect(vss.locator('> summary')).toContainText('For comparison only · not used in the answer');
  await expect(vss.locator('li')).toBeHidden();
  await vss.locator('> summary').click();
  await expect(vss.locator('li')).toContainText('A worker steps back as a forklift approaches.');
  await expect(vss).toContainText('FROM "vss-collection"');
});

test('the Weave link shows only with a weave_url; a run without model or VSS steps omits those sections', async ({ page }) => {
  const rp = await open(page, { ...RUN, steps: answered(['e_5d60fc7e6ed93290']) });
  await expect(rp.locator('.rp-top .rp-back')).toContainText('wh-sdg-fixture-01');
  await expect(rp.locator('.rp-top a[href="#/runs"]')).toHaveText('All runs');
  await expect(rp.locator('.rp-weave')).toHaveCount(0);
  await expect(rp.locator('.rp-nav button')).toHaveText(['Verdict', 'What changed', 'Steps · 9', 'Evidence · 4']);
  await expect(rp.locator('section[data-sec="models"], section[data-sec="vss"]')).toHaveCount(0);
  await expect(rp.locator('.rp-sent .cite')).toHaveText('ceiling_04 · 4.6s');

  const weaved = await open(page, { ...FULL, weave_url: 'https://wandb.ai/x/weave/calls/1' });
  await expect(weaved.locator('a.rp-weave')).toHaveText('Open in Weave ↗');
  await expect(weaved.locator('a.rp-weave')).toHaveAttribute('href', 'https://wandb.ai/x/weave/calls/1');
});

test('an answer sentence without cites is muted and says Not supported', async ({ page }) => {
  const rp = await open(page, { ...RUN, steps: answered([]) });
  const s = rp.locator('.rp-sent');
  await expect(s).toHaveClass(/uncited/);
  await expect(s).toContainText(TEXT);
  await expect(s.locator('.rp-ns')).toHaveText('Not supported');
  await expect(s.locator('.cite')).toHaveCount(0);
});

test('#/runs lists the runs in the page column with verdict, question and meta', async ({ page }) => {
  await page.goto('#/test');
  await page.evaluate(() => (location.hash = '#/runs'));
  const items = page.locator('#runpage .rp-list li');
  await expect(items).toHaveCount(1);
  await expect(items.locator('.verdict')).toHaveText('Supported');
  await expect(items.locator('.rp-lq')).toHaveText(RUN.q);
  await expect(items.locator('.rp-lmeta')).toContainText('r_fixturewh0001 · 2026-10-09 00:00 · 13 steps · 95 ms');
  expect((await page.locator('#runpage .rp-col').boundingBox()).width).toBe(920);
  await items.locator('a').click();
  await expect(page).toHaveURL(/#\/run\/r_fixturewh0001$/);
  await expect(page.locator('#runpage .rp-nav')).toBeVisible();
});
