const { test, expect } = require('@playwright/test');
const { mockApi, mockSceneAlias, fixtureWorkspaces } = require('./mockapi');
const fs = require('fs'), path = require('path');
const T1 = 'fl-pie-fixture-01';
const out = path.resolve(__dirname, '../../../fixtures/sva-out');
const mod = (page, rel, expr) => page.evaluate(async ([r,e]) => new Function('m',`return (${e})`)(await import(new URL(r,document.baseURI).href)),[rel,expr]);

test('Fleet caps route requests and defers every unselected trip detail', async ({ page }) => {
  await mockApi(page);
  const ids = [T1, ...Array.from({length:9},(_,i)=>`fl-load-${i}`)], ws = fixtureWorkspaces();
  for (const id of ids.slice(1)) {
    ws.find(w=>w.id==='fleet').scenes.push(await mockSceneAlias(page,id,T1));
    const gj = JSON.parse(fs.readFileSync(path.join(out,T1,'trip.geojson'))); gj.features[0].properties.replay.trip_id=id;
    const dx = (ids.indexOf(id)) * .005;
    gj.features[0].geometry.coordinates = gj.features[0].geometry.coordinates.map(([lon,lat])=>[lon+dx,lat]);
    for (const f of gj.features.slice(1)) f.properties.lon += dx;
    await page.route(new RegExp(`/api/assets/${id}/trip.geojson$`),r=>r.fulfill({json:gj}));
  }
  await page.route(/\/api\/workspaces$/,r=>r.fulfill({json:ws}));
  await page.route(/\/api\/fleet\/trips$/,r=>r.fulfill({json:ids.map(trip_id=>({trip_id,title:trip_id,t0_ms:0,t1_ms:10000,full:true,n_events:trip_id===T1?5:1,geojson_url:`api/assets/${trip_id}/trip.geojson`}))}));
  let active=0, peak=0; const details=[];
  await page.route(/\/api\/(scene\/fl-|assets\/fl-)/,async r=>{
    const p=new URL(r.request().url()).pathname;
    if (/\/(buildings|entities|timeline|places)\.json$/.test(p)) details.push(p);
    if (!/(\/manifest|\/trip.geojson|\/events.json|\/(buildings|entities|timeline|places)\.json)$/.test(p)) return r.fallback();
    active++; peak=Math.max(peak,active);
    await new Promise(resolve=>setTimeout(resolve,40));
    try { await r.fallback(); } finally { active--; }
  });
  await page.goto('#/fleet'); await expect(page.locator('.fl-car')).toHaveCount(10);
  await expect.poll(()=>mod(page,'fleet/page.js','m.view.ix?.trip.id')).toBe(T1);
  expect(peak).toBeLessThanOrEqual(6); expect(peak).toBeGreaterThan(1);
  expect(details.length).toBe(4); expect(details.every(p=>p.includes(`/${T1}/`))).toBe(true);
  expect(await mod(page,'fleet/page.js','m.view.trips.filter(t=>t.detailsLoaded).map(t=>t.id)')).toEqual([T1]);
  await page.keyboard.press('Escape');
  await page.locator('.fl-car[data-trip="fl-load-8"] .fl-tag').click();
  await expect.poll(()=>mod(page,'fleet/page.js','m.view.ix?.trip.id')).toBe('fl-load-8');
  expect(details.filter(p=>p.includes('/fl-load-8/')).length).toBe(4);
  expect(details.length).toBe(8);
});

test('one trip route failure keeps healthy trips usable and retries in place', async ({ page }) => {
  await mockApi(page); let broken=true;
  await page.route(/\/api\/assets\/fl-pie-fixture-02\/trip.geojson$/,r=>broken?r.fulfill({status:503,json:{detail:'offline'}}):r.fallback());
  await page.goto('#/fleet');
  await expect(page.locator('.fl-car')).toHaveCount(1);
  await expect.poll(()=>mod(page,'fleet/page.js','m.view.ix?.trip.id')).toBe(T1);
  const failure=page.locator('[data-trip-error="fl-pie-fixture-02"]');
  await expect(failure).toContainText('route unavailable');
  await expect(page.locator('.page-retry')).toHaveCount(0);
  broken=false; await failure.locator('button').click();
  await expect(page.locator('.fl-car')).toHaveCount(2);
  await expect(failure).toHaveCount(0);
  expect(await mod(page,'fleet/page.js','m.view.ix.trip.id')).toBe(T1);
});

test('a selected trip asset failure keeps video usable and retries only the failed detail', async ({ page }) => {
  const errors=[]; page.on('pageerror',e=>errors.push(String(e)));
  await mockApi(page); let broken=true; const reads={buildings:0,entities:0,timeline:0,places:0};
  await page.route(/\/api\/assets\/fl-pie-fixture-01\/(buildings|entities|timeline|places)\.json$/,r=>{
    const kind=new URL(r.request().url()).pathname.match(/\/(\w+)\.json$/)[1]; reads[kind]++;
    return kind==='buildings'&&broken?r.fulfill({status:503,json:{detail:'offline'}}):r.fallback();
  });
  await page.goto(`#/fleet/${T1}@3000`);
  const failure=page.locator(`[data-trip-error="${T1}"]`);
  await expect(failure).toContainText('buildings unavailable');
  await expect(page.locator('.fl-vbox')).toHaveAttribute('data-media-state','ready');
  await expect(page.locator('.page-retry')).toHaveCount(0);
  broken=false; await failure.locator('button').click();
  await expect(failure).toHaveCount(0);
  await expect(page.locator('.fl-cols .fl-col')).toHaveCount(2);
  expect(reads).toEqual({buildings:2,entities:1,timeline:1,places:1}); expect(errors).toEqual([]);
});
