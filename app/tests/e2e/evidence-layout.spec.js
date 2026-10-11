const { test, expect } = require('@playwright/test');
const { mockApi, mockAskFrames } = require('./mockapi');

test('long closest-approach values wrap beside readable labels without overlapping their source', async ({ page }, testInfo) => {
  await mockApi(page);
  const base = { run_id: 'r_evidence_layout' }, key = 'wh-sdg-fixture-01';
  const shared = { scene_key: key, cam_id: 'ceiling_00', t0_ms: 2800, t1_ms: 2801, observation_ids: [], analysis_rev: 'fx-0001', kind: 'measure', artifact_ref: null };
  const summary = { scene_key: 'wh-sdg-001e5345_run_1', a: 'P1', b: 'F1', anchor_a: 'forklift_center', anchor_b: 'person_foot', checked: 'valid sampled pairs' };
  await mockAskFrames(page, [
    { event: 'step', data: { ...base, step_id: 's0', i: 0, state: 'measure', elapsed_ms: 1, summary: 'Measure distance' } },
    { event: 'tool_call', data: { ...base, step_id: 's0', call_id: 'call0', name: 'closest_approach', args: { scene_key: key }, why: 'Scan tracks' } },
    { event: 'tool_result', data: { ...base, step_id: 's0', call_id: 'call0', ok: true, envelope: { input_range: { scene_key: key, t0_ms: 0, t1_ms: 10000 }, elapsed_ms: 1 }, evidence: [
      { ...shared, eid: 'e_distance', method: 'ground_distance_tri', value: 2.343, sigma: 0.09, unit: 'm' },
      { ...shared, eid: 'e_scan', method: 'closest_approach', value: summary, sigma: null, unit: null },
    ] } },
    { event: 'answer', data: { ...base, sentence_id: 'a0', text: 'The minimum distance was 2.343 m.', claim_ids: [], cites: ['e_distance', 'e_scan'], measure_refs: [{ eid: 'e_distance', field: 'value' }] } },
    { event: 'status', data: { ...base, final: 'SUPPORTED', stop_reason: 'enough_evidence' } },
    { event: 'done', data: base },
  ]);
  await page.goto('#/test');
  await page.locator('.preset').first().click();
  await expect(page.locator('body')).toHaveAttribute('data-run', 'done');
  await page.locator('#tabs [data-tab="evidence"]').click();
  const row = page.locator('.ev[data-eid="e_scan"]');
  await expect(row.locator('.ev-label')).toHaveText('Closest approach');
  await expect(row.locator('.ev-val')).toHaveText(Object.values(summary).join(' · '));
  await expect(page.locator('.ev[data-eid="e_distance"] .ev-val')).toHaveText('2.343 m ±0.09');
  for (const width of [1440, 486]) {
    await page.setViewportSize({ width, height: 900 });
    const layout = await row.evaluate((el) => {
      const box = (node) => { const { x, y, width, height, right, bottom } = node.getBoundingClientRect?.() ?? node; return { x, y, width, height, right, bottom }; };
      const label = el.querySelector('.ev-label'), value = el.querySelector('.ev-val'), source = el.querySelector('.ev-where');
      const range = document.createRange(); range.selectNodeContents(label);
      return { row: box(el), label: box(label), value: box(value), source: box(source), text: [...range.getClientRects()].map(box) };
    });
    expect(layout.label.width).toBeGreaterThanOrEqual(layout.row.width * 0.3);
    expect(layout.value.x - layout.label.right).toBeGreaterThanOrEqual(8);
    expect(layout.value.right).toBeLessThanOrEqual(layout.row.right - 8);
    expect(layout.source.y).toBeGreaterThanOrEqual(Math.max(layout.label.bottom, layout.value.bottom));
    expect(layout.text.every((r) => r.right <= layout.label.right + 1)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`evidence-layout-${width}.png`) });
  }
  await row.locator('.ev-raw-btn').click();
  await expect(row.locator('.ev-raw')).toContainText('forklift_center');
  await expect(row.locator('.ev-raw')).toContainText('valid sampled pairs');
});
