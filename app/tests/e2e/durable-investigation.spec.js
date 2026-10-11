const { test, expect } = require('@playwright/test');
const { mockApi } = require('./mockapi');
const SCENE='wh-sdg-fixture-01', SID='s_investigation',TID='t_turn',JID='j_job';
const wire = frames => frames.map((f,i)=>`id: ${f.seq ?? i+1}\nevent: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');
const frames = () => {
  const base={run_id:'r_durable'},eid='e_count',observation={observation_id:'o_seen',eid:'e_observation',scene_key:SCENE,cam_id:'eye_00',t0_ms:2800,t1_ms:2801,frame_refs:[{cam_id:'eye_00',t_ms:2800}],subject_refs:[],facts:[{subject_ids:[],text:'A worker stands beside a forklift. <img src=x onerror="window.bad=1">',frame_refs:[{cam_id:'eye_00',t_ms:2800}],uncertainty:.3}],model_rev:'cosmos-test',prompt_rev:'observe-v1',verification:'unverified'};
  const text='🚦 Camera 5 observed 5 tracks.';const chars=Array.from(text); // Unicode codepoint index of the second 5.
  const point=chars.findIndex((x,i)=>x==='5'&&i>12);
  return [
    {event:'step',data:{...base,step_id:'s0',i:0,state:'inspect',elapsed_ms:1,summary:'Observe actual frame'}},
    {event:'tool_call',data:{...base,step_id:'s0',call_id:'call0',name:'count',args:{scene_key:SCENE},why:'Count'}},
    {event:'tool_result',data:{...base,step_id:'s0',call_id:'call0',ok:true,envelope:{input_range:{scene_key:SCENE,t0_ms:0,t1_ms:10000},elapsed_ms:1},evidence:[{eid,scene_key:SCENE,cam_id:'eye_00',t0_ms:2800,t1_ms:2801,observation_ids:[],kind:'measure',method:'count',value:{count:5},unit:null,sigma:null,analysis_rev:'test',artifact_ref:null}]}},
    {event:'observation',data:{...base,observation}},
    {event:'answer',data:{...base,sentence_id:'a0',text,claim_ids:[],cites:[eid],measure_refs:[{eid,field:'value.count'}],text_spans:[{start:point,end:point+1,eid,field:'value.count'}],model_refs:[{observation_id:'o_seen',eid:'e_observation',field:'facts.0.text'}]}},
    {event:'status',data:{...base,final:'UNRESOLVED',stop_reason:'enough_evidence'}},
    {event:'done',data:base},
  ];
};
async function durable(page,{drop=false,attempt=1,stop=false,terminalNoFinal=false}={}){
  await mockApi(page);
  await page.route(/\/health$/,r=>r.fulfill({json:{ok:true,mode:'dev',backend:'duckdb',agent_protocols:[1,2],index_rows:{scenes:1}}}));
  const sent=[],subscribes=[],cancels=[];let turn=null;
  const session=()=>({session_id:SID,revision:turn?1:0,title:'Worker investigation',created:'2026-10-10T00:00:00Z',updated:'2026-10-10T00:00:01Z',turns:turn?[turn]:[]});
  await page.route(/\/api\/agent\/sessions(?:\/[^/?]+)?$/,r=>{
    if(r.request().method()==='POST')return r.fulfill({json:session()});
    return r.fulfill({json:new URL(r.request().url()).pathname.endsWith('/sessions')?{sessions:[session()]}:session()});
  });
  await page.route(/\/api\/agent\/jobs\/j_job$/,r=>r.fulfill({json:{job_id:JID,session_id:SID,turn_id:TID,run_id:'r_durable',status:terminalNoFinal?'cancelled':stop?'running':'done',attempt,last_seq:7,current_attempt_start_seq:attempt===2?10:1}}));
  await page.route(/\/api\/agent\/jobs\/j_job\/events\?/,r=>{
    subscribes.push(new URL(r.request().url()).searchParams.get('after_seq'));
    const events=(terminalNoFinal?frames().slice(0,1):frames()).map((f,i)=>({...f,seq:attempt===2?10+i:i+1}));
    return r.fulfill({contentType:'text/event-stream',body:wire(events)});
  });
  await page.route(/\/api\/agent\/jobs\/j_job\/cancel$/,r=>{cancels.push(r.request().postDataJSON());return r.fulfill({json:{cancelled:true}});});
  await page.route(/\/api\/agent\/ask$/,r=>{
    const body=r.request().postDataJSON();sent.push(body);turn={turn_id:TID,parent_turn_id:body.parent_turn_id,q:body.q,context:body.context,job_id:JID,run_id:'r_durable',status:'done',created:'2026-10-10T00:00:00Z',observations:[],answers:[],final:'UNRESOLVED'};
    return r.fulfill({headers:{'content-type':'text/event-stream','X-SVA-Session':SID,'X-SVA-Turn':TID,'X-SVA-Job':JID},body:wire(drop||stop?frames().slice(0,1):frames())});
  });
  return {sent,subscribes,cancels};
}
async function ask(page){await page.goto(`#/warehouse/${SCENE}`);await expect(page.locator('.wh')).toBeVisible();await page.locator('#ask-input').fill('What do you observe?');await page.locator('#ask-input').press('Enter');}

test('v2 answer uses exact Unicode text spans and model observations link to their actual source frame',async({page})=>{
  const {sent}=await durable(page);await ask(page);
  await expect(page.locator('body')).toHaveAttribute('data-run','done');
  expect(sent[0]).toMatchObject({protocol_version:2,session_id:SID,expected_revision:0,parent_turn_id:null});
  await expect(page.locator('#answer .ans-num')).toHaveCount(1);await expect(page.locator('#answer .ans-num')).toHaveText('5');
  expect(await page.locator('#answer .ans-num').evaluate(el=>el.previousSibling.textContent)).toContain('Camera 5 observed ');
  await expect(page.locator('#observations')).toContainText('A worker stands beside a forklift. <img');
  await expect(page.locator('#observations img')).toHaveCount(0);expect(await page.evaluate(()=>window.bad)).toBeUndefined();
  await expect(page.locator('#answer .verdict')).toHaveText('Unresolved');
  await page.locator('#observations .observation-frame').click();
  await expect.poll(()=>page.evaluate(async()=> (await import(new URL('kernel/clock.js',document.baseURI).href)).clock.t)).toBe(2800);
  await expect(page.locator('.wh-stage-video video')).toHaveCount(1);
  await expect(page.locator('.wh-stage-video .wh-vcell[data-cam="eye_00"]')).toHaveCount(1);
  await page.locator('#followup-input').fill('What changed?');await page.locator('#followup-input').press('Enter');
  await expect.poll(()=>sent.length).toBe(2);expect(sent[1]).toMatchObject({session_id:SID,parent_turn_id:TID,expected_revision:1,context:{cam_id:'eye_00'}});
});

test('refresh restores the saved turn and investigation history through durable job replay',async({page})=>{
  const {subscribes}=await durable(page);await ask(page);await expect(page.locator('body')).toHaveAttribute('data-run','done');
  await page.reload();await expect(page.locator('#answer')).toContainText('Camera 5 observed 5 tracks.');
  await expect(page.locator('#observations')).toContainText('worker stands');expect(subscribes).toContain('0');
  await page.locator('#investigations-btn').click();await expect(page.locator('[data-turn="t_turn"]')).toContainText('What do you observe?');
  await page.locator('[data-turn="t_turn"]').click();await expect(page.locator('#investigations')).toBeHidden();
  await expect(page.locator('#answer .ans-num')).toHaveCount(1);
});

test('interrupted subscription reconnects and a new attempt replays from its own sequence boundary',async({page})=>{
  const {subscribes}=await durable(page,{drop:true,attempt:2});await ask(page);
  await expect(page.locator('body')).toHaveAttribute('data-run','done');expect(subscribes).toEqual(['9']);
  await expect(page.locator('#answer .ans-num')).toHaveCount(1);await expect(page.locator('#observations article')).toHaveCount(1);
});

test('Stop explicitly cancels the durable server job instead of only aborting its browser subscription',async({page})=>{
  const {cancels}=await durable(page,{stop:true});await ask(page);
  await expect(page.locator('.nr-stop')).toBeVisible();await page.locator('.nr-stop').click();
  await expect.poll(()=>cancels.length).toBe(1);await expect(page.locator('#investigation-status')).toContainText('cancellation requested');
  await expect(page.locator('body')).toHaveAttribute('data-run','done');
});

test('terminal cancelled job without final SSE stops reconnecting and exposes its saved status',async({page})=>{
  const {subscribes}=await durable(page,{drop:true,terminalNoFinal:true});await ask(page);
  await expect(page.locator('#answer')).toContainText('Investigation cancelled');
  await expect(page.locator('body')).toHaveAttribute('data-run','failed');
  expect(subscribes).toEqual(['0']);
});

test('server advertising persistence does not silently fall back when session creation fails',async({page})=>{
  const {sent}=await durable(page);
  await page.route(/\/api\/agent\/sessions$/,r=>r.fulfill({status:503,json:{detail:'storage unavailable'}}));
  await ask(page);await expect(page.locator('body')).toHaveAttribute('data-run','failed');
  await expect(page.locator('#investigation-status')).toContainText('Could not save the investigation');expect(sent).toEqual([]);
});

test('removing follow-up constraints sends explicit clears so persistent parents cannot restore them',async({page})=>{
  const {sent}=await durable(page);await ask(page);await expect(page.locator('body')).toHaveAttribute('data-run','done');
  await page.locator('#followup [data-context="time"]').click();
  await page.locator('#followup-input').fill('Describe only the current selected source');await page.locator('#followup-input').press('Enter');
  await expect.poll(()=>sent.length).toBe(2);
  expect(sent[1].context).toMatchObject({t_ms:null,t_range:null,zone:null,selection:null});
});

test('persisted Run details show typed observations and their source frame links',async({page})=>{
  await durable(page);const observation=frames().find(f=>f.event==='observation').data.observation;
  await page.route(/\/api\/agent\/runs\/r_durable$/,r=>r.fulfill({json:{run_id:'r_durable',q:'What do you observe?',context:{workspace:'warehouse',scene_key:SCENE},created:'2026-10-10T00:00:00Z',status:'done',final:'UNRESOLVED',elapsed_ms:1,evidence:[],claims:[],hits:[],steps:[{i:0,name:'observe_scene',input:{},output:{observation},elapsed_ms:1,evidence_ids:[]}],weave_url:null}}));
  await page.goto(`#/run/r_durable`);await expect(page.locator('#runpage .rp-observation')).toContainText('worker stands');
  await expect(page.locator('#runpage .rp-observation img')).toHaveCount(0);
  await page.locator('#runpage [data-observed-frame]').click();
  await expect(page.locator('#runpage')).toBeHidden();
  await expect.poll(()=>page.evaluate(async()=> (await import(new URL('kernel/clock.js',document.baseURI).href)).clock.t)).toBe(2800);
  await expect(page.locator('.wh-stage-video .wh-vcell[data-cam="eye_00"]')).toHaveCount(1);
});


test('browsing another investigation preserves the active turn until a saved turn is opened',async({page})=>{
  const {sent}=await durable(page);await ask(page);await expect(page.locator('body')).toHaveAttribute('data-run','done');
  const other={session_id:'s_other',revision:0,title:'Other investigation',turns:[]};
  await page.route(/\/api\/agent\/sessions$/,r=>r.fulfill({json:{sessions:[other]}}));
  await page.route(/\/api\/agent\/sessions\/s_other$/,r=>r.fulfill({json:other}));
  await page.locator('#investigations-btn').click();await page.locator('[data-session="s_other"]').click();
  await expect(page.locator('[aria-label="Investigation turns"]')).toContainText('Ask a question to begin.');
  await page.locator('[data-close]').click();
  await page.locator('#followup-input').fill('What else is visible?');await page.locator('#followup-input').press('Enter');
  await expect.poll(()=>sent.length).toBe(2);expect(sent[1]).toMatchObject({session_id:SID,parent_turn_id:TID});
});


test('rejected follow-up exposes the request error instead of replaying its previous job',async({page})=>{
  const {subscribes}=await durable(page);await ask(page);await expect(page.locator('body')).toHaveAttribute('data-run','done');
  await page.route(/\/api\/agent\/ask$/,r=>r.fulfill({status:409,json:{detail:'Investigation revision changed; reload its history'}}));
  await page.locator('#followup-input').fill('What changed?');await page.locator('#followup-input').press('Enter');
  await expect(page.locator('body')).toHaveAttribute('data-run','failed');
  await expect(page.locator('#answer')).toContainText('Investigation revision changed');expect(subscribes).toEqual([]);
});


test('new scene questions preserve the camera visibly selected before the ask',async({page})=>{
  const {sent}=await durable(page);await page.goto(`#/warehouse/${SCENE}`);await expect(page.locator('.wh')).toBeVisible();
  await page.getByRole('button',{name:'View from eye_00',exact:true}).click();
  await page.locator('#ask-input').fill('Describe this camera view');await page.locator('#ask-input').press('Enter');
  await expect.poll(()=>sent.length).toBe(1);expect(sent[0].context).toMatchObject({scene_key:SCENE,cam_id:'eye_00'});
});
