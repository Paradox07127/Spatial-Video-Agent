const { test, expect } = require('@playwright/test');
const { mockApi, mockAskFrames } = require('./mockapi');
const GPS = 'fl-pie-fixture-02';

test('GPS latitude and longitude keep six decimal precision and clickable signed or scientific references', async ({ page }) => {
  await mockApi(page);
  const base = { run_id: 'r_gps_refs' }, eid = 'e_gps_position';
  const evidence = { eid, scene_key: GPS, cam_id: null, t0_ms: 5000, t1_ms: 5001, observation_ids: [], kind: 'measure', method: 'gps_position', value: { lat: 43.656684, lon: -79.4070487, t_ms: 5000 }, unit: null, sigma: null, analysis_rev: 'fx-0001', artifact_ref: null };
  await mockAskFrames(page, [
    { event: 'step', data: { ...base, step_id: 's0', i: 0, state: 'measure', elapsed_ms: 1, summary: 'Locate car' } },
    { event: 'tool_call', data: { ...base, step_id: 's0', call_id: 'call0', name: 'gps_position', args: { scene_key: GPS }, why: 'GPS fix' } },
    { event: 'tool_result', data: { ...base, step_id: 's0', call_id: 'call0', ok: true, envelope: { input_range: { scene_key: GPS, t0_ms: 0, t1_ms: 10000 }, elapsed_ms: 1 }, evidence: [evidence] } },
    { event: 'hits', data: { ...base, query_id: 'q0', moments: [{ moment_id: 'm_gps', scene_key: GPS, t0_ms: 5000, t1_ms: 5001, cams: [], primary_cam: '', actor_refs: [], evidence_ids: [eid], why: ['GPS fix'], score: 1, thumb: null }] } },
    ...['The recording vehicle GPS position was latitude 43.656684°, longitude -79.407049° at GPS sample time 5 s (observed; uncertainty unavailable). Camera observations were not checked.', 'Equivalent coordinates: 4.3656684e+1°, -7.9407049e+1°.'].map((text, i) => ({ event: 'answer', data: { ...base, sentence_id: `a${i}`, text, claim_ids: [], cites: [eid], measure_refs: ['value.lat', 'value.lon', ...(i ? [] : ['value.t_ms'])].map((field) => ({ eid, field })) } })),
    { event: 'status', data: { ...base, final: 'SUPPORTED', stop_reason: 'enough_evidence' } },
    { event: 'done', data: base },
  ]);
  await page.goto(`#/fleet/${GPS}@0`);
  await page.locator('#ask-input').fill('Where was this car?'); await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  const latitude = page.locator('#answer .ans-num[data-ref-field="value.lat"]'), longitude = page.locator('#answer .ans-num[data-ref-field="value.lon"]');
  await expect(latitude).toHaveText(['43.656684', '4.3656684e+1']);
  await expect(longitude).toHaveText(['-79.407049', '-7.9407049e+1']);
  await expect(page.locator('#answer .ans-time[data-ref-field="value.t_ms"]')).toHaveText('5 s');
  for (const ref of [latitude.first(), longitude.first(), latitude.last(), longitude.last()]) {
    await ref.click();
    await expect(page.locator('#source-tag')).toContainText(GPS);
    await expect(page.locator('#followup [data-context="time"]')).toContainText('00:05.0');
    await expect(page.locator('.fl-ix')).toHaveCount(0);
  }
  const stored = await page.evaluate(async (eid) => (await import(document.querySelector('script[src*="app.js"]').src)).currentRun().evidence.get(eid).value, eid);
  expect(stored).toEqual(evidence.value);
});
