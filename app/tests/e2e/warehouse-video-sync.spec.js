const { test, expect } = require('@playwright/test');
const { mockApi } = require('./mockapi');

// Exercise browser-permitted ordering explicitly: a presented-frame callback may
// arrive during seeking, before seeked makes the paused video ready again.
async function controlledVideo(page) {
  await mockApi(page);
  await page.goto('#/');
  await page.evaluate(async () => {
    const { createVideos } = await import(new URL('warehouse/videos.js', document.baseURI).href);
    const callbacks = [];
    const request = (callback) => { callbacks.push(callback); return callbacks.length; };
    const original = HTMLVideoElement.prototype.requestVideoFrameCallback;
    HTMLVideoElement.prototype.requestVideoFrameCallback = request;
    const clock = { t: 5184, playing: false, rate: 1 };
    let videos;
    try {
      videos = createVideos({
        api: { asset: (p) => new URL(p, document.baseURI).href, get: async () => ({ rows: [] }) },
        clock, key: 'controlled', mf: { fps: 30, assets: { boxes_dir: 'boxes' } },
        cams: [{ id: 'camera', offset_ms: 0, video: 'controlled.mp4', w: 640, h: 360 }],
        lanes: { camera: [[0, 10000, 'video']] }, entities: { entities: [] },
      });
    } finally { HTMLVideoElement.prototype.requestVideoFrameCallback = original; }
    const video = videos.el.querySelector('video');
    // Native network/decode events are irrelevant to this deterministic ordering
    // test; the separate real-video dock and media-reliability specs cover them.
    for (const event of ['loadstart', 'seeking', 'seeked', 'loadedmetadata', 'loadeddata', 'canplay', 'playing', 'waiting', 'stalled', 'error']) {
      video.addEventListener(event, (e) => { if (e.isTrusted) e.stopImmediatePropagation(); }, true);
    }
    const state = { seeking: false, readyState: 2, currentTime: 5.184, paused: true, error: null };
    for (const name of Object.keys(state)) Object.defineProperty(video, name, {
      configurable: true, get: () => state[name], set: (value) => { state[name] = value; },
    });
    video.requestVideoFrameCallback = request;
    document.body.append(videos.el);
    const event = (name) => video.dispatchEvent(new Event(name));
    window.videoSync = {
      seek(ms) { clock.t = ms; state.currentTime = ms / 1000; state.seeking = true; state.readyState = 1; event('seeking'); },
      presented(seconds) { callbacks.shift()(performance.now(), { mediaTime: seconds }); },
      seeked() { state.seeking = false; state.readyState = 2; event('seeked'); },
      source() { video.src = new URL('replacement.mp4', document.baseURI).href; event('loadstart'); },
      snapshot() { return { sceneMs: video.dataset.sceneMs ?? null, currentTime: video.currentTime, state: video.parentElement.dataset.mediaState }; },
      dispose() { videos.dispose(); },
    };
  });
}

test('paused seek publishes its actual presented frame when rVFC precedes seeked', async ({ page }) => {
  await controlledVideo(page);
  await page.evaluate(() => { videoSync.seek(5184); videoSync.presented(5.166667); });
  expect(await page.evaluate(() => videoSync.snapshot())).toEqual({ sceneMs: null, currentTime: 5.184, state: 'loading' });
  await page.evaluate(() => videoSync.seeked());
  // The value is the callback's real frame time, not the requested currentTime.
  expect(await page.evaluate(() => videoSync.snapshot())).toEqual({ sceneMs: '5167', currentTime: 5.184, state: 'ready' });
});

test('a later seek or source replacement cancels pending presented-frame metadata', async ({ page }) => {
  await controlledVideo(page);
  // Even a new seek to the same timestamp must cancel the previous pending frame.
  await page.evaluate(() => { videoSync.seek(5184); videoSync.presented(5.166667); videoSync.seek(5184); videoSync.seeked(); });
  expect(await page.evaluate(() => videoSync.snapshot().sceneMs)).toBeNull();
  await page.evaluate(() => videoSync.presented(5.166667));
  expect(await page.evaluate(() => videoSync.snapshot().sceneMs)).toBe('5167');
  await page.evaluate(() => { videoSync.seek(8184); videoSync.presented(8.166667); videoSync.source(); videoSync.seeked(); });
  expect(await page.evaluate(() => videoSync.snapshot().sceneMs)).toBeNull();
  // A callback registered for the old source is also rejected after replacement.
  await page.evaluate(() => videoSync.presented(8.166667));
  expect(await page.evaluate(() => videoSync.snapshot().sceneMs)).toBeNull();
  await page.evaluate(() => videoSync.presented(8.166667));
  expect(await page.evaluate(() => videoSync.snapshot().sceneMs)).toBe('8167');
  await page.evaluate(() => { videoSync.seek(9184); videoSync.presented(9.166667); videoSync.dispose(); videoSync.seeked(); });
  await expect(page.locator('.wh-videos')).toHaveCount(0);
});
