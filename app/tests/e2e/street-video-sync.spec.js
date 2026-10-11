const { test, expect } = require('@playwright/test');
const { mockApi } = require('./mockapi');
const KEY = 'st-fixture_cam_1-clip01';

async function controlledStreet(page) {
  await mockApi(page);
  await page.addInitScript(() => {
    const callbacks = []; let serial = 0, video;
    const state = { seeking: false, readyState: 1, currentTime: 0, paused: true, error: null };
    HTMLVideoElement.prototype.requestVideoFrameCallback = function(callback) {
      video = this;
      for (const name of Object.keys(state)) if (!Object.hasOwn(this, name)) Object.defineProperty(this, name, {
        configurable: true, get: () => state[name], set: (value) => { state[name] = value; },
      });
      callbacks.push({ callback, id: ++serial, canceled: false }); return serial;
    };
    HTMLVideoElement.prototype.cancelVideoFrameCallback = (id) => {
      const item = callbacks.find((x) => x.id === id); if (item) item.canceled = true;
    };
    // Network/decode are covered by the original native-video specs. Here only
    // manually dispatched readiness events determine the callback ordering.
    for (const name of ['loadstart', 'seeking', 'seeked', 'loadedmetadata', 'loadeddata', 'canplay', 'playing', 'waiting', 'stalled', 'error']) {
      document.addEventListener(name, (e) => { if (e.target.tagName === 'VIDEO' && e.isTrusted) e.stopImmediatePropagation(); }, true);
    }
    const event = (name) => video.dispatchEvent(new Event(name));
    window.streetFrames = {
      seek(seconds) { state.currentTime = seconds; state.seeking = true; state.readyState = 1; event('seeking'); },
      presented(seconds) { const index = callbacks.findIndex((x) => !x.canceled); const [item] = callbacks.splice(index, 1); item.callback(performance.now(), { mediaTime: seconds }); },
      stale(seconds) { const index = callbacks.findLastIndex((x) => x.canceled); const [item] = callbacks.splice(index < 0 ? callbacks.length - 1 : index, 1); item.callback(performance.now(), { mediaTime: seconds }); },
      seeked() { state.seeking = false; event('seeked'); },
      ready() { state.readyState = 2; event('canplay'); },
      source() { video.src = new URL('replacement.mp4', document.baseURI).href; event('loadstart'); },
    };
  });
  await page.goto(`#/street/${KEY}`);
  await expect(page.locator('.st-root video')).toHaveAttribute('src', /chunk/);
}

test('Street keeps an early presented frame through seeked before canplay and retries detections', async ({ page }) => {
  let failed = true, requests = 0;
  await controlledStreet(page);
  await page.route(/\/api\/street\/.+\/detections\?/, (r) => {
    requests++;
    return failed ? r.fulfill({ status: 503, body: '' }) : r.fulfill({ json: { rows: [[1200, 'car', 10, 10, 100, 100, 0.9]], gaps: [] } });
  });
  await page.evaluate(() => { streetFrames.seek(0); streetFrames.presented(0); streetFrames.seeked(); streetFrames.ready(); });
  await expect(page.locator('.st-chip')).toContainText('Detections unavailable');
  expect(requests).toBeGreaterThan(0);
  failed = false;
  await page.evaluate(async () => {
    const { clock } = await import(new URL('kernel/clock.js', document.baseURI).href);
    clock.seek(1226); streetFrames.seek(1.226); streetFrames.presented(1.2); streetFrames.seeked(); streetFrames.ready();
  });
  await expect.poll(() => requests).toBeGreaterThan(1);
  // Actual frame 1.2s has a box; requested currentTime 1.226s would select the
  // empty 1.233s sample instead. A seek target cannot stand in for frame metadata.
  await expect.poll(() => page.evaluate(async () => (await import(new URL('street/page.js', document.baseURI).href)).probe().boxes)).toBe(1);
  await expect(page.locator('.st-chip')).toHaveText('L1 · 2D video');
});

test('Street cancels queued old seek/source frames and accepts the new source first frame once', async ({ page }) => {
  await controlledStreet(page);
  await page.route(/\/api\/street\/.+\/detections\?/, (r) => r.fulfill({ json: { rows: [[0, 'car', 10, 10, 100, 100, 0.9]], gaps: [] } }));
  await page.evaluate(() => { streetFrames.seek(0); streetFrames.presented(0); streetFrames.seek(0); streetFrames.stale(0); streetFrames.seeked(); streetFrames.ready(); });
  const boxes = () => page.evaluate(async () => (await import(new URL('street/page.js', document.baseURI).href)).probe().boxes);
  expect(await boxes()).toBe(0);
  await page.evaluate(() => streetFrames.presented(0));
  await expect.poll(boxes).toBe(1);
  await page.evaluate(() => { streetFrames.seek(0); streetFrames.presented(0); streetFrames.source(); streetFrames.stale(0); streetFrames.seeked(); streetFrames.ready(); });
  expect(await boxes()).toBe(0);
  await page.evaluate(() => streetFrames.presented(0));
  await expect.poll(boxes).toBe(1);
});
