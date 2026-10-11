const { test, expect } = require('@playwright/test');
const { mockApi, mockAskFrames } = require('./mockapi');
const fs = require('fs'), path = require('path');
const WH = 'wh-sdg-fixture-01', ST = 'st-fixture_cam_1-clip01', FL = 'fl-pie-fixture-01';
const clock = (page, expr) => page.evaluate(async expr => new Function('c', `return (${expr})`)((await import(new URL('kernel/clock.js',document.baseURI))).clock), expr);

for (const [workspace, key, cam, field, value, sourceMs, wallMs] of [
  ['street',ST,'fixture_cam-1','t_s',6.4,6400,6400],
  ['warehouse',WH,'ceiling_04','t_ms',7500,7500,7500],
  ['fleet',FL,'dashcam','t_min_ms',6500,6500,23250],
]) test(`${workspace}: a referenced time seeks its numeric field rather than the evidence window start`, async ({page})=>{
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  await mockApi(page);
  if (workspace==='fleet') {
    const route=JSON.parse(fs.readFileSync(path.resolve(__dirname,`../../../fixtures/sva-out/${FL}/trip.geojson`)));
    Object.assign(route.features[0].properties.replay,{source_start_ms:0,replay_start_ms:20000,rate:2});
    await page.route(new RegExp(`/api/assets/${FL}/trip.geojson$`),r=>r.fulfill({json:route}));
  }
  const eid='e_peak_time', id='r_field_time', base={run_id:id};
  const evidence={eid,scene_key:key,cam_id:cam,t0_ms:0,t1_ms:10000,observation_ids:[],analysis_rev:'fx-0001',kind:'measure',method:'yolo_people_peak',
    value:{count:5,[field]:value,t0_s:0,t1_s:10,coverage:1,duration_s:2.2,speed_m_s:3.3,sigma_s:.4,duration_ms:1500},unit:null,sigma:null,artifact_ref:null};
  await mockAskFrames(page,[
    {event:'step',data:{...base,step_id:'s0',i:0,state:'measure',elapsed_ms:1,summary:'Measure peak',exec_state:'precomputed'}},
    {event:'tool_call',data:{...base,step_id:'s0',call_id:'c0',name:'measure',args:{scene_key:key},why:'Find peak'}},
    {event:'tool_result',data:{...base,step_id:'s0',call_id:'c0',ok:true,envelope:{input_range:{scene_key:key,t0_ms:0,t1_ms:10000},elapsed_ms:1,exec_state:'precomputed'},evidence:[evidence]}},
    {event:'hits',data:{...base,query_id:'q0',moments:[{moment_id:'m_peak',scene_key:key,t0_ms:0,t1_ms:10000,cams:[cam],primary_cam:cam,actor_refs:[],evidence_ids:[eid],why:['Peak'],score:1,thumb:null}]}},
    {event:'answer',data:{...base,sentence_id:'a0',text:`Measured peak of 5 simultaneous person detections at ${sourceMs/1000} s across 0–10 s. Other references: 2.2 s duration, 3.3 m/s speed, 0.4 s uncertainty, 1500 ms duration.`,claim_ids:[],cites:[eid],measure_refs:[{eid,field:'value.count'},{eid,field:`value.${field}`},{eid,field:'value.t0_s'},{eid,field:'value.t1_s'},...['duration_s','speed_m_s','sigma_s','duration_ms'].map(field=>({eid,field:`value.${field}`}))]}},
    {event:'status',data:{...base,final:'SUPPORTED',stop_reason:'enough_evidence'}},
    {event:'done',data:base},
  ]);
  await page.goto(workspace==='fleet'?'#/fleet':`#/${workspace}/${key}`);
  await expect(page.locator(workspace==='fleet'?'.fl-ix':workspace==='warehouse'?'.wh':'.st-root').first()).toBeVisible();
  await page.locator('#ask-input').fill('When was the simultaneous detection peak?'); await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run','done');
  const time=page.locator('#answer .ans-time').filter({hasText:`${sourceMs/1000} s`});
  await expect(time).toHaveCount(1);
  await time.click();
  await expect.poll(()=>clock(page,'c.t')).toBe(wallMs);
  await expect(page.locator('#followup [data-context="time"]')).toContainText(workspace==='street'?'00:06.4':workspace==='warehouse'?'00:07.5':'00:06.5');
  await expect(time).toHaveAttribute('data-ref-field',`value.${field}`);
  if (workspace==='street') await expect.poll(()=>page.locator('.st-root video').evaluate(v=>v.currentTime)).toBeCloseTo(6.4,1);
  const storedStart=await page.evaluate(async eid=>(await import(document.querySelector('script[src*="app.js"]').src)).currentRun().evidence.get(eid).t0_ms,eid);
  expect(storedStart).toBe(0);
  // Numeric counts and ordinary evidence marks still open the original window start.
  for (const ordinary of ['count','duration_s','speed_m_s','sigma_s','duration_ms']) {
    await time.click(); await expect.poll(()=>clock(page,'c.t')).toBe(wallMs);
    const ref=page.locator(`#answer .ans-num[data-ref-field="value.${ordinary}"]`);
    await expect(ref).toBeVisible(); await ref.click();
    await expect.poll(()=>clock(page,'c.t')).toBe(workspace==='fleet'?20000:0);
  }
  await time.click(); await expect.poll(()=>clock(page,'c.t')).toBe(wallMs);
  await page.locator(`.tl-evm[data-eid="${eid}"]`).click();
  await expect.poll(()=>clock(page,'c.t')).toBe(workspace==='fleet'?20000:0);
  expect(errors).toEqual([]);
});

test('window endpoint references focus a valid instant outside the stored measurement and clamp the scene clock', async ({page})=>{
  await mockApi(page);
  const eid='e_closest_endpoint', base={run_id:'r_endpoint'};
  const evidence={eid,scene_key:WH,cam_id:'ceiling_04',t0_ms:4900,t1_ms:4901,observation_ids:[],analysis_rev:'fx-0001',kind:'measure',method:'closest_approach',
    value:{distance_m:1.5,t_s:4.9004,window_t0_s:0,window_t1_s:10},unit:null,sigma:null,artifact_ref:null};
  await mockAskFrames(page,[
    {event:'step',data:{...base,step_id:'s0',i:0,state:'measure',elapsed_ms:1,summary:'Closest approach',exec_state:'precomputed'}},
    {event:'tool_call',data:{...base,step_id:'s0',call_id:'c0',name:'closest_approach',args:{scene_key:WH},why:'Scan window'}},
    {event:'tool_result',data:{...base,step_id:'s0',call_id:'c0',ok:true,envelope:{input_range:{scene_key:WH,t0_ms:0,t1_ms:10000},elapsed_ms:1,exec_state:'precomputed'},evidence:[evidence]}},
    {event:'answer',data:{...base,sentence_id:'a0',text:'Closest distance was 1.5 m at 4.9 s in the window 0–10 s.',claim_ids:[],cites:[eid],measure_refs:['distance_m','t_s','window_t0_s','window_t1_s'].map(field=>({eid,field:`value.${field}`}))}},
    {event:'status',data:{...base,final:'SUPPORTED',stop_reason:'enough_evidence'}},
    {event:'done',data:base},
  ]);
  await page.goto('#/test');
  await page.evaluate(async()=> (await import(new URL('kernel/bus.js',document.baseURI))).bus.on('evidence:focus',e=>window.__referenceFocus={t0:e.t0_ms,t1:e.t1_ms}));
  await page.locator('#ask-input').fill('What happened within this window?'); await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run','done');
  for (const [field,t0,target] of [['window_t1_s',10000,9999],['window_t0_s',0,0],['t_s',4900,4900]]) {
    await page.locator(`#answer [data-ref-field="value.${field}"]`).click();
    await expect.poll(()=>page.evaluate(()=>window.__referenceFocus?.t0)).toBe(t0);
    expect(await page.evaluate(()=>window.__referenceFocus)).toEqual({t0,t1:t0+1});
    await expect.poll(()=>clock(page,'c.t')).toBe(target);
  }
  const stored=await page.evaluate(async eid=>{const e=(await import(document.querySelector('script[src*="app.js"]').src)).currentRun().evidence.get(eid); return [e.t0_ms,e.t1_ms];},eid);
  expect(stored).toEqual([4900,4901]);
});
