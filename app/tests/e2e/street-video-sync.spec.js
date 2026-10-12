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
      // currentTime was set by the page; the seeking event is still queued.
      seekQuiet() { state.seeking = true; },
      seekingEvent() { event('seeking'); },
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

// After a citation click the paused video seeks 5 s -> 0 s and Chromium presents the 0 s frame before the seeking event; the
// boxes then never appeared in Answer mode.
test('Street keeps the frame a paused seek presents before its seeking event', async ({ page }) => {
  await controlledStreet(page);
  await page.route(/\/api\/street\/.+\/detections\?/, (r) => r.fulfill({ json: { rows: [[1200, 'car', 10, 10, 100, 100, 0.9]], gaps: [] } }));
  await page.evaluate(() => { streetFrames.seek(0); streetFrames.presented(0); streetFrames.seeked(); streetFrames.ready(); });
  await page.evaluate(async () => {
    const { clock } = await import(new URL('kernel/clock.js', document.baseURI).href);
    clock.seek(1200); streetFrames.seekQuiet(); streetFrames.presented(1.2); streetFrames.seekingEvent(); streetFrames.seeked(); streetFrames.ready();
  });
  await expect.poll(() => page.evaluate(async () => (await import(new URL('street/page.js', document.baseURI).href)).probe().boxes)).toBe(1);
});

const fs = require('fs'), path = require('path');
const MP4 = path.resolve(__dirname, '../../../fixtures/l1', KEY, 'chunks/fixture_cam-1/0000.mp4');
// The fixture video served as contiguous chunks; `ends` is each chunk's t1_ms.
async function chunked(page, ends) {
  await mockApi(page);
  await page.route(/\/api\/street\/[^/]+\/chunks\?/, (r) => r.fulfill({ json: ends.map((t1_ms, index) => ({ index, t0_ms: ends[index - 1] ?? 0, t1_ms, url: `/api/street/${KEY}/chunk/fixture_cam-1/${index}` })) }));
  await page.route(/\/api\/street\/[^/]+\/chunk\/[^/]+\/\d+$/, (route) => {
    const buf = fs.readFileSync(MP4), m = (route.request().headers().range ?? '').match(/^bytes=(\d*)-(\d*)$/);
    if (!m) return route.fulfill({ status: 200, contentType: 'video/mp4', headers: { 'accept-ranges': 'bytes' }, body: buf });
    const a = m[1] ? +m[1] : buf.length - +m[2], b = m[1] && m[2] ? Math.min(+m[2], buf.length - 1) : buf.length - 1;
    return route.fulfill({ status: 206, contentType: 'video/mp4', headers: { 'accept-ranges': 'bytes', 'content-range': `bytes ${a}-${b}/${buf.length}` }, body: buf.subarray(a, b + 1) });
  });
}
const seek = (page, t) => page.evaluate(async (t) => (await import(new URL('kernel/clock.js', document.baseURI).href)).clock.seek(t), t);
const boxes = (page) => page.evaluate(async () => (await import(new URL('street/page.js', document.baseURI).href)).probe().boxes);

// The black video after "When does a person cross the street?" on st-sf_streets_cam_1-sf1: its chunks are HEVC Main 10 with
// AAC audio. A browser without an HEVC decoder plays the audio track alone: readyState 4, no error, videoWidth 0, so the
// media status says ready and the box stays black. The init script gives chunk 1 that state.
test('Street shows the frame, not a silent black box, when the browser plays a chunk without its picture', async ({ page }) => {
  await chunked(page, [5000, 10000]);
  await page.addInitScript(() => {
    const width = Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'videoWidth');
    Object.defineProperty(HTMLVideoElement.prototype, 'videoWidth', { configurable: true, get() { return /\/chunk\/[^/]+\/1$/.test(this.currentSrc) ? 0 : width.get.call(this); } });
  });
  await page.goto(`#/street/${KEY}`);
  const host = page.locator('.st-vbox'), still = page.locator('.st-still');
  await expect(host).toHaveAttribute('data-media-state', 'ready');
  // Paused, across the chunk boundary.
  await seek(page, 6200);
  await expect(page.locator('#stage video')).toHaveAttribute('src', /chunk\/fixture_cam-1\/1$/);
  await expect(host).toHaveAttribute('data-media-state', 'ready');
  await expect(still).toBeVisible();
  await expect(still).toHaveAttribute('src', /\/thumb\?cam=fixture_cam-1&t=6000$/);
  await expect.poll(() => still.evaluate((i) => i.complete && i.naturalWidth)).toBeGreaterThan(0);
  await expect(page.locator('.st-vnote')).toHaveText("Still frame each second · this browser can't decode this video");
  await expect.poll(() => boxes(page)).toBe(2);
  await seek(page, 1000);
  await expect(still).toBeHidden();
  await expect(page.locator('.st-vnote')).toBeHidden();
  await expect(page.locator('#stage video')).toBeVisible();
});

test('Street says so when no chunk covers the clock', async ({ page }) => {
  await chunked(page, [4000, 8000]);
  await page.goto(`#/street/${KEY}`);
  await expect(page.locator('.st-vbox')).toHaveAttribute('data-media-state', 'ready');
  await seek(page, 9000);
  await expect(page.locator('.st-vnote')).toHaveText('No video at this time');
  await expect(page.locator('#stage video')).toBeHidden();
  expect(await boxes(page)).toBe(0);
  await seek(page, 7000);
  await expect(page.locator('.st-vnote')).toBeHidden();
  await expect(page.locator('#stage video')).toBeVisible();
});
