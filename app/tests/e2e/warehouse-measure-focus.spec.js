const { test, expect } = require('@playwright/test');
const { mockApi, mockAskFrames } = require('./mockapi');
const WH='wh-sdg-fixture-01';
const probe=page=>page.evaluate(async()=>(await import(new URL('warehouse/page.js',document.baseURI))).probe());
const target=id=>({scene_key:WH,entity_id:id,kind:id.startsWith('F')?'forklift':'person'});

for (const [name,method,value,args,id,field,text] of [
  ['position value','ground_position',{entity_id:'P1',x_m:4,y_m:5,t_s:3},{target:target('P1')},'P1','value.x_m','P1 position x is 4 m.'],
  ['position request target','ground_position',{x_m:4,y_m:5,t_s:3},{target:target('P1')},'P1','value.x_m','P1 position x is 4 m.'],
  ['speed request actor','track_speed',2.5,{a:target('F1'),kind:'speed'},'F1','value','F1 speed is 2.5 m/s.'],
]) test(`${name}: clicking a measured numeric citation focuses the named actor without camera observations`,async({page})=>{
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  await mockApi(page);
  const eid='e_actor_focus',base={run_id:'r_actor_focus'};
  const evidence={eid,scene_key:WH,cam_id:null,t0_ms:3000,t1_ms:3001,observation_ids:[],analysis_rev:'fx-0001',kind:'measure',method,value,unit:method==='ground_position'?'m':'m/s',sigma:.1,artifact_ref:null};
  await mockAskFrames(page,[
    {event:'step',data:{...base,step_id:'s0',i:0,state:'measure',elapsed_ms:1,summary:'Measure actor',exec_state:'live'}},
    {event:'tool_call',data:{...base,step_id:'s0',call_id:'c0',name:method,args:{scene_key:WH,t_ms:3000,...args},why:'Measure named actor'}},
    {event:'tool_result',data:{...base,step_id:'s0',call_id:'c0',ok:true,envelope:{input_range:{scene_key:WH,t0_ms:3000,t1_ms:3001},elapsed_ms:1,exec_state:'live'},evidence:[evidence]}},
    {event:'hits',data:{...base,query_id:'q0',moments:[{moment_id:'m_actor',scene_key:WH,t0_ms:3000,t1_ms:3001,cams:['ceiling_04'],primary_cam:'ceiling_04',actor_refs:[target(id)],evidence_ids:[eid],why:['Named actor'],score:1,thumb:null}]}},
    {event:'ui',data:{...base,cmd:'highlight',args:{ids:[id],mode:'outline'}}},
    {event:'answer',data:{...base,sentence_id:'a0',text,claim_ids:[],cites:[eid],measure_refs:[{eid,field}]}},
    {event:'status',data:{...base,final:'SUPPORTED',stop_reason:'enough_evidence'}},
    {event:'done',data:base},
  ]);
  await page.goto(`#/warehouse/${WH}`); await expect(page.locator('.wh')).toHaveAttribute('data-scene',WH);
  await page.locator('#ask-input').fill('Measure this actor'); await page.locator('#ask-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run','done');
  await page.locator(`#answer .ans-num[data-ref-field="${field}"]`).click();
  await expect(page.locator('body')).toHaveAttribute('data-stage','evidence');
  await expect.poll(async()=>(await probe(page)).dimmed.sort()).toEqual(['P1','P2','F1'].filter(actor=>actor!==id).sort());
  expect((await probe(page)).highlighted).toEqual([id]);
  expect(errors).toEqual([]);
});

test('actor fallback ignores model descriptions, unknown IDs and request actors from another scene',async({page})=>{
  await mockApi(page); await page.goto(`#/warehouse/${WH}`); await expect(page.locator('.wh')).toHaveAttribute('data-scene',WH);
  for (const fields of [
    {kind:'model',method:'ground_position',value:{entity_id:'P1'}},
    {kind:'measure',method:'ground_position',value:{entity_id:'P999'}},
    {kind:'measure',method:'track_speed',params:{a:{scene_key:'another-scene',entity_id:'P1'}}},
    {kind:'measure',method:'unrecognized_measure',value:{entity_id:'P1'}},
  ]) {
    await page.evaluate(async fields=>(await import(new URL('kernel/bus.js',document.baseURI))).bus.emit('evidence:focus',{eid:'e_rejected',scene_key:'wh-sdg-fixture-01',cam_id:null,t0_ms:3000,t1_ms:3001,observation_ids:[],...fields}),fields);
    await expect.poll(async()=>(await probe(page)).dimmed).toEqual([]);
  }
});
