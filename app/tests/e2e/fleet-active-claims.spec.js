const { test, expect } = require('@playwright/test');
const { mockApi, mockAskFrames, mockSceneAlias, fixtureWorkspaces } = require('./mockapi');
const fs = require('fs'), path = require('path');
const FIRST = 'fl-pie-fixture-01', SECOND = 'fl-pie-fixture-03', GPS = 'fl-pie-fixture-02';

test('local verdict follows the active candidate and GPS-only does not inherit the primary verdict', async ({ page }) => {
  await mockApi(page);
  const alias = await mockSceneAlias(page, SECOND, FIRST), workspaces = fixtureWorkspaces();
  workspaces.find((w) => w.id === 'fleet').scenes.push(alias);
  await page.route(/\/api\/workspaces$/, (r) => r.fulfill({ json: workspaces }));
  const geo = JSON.parse(fs.readFileSync(path.resolve(__dirname, `../../../fixtures/sva-out/${FIRST}/trip.geojson`)));
  geo.features[0].properties.replay.trip_id = SECOND;
  await page.route(new RegExp(`/api/assets/${SECOND}/trip.geojson$`), (r) => r.fulfill({ json: geo }));
  await page.route(/\/api\/fleet\/trips$/, (r) => r.fulfill({ json: [FIRST, SECOND, GPS].map((trip_id) => ({ trip_id, title: trip_id, full: trip_id !== GPS, geojson_url: `api/assets/${trip_id}/trip.geojson` })) }));
  const base = { run_id: 'r_active_claim' };
  const evidence = (eid, key, t, speed) => ({ eid, scene_key: key, cam_id: key === GPS ? null : 'dashcam', t0_ms: t, t1_ms: t + 1, observation_ids: [], kind: 'measure', method: 'obd_speed', value: speed, unit: 'km/h', sigma: null, analysis_rev: 'fx-0001', artifact_ref: null });
  const items = [evidence('e_first', FIRST, 3000, 72.02), evidence('e_second', SECOND, 5000, 48.1), evidence('e_gps', GPS, 7000, 66.0052)];
  const moments = items.map((e, i) => ({ moment_id: `moment${i}`, scene_key: e.scene_key, t0_ms: e.t0_ms, t1_ms: e.t1_ms, cams: e.cam_id ? [e.cam_id] : [], primary_cam: e.cam_id ?? '', actor_refs: [], evidence_ids: [e.eid], why: ['OBD speed minus map speed limit'], score: 3 - i, thumb: null, evidence_level: 'L2' }));
  const claim = (id, eid) => ({ ...base, claim_id: id, text: 'Speed was over 40 km/h', predicates: [{ predicate_id: `p_${id}`, subject_refs: [], operator: 'gt', value: 40, unit: 'km/h', status: 'supported', eids: [eid] }], aggregate: 'SUPPORTED', before: 'SUPPORTED', after: 'SUPPORTED', examined_eids: [eid], available_eids: [], evidence_level: 'L2' });
  await mockAskFrames(page, [
    { event: 'step', data: { ...base, step_id: 's0', i: 0, state: 'measure', elapsed_ms: 1, summary: 'Inspect speed' } },
    { event: 'tool_call', data: { ...base, step_id: 's0', call_id: 'call0', name: 'obd_speed', args: {}, why: 'Compare speed' } },
    { event: 'tool_result', data: { ...base, step_id: 's0', call_id: 'call0', ok: true, envelope: { input_range: { scene_key: FIRST, t0_ms: 0, t1_ms: 10000 }, elapsed_ms: 1 }, evidence: items } },
    { event: 'hits', data: { ...base, query_id: 'q0', moments, auto_open: null } },
    { event: 'claim', data: claim('c_first', 'e_first') },
    { event: 'claim', data: claim('c_second', 'e_second') },
    { event: 'answer', data: { ...base, sentence_id: 'a0', text: 'The vehicle was speeding: OBD speed 72.02 km/h.', claim_ids: ['c_first'], cites: ['e_first'], measure_refs: [{ eid: 'e_first', field: 'value' }] } },
    { event: 'status', data: { ...base, final: 'SUPPORTED', stop_reason: 'enough_evidence' } },
    { event: 'done', data: base },
  ]);
  await page.goto('#/fleet');
  await expect(page.locator('.fl-car')).toHaveCount(3);
  await page.locator('#ask-input').fill('Was this car speeding?'); await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  await expect(page).toHaveURL(new RegExp(`${FIRST}@3000$`));
  await page.locator('.p-moments-head .p-more').click();
  await page.locator('.moment[data-i="2"]').click();
  await expect(page).toHaveURL(new RegExp(`${GPS}@7000$`));
  await expect(page.locator('#keyframe')).toContainText('No thumbnail');
  await expect(page.locator('#followup .fu-ctx')).toContainText('00:07.0');
  await expect(page.locator('.fl-ix')).toHaveCount(0);
  await expect(page.locator('#claims .verdict-card')).toHaveCount(0);
  await expect(page.locator('#claims')).toContainText('No claim was assessed for this moment.');
  expect(await page.evaluate(async () => (await import(document.querySelector('script[src*="app.js"]').src)).currentRun().claims.size)).toBe(2);
  await expect(page.locator('#answer')).toContainText('72.02');
  await page.locator('.moment[data-i="1"]').click();
  await expect(page.locator('#claims .verdict-card')).toHaveCount(1);
  await expect(page.locator('#claims .pv')).toHaveText('48.1 km/h');
  await expect(page.locator('#claims')).toContainText('dashcam · 00:05.0');
  await page.locator('#answer .ans-num[data-eid="e_first"]').click();
  await expect(page).toHaveURL(new RegExp(`${FIRST}@3000$`));
  await expect(page.locator('#claims .verdict-card')).toHaveCount(1);
  await expect(page.locator('#claims .pv')).toHaveText('72.02 km/h');
  await expect(page.locator('#claims')).toContainText('dashcam · 00:03.0');
  await expect(page.locator('#keyframe .kf-at')).toContainText('00:03.0');
});
