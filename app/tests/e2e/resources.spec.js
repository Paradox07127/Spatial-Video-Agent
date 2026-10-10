const { test, expect } = require('@playwright/test');
const { mockApi } = require('./mockapi');
const MF = require('../../../fixtures/sva-out/wh-sdg-fixture-01/manifest.json');

test('replacing model geometry releases GPU buffers and closing the stage releases the remaining scene', async ({ page }) => {
  await mockApi(page);
  await page.goto('./');
  const counts = await page.evaluate(async (mf) => {
    const { createStage } = await import(new URL('warehouse/stage3d.js', document.baseURI));
    const host = document.createElement('div');
    host.style.cssText = 'position:absolute;width:640px;height:480px';
    const canvas = document.createElement('canvas'); host.append(canvas); document.body.append(host);
    const stage = createStage(canvas);
    stage.loadManifest({ ...mf, cams: [] });
    const frame = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const values = [];
    for (let i = 0; i < 8; i++) {
      stage.setModelResults([{ task: 'C2', record_id: 'same', actor_refs: [{ entity_id: 'P1' }], t_ms: i,
        payload: { ghost: [[0, 1, 1], [100, 2, 2]], actions: ['continue'], horizon_s: 1 } }]);
      await frame(); values.push(stage.probe().resources.geometries);
    }
    stage.dispose(); stage.dispose();
    const remaining = stage.probe().resources.geometries;
    host.remove();
    return { values, remaining };
  }, MF);
  expect(counts.values[0]).toBeGreaterThan(0);
  expect(new Set(counts.values).size).toBe(1);
  expect(counts.remaining).toBe(0);
});

test('resource cleanup deduplicates shared geometry and preserves the reusable picking material', async ({ page }) => {
  await mockApi(page); await page.goto('./');
  const counts = await page.evaluate(async () => {
    const THREE = await import('three');
    const { releaseTree } = await import(new URL('warehouse/dispose.js', document.baseURI));
    const group = new THREE.Group(), geometry = new THREE.BoxGeometry(), material = new THREE.MeshBasicMaterial(), keep = new THREE.MeshBasicMaterial();
    const result = { geometry: 0, material: 0, keep: 0 };
    geometry.addEventListener('dispose', () => result.geometry++);
    material.addEventListener('dispose', () => result.material++);
    keep.addEventListener('dispose', () => result.keep++);
    group.add(new THREE.Mesh(geometry, [material, material]), new THREE.Mesh(geometry, keep));
    releaseTree(group, new Set([keep]));
    keep.dispose();
    return { ...result, keptUntilOwnerDisposal: result.keep === 1, children: group.children.length };
  });
  expect(counts).toEqual({ geometry: 1, material: 1, keep: 1, keptUntilOwnerDisposal: true, children: 0 });
});
