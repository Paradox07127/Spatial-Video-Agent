// Shell layers: home page, Esc stepping back, result tabs, hover linking, narration and take-over (redesign v2).
const { test, expect } = require('@playwright/test');
const { mockApi, mockAsk, mockAskHeld } = require('./mockapi');

const LIVE = process.env.SVA_E2E_LIVE === '1';
const HEALTH = { ok: true, mode: 'dev', libs_ready: true, index_rows: { clips: 6, events: 3 }, assets_ok: true };
const DIST = 'e_5d60fc7e6ed93290';

const kernel = (page, mod, expr) =>
  page.evaluate(async ([m, e]) => {
    const k = await import(new URL(`kernel/${m}.js`, document.baseURI).href);
    return new Function('k', `return (${e})`)(k);
  }, [mod, expr]);
const body = (page) => page.locator('body');
const uiCmds = (page) => page.evaluate(() => window.__pageCalls.filter((c) => c[0] === 'applyUI').map((c) => c[1].cmd));

let errors;
test.beforeEach(async ({ page }) => {
  errors = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(`${m.text()} @ ${m.location().url}`));
  page.on('pageerror', (e) => errors.push(String(e)));
  await mockApi(page);
  if (!LIVE) await page.route(/\/health$/, (r) => r.fulfill({ json: HEALTH }));
});
test.afterEach(() => expect(errors).toEqual([]));

async function answered(page) {
  await mockAsk(page, 'wh-main');
  await page.goto('#/test');
  await page.locator('#presets .preset').first().click();
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
}

test('#/ and the bare URL show the home page without redirecting; ask, panel and timeline are hidden', async ({ page }) => {
  await page.goto('#/');
  await expect(page.locator('#home-input')).toBeVisible();
  await expect(page.locator('#home-input')).toHaveAttribute('placeholder', 'Ask about any moment in the footage');
  await expect(page.locator('.home-chips .preset')).toHaveCount(3);
  await expect(body(page)).toHaveAttribute('data-view', 'home');
  await expect(page.locator('#ask-form')).toBeHidden();
  await expect(page.locator('#panel')).toBeHidden();
  await expect(page.locator('#timeline')).toBeHidden();
  await expect(page).toHaveURL(/#\/$/);
  await page.goto('');
  await expect(page.locator('#home-input')).toBeVisible();
  await expect.poll(() => page.evaluate(() => location.hash)).toBe('');
});

test('Esc steps back one layer: evidence mode, then the Answer tab, then the run, then home', async ({ page }) => {
  await answered(page);
  await page.locator('#answer .ans-num').click();
  await expect(body(page)).toHaveAttribute('data-stage', 'evidence');
  expect(await kernel(page, 'state', '[k.state.stageMode, k.state.focusEid]')).toEqual(['evidence', DIST]);
  await page.keyboard.press('Escape');
  await expect(body(page)).toHaveAttribute('data-stage', 'explore');
  await page.locator('#tabs [data-tab="evidence"]').click();
  await expect(page.locator('#tab-evidence')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#tab-answer')).toBeVisible();
  await expect(page.locator('#tab-evidence')).toBeHidden();
  await page.keyboard.press('Escape');
  await expect(page.locator('#answer')).toBeEmpty();
  await expect(body(page)).toHaveAttribute('data-view', 'default');
  await expect(page.locator('#presets .preset').first()).toBeVisible();
  await expect(page).toHaveURL(/#\/test$/);
  await page.keyboard.press('Escape');
  await expect(page).toHaveURL(/#\/$/);
  await expect(page.locator('#home-input')).toBeVisible();
});

test('the panel tabs reset to Answer on a new run', async ({ page }) => {
  await answered(page);
  await page.locator('#tabs [data-tab="steps"]').click();
  await expect(page.locator('#tab-steps')).toBeVisible();
  await page.locator('#ask-input').fill('How far was it from the forklift?');
  await page.keyboard.press('Enter');
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  await expect(page.locator('#tabs [data-tab="answer"]')).toHaveClass(/\bon\b/);
  await expect(page.locator('#tab-answer')).toBeVisible();
  await expect(page.locator('#tab-steps')).toBeHidden();
  expect(await kernel(page, 'state', 'k.state.panelTab')).toBe('answer');
});

test('hovering an answer number marks its evidence row and broadcasts evidence:hover', async ({ page }) => {
  await answered(page);
  await page.evaluate(async () => {
    const { bus } = await import(new URL('kernel/bus.js', document.baseURI).href);
    window.__hover = [];
    bus.on('evidence:hover', (eid) => window.__hover.push(eid));
  });
  const row = page.locator(`#tab-evidence .ev[data-eid="${DIST}"]`);
  await expect(row).not.toHaveClass(/\bhover\b/);
  await page.locator('#answer .ans-num').hover();
  await expect(row).toHaveClass(/\bhover\b/);
  await page.locator('#answer .verdict').hover();
  await expect(row).not.toHaveClass(/\bhover\b/);
  expect(await page.evaluate(() => window.__hover)).toEqual([DIST, null]);
});

test('an UNSUPPORTED question asked from home is declined in place; the URL stays #/', async ({ page }) => {
  await mockAsk(page, 'wh-refuse');
  await page.goto('#/');
  await page.locator('#home-input').fill('Who is the person in eye_00?');
  await page.keyboard.press('Enter');
  const card = page.locator('.home-refusal');
  await expect(card).toBeVisible();
  await expect(card).toContainText('Not answerable from footage');
  await expect(card).toContainText('identity, intent, blame');
  await expect(card.locator('.preset')).toHaveCount(2);
  await expect(page).toHaveURL(/#\/$/);
  await expect(body(page)).toHaveAttribute('data-view', 'home');
});

test('an answerable question from home opens its source route on the first hits and keeps the run', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('sva.lastRoute', '#/test'));
  await mockAsk(page, 'wh-main');
  const sent = [];
  page.on('request', (r) => r.url().endsWith('/api/agent/ask') && sent.push(r.postDataJSON().context));
  await page.goto('#/');
  await page.locator('.home .preset').first().click();
  await expect(page).toHaveURL(/#\/test$/);
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  await expect(page.locator('#moments .moment')).toHaveCount(1);
  expect(sent).toEqual([{ workspace: 'warehouse', scene_key: null }]);
  const calls = await page.evaluate(() => window.__pageCalls.map((c) => c[0]));
  expect(calls).toEqual(expect.arrayContaining(['mount', 'applyHits', 'applyModelResults', 'applyUI']));
});

test('a fleet place chip on home runs against all trips; a typed question searches the corpus', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('sva.lastRoute', '#/fleet/fl-pie-fixture-01@0'));
  await page.route(/\/api\/agent\/ask$/, () => {});
  const sent = [];
  page.on('request', (r) => r.url().endsWith('/api/agent/ask') && sent.push(r.postDataJSON().context));
  await page.goto('#/');
  await page.locator('.home-chips .preset[data-ws="fleet"]').click();
  await expect.poll(() => sent.length).toBe(1);
  await page.locator('#home-input').fill('Near the ROM, did a pedestrian cross in front of the car?');
  await page.keyboard.press('Enter');
  await expect.poll(() => sent.length).toBe(2);
  expect(sent).toEqual([{ workspace: 'fleet', scene_key: null }, { scope: 'corpus' }]);
});

test('while running: the narration card names the step, Stop ends the run as stopped', async ({ page }) => {
  test.skip(LIVE, 'needs a held mock stream');
  await mockAskHeld(page, 'wh-main', 'ui');
  await page.goto('#/test');
  await page.locator('#presets .preset').first().click();
  const card = page.locator('#narration');
  await expect(card).toBeVisible();
  await expect(card.locator('.nr-step')).toHaveText('STEP 2 OF 6');
  await expect(card.locator('.nr-sum')).toContainText('Hard-filter preselection');
  await expect(card.locator('.nr-seg.on')).toHaveCount(1);
  await expect(page.locator('#answer .ans-slot')).toContainText('Appears here once the distance is measured.');
  await expect(page.locator('#moments .moment')).toHaveCount(1);
  await card.locator('.nr-stop').click();
  await expect(card).toBeHidden();
  await expect(page.locator('#answer .verdict')).toHaveText('Stopped');
});

test('after Take over, agent focus commands are dropped while highlight and seek still reach the page', async ({ page }) => {
  test.skip(LIVE, 'needs a held mock stream');
  await mockAskHeld(page, 'wh-steer', 'ui');
  await page.goto('#/test');
  await page.locator('#presets .preset').first().click();
  const pill = page.locator('#takeover');
  await expect(pill).toBeVisible();
  await expect(pill).toContainText('Agent is steering the view');
  await pill.locator('button').click();
  await expect(pill).toBeHidden();
  expect(await kernel(page, 'state', 'k.state.agentCamera')).toBe(false);
  await page.evaluate(() => window.__releaseAsk());
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  expect(await uiCmds(page)).toEqual(['seek', 'highlight']);
});

test('without Take over the agent camera commands reach the page', async ({ page }) => {
  test.skip(LIVE, 'needs the wh-steer replay');
  await mockAsk(page, 'wh-steer');
  await page.goto('#/test');
  await page.locator('#presets .preset').first().click();
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  expect(await uiCmds(page)).toEqual(['focus', 'seek', 'highlight']);
});

test('a follow-up carries the selection and the clock time as context', async ({ page }) => {
  await answered(page);
  const sent = [];
  page.on('request', (r) => r.url().endsWith('/api/agent/ask') && sent.push(r.postDataJSON()));
  await kernel(page, 'selection', "k.selection.set(['P1', 'F1'], { source: 'user' })");
  await kernel(page, 'clock', 'k.clock.seek(4600)');
  await expect(page.locator('#followup [data-context="time"]')).toHaveText('about 00:04.6 ×');
  await expect(page.locator('#followup [data-context="selection"]')).toHaveText('P1–F1 ×');
  await expect(page.locator('#followup .preset')).toHaveCount(2);
  await page.locator('#followup-input').fill('Could the driver see the worker?');
  await page.locator('#followup-input').press('Enter');
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0].q).toBe('Could the driver see the worker?');
  expect(sent[0].context).toEqual({ workspace: 'warehouse', scene_key: null, selection: ['P1', 'F1'], t_ms: 4600 });
});

test('street scenes, which have a page template now, are listed in ⌘K and the home @ list', async ({ page }) => {
  await page.route(/\/api\/workspaces$/, async (r) => {
    const ws = await (await r.fetch()).json();
    const street = { scene_id: 'st1', scene_key: 'st-sf-cam-4', title: 'SF street cam 4', t0_ms: 0, t1_ms: 60000, thumb: null, n_events: 0, evidence_level: 'L1', caps: {} };
    r.fulfill({ json: [...ws.filter((w) => w.id !== 'street'), { id: 'street', title: 'Street', scenes: [street] }] });
  });
  await page.goto('#/');
  await page.locator('#home-input').fill('Was anyone here @');
  await expect(page.locator('.home-at')).toBeVisible();
  await expect(page.locator('.home-at')).toContainText('st-sf-cam-4');
  await page.locator('#home-input').press('Escape');
  await page.locator('#cmdk-btn').click();
  await expect(page.locator('#cmdk li').first()).toBeVisible();
  await expect(page.locator('#cmdk')).toContainText('st-sf-cam-4');
});

test('measured values in the answer panel are rounded like the 3D label (4 significant digits, σ to 2)', async ({ page }) => {
  if (LIVE) return;
  const { sseBody } = require('./mockapi');
  const body = sseBody('wh-main').replaceAll('"value":1.55,"unit":"m","sigma":0.12', '"value":1.5467385040788246,"unit":"m","sigma":0.16999999999999998');
  await page.route(/\/api\/agent\/ask$/, (r) => r.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body }));
  await page.goto('#/test');
  await page.locator('#presets .preset').first().click();
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  await expect(page.locator('#tab-answer .pv').first()).toHaveText('1.547 m ±0.17');
  await page.locator('#tabs [data-tab="evidence"]').click();
  await expect(page.locator(`#tab-evidence .ev[data-eid="${DIST}"]`)).toContainText('1.547 m ±0.17');
});

test('changing workspace clears the selection, so the follow-up context does not carry ids from another scene', async ({ page }) => {
  await page.goto('#/test');
  await kernel(page, 'selection', "k.selection.set(['P1', 'F1'], { source: 'user' })");
  expect(await kernel(page, 'selection', 'k.selection.ids.size')).toBe(2);
  await page.goto('#/fleet');
  await expect(body(page)).toHaveAttribute('data-ws', 'fleet');
  await expect.poll(() => kernel(page, 'selection', 'k.selection.ids.size')).toBe(0);
});
