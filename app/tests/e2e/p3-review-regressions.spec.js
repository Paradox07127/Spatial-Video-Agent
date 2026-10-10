const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { ASKS, mockApi, mockAskFrames } = require('./mockapi');
const WH = 'wh-sdg-fixture-01';
const RUN = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../../fixtures/runs/r_fixturewh0001.json'), 'utf8'));

test('small, large and signed .4g values keep clickable measurement citations', async ({ page }) => {
  await mockApi(page);
  await page.goto(`#/warehouse/${WH}`);
  for (const [value, spelling] of [[.00001,'1e-05'],[1235000000,'1.235e+09'],[-.00001,'-1e-05']]) {
    const frames = structuredClone(ASKS.find(a => a.ask_id === 'wh-main').events);
    const evidence = frames.flatMap(f => f.event === 'tool_result' ? f.data.evidence : []).find(e => e.method.startsWith('ground_distance'));
    evidence.value = value;
    for (const f of frames) if (f.event === 'answer') Object.assign(f.data, { text: `P1 measured ${spelling} m.`, measure_refs: [{ eid: evidence.eid, field: 'value' }], cites: [evidence.eid] });
    await mockAskFrames(page, frames);
    await page.locator('#ask-input').fill('Measure the distance'); await page.locator('#ask-input').press('Enter');
    await expect(page.locator('body')).toHaveAttribute('data-run','done');
    await expect(page.locator('#answer .ans-num')).toHaveText(spelling);
    await expect(page.locator('#answer .ans-num')).toHaveAttribute('data-eid', evidence.eid);
    await page.locator('#answer .ans-num').click();
    await expect(page.locator('body')).toHaveAttribute('data-stage','evidence');
  }
});

test('home length limit and a readable server validation failure', async ({ page }) => {
  await mockApi(page);
  await page.route(/\/api\/agent\/ask$/, r => r.fulfill({ status:422, json:{ detail:[{ loc:['body','q'],msg:'String should have at most 500 characters' }] } }));
  await page.goto('#/');
  await expect(page.locator('#home-input')).toHaveAttribute('maxlength','500');
  await page.locator('#home-input').fill('x'.repeat(501));
  await expect(page.locator('#home-input')).toHaveValue('x'.repeat(500));
  await page.locator('#home-input').fill('A short question'); await page.locator('#home-input').press('Enter');
  await expect(page.locator('#ask-status')).toContainText('The investigation request is invalid. q: String should have at most 500 characters');
});

async function harness(page, name) {
  await page.route(`**/${name}.html`, r => r.fulfill({ contentType:'text/html',body:'<!doctype html><div id="fixture"></div><div id="runpage"></div>' }));
  await page.goto(`${name}.html`);
}

test('building model failure is local, evicted and retried without losing map facts', async ({ page }) => {
  await harness(page,'buildings');
  const result = await page.evaluate(async () => {
    const { createBuildings } = await import(new URL('fleet/buildings.js',document.baseURI));
    const host = document.querySelector('#fixture');
    let models = 0;
    const api = { get:async p => {
      if (p.startsWith('api/model-results')) { if (++models === 1) throw new Error('offline'); return []; }
      return { name:'Known building',addr:'Main Street',pois:[],source:'OSM',map_snapshot_date:'fixture' };
    } };
    window.card = createBuildings(host,{ api,tripOf:()=>({ id:'trip',mf:{t0_ms:0,t1_ms:10000} }),onFocus:()=>{} });
    await window.card.focus('b1');
    const first = host.textContent;
    host.querySelector('.fl-retry').click();
    await new Promise(resolve=>setTimeout(resolve));
    return { first,second:host.textContent,models };
  });
  expect(result.first).toContain('Known building'); expect(result.first).toContain('Sign reading could not load');
  expect(result.second).toContain('No sign reading'); expect(result.models).toBe(2);
});

test('failed building details show retry and focus errors do not escape', async ({ page }) => {
  await harness(page,'building-details');
  const result = await page.evaluate(async () => {
    const { createBuildings } = await import(new URL('fleet/buildings.js',document.baseURI));
    const host = document.querySelector('#fixture'); let details = 0;
    const card = createBuildings(host,{ api:{ get:async p => {
      if(p.startsWith('api/model-results')) return [];
      if(++details === 1) throw new Error('offline');
      return { name:'Recovered building',pois:[],source:'OSM',map_snapshot_date:'fixture' };
    } },tripOf:()=>({id:'trip',mf:{t0_ms:0,t1_ms:1000}}),onFocus:()=>{} });
    await card.focus('b1'); const first = host.textContent;
    host.querySelector('.fl-retry').click(); await new Promise(resolve=>setTimeout(resolve));
    return {first,second:host.textContent};
  });
  expect(result.first).toContain('Building details could not load'); expect(result.second).toContain('Recovered building');
});

test('running reasoning refreshes to completion and stops after close', async ({ page }) => {
  await mockApi(page); let reads=0;
  await page.route(/\/api\/agent\/runs\/r_fixturewh0001$/, r => {
    reads++; return r.fulfill({ json:{ ...RUN,status:reads===1?'running':'done',final:reads===1?null:'SUPPORTED',q:reads===1?'Still investigating':'Finished investigation' } });
  });
  await page.goto('#/test'); await page.evaluate(()=>location.hash='#/run/r_fixturewh0001');
  await expect(page.locator('.rp-q')).toHaveText('Still investigating');
  await expect(page.locator('.rp-q')).toHaveText('Finished investigation');
  await page.evaluate(async()=> (await import(new URL('run/page.js',document.baseURI))).runpage.close());
  const completed=reads; await page.waitForTimeout(1200); expect(reads).toBe(completed);
});

test('multiple verdict changes retain each claims own step and verdict', async ({ page }) => {
  await mockApi(page);
  const r = structuredClone(RUN), first = r.claims[0], e1=r.evidence.at(-1), e2={...e1,eid:'e_second',value:3};
  e2.scene_key='fl-pie-fixture-01';
  r.evidence.push(e2);
  r.hits=[{moment_id:'first-moment',scene_key:WH,evidence_ids:[e1.eid],thumb:`/api/assets/${WH}/thumbs/ceiling_04/00004.jpg`},
    {moment_id:'second-moment',scene_key:e2.scene_key,evidence_ids:[e2.eid],thumb:`/api/assets/${e2.scene_key}/thumbs/dashcam/00003.jpg`}];
  const base=r.steps.find(s=>s.evidence_ids.includes(e1.eid));
  r.steps.push({...base,i:100,step_id:'second-step',evidence_ids:[e2.eid]});
  r.claims=[{...first,claim_id:'first',scene_key:WH,moment_id:'first-moment',text:'First claim',before:'UNRESOLVED',after:'SUPPORTED',predicates:[{...first.predicates[0],eids:[e1.eid]}]},
    {...first,claim_id:'second',scene_key:e2.scene_key,moment_id:'second-moment',text:'Second claim',before:'SUPPORTED',after:'CONTRADICTED',predicates:[{...first.predicates[0],eids:[e2.eid]}]}];
  await page.route(/\/api\/agent\/runs\/r_fixturewh0001$/, route=>route.fulfill({json:r}));
  await page.goto('#/test'); await page.evaluate(()=>location.hash='#/run/r_fixturewh0001');
  const cards=page.locator('.rp-changed'); await expect(cards).toHaveCount(2);
  await expect(cards.nth(0)).toContainText('First claim'); await expect(cards.nth(0).locator('.after .verdict')).toContainText('SUPPORTED');
  await expect(cards.nth(1)).toContainText('Second claim'); await expect(cards.nth(1).locator('.after .verdict')).toContainText('CONTRADICTED');
  await expect(cards.nth(1)).toContainText('STEP 101');
  await expect(cards.nth(0).locator('.after img')).toHaveAttribute('src',new RegExp(`${WH}/thumbs/`));
  await expect(cards.nth(1).locator('.after img')).toHaveAttribute('src',/fl-pie-fixture-01\/thumbs\/dashcam/);
});


test('closing a running reasoning page cancels polling and ignores its in-flight snapshot', async ({ page }) => {
  await mockApi(page); let reads=0,release;
  await page.route(/\/api\/agent\/runs\/r_fixturewh0001$/, async route => {
    reads++;
    if(reads===2) await new Promise(resolve=>release=resolve);
    await route.fulfill({json:{...RUN,status:'running',final:null,q:'Pending investigation'}});
  });
  await page.goto('#/test'); await page.evaluate(()=>location.hash='#/run/r_fixturewh0001');
  await expect(page.locator('.rp-q')).toHaveText('Pending investigation');
  await expect.poll(()=>reads).toBe(2);
  await page.evaluate(async()=>{
    (await import(new URL('run/page.js',document.baseURI))).runpage.close();
    document.querySelector('#runpage').textContent='Closed snapshot';
  });
  release(); await page.waitForTimeout(1200);
  expect(reads).toBe(2); await expect(page.locator('#runpage')).toHaveText('Closed snapshot');
});


test('home source picker exposes and updates its combobox keyboard selection', async ({ page }) => {
  await mockApi(page); let asks=0;
  page.on('request',r=>{ if(r.url().endsWith('/api/agent/ask')) asks++; });
  await page.goto('#/');
  const input=page.getByRole('combobox');
  await expect(input).toHaveAttribute('aria-controls','home-source-list');
  await input.fill('@wh-');
  await expect(input).toHaveAttribute('aria-expanded','true');
  await expect(input).toHaveAttribute('aria-activedescendant','home-source-0');
  await input.press('ArrowDown');
  await expect(page.locator('#home-source-list [aria-selected="true"]')).toHaveAttribute('id','home-source-1');
  await expect(input).toHaveAttribute('aria-activedescendant','home-source-1');
  await input.press('ArrowUp'); await input.press('Enter');
  await expect(input).toHaveAttribute('aria-expanded','false');
  await expect(input).not.toHaveAttribute('aria-activedescendant',/.+/);
  await expect(page.locator('.home-src')).toContainText(WH);
  expect(asks).toBe(0);
});
