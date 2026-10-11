// Reasoning page (card U5): #/run/<run_id> shows one persisted run, #/runs the recent ones; hidden unless opened, Esc returns.
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { ASKS, mockApi, mockAsk, mockAskHeld } = require('./mockapi');

const LIVE = process.env.SVA_E2E_LIVE === '1';
const RUN = JSON.parse(fs.readFileSync(path.resolve(__dirname, '..', '..', '..', 'fixtures', 'runs', 'r_fixturewh0001.json'), 'utf8'));
const CACHED = { ...RUN, run_id: 'r_fixturewh0009', final: 'UNSUPPORTED', q: 'Who is the person in eye_00?', source_run_id: 'r_fixturewh0001', steps: [], claims: [], evidence: [] };

let errors, allowed;
test.beforeEach(async ({ page }) => {
  errors = [];
  allowed = /^$/;
  page.on('console', (m) => m.type() === 'error' && errors.push(`${m.text()} @ ${m.location().url}`));
  page.on('pageerror', (e) => errors.push(String(e)));
  await mockApi(page);
  if (!LIVE) await page.route(/\/api\/agent\/runs$/, (r) => r.fulfill({ json: [CACHED, RUN] }));
});
test.afterEach(() => expect(errors.filter((e) => !allowed.test(e))).toEqual([]));

async function answered(page) {
  await mockAsk(page, 'wh-main');
  await page.goto('#/test');
  await page.locator('.preset').first().click();
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
}

test('the run page is hidden until opened from the answer card; Esc returns to the answered workspace', async ({ page }) => {
  test.skip(LIVE, 'needs a persisted answer text');
  const text = 'The worker started to dodge at 4.6 s, 1.55 m (±0.12 m) from the forklift.';
  const steps = RUN.steps.map((s) => (s.name === 'answer' ? { ...s, output: { sentence_id: 'a1', text } } : s));
  await page.route(/\/api\/agent\/runs\/r_fixturewh0001$/, (r) => r.fulfill({ json: { ...RUN, steps } }));
  await answered(page);
  const rp = page.locator('#runpage');
  await expect(rp).toBeHidden();
  await page.locator('#answer a.ans-run').click();
  await expect(page).toHaveURL(/#\/run\/r_fixturewh0001$/);
  await expect(rp).toBeVisible();
  await expect(rp.locator('.rp-q')).toHaveText(RUN.q);
  await expect(rp.locator('.rp-meta .verdict')).toHaveText('Supported');
  await expect(rp.locator('.rp-answer')).toContainText('The worker started to dodge at 4.6 s');

  await page.keyboard.press('Escape');
  await expect(page).toHaveURL(/#\/test$/);
  await expect(rp).toBeHidden();
  await expect(page.locator('body')).toHaveAttribute('data-view', 'hit');
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  expect(await page.evaluate(() => window.__pageCalls.filter((c) => c[0] === 'mount').length)).toBe(1);
});

test('step timeline: name, summary, elapsed ms, exec state; a step expands to input / output JSON, its tool call and evidence', async ({ page }) => {
  await answered(page);
  await page.locator('#answer a.ans-run').click();
  const steps = page.locator('#runpage .trace-step');
  await expect(steps).toHaveCount(9);
  const parse = steps.nth(0);
  await expect(parse.locator('.ts-name')).toHaveText('parse');
  await expect(parse.locator('.ts-note')).toHaveText(ASKS.find((a) => a.ask_id === 'wh-main').events.find((f) => f.event === 'step').data.summary);
  await expect(parse.locator('.ts-ms')).toHaveText('5 ms');
  await expect(parse.locator('.exec')).toHaveText('precomputed');
  const tool = page.locator('#runpage .trace-step[data-i="4"]');
  await expect(tool).toHaveClass(/child/);
  await expect(tool.locator('.ts-name')).toHaveText('tool.visibility');
  await tool.locator('.trace-row').click();
  const detail = tool.locator('.trace-detail');
  await expect(detail).toContainText('floor_cell: {2}');
  await expect(detail.locator('summary[title="Tool envelope"]')).toHaveText('Response');
  await expect(detail).toContainText('"ceiling_00"');
  await expect(detail.locator('.cite')).toHaveText('e_1449f20a');
  await expect(detail).toContainText('Weave: local');
  await tool.locator('.trace-row').click();
  await expect(detail).toHaveCount(0);
});

test('claims before → after and the evidence rows with source, label, cams · time, value', async ({ page }) => {
  await answered(page);
  await page.locator('#answer a.ans-run').click();
  const rp = page.locator('#runpage');
  await expect(rp.locator('.rp-changed .verdict')).toHaveText(['UNRESOLVED', 'SUPPORTED']);
  await expect(rp.locator('.rp-changed')).toContainText(RUN.claims[0].text);
  const rows = rp.locator('.rp-ev .rp-evrow');
  await expect(rows).toHaveCount(4);
  const dist = rp.locator('.rp-evrow[data-eid="e_5d60fc7e6ed93290"] > :not(pre)');
  await expect(dist).toHaveText(['Measured', 'Distance P1 to F1', 'ceiling_04 + eye_00 · 00:04.6', '1.55 m ±0.12', 'raw']);
  // Ground-truth events keep their provenance label in the label column without a Measured badge.
  const gt = rp.locator('.rp-evrow[data-eid="e_4a18f3e5e7eeb929"]');
  await expect(gt.locator('.src-measure')).toHaveCount(0);
  await expect(gt.locator('.rp-evlabel')).toHaveText('Dodge event (ground truth)');
  const labelLeft = (row) => row.locator('.rp-evlabel').evaluate((e) => e.getBoundingClientRect().left);
  expect(await labelLeft(gt)).toBeCloseTo(await labelLeft(rp.locator('.rp-evrow[data-eid="e_5d60fc7e6ed93290"]')), 1);
  await expect(rp.locator('.rp-weave')).toHaveCount(0);
  // a cite in a step detail flashes its evidence row
  await rp.locator('.trace-step[data-i="7"] .cite').click();
  await expect(rp.locator('.rp-evrow[data-eid="e_5d60fc7e6ed93290"]')).toHaveClass(/flash/);
});

test('#/runs lists recent runs from GET /api/agent/runs; ⌘K Recent runs opens it and a row opens its run', async ({ page }) => {
  test.skip(LIVE, 'needs a mocked run list');
  await page.goto('#/test');
  await page.keyboard.press('Control+k');
  await page.locator('.cmdk-input').fill('Recent runs');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#\/runs$/);
  const items = page.locator('#runpage .rp-list li');
  await expect(items).toHaveCount(2);
  await expect(items.nth(0)).toContainText('Out of scope');
  await expect(items.nth(0)).toContainText(CACHED.q);
  await expect(items.nth(0)).toContainText('cached replay');
  await expect(items.nth(1)).toContainText('Supported');
  await items.nth(1).locator('a').click();
  await expect(page).toHaveURL(/#\/run\/r_fixturewh0001$/);
  await expect(page.locator('#runpage .trace-step')).toHaveCount(9);
  await page.keyboard.press('Escape');
  await expect(page).toHaveURL(/#\/test$/);
  await expect(page.locator('#runpage')).toBeHidden();
});

test('a cache replay shows the steps of the run it replayed', async ({ page }) => {
  test.skip(LIVE, 'needs a mocked cache replay');
  const replayed = { ...CACHED, steps: [{ ...RUN.steps[0], step_id: 'r_fixturewh0009.s0', name: 'cache_replay', exec_state: 'cached', note: null }] };
  await page.route(/\/api\/agent\/runs\/r_fixturewh0009$/, (r) => r.fulfill({ json: replayed }));
  await page.goto('#/test');
  await page.evaluate(() => (location.hash = '#/run/r_fixturewh0009'));
  const rp = page.locator('#runpage');
  await expect(rp.locator('.rp-meta')).toContainText('Cached replay; the steps below are from the original run r_fixturewh0001');
  await expect(rp.locator('.rp-meta')).toContainText('1 step');
  await expect(rp.locator('.trace-step')).toHaveCount(9);
  await expect(rp.locator('.rp-ev .rp-evrow')).toHaveCount(4);
});

test('the answer shows before VSS native search, whose collapsed block is added later at the end of the Steps tab with hits and SQL on demand', async ({ page }) => {
  test.skip(LIVE, 'needs a mocked site ask');
  await mockAskHeld(page, 'wh-vss', 'vss_native');
  await page.goto('#/test');
  await page.locator('.preset').first().click();
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  const text = await page.locator('#answer .ans-text').textContent();
  await expect(page.locator('#answer .ans-text')).toBeVisible();
  expect(text).toContain('The worker');
  const block = page.locator('#tab-steps details.ans-vss');
  await expect(block).toHaveCount(0);
  await page.locator('#tabs [data-tab="steps"]').click();
  await page.evaluate(() => window.__releaseAsk());
  await expect(block.locator('> summary')).toHaveText('VSS native search · done · 3.8 s');
  await expect(page.locator('#answer .ans-text')).toHaveText(text);
  await expect(block).not.toHaveAttribute('open');
  await expect(block.locator('li').first()).toBeHidden();
  await block.locator('> summary').click();
  await expect(block.locator('li')).toHaveCount(2);
  await expect(block.locator('li').first()).toContainText('A worker steps back as a forklift approaches.');
  await expect(block.locator('li').first()).toContainText('ceiling_04 · wh-sdg-fixture-01 · 4.0 s · similarity 0.412');
  await expect(block.locator('li').nth(1)).not.toContainText('wh-sdg');
  const sql = block.locator('details.ans-sql pre');
  await expect(sql).toBeHidden();
  await block.locator('details.ans-sql > summary').click();
  await expect(sql).toBeVisible();
  await expect(sql).toContainText('FROM "vss-collection"');
});

test('a run that cannot be read says so', async ({ page }) => {
  test.skip(LIVE, 'needs a missing run');
  allowed = /404.*\/agent\/runs\/r_missing000000$/;
  await page.goto('#/test');
  await page.evaluate(() => (location.hash = '#/run/r_missing000000'));
  await expect(page.locator('#runpage')).toContainText('Could not read run r_missing000000');
});

test('a run page opened first has the default workspace under it: Esc lands there, the next Esc goes home', async ({ page }) => {
  test.skip(LIVE, 'needs a mocked run list');
  await page.goto('#/runs');
  await expect(page.locator('#runpage .rp-list li')).toHaveCount(2);
  await expect(page.locator('#source-tag')).toHaveText('wh-sdg-fixture-01');
  await page.keyboard.press('Escape');
  await expect(page).toHaveURL(/#\/warehouse\/[a-z0-9_-]+$/);
  await expect(page.locator('#runpage')).toBeHidden();
  await page.keyboard.press('Escape');
  await expect(page).toHaveURL(/#\/$/);
  await expect(page.locator('#home-input')).toBeVisible();
});

test('the Runs button in the top bar opens the run list', async ({ page }) => {
  test.skip(LIVE, 'needs a mocked run list');
  await page.goto('#/test');
  await page.locator('#runs-btn').click();
  await expect(page).toHaveURL(/#\/runs$/);
  await expect(page.locator('#runpage .rp-list li')).toHaveCount(2);
});
