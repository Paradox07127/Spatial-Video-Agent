const { test, expect } = require('@playwright/test');
const { mockApi } = require('./mockapi');

// Exercise browser-permitted ordering explicitly: a presented-frame callback may
// arrive during seeking, before seeked makes the paused video ready again.
async function controlledFleetVideo(page) {
  await mockApi(page);
  await page.goto('#/');
  await page.evaluate(async () => {
    const { createVideo } = await import(new URL('fleet/videooverlay.js', document.baseURI).href);
    const callbacks = []; let serial = 0;
    const request = (callback) => { callbacks.push({ callback, id: ++serial, canceled: false }); return serial; };
    const cancel = (id) => { const item = callbacks.find((x) => x.id === id); if (item) item.canceled = true; };
    const original = HTMLVideoElement.prototype.requestVideoFrameCallback;
    HTMLVideoElement.prototype.requestVideoFrameCallback = request;
    const clock = { t: 5184, playing: false, rate: 1 };
    let videos;
    try {
      const host = document.createElement('div'); document.body.append(host);
      const times = [];
      const player = createVideo(host, {
        cam: { id: 'camera', offset_ms: 0, video: 'controlled.mp4', w: 640, h: 360, model: 'pinhole', K: [100,100,50,50], dist: [] },
        src: new URL('controlled.mp4', document.baseURI).href, fps: 30, names: new Map(), bname: x=>x,
        onPresented: (t, epoch) => times.push({t, epoch}),
      });
      videos = { el: host, dispose: () => { player.dispose(); host.remove(); }, player, times };
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
    video.cancelVideoFrameCallback = cancel;
    document.body.append(videos.el);
    videos.player.sync(5184, false);
    const event = (name) => video.dispatchEvent(new Event(name));
    window.videoSync = {
      seek(ms) { clock.t = ms; videos.player.sync(ms, false); state.currentTime = ms / 1000; state.seeking = true; state.readyState = 1; event('seeking'); },
      delayedSeeking() { event('seeking'); },
      sync(ms, seeking=true) { videos.player.sync(ms, false); state.seeking = seeking; state.readyState = seeking ? 1 : 2; },
      presented(seconds) { const index = callbacks.findIndex((x) => !x.canceled); const [item] = callbacks.splice(index, 1); item.callback(performance.now(), { mediaTime: seconds }); },
      stale(seconds) { const index = callbacks.findLastIndex((x) => x.canceled); const [item] = callbacks.splice(index, 1); item.callback(performance.now(), { mediaTime: seconds }); },
      seeked() { state.seeking = false; state.readyState = 2; event('seeked'); },
      initialLoad() { event('loadstart'); },
      source() { video.src = new URL('replacement.mp4', document.baseURI).href; event('loadstart'); },
      presentations() { return videos.times; },
      snapshot() { return { sceneMs: video.dataset.sceneMs ?? null, currentTime: video.currentTime, state: video.parentElement.dataset.mediaState }; },
      dispose() { videos.dispose(); },
    };
  });
}

test('Fleet paused seek publishes its actual presented frame when rVFC precedes seeked', async ({ page }) => {
  await controlledFleetVideo(page);
  await page.evaluate(() => { videoSync.seek(5184); videoSync.presented(5.166667); });
  expect(await page.evaluate(() => videoSync.snapshot())).toEqual({ sceneMs: null, currentTime: 5.184, state: 'loading' });
  await page.evaluate(() => videoSync.seeked());
  // The value is the callback's real frame time, not the requested currentTime.
  expect(await page.evaluate(() => videoSync.snapshot())).toEqual({ sceneMs: '5167', currentTime: 5.184, state: 'ready' });
});

test('Fleet explicit seek keeps its presented frame when native seeking arrives after rVFC', async ({page})=>{
  await controlledFleetVideo(page);
  await page.evaluate(()=>{videoSync.sync(8184);videoSync.presented(8.166667);videoSync.delayedSeeking();videoSync.seeked();});
  expect(await page.evaluate(()=>videoSync.snapshot().sceneMs)).toBe('8167');
});

test('Fleet retains its actual receipt when the ready callback precedes native seeking', async ({page})=>{
  await controlledFleetVideo(page);
  await page.evaluate(()=>{videoSync.sync(8184,false);videoSync.presented(8.166667);videoSync.delayedSeeking();videoSync.seeked();});
  expect(await page.evaluate(()=>videoSync.snapshot().sceneMs)).toBe('8167');
  expect(await page.evaluate(()=>videoSync.presentations().map(x=>x.t))).toEqual([8167,8167]);
});

test('Fleet a later seek or source replacement cancels pending presented-frame metadata', async ({ page }) => {
  await controlledFleetVideo(page);
  // Even a new seek to the same timestamp must cancel the previous pending frame.
  await page.evaluate(() => { videoSync.seek(5184); videoSync.presented(5.166667); videoSync.seek(5184); videoSync.stale(5.166667); videoSync.seeked(); });
  expect(await page.evaluate(() => videoSync.snapshot().sceneMs)).toBeNull();
  await page.evaluate(() => videoSync.presented(5.166667));
  expect(await page.evaluate(() => videoSync.snapshot().sceneMs)).toBe('5167');
  await page.evaluate(() => { videoSync.seek(8184); videoSync.presented(8.166667); videoSync.source(); videoSync.seeked(); });
  expect(await page.evaluate(() => videoSync.snapshot().sceneMs)).toBeNull();
  // An already queued callback for the old source must not replace the new subscription.
  await page.evaluate(() => videoSync.stale(8.166667));
  expect(await page.evaluate(() => videoSync.snapshot().sceneMs)).toBeNull();
  await page.evaluate(() => videoSync.presented(8.166667));
  expect(await page.evaluate(() => videoSync.snapshot().sceneMs)).toBe('8167');
  await page.evaluate(() => { videoSync.seek(9184); videoSync.presented(9.166667); videoSync.dispose(); videoSync.seeked(); });
  expect(await page.evaluate(() => videoSync.snapshot().sceneMs)).toBeNull();
});

test('Fleet source replacement subscribes immediately and publishes its only initial frame', async ({ page }) => {
  await controlledFleetVideo(page);
  await page.evaluate(() => { videoSync.source(); videoSync.presented(5.166667); });
  expect(await page.evaluate(() => videoSync.snapshot())).toEqual({ sceneMs: '5167', currentTime: 5.184, state: 'ready' });
});


test('Fleet initial load keeps its queued actual frame while a later same-source reload invalidates it',async({page})=>{
  await controlledFleetVideo(page);
  await page.evaluate(()=>{videoSync.presented(5.166667);videoSync.initialLoad();videoSync.seeked();});
  expect(await page.evaluate(()=>videoSync.snapshot().sceneMs)).toBe('5167');
  expect(await page.evaluate(()=>videoSync.presentations().map(x=>x.t))).toEqual([5167,5167]);
  await page.evaluate(()=>{videoSync.initialLoad();videoSync.seeked();});
  expect(await page.evaluate(()=>videoSync.snapshot().sceneMs)).toBeNull();
  await page.evaluate(()=>videoSync.presented(5.166667));
  expect(await page.evaluate(()=>videoSync.snapshot().sceneMs)).toBe('5167');
});

test('camera owner is resolved at detection time and conflicting associations stay ambiguous', async ({ page }) => {
  await mockApi(page); await page.goto('#/');
  const result = await page.evaluate(async () => {
    const { owners } = await import(new URL('warehouse/videos.js', document.baseURI).href);
    const make = (id,a,b) => ({entity_id:id, observations:[{cam_id:'cam',local_track_id:7,t0_ms:a,t1_ms:b}],association:{valid:[a,b]}});
    const owner = owners({entities:[make('P1',0,1000),make('P2',2000,3000),make('P3',2500,2800)]});
    return [500,1500,2100,2600,3000].map(t=>owner.get('cam:7',t) ?? null);
  });
  expect(result).toEqual(['P1',null,'P2',null,null]);
});

test('pinhole projection stays finite and remap is identity while fisheye keeps distortion', async ({ page }) => {
  await mockApi(page); await page.goto('#/');
  const result = await page.evaluate(async () => {
    const { fisheye, undistortPx } = await import(new URL('fleet/videooverlay.js', document.baseURI).href);
    const cam={model:'pinhole',K:[100,100,50,50],dist:[]};
    return { projected:fisheye(cam,[1,0,2]), remap:undistortPx(cam,[75,50]), behind:fisheye(cam,[0,0,-1]), curved:fisheye({...cam,model:'fisheye',dist:[0,0,0,0]},[1,0,2]) };
  });
  expect(result.projected).toEqual([100,50]); expect(result.remap).toEqual([75,50]); expect(result.behind).toBeNull();
  expect(result.curved[0]).toBeCloseTo(96.3647609);
});

// Run the actual async renderer with controlled sidecar completion: browser timing
// varies, but an old read must never regain ownership after a seek epoch changes.
test('Fleet sidecar completion from an old seek epoch cannot redraw the new frame', async () => {
  const fs = require('fs'), path = require('path'), vm = require('vm');
  const src = fs.readFileSync(path.resolve(__dirname,'../../static/fleet/page.js'),'utf8');
  const code = src.slice(src.indexOf('async function presented('),src.indexOf('function tick(t)'));
  let release; const deferred = new Promise(r=>release=r), draws=[];
  const cur={trip:{},video:{epoch:3,draw:(f)=>draws.push(f.t_ms),clear:()=>draws.push('clear')}};
  const c={ix:cur,chunk:async(_,kind)=>{await deferred;return kind==='ego'?[{t_ms:1000,peds:[],vehs:[],buildings:[]}]:[];}};
  vm.runInNewContext(code+'globalThis.presented=presented;',c);
  const pending=c.presented(cur,1000,3);cur.video.epoch=4;release();await pending;
  expect(draws).toEqual([]);expect(cur.presented).toBeUndefined();
});

test('GPS quality intersects state spans, normalizes point moments, and never treats missing samples as live', async () => {
  const fs = require('fs'), path = require('path'), vm = require('vm');
  const src = fs.readFileSync(path.resolve(__dirname,'../../static/fleet/page.js'),'utf8');
  const code = src.slice(src.indexOf('async function renderTrip()'),src.indexOf('async function enterIx('));
  async function quality(pts,moment){
    const el={innerHTML:'',replaceChildren(){}},trip={id:'test',title:'test',pts,mf:{t0_ms:0,t1_ms:3000},events:[],line:[]};
    const c={document:{getElementById:()=>el},ix:{trip,moment},chunk:async()=>[],places:new Map(),trips:[trip],posAt:()=>null,esc:x=>x,sec:x=>x,tc:x=>x,icon:()=>'',en:()=>[0,0]};
    vm.runInNewContext(code+'globalThis.renderTrip=renderTrip;',c);await c.renderTrip();return el.innerHTML;
  }
  const pts=[{t_ms:1000,state:'last_known'},{t_ms:2000,state:'observed'}];
  expect(await quality(pts,{t0_ms:1500})).toContain('last-known fix');
  expect(await quality(pts,{t0_ms:1500,t1_ms:1501})).toContain('last-known fix');
  expect(await quality([],{t0_ms:1500})).toContain('GPS quality is unknown');
  expect(await quality(pts,{t0_ms:500,t1_ms:900})).toContain('GPS quality is unknown');
  expect(await quality(pts,{t0_ms:2100,t1_ms:2200})).toContain('observed or interpolated fixes throughout');
});

test('All trips new questions stay unscoped while map-selected GPS trip becomes the authoritative source', async ({ page }) => {
  await mockApi(page);
  const requests=[];
  await page.route(/\/api\/agent\/ask$/,r=>{
    requests.push(r.request().postDataJSON());
    return r.fulfill({contentType:'text/event-stream',body:'event: step\ndata: '+JSON.stringify({run_id:'r_scope',step_id:'s0',i:0,state:'parse',elapsed_ms:1,summary:'check scope'})+'\n\nevent: status\ndata: '+JSON.stringify({run_id:'r_scope',final:'NO_MATCH',stop_reason:'enough_evidence'})+'\n\nevent: done\ndata: '+JSON.stringify({run_id:'r_scope'})+'\n\n'});
  });
  await page.goto('#/fleet');await expect(page.locator('.fl-ix')).toHaveCount(1);
  await expect(page.locator('#source-tag')).toContainText('all trips');
  await page.locator('#ask-input').fill('Where did any car pass?');await page.locator('#ask-input').press('Enter');
  await expect.poll(()=>requests.length).toBe(1);expect(requests[0].context.scene_key).toBeNull();
  await page.keyboard.press('Escape');
  await page.locator('.fl-car[data-trip="fl-pie-fixture-02"] .fl-tag').click();
  await expect(page).toHaveURL(/fleet\/fl-pie-fixture-02@/);
  await expect(page.locator('#source-tag')).toContainText('fl-pie-fixture-02');
  await expect(page.locator('.fl-ix')).toHaveCount(0);
  await page.locator('#ask-input').fill('Where is this car?');await page.locator('#ask-input').press('Enter');
  await expect.poll(()=>requests.length).toBe(2);expect(requests[1].context.scene_key).toBe('fl-pie-fixture-02');
});

test('default Fleet brush uses the active source mapping and commits its explicit context', async () => {
  const fs=require('fs'),path=require('path'),vm=require('vm');
  const s=fs.readFileSync(path.resolve(__dirname,'../../static/app.js'),'utf8');
  const code=s.slice(s.indexOf('timeline.onBrush(({ t0, t1 }) => {'),s.indexOf("bus.on('context:zone'"));
  const events=[],c={timeline:{onBrush:fn=>fn({t0:22000,t1:23000})},ctx:{route:{workspace:'fleet',page:'fleet'}},run:null,state:{activeFleetScene:'new'},clock:{domain:{kind:'fleet',replay:[{trip_id:'new',source_start_ms:0,replay_start_ms:20000,rate:2}]}},followContext:{},renderContext:()=>{},bus:{emit:(...x)=>events.push(x)}};
  vm.runInNewContext(code,c);expect(Array.from(c.followContext.t_range)).toEqual([4000,6000]);
  expect(events).toEqual([['fleet:active',{scene_key:'new',t_ms:4000}]]);
});

test('Fleet media playback multiplies wall-clock rate by the trip mapping and Street follows scene rate',async({page})=>{
  await mockApi(page);await page.goto('#/fleet/fl-pie-fixture-01@3000');
  await expect(page.locator('.fl-vbox video')).toBeVisible();
  await page.evaluate(async()=>{const {view}=await import(new URL('fleet/page.js',document.baseURI).href);const {clock}=await import(new URL('kernel/clock.js',document.baseURI).href);view.ix.trip.replay.rate=2;clock.rate=1.5;clock.seek(clock.t);});
  await expect.poll(()=>page.locator('.fl-vbox video').evaluate(v=>v.playbackRate)).toBe(3);
  await page.goto('#/street/st-fixture_cam_1-clip01');
  await expect(page.locator('.st-vbox video')).toBeVisible();
  await page.evaluate(async()=>{const {clock}=await import(new URL('kernel/clock.js',document.baseURI).href);clock.rate=2;clock.seek(1000);});
  await expect.poll(()=>page.locator('.st-vbox video').evaluate(v=>v.playbackRate)).toBe(2);
});
