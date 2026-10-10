// Kernel and shell (card A2). Most tests run on #/test, which loads kernel/_testpage.js and records page calls in window.__pageCalls.
const { test, expect } = require('@playwright/test');
const { ASKS, mockApi, mockAsk, mockAskHeld } = require('./mockapi');

const LIVE = process.env.SVA_E2E_LIVE === '1';
const HEALTH = { ok: true, mode: 'dev', libs_ready: true, index_rows: { clips: 6, events: 3 }, assets_ok: true };

// Reads a kernel singleton through the same module URL the shell imported, so it is the same instance.
const kernel = (page, mod, expr) =>
  page.evaluate(async ([m, e]) => {
    const k = await import(new URL(`kernel/${m}.js`, document.baseURI).href);
    return new Function('k', `return (${e})`)(k);
  }, [mod, expr]);
const width = async (page, sel) => (await page.locator(sel).boundingBox()).width;
const view = (page) => page.locator('body');

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

const frames = (id) => ASKS.find((a) => a.ask_id === id).events;
// Texts the fixture streams give, not the shell.
const ANSWER = frames('wh-main').find((f) => f.event === 'answer').data.text;
const REFUSE_STEP = frames('wh-refuse').filter((f) => f.event === 'step').at(-1).data.summary;
// Replays hand-built SSE frames on #/test.
async function replayFrames(page, list) {
  const body = list.map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');
  await page.route(/\/api\/agent\/ask$/, (r) => r.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body }));
  await page.goto('#/test');
  await page.locator('.preset').first().click();
}

test('default state: top-bar Ask with its source tag, presets in the 420 px panel, timeline, no video, no page scroll', async ({ page }) => {
  await page.goto('#/test');
  await expect(view(page)).toHaveAttribute('data-view', 'default');
  await expect(page.locator('#ask-input')).toBeVisible();
  await expect(page.locator('#source-tag')).toHaveText('test');
  await expect(page.locator('#panel')).toContainText('Ask about this scene');
  await expect(page.locator('.preset')).toHaveCount(3);
  await expect(page.locator('#timeline .tl-ruler')).toBeVisible();
  await expect(page.locator('#narration')).toBeHidden();
  expect(await page.locator('video').count()).toBe(0);
  expect(await width(page, '#panel')).toBe(420);
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight)).toBe(true);
});

test('⌘K: scenes and trips in one list with an icon per row, recent runs, and the data badge in its footer', async ({ page }) => {
  await page.goto('#/test');
  await page.locator('#cmdk-btn').click();
  const rows = page.locator('#cmdk li');
  await expect(rows.first()).toBeVisible();
  const labels = await rows.locator('.cmdk-label').allTextContents();
  expect(labels.some((l) => /^(Warehouse|Fleet) ·/.test(l))).toBe(false);
  expect(labels).toEqual(expect.arrayContaining(['All trips', 'Recent runs']));
  expect(await rows.evaluateAll((ls) => ls.every((l) => l.querySelector('svg.i')))).toBe(true);
  await expect(page.locator('#cmdk .cmdk-foot #data-badge')).toContainText('dev · 9 rows');
  await page.keyboard.press('Escape');
  await expect(page.locator('#cmdk')).toBeHidden();
  await page.locator('#source-tag').click();
  await expect(page.locator('#cmdk')).toBeVisible();
});

test('missing page module: stage says Page not loaded', async ({ page }) => {
  allowed = /404.*\/warehouse\/page\.js$/;
  await page.route(/\/warehouse\/page\.js$/, (r) => r.fulfill({ status: 404, body: '' }));
  await page.goto('#/warehouse/wh-sdg-fixture-01');
  await expect(page.locator('#source-tag')).toHaveText('wh-sdg-fixture-01');
  await expect(page.locator('#stage')).toContainText('Page not loaded:');
  await expect(page.locator('#stage .page-retry')).toBeVisible();
  await expect(page.locator('#timeline .lane-coverage')).toHaveCount(3);
  await expect(page.locator('#timeline .ev-mark')).toHaveCount(2);
});

test('dragging the timeline ruler seeks clock.t', async ({ page }) => {
  await page.goto('#/test');
  await expect.poll(() => kernel(page, 'clock', 'k.clock.range.t1')).toBe(10000);
  const box = await page.locator('#timeline .tl-ruler .tl-track').boundingBox();
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width * 0.1, y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.3, y, { steps: 4 });
  expect(Math.abs((await kernel(page, 'clock', 'k.clock.t')) - 3000)).toBeLessThan(150);
  await page.mouse.move(box.x + box.width * 0.5, y, { steps: 4 });
  await page.mouse.up();
  expect(Math.abs((await kernel(page, 'clock', 'k.clock.t')) - 5000)).toBeLessThan(150);
  await expect(page.locator('.tl-now')).toHaveText(/^00:0[45]\./);
});

test('selection.set broadcasts to the page', async ({ page }) => {
  await page.goto('#/test');
  await expect.poll(() => page.evaluate(() => window.__pageCalls?.[0]?.[0])).toBe('mount');
  await kernel(page, 'selection', "k.selection.set(['P1', 'F1'], { source: 'agent' })");
  expect(await page.evaluate(() => window.__pageCalls.filter((c) => c[0] === 'selection'))).toEqual([['selection', ['P1', 'F1'], 'agent']]);
});

test('wh-main replay: hit, pinned answer header, tabs, claim UNRESOLVED → SUPPORTED, clickable evidence row, Esc back to default', async ({ page }) => {
  await replay(page, 'wh-main');
  await expect(view(page)).toHaveAttribute('data-view', 'hit');
  await expect(page.locator('#moments .moment')).toHaveCount(1);
  await expect(page.locator('#claims .claim-head')).toHaveText('UNRESOLVED→SUPPORTED');
  await expect(page.locator('#claims .preds')).toContainText('P1–F1 < 2 m');
  await expect(page.locator('#claims .pv')).toHaveText('1.55 m ±0.12');
  const card = page.locator('#answer');
  await expect(card.locator('.verdict')).toHaveText('Supported');
  await expect(card.locator('.ans-meta')).toHaveText('6 steps · 0.1 s');
  await expect(card.locator('.ans-text')).toContainText(ANSWER);
  await expect(card.locator('.ans-src .src')).toHaveText(['Measured']);
  await expect(card.locator('a.ans-run')).toHaveAttribute('href', '#/run/r_fixturewh0001');
  await expect(page.locator('#tabs [data-tab]')).toHaveText(['Answer', 'Evidence 4', 'Steps 6', 'Videos']);
  await expect(page.locator('#keyframe figcaption')).toContainText('ceiling_00 · 00:04.6');
  await expect(page.locator('#keyframe .kf-play')).toHaveText('Play 00:02–00:07');
  await expect(page.locator('#ask-status')).toContainText('Done');
  const calls = await page.evaluate(() => window.__pageCalls.map((c) => c[0]));
  expect(calls).toEqual(expect.arrayContaining(['applyHits', 'applyModelResults', 'applyUI']));

  await page.locator('#tabs [data-tab="evidence"]').click();
  const row = page.locator('#tab-evidence .ev[data-eid="e_5d60fc7e6ed93290"]');
  await expect(page.locator('#tab-evidence .ev-head')).toHaveText(['MEASURED · 2', 'MEASURED · 1', 'MODEL · 1']);
  await expect(row.locator('.ev-label')).toHaveText('Distance P1 to F1');
  await expect(row.locator('.ev-val')).toHaveText('1.55 m ±0.12');
  await expect(row.locator('.ev-where')).toHaveText('ceiling_04 + eye_00 · 00:04.6');
  await row.click();
  await expect(row).toHaveClass(/flash/);
  await expect(row).toHaveClass(/\bsel\b/);
  expect(await kernel(page, 'clock', 'k.clock.t')).toBe(4600);

  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');
  await expect(view(page)).toHaveAttribute('data-view', 'default');
  await expect(page.locator('#moments .moment')).toHaveCount(0);
  await expect(page.locator('#answer')).toBeEmpty();
  await expect(page.locator('#tabs')).toBeHidden();
});

test('evidence rows fold their raw fields into an aligned key / value block', async ({ page }) => {
  await replay(page, 'wh-main');
  await page.locator('#tabs [data-tab="evidence"]').click();
  const row = page.locator('#tab-evidence .ev[data-eid="e_5d60fc7e6ed93290"]');
  const raw = row.locator('.ev-raw');
  await expect(raw).toBeHidden();
  await row.locator('.ev-raw-btn').click();
  await expect(raw).toBeVisible();
  await expect(raw).toHaveText(['eid     e_5d60fc7e6ed93290', 'method  ground_distance_tri', 'value   1.55', 'unit    m', 'sigma   0.12', 'cams    ceiling_04, eye_00'].join('\n'));
  await expect(raw.locator('.num')).toHaveText(['1.55', '0.12']);
  // opening raw is not a citation click
  expect(await kernel(page, 'state', 'k.state.stageMode')).toBe('explore');
  await expect(page.locator('#tab-evidence .ev-k-model .ev-label')).toHaveText('Caption and visual search rank');
});

test('Evidence tab: what the answer cites or a predicate uses first; the rest the run read folds into one row', async ({ page }) => {
  await replay(page, 'wh-main');
  await page.locator('#tabs [data-tab="evidence"]').click();
  const pane = page.locator('#tab-evidence');
  // cited: the dodge event and the distance (also its predicate's); examined only: the visibility cells and the search rank
  await expect(pane.locator('.ev-group:visible .ev-head')).toHaveText(['MEASURED · 2']);
  const more = pane.locator('.ev-more');
  await expect(more).toHaveText('Other evidence read · 2');
  const model = pane.locator('.ev[data-eid="e_a363d220af21b141"]');
  await expect(model).toBeHidden();
  await more.click();
  await expect(model).toBeVisible();
  await expect(pane.locator('.ev-group:visible .ev-head')).toHaveText(['MEASURED · 2', 'MEASURED · 1', 'MODEL · 1']);
});

test('a run that read evidence but ends without claims or cites lists all of it as read, not used; the timeline marks none', async ({ page }) => {
  test.skip(LIVE, 'needs a spliced mock stream');
  const ev = frames('wh-main').flatMap((f) => (f.event === 'tool_result' ? f.data.evidence : [])).find((e) => e.eid === 'e_5d60fc7e6ed93290');
  const span = { ...ev, eid: 'e_00000000000000aa', kind: 'map', method: 'gps_pass', value: { t0_s: 496.5, t1_s: 505.5 }, unit: null, sigma: null };
  await replayFrames(page, frames('wh-nomatch').map((f) => {
    if (f.event === 'tool_result') return { ...f, data: { ...f.data, evidence: [ev, span] } };
    if (f.event === 'status') return { ...f, data: { ...f.data, final: 'OUT_OF_RANGE' } };
    return f;
  }));
  await expect(view(page)).toHaveAttribute('data-view', 'abstained');
  await expect(page.locator('#tabs [data-tab="evidence"]')).toHaveText('Evidence 2');
  await page.locator('#tabs [data-tab="evidence"]').click();
  const pane = page.locator('#tab-evidence');
  await expect(pane.locator('.ev-read')).toHaveText('Read, not used in an answer');
  await expect(pane.locator('.ev-more')).toHaveCount(0);
  await expect(pane.locator('.ev-group:visible .ev-head')).toHaveText(['MEASURED · 1', 'MAP · 1']);
  await expect(pane.locator('.ev:visible')).toHaveCount(2);
  await expect(page.locator('#timeline .tl-evidence')).toBeHidden();
});

test('Steps tab: a row per step and tool call, the claim-changing step expanded, duration bar and reasoning link', async ({ page }) => {
  await replay(page, 'wh-main');
  await page.locator('#tabs [data-tab="steps"]').click();
  const steps = page.locator('#tab-steps');
  await expect(steps.locator('.st-step')).toHaveCount(6);
  await expect(steps.locator('.st-tool')).toHaveCount(3);
  await expect(steps.locator('.st-step').first()).toContainText(frames('wh-main').find((f) => f.event === 'step').data.summary);
  await expect(steps.locator('.st-tool .tool-name')).toHaveText(['tool.search', 'tool.visibility', 'tool.measure']);
  await expect(steps.locator('.st-step.changed')).toHaveCount(1);
  await expect(steps.locator('.st-step.changed .st-claims')).toHaveText('UNRESOLVED→SUPPORTED');
  await expect(steps.locator('.st-bar i')).toHaveCount(6);
  await expect(steps.locator('.st-bar i.on')).toHaveCount(1);
  await expect(steps.locator('a.st-link')).toHaveAttribute('href', '#/run/r_fixturewh0001');
});

test('answer card: a number taken from evidence is a button to that evidence', async ({ page }) => {
  await replay(page, 'wh-main');
  const num = page.locator('#answer .ans-num');
  // measure_refs of the fixture: value.t0_ms (a time, marked apart) and value (1.55)
  await expect(num).toHaveText(['1.55']);
  await expect(num).toHaveAttribute('data-eid', 'e_5d60fc7e6ed93290');
  await num.click();
  await expect(page.locator('#tab-evidence .ev[data-eid="e_5d60fc7e6ed93290"]')).toHaveClass(/flash/);
  expect(await kernel(page, 'clock', 'k.clock.t')).toBe(4600);
});

test('answer card: a time field in the text is a time (mono, grey underline, ink), not a measured number, and still opens its evidence', async ({ page }) => {
  await replay(page, 'wh-main');
  const t = page.locator('#answer .ans-time');
  await expect(t).toHaveText(['4.6 s']);
  await expect(t).toHaveAttribute('data-eid', 'e_4a18f3e5e7eeb929');
  const css = await t.evaluate((e) => { const s = getComputedStyle(e); return [s.fontFamily.includes('IBM Plex Mono'), s.fontWeight, s.borderBottom, s.color]; });
  expect(css).toEqual([true, '500', '1px solid rgb(74, 84, 98)', 'rgb(237, 240, 244)']);
  await t.hover();
  await expect(t).toHaveClass(/\bhover\b/);
  await t.click();
  expect(await kernel(page, 'state', '[k.state.stageMode, k.state.focusEid]')).toEqual(['evidence', 'e_4a18f3e5e7eeb929']);
  expect(await kernel(page, 'clock', 'k.clock.t')).toBe(4600);
});

test('answer card: a time field in seconds (value.t_s, as the live agent sends it) is a time too', async ({ page }) => {
  const EID = 'e_4a18f3e5e7eeb929';
  await replayFrames(page, frames('wh-main').map((f) => {
    if (f.event === 'tool_result') return { ...f, data: { ...f.data, evidence: f.data.evidence.map((e) => (e.eid === EID ? { ...e, value: { ...e.value, t_s: 4.6 } } : e)) } };
    if (f.event === 'answer') return { ...f, data: { ...f.data, measure_refs: f.data.measure_refs.map((r) => (r.eid === EID ? { ...r, field: 'value.t_s' } : r)) } };
    return f;
  }));
  await expect(page.locator('#answer .ans-time')).toHaveText(['4.6 s']);
});

test('a claim whose verdict did not change shows one badge, not X → X', async ({ page }) => {
  const list = frames('wh-main');
  const last = list.findLast((f) => f.event === 'claim');
  const k = list.indexOf(last);
  await replayFrames(page, [...list.slice(0, k + 1), { event: 'claim', data: { ...last.data, before: 'SUPPORTED', after: 'SUPPORTED' } }, ...list.slice(k + 1)]);
  await expect(page.locator('#ask-status')).toContainText('Done');
  const head = page.locator('#claims .claim-head');
  await expect(head).toHaveText('SUPPORTED');
  await expect(head.locator('.verdict')).toHaveCount(1);
  await expect(head.locator('.arrow')).toHaveCount(0);
  await page.locator('#tabs [data-tab="steps"]').click();
  await expect(page.locator('#tab-steps .st-claims')).toHaveText('UNRESOLVED→SUPPORTED');
});

test('a moment card without actors starts its meta line with the primary camera', async ({ page }) => {
  const noActors = (f) => (f.event === 'hits' ? { ...f, data: { ...f.data, moments: f.data.moments.map((m) => ({ ...m, actor_refs: [] })) } } : f);
  await replayFrames(page, frames('wh-main').map(noActors));
  await expect(page.locator('#moments .m-meta')).toHaveText('main cam ceiling_00 · 3 cams');
});

test('the answer header links to the reasoning page only after final; a new question reserves the answer slot', async ({ page }) => {
  await replay(page, 'wh-main');
  await expect(view(page)).toHaveAttribute('data-view', 'hit');
  const link = page.locator('#answer a.ans-run');
  await expect(link).toHaveAttribute('href', '#/run/r_fixturewh0001');
  await expect(link).toBeVisible();
  await expect(page.locator('#ask-form .ask-hint')).toBeVisible();
  expect(await page.evaluate(() => document.body.scrollWidth <= document.body.clientWidth)).toBe(true);
  await page.locator('#ask-input').fill('How far was it from the forklift?');
  await page.route(/\/api\/agent\/ask$/, () => {});
  await page.keyboard.press('Enter');
  await expect(page.locator('#ask-input')).toHaveValue('How far was it from the forklift?');
  await expect(page.locator('#answer .ans-slot')).toContainText('ANSWER');
  await expect(page.locator('#answer .skel')).toHaveCount(2);
  await expect(page.locator('#narration .nr-sum')).toHaveText('Sent, waiting for the agent…');
  await expect(page.locator('#tabs')).toBeHidden();
  await expect(link).toHaveCount(0);
});

test('while running: moments found and claims so far with what they still need', async ({ page }) => {
  test.skip(LIVE, 'needs a held mock stream');
  await mockAskHeld(page, 'wh-main', 'answer');
  await page.goto('#/test');
  await page.locator('.preset').first().click();
  await expect(page.locator('.p-moments h2')).toHaveText('MOMENTS FOUND · 1');
  await expect(page.locator('#claims .claim-card .verdict')).toHaveText('SUPPORTED');
  await expect(page.locator('#claims')).toContainText('CLAIMS SO FAR');
  await expect(page.locator('#claims .claim-needs')).toHaveCount(0);
  await expect(page.locator('#modelpanel')).toBeHidden();
  await page.evaluate(() => window.__releaseAsk());
  await expect(page.locator('#answer .verdict')).toHaveText('Supported');
  await expect(page.locator('#claims .claim-card')).toHaveCount(0);
  await expect(page.locator('.p-moments')).toBeHidden();
});

test('model line: situation and prediction with the measurement check by default, four rows when expanded', async ({ page }) => {
  await replay(page, 'wh-main');
  const mp = page.locator('#modelpanel');
  await expect(mp.locator('.mp-summary')).toContainText('Situation yielding');
  await expect(mp.locator('.mp-summary')).toContainText('predicts continue');
  await expect(mp.locator('.mp-summary')).toContainText('located 4.75–5.60 s');
  await expect(mp.locator('.mp-sub')).toHaveText('Checked by measure: yes · describes only, no numbers');
  await expect(mp.locator('.mp-row')).toHaveCount(0);
  await mp.locator('.mp-summary').click();
  await expect(mp.locator('.mp-row')).toHaveCount(4);
  await expect(mp.locator('.mp-row').first()).toContainText('Checked by measure: yes');
  await mp.locator('.mp-summary').click();
  await expect(mp.locator('.mp-row')).toHaveCount(0);
});

test('model line: a markup string in a C2 payload is shown as text, not run', async ({ page }) => {
  test.skip(LIVE, 'needs a hand-built stream');
  const probe = '<img src=x id=xss-probe onerror="window.__xss=1">';
  await replayFrames(page, frames('wh-main').map((f) => f.event !== 'model_result' ? f
    : { ...f, data: { ...f.data, results: f.data.results.map((r) => r.task === 'C2' ? { ...r, payload: { ...r.payload, horizon_s: probe } } : r) } }));
  const mp = page.locator('#modelpanel');
  await mp.locator('.mp-summary').click();
  await expect(mp.locator('.mp-row').nth(1)).toContainText(`within ${probe} s`);
  await expect(page.locator('#xss-probe')).toHaveCount(0);
  expect(await page.evaluate(() => window.__xss)).toBeUndefined();
});

test('wh-refuse replay: abstained; the answer card says why and what to ask instead, no moment card', async ({ page }) => {
  await mockAsk(page, 'wh-refuse');
  await page.goto('#/test');
  await page.locator('#ask-input').fill('Who is the person in eye_00?');
  await page.keyboard.press('Enter');
  await expect(view(page)).toHaveAttribute('data-view', 'abstained');
  const card = page.locator('#answer');
  await expect(card.locator('.verdict')).toHaveText('Out of scope');
  await expect(card.locator('.ans-text')).toContainText('identity, intent, blame');
  await expect(card.locator('.ans-text')).toContainText(REFUSE_STEP);
  await expect(card.locator('.ans-src')).toHaveCount(0);
  await expect(card.locator('.ans-instead')).toContainText('Ask what can be measured');
  const instead = card.locator('.ans-instead .preset');
  await expect(instead).toHaveText(['When did the worker start to dodge the forklift, and how far was it?', 'How close did the forklift come to a person?']);
  await expect(page.locator('#moments .moment')).toHaveCount(0);
  await expect(page.locator('#moments').locator('xpath=..')).toBeHidden();
  await expect(page.locator('#keyframe')).toBeHidden();
  await mockAsk(page, 'wh-main');
  await instead.first().click();
  await expect(page.locator('#ask-input')).toHaveValue('When did the worker start to dodge the forklift, and how far was it?');
  await expect(card.locator('.verdict')).toHaveText('Supported');
});

test('NO_MATCH with an answer frame: the card shows the plain verdict, the agent sentence with its numbers and cite', async ({ page }) => {
  test.skip(LIVE, 'needs a spliced mock stream');
  const ev = frames('wh-main').flatMap((f) => (f.event === 'tool_result' ? f.data.evidence : [])).find((e) => e.eid === 'e_5d60fc7e6ed93290');
  const span = { ...ev, eid: 'e_00000000000000aa', kind: 'map', method: 'gps_pass', value: { t0_s: 496.5, t1_s: 505.5 }, unit: null, sigma: null };
  const text = 'Checked 1 clip passing the ROM (set03·0010, 496.5–505.5 s); no pedestrian was seen crossing in front of the car.';
  await replayFrames(page, frames('wh-nomatch').flatMap((f) => {
    if (f.event === 'tool_result') return [{ ...f, data: { ...f.data, evidence: [ev, span] } }];
    if (f.event !== 'status') return [f];
    const refs = [{ eid: span.eid, field: 'value.t0_s' }, { eid: span.eid, field: 'value.t1_s' }];
    return [{ event: 'answer', data: { run_id: f.data.run_id, sentence_id: 'a1', text, claim_ids: [], cites: [ev.eid, span.eid], measure_refs: refs } }, f];
  }));
  await expect(view(page)).toHaveAttribute('data-view', 'abstained');
  const card = page.locator('#answer');
  await expect(card.locator('.verdict')).toHaveText('No match');
  await expect(card.locator('.ans-text')).toHaveText(text);
  await expect(card.locator('.ans-time')).toHaveText(['496.5', '505.5 s']);
  await expect(card.locator('.ans-src .src')).toHaveText(['Measured', 'Map']);
  await expect(page.locator('#tab-evidence .ev')).toHaveCount(2);
  await expect(page.locator('#tab-evidence .ev[data-eid="e_00000000000000aa"] .ev-label')).toHaveText('GPS pass near the place');
  await expect(card).not.toContainText('No matching clip was found');
  await expect(card.locator('.ans-instead')).toContainText('Widen');
});

test('a final answer without hits shows the full answer header in the same 420 px panel', async ({ page }) => {
  test.skip(LIVE, 'needs a spliced mock stream');
  const text = 'Someone is where ceiling_00 cannot see: worker P1 at 0–6 s (seen by ceiling_04, eye_00).';
  await replayFrames(page, frames('wh-main').filter((f) => f.event !== 'hits').map((f) => (f.event === 'answer' ? { ...f, data: { ...f.data, text } } : f)));
  await expect(page.locator('#answer .ans-text')).toHaveText(text);
  await expect(view(page)).toHaveAttribute('data-view', 'default');
  await expect(page.locator('#moments').locator('xpath=..')).toBeHidden();
  await expect(page.locator('#answer .ans-meta')).toHaveText(/^\d+ steps · 0\.1 s$/);
  await expect(page.locator('#ask-status')).toHaveText(/^Done · \d+ steps · 0\.1 s$/);
  expect(await width(page, '#panel')).toBe(420);
  expect(await width(page, '#stage')).toBe(1440 - 420);
});

test('a new Ask aborts the old stream; frames of another run_id are dropped', async ({ page }) => {
  test.skip(LIVE, 'needs a held mock request');
  const refuse = frames('wh-refuse');
  // wh-refuse with a stale wh-main `hits` frame spliced in after its first frame.
  const body = [refuse[0], frames('wh-main').find((f) => f.event === 'hits'), ...refuse.slice(1)]
    .map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');
  let held = false;
  await page.route(/\/api\/agent\/ask$/, (route) => {
    if (held) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body });
    held = true;
  });
  await page.goto('#/test');
  const aborted = page.waitForEvent('requestfailed', (r) => r.url().endsWith('/api/agent/ask'));
  await page.locator('.preset').first().click();
  await expect(view(page)).toHaveAttribute('data-view', 'searching');
  await page.locator('#ask-input').fill('Who is the person in eye_00?');
  await page.keyboard.press('Enter');
  await aborted;
  await expect(view(page)).toHaveAttribute('data-view', 'abstained');
  await expect(page.locator('#moments .moment')).toHaveCount(0);
  expect(await page.evaluate(() => window.__pageCalls.some((c) => c[0] === 'applyHits'))).toBe(false);
});

test('Ask sends the current selection in context; no selection, no key', async ({ page }) => {
  await mockAsk(page, 'wh-main');
  await page.goto('#/test');
  const sent = [];
  page.on('request', (r) => r.url().endsWith('/api/agent/ask') && sent.push(r.postDataJSON().context));
  await page.locator('#ask-input').fill('How far was it from the forklift?');
  await page.keyboard.press('Enter');
  await expect.poll(() => sent.length).toBe(1);
  await kernel(page, 'selection', "k.selection.set(['P1'], { source: 'user' })");
  await page.locator('#ask-input').fill('How far was it from the forklift?');
  await page.keyboard.press('Enter');
  await expect.poll(() => sent.length).toBe(2);
  expect(sent[0].selection).toBeUndefined();
  expect(sent[1].selection).toEqual(['P1']);
});

test('clock.toWall inverts clock.map for a fleet trip and is the identity in the scene domain', async ({ page }) => {
  await page.goto('#/test');
  const r = await kernel(page, 'clock', `(() => {
    const id = k.clock.toWall('T', 9000);
    k.clock.bind({ kind: 'fleet', replay: [{ trip_id: 'T', replay_start_ms: 1000, source_start_ms: 5000, rate: 2, coverage_intervals: [[0, 99999]] }] });
    const w = k.clock.toWall('T', 9000);
    const back = k.clock.map(w);
    k.clock.bind({ kind: 'scene' });
    return [id, w, back];
  })()`);
  expect(r).toEqual([9000, 3000, { trip_id: 'T', t_ms: 9000 }]);
});

test('the shell requests nothing outside its own host and its fonts load from /lib', async ({ page }) => {
  const external = [];
  page.on('request', (r) => {
    const u = new URL(r.url());
    if (/^https?:$/.test(u.protocol) && u.host !== new URL(page.url() || 'http://x').host && u.hostname !== '127.0.0.1') external.push(r.url());
  });
  await page.goto('#/test');
  await expect(page.locator('#presets .preset').first()).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  expect(await page.evaluate(() => ['400 13px Geist', '400 13px "IBM Plex Mono"'].map((f) => document.fonts.check(f) && [...document.fonts].some((x) => x.status === 'loaded' && f.includes(x.family.replace(/"/g, '')))))).toEqual([true, true]);
  expect(external).toEqual([]);
});
