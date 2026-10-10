const { test, expect } = require('@playwright/test');
const { mockApi } = require('./mockapi');
const cases = [
  ['warehouse', '#/warehouse/wh-sdg-fixture-01', /\/api\/assets\/wh-sdg-fixture-01\/video\/ceiling_00\.mp4/, '.wh-stage-video .wh-vframe'],
  ['fleet', '#/fleet/fl-pie-fixture-01@3000', /\/api\/assets\/fl-pie-fixture-01\/video\/dashcam\.mp4/, '.fl-vbox'],
  ['street', '#/street/st-fixture_cam_1-clip01', /\/api\/street\/st-fixture_cam_1-clip01\/chunk\//, '.st-vbox'],
];
for (const [name, hash, mediaUrl, selector] of cases) {
  test(`${name}: a real media 404 is visible, clears annotations, and Retry recovers without replacing the investigation`, async ({ page }) => {
    const errors = []; page.on('pageerror', e => errors.push(String(e)));
    await mockApi(page);
    let broken = true;
    await page.route(mediaUrl, r => broken ? r.fulfill({ status: 404, body: '' }) : r.fallback());
    await page.goto(hash);
    const host = page.locator(selector), video = host.locator('video');
    await expect(host).toHaveAttribute('data-media-state', 'error');
    await expect(host.locator('.media-status')).toContainText('Video unavailable');
    await expect(video).toBeHidden();
    const inked = () => host.locator('canvas').first().evaluate(c => c.getContext('2d').getImageData(0,0,c.width,c.height).data.some((x,i) => i%4 === 3 && x > 0));
    // Fleet's first canvas is an optional remap surface; all annotation surfaces are hidden on failure.
    await expect(host.locator('canvas').last()).toBeHidden();
    expect(await inked()).toBe(false);
    broken = false;
    await host.locator('.media-retry').click();
    await expect(host).toHaveAttribute('data-media-state', 'ready');
    await expect(host.locator('.media-status')).toBeHidden();
    await expect(video).toBeVisible();
    await expect.poll(() => video.evaluate(v => v.readyState >= 2 && !v.error)).toBe(true);
    await expect(page).toHaveURL(new RegExp(hash.replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + '$'));
    expect(errors).toEqual([]);
  });
  test(`${name}: buffering is explicit and hides stale pixels until a playable frame returns`, async ({ page }) => {
    await mockApi(page); await page.goto(hash);
    const host = page.locator(selector), video = host.locator('video');
    await expect(host).toHaveAttribute('data-media-state', 'ready');
    await video.dispatchEvent('waiting');
    await expect(host).toHaveAttribute('data-media-state', 'waiting');
    await expect(host.locator('.media-status')).toHaveText('Waiting for video…');
    await expect(video).toBeHidden();
    await expect(host.locator('canvas').last()).toBeHidden();
    await video.dispatchEvent('canplay');
    await expect(host).toHaveAttribute('data-media-state', 'ready');
    await expect(host.locator('.media-status')).toBeHidden();
    await expect(video).toBeVisible();
  });
}
