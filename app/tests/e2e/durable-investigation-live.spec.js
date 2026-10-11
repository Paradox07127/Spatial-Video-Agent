// Exercise real protocol negotiation and durable storage, while fixture assets
// keep the browser independent from external maps and model endpoints.
const {test,expect}=require('@playwright/test');
const {mockApi}=require('./mockapi');

test('actual v2 backend persists and replays a browser investigation across refresh',async({page})=>{
  await mockApi(page);
  await page.route(/\/health$/,route=>route.continue());
  await page.route(/\/api\/agent\/(?:sessions|jobs|ask)(?:\/|\?|$)/,route=>route.continue());
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
