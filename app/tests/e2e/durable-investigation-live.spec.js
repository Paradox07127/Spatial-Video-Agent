// Exercise real protocol negotiation and durable storage, while fixture assets
// keep the browser independent from external maps and model endpoints.
const {test,expect}=require('@playwright/test');
const {mockApi}=require('./mockapi');


async function realInvestigationApi(page) {
  await mockApi(page);
  await page.route(/\/health$/,route=>route.continue());
  await page.route(/\/api\/agent\/(?:sessions|jobs|ask)(?:\/|\?|$)/,route=>route.continue());
}

test('fresh browser opens an empty investigation list without an active local session',async({page})=>{
  await realInvestigationApi(page);
  await page.route(/\/api\/agent\/sessions$/,route=>route.fulfill({json:{sessions:[]}}));
  await page.goto('#/');
  expect(await page.evaluate(()=>localStorage.getItem('sva.investigation'))).toBeNull();
  await page.locator('#investigations-btn').click();
  await expect(page.locator('#investigations')).toContainText('No investigations yet.');
  await expect(page.locator('#investigations [role="alert"]')).toHaveCount(0);
  await expect(page.locator('#investigations [data-new]')).toBeVisible();
});

test('fresh browser browses an API-created session without creating local current state',async({page},testInfo)=>{
  await realInvestigationApi(page);
  const endpoint=new URL('api/agent/sessions',testInfo.project.use.baseURL).href;
  const response=await page.request.post(endpoint,{data:{title:`Created outside browser ${testInfo.project.name}`}});
  expect(response.status()).toBe(201);const created=await response.json();
  await page.goto('#/');
  expect(await page.evaluate(()=>localStorage.getItem('sva.investigation'))).toBeNull();
  await page.locator('#investigations-btn').click();
  const saved=page.locator(`#investigations [data-session="${created.session_id}"]`);
  await expect(saved).toBeVisible();await saved.click();
  await expect(page.locator('#investigations [aria-label="Investigation turns"]')).toContainText('Ask a question to begin.');
  await expect(page.locator('#investigations [role="alert"]')).toHaveCount(0);
  expect(await page.evaluate(()=>localStorage.getItem('sva.investigation'))).toBeNull();
});

test('actual v2 backend persists and replays a browser investigation across refresh',async({page})=>{
  await realInvestigationApi(page);
  const question="Where is P1 at 4 seconds?";
  await page.goto('#/warehouse/wh-sdg-fixture-01');
  await expect(page.locator('.wh')).toBeVisible();
  const request=page.waitForRequest(r=>/\/api\/agent\/ask$/.test(r.url())&&r.method()==='POST');
  const response=page.waitForResponse(r=>/\/api\/agent\/ask$/.test(r.url()));
  await page.locator('#ask-input').fill(question);await page.locator('#ask-input').press('Enter');
  const submitted=(await request).postDataJSON();expect(submitted.protocol_version).toBe(2);
  const headers=(await response).headers();
  expect(headers['x-sva-session']).toBe(submitted.session_id);expect(headers['x-sva-job']).toMatch(/^j_/);
  await expect(page.locator('body')).toHaveAttribute('data-run','done',{timeout:20000});
  const endpoint=new URL(`api/agent/sessions/${submitted.session_id}`,page.url()).href;
  await expect.poll(async()=> (await (await page.request.get(endpoint)).json()).turns[0]?.status).toBe('done');
  const history=await (await page.request.get(endpoint)).json();
  expect(history.turns).toHaveLength(1);expect(history.turns[0]).toMatchObject({q:question,status:'done',final:'SUPPORTED',job_id:headers['x-sva-job']});
  const numericRefs=await page.locator('#answer .ans-num').count();expect(numericRefs).toBeGreaterThan(0);
  const answer=await page.locator('#answer').innerText();
  await page.reload();
  await expect(page.locator('body')).toHaveAttribute('data-run','done',{timeout:20000});
  await expect.poll(()=>page.locator('#answer').innerText()).toBe(answer);
  await expect(page.locator('#answer .ans-num')).toHaveCount(numericRefs);
  await page.locator('#investigations-btn').click();
  await expect(page.locator(`[data-turn="${history.turns[0].turn_id}"]`)).toContainText(question);
});
