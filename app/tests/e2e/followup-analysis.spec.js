// A follow-up the agent cannot resolve: the unmet explanation, facts measured at the focused moment, an unverified
// model description and clickable follow-up suggestions.
const { test, expect } = require('@playwright/test');
const { mockApi } = require('./mockapi');

const SCENE = 'wh-sdg-fixture-01', SID = 's_followup', TID = 't_followup', JID = 'j_followup', RID = 'r_followup';
const wire = (frames) => frames.map((f, i) => `id: ${i + 1}\nevent: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');
const UNMET = 'What a person is doing is not measured; below are the measured distance, speed, camera views and recorded events at that moment. “That moment” refers to the time and objects selected on the timeline.';
const FACT = 'At 4.9 s, the worker P1 was 1.409 m (±0.17 m) from forklift F1 (worker ground point to forklift center).';
const EVENT = 'Recorded event: the worker started to dodge the forklift at 4.6 s.';
const MODEL = 'Model description (Cosmos, unverified): The worker steps back from the forklift.';
const QUESTIONS = ['How far was the worker from the forklift at 4.9 s?', 'Which cameras saw the worker at 4.9 s?'];
const claim = (operator) => ({
  claim_id: 'c1', text: 'What was the worker doing', aggregate: 'UNRESOLVED', before: null, after: 'UNRESOLVED', examined_eids: [], available_eids: [], explain: null,
  predicates: [{ predicate_id: 'p1', subject_refs: [{ entity_id: 'P1' }], operator, value: true, unit: null, status: 'unknown', eids: [] }],
});

const analysisFrames = () => {
  const base = { run_id: RID }, eid = 'e_dist', start = FACT.indexOf('1.409');
  return [
    { event: 'step', data: { ...base, step_id: 's0', i: 0, state: 'parse', elapsed_ms: 1, summary: 'Follow-up at the selected moment' } },
    { event: 'claim', data: { ...base, ...claim('unconsumed:doing') } },
    { event: 'step', data: { ...base, step_id: 's1', i: 1, state: 'measure', elapsed_ms: 2, summary: 'Measure P1 to F1' } },
    { event: 'tool_call', data: { ...base, step_id: 's1', call_id: 'call0', name: 'ground_distance', args: { scene_key: SCENE, a: { entity_id: 'P1' }, b: { entity_id: 'F1' } } } },
    { event: 'tool_result', data: { ...base, step_id: 's1', call_id: 'call0', ok: true, envelope: { input_range: { scene_key: SCENE, t0_ms: 4900, t1_ms: 4901 }, elapsed_ms: 1 },
      evidence: [{ eid, scene_key: SCENE, cam_id: null, t0_ms: 4900, t1_ms: 4901, observation_ids: [], kind: 'measure', method: 'ground_distance', value: 1.409, unit: 'm', sigma: 0.17, analysis_rev: 'test', artifact_ref: null }] } },
    { event: 'ui', data: { ...base, cmd: 'seek', args: { t_ms: 4900, scene_key: SCENE } } },
    { event: 'ui', data: { ...base, cmd: 'highlight', args: { ids: ['P1', 'F1'], mode: 'outline' } } },
    { event: 'answer', data: { ...base, sentence_id: 'a0', part: 'unmet', text: UNMET, claim_ids: ['c1'], cites: [], measure_refs: [], text_spans: [] } },
    { event: 'answer', data: { ...base, sentence_id: 'a1', part: 'fact', text: FACT, claim_ids: [], cites: [eid], measure_refs: [{ eid, field: 'value' }], text_spans: [{ start, end: start + 5, eid, field: 'value' }] } },
    { event: 'answer', data: { ...base, sentence_id: 'a2', part: 'fact', text: EVENT, claim_ids: [], cites: [], measure_refs: [], text_spans: [] } },
    { event: 'answer', data: { ...base, sentence_id: 'a3', part: 'model', text: MODEL, claim_ids: [], cites: [], measure_refs: [], text_spans: [] } },
    { event: 'suggest', data: { ...base, questions: QUESTIONS } },
    { event: 'status', data: { ...base, final: 'UNRESOLVED', stop_reason: 'enough_evidence' } },
    { event: 'done', data: base },
  ];
};

let errors;
test.beforeEach(({ page }) => {
  errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
});
test.afterEach(() => expect(errors).toEqual([]));

async function askHere(page, q) {
  await page.goto(`#/warehouse/${SCENE}`);
  await expect(page.locator('.wh')).toBeVisible();
  await page.locator('#ask-input').fill(q);
  await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
}

test('unresolved follow-up shows the unmet reason, measured facts, model description and suggestions that ask as follow-ups', async ({ page }) => {
  await mockApi(page);
  await page.route(/\/health$/, (r) => r.fulfill({ json: { ok: true, mode: 'dev', backend: 'duckdb', agent_protocols: [1, 2], index_rows: { scenes: 1 } } }));
  const sent = [];
  let turn = null;
  const session = () => ({ session_id: SID, revision: turn ? 1 : 0, title: 'Follow-up', created: '2026-10-10T00:00:00Z', updated: '2026-10-10T00:00:01Z', turns: turn ? [turn] : [] });
  await page.route(/\/api\/agent\/sessions(?:\/[^/?]+)?$/, (r) => {
    if (r.request().method() === 'POST') return r.fulfill({ json: session() });
    return r.fulfill({ json: new URL(r.request().url()).pathname.endsWith('/sessions') ? { sessions: [session()] } : session() });
  });
  await page.route(/\/api\/agent\/jobs\/j_followup$/, (r) => r.fulfill({ json: { job_id: JID, session_id: SID, turn_id: TID, run_id: RID, status: 'done', attempt: 1, last_seq: 14, current_attempt_start_seq: 1 } }));
  await page.route(/\/api\/agent\/jobs\/j_followup\/events\?/, (r) => r.fulfill({ contentType: 'text/event-stream', body: wire(analysisFrames()) }));
  await page.route(/\/api\/agent\/ask$/, (r) => {
    const body = r.request().postDataJSON();
    sent.push(body);
    turn = { turn_id: TID, parent_turn_id: body.parent_turn_id, q: body.q, context: body.context, job_id: JID, run_id: RID, status: 'done', created: '2026-10-10T00:00:00Z', observations: [], answers: [], final: 'UNRESOLVED' };
    return r.fulfill({ headers: { 'content-type': 'text/event-stream', 'X-SVA-Session': SID, 'X-SVA-Turn': TID, 'X-SVA-Job': JID }, body: wire(analysisFrames()) });
  });

  await askHere(page, 'What was the worker doing at that moment?');
  const answer = page.locator('#answer');
  await expect(answer.locator('.verdict')).toHaveText('Unresolved');
  await expect(answer.locator('.ans-unmet')).toHaveText(UNMET);
  const facts = answer.locator('.ans-facts li');
  await expect(facts).toHaveCount(2);
  expect(await answer.locator('.ans-unmet').evaluate((el) => !!(el.compareDocumentPosition(document.querySelector('#answer .ans-facts')) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
  await expect(answer.locator('.p-label', { hasText: 'MEASURED AT THAT MOMENT' })).toBeVisible();
  await expect(facts.first()).toHaveText(FACT);
  const num = facts.first().locator('button.ans-num');
  await expect(num).toHaveText('1.409');
  await expect(num).toHaveAttribute('data-eid', 'e_dist');
  await num.click();
  await expect(facts.nth(1)).toHaveText(EVENT);

  const text = await page.locator('body').innerText();
  expect(text).not.toContain('unconsumed');
  expect(text).not.toContain('Not measured:');
  await expect(answer.locator('.ans-not-measured')).toHaveCount(0);

  await expect(answer.locator('.ans-model')).toContainText(MODEL);
  await expect(answer.locator('.ans-model .src-model')).toBeVisible();

  const suggest = answer.locator('.ans-suggest .suggest');
  await expect(suggest).toHaveText(QUESTIONS);
  await suggest.first().click();
  await expect.poll(() => sent.length).toBe(2);
  expect(sent[1].q).toBe(QUESTIONS[0]);
  expect(sent[1].parent_turn_id).toBe(TID);
});

test('a plain unresolved run without parts or suggestions keeps the Not measured line and quotes an unconsumed word', async ({ page }) => {
  await mockApi(page);
  const base = { run_id: 'r_plain' };
  const frames = [
    { event: 'step', data: { ...base, step_id: 's0', i: 0, state: 'parse', elapsed_ms: 1, summary: 'No template matched' } },
    { event: 'claim', data: { ...base, ...claim('unconsumed:doing') } },
    { event: 'answer', data: { ...base, sentence_id: 'a0', text: 'The full question remains unresolved.', claim_ids: ['c1'], cites: [], measure_refs: [] } },
    { event: 'status', data: { ...base, final: 'UNRESOLVED', stop_reason: 'enough_evidence' } },
    { event: 'done', data: base },
  ];
  await page.route(/\/api\/agent\/ask$/, (r) => r.fulfill({ contentType: 'text/event-stream', body: wire(frames) }));

  await askHere(page, 'What was the worker doing?');
  const answer = page.locator('#answer');
  await expect(answer.locator('.ans-text')).toHaveText('The full question remains unresolved.');
  await expect(answer.locator('.ans-not-measured')).toContainText('Not measured: “doing” — not checked');
  await expect(answer).not.toContainText('unconsumed');
  await expect(answer.locator('.ans-unmet, .ans-facts, .ans-model, .ans-suggest, .suggest')).toHaveCount(0);
});
