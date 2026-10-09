// Contract-level mock of the app API, served from fixtures/, so page specs run before (or without) the real backend.
// Usage: const { mockApi, mockAsk } = require('./mockapi'); await mockApi(page); await mockAsk(page, 'wh-main');
// Skipped when SVA_E2E_LIVE=1. Mirrors CONTRACT §6.2 response shapes; the real app is checked in W2 integration.
const fs = require('fs');
const path = require('path');

const FX = path.resolve(__dirname, '..', '..', '..', 'fixtures');
const OUT = path.join(FX, 'sva-out');
const LIVE = process.env.SVA_E2E_LIVE === '1';
const ASKS = JSON.parse(fs.readFileSync(path.join(FX, 'asks.json'), 'utf8')).asks;
const read = (rel) => JSON.parse(fs.readFileSync(path.join(OUT, rel), 'utf8'));
const scenes = () => fs.readdirSync(OUT).filter((k) => fs.existsSync(path.join(OUT, k, 'manifest.json')));
const MIME = { '.json': 'application/json', '.geojson': 'application/geo+json', '.jpg': 'image/jpeg', '.mp4': 'video/mp4', '.bin': 'application/octet-stream' };
const actions = [], zones = [];

// wh-dodge: wh-main as the agent streams it: the moment's person is highlighted first, then the ground distance to
// the forklift is measured and both are highlighted.
const MAIN = ASKS.find((a) => a.ask_id === 'wh-main');
ASKS.push({ ...MAIN, ask_id: 'wh-dodge', events: MAIN.events.flatMap((f) => {
  if (f.event === 'ui' && f.data.cmd === 'highlight') return [{ ...f, data: { ...f.data, args: { ...f.data.args, ids: ['P1'] } } }];
  if (f.event !== 'tool_result' || !f.data.evidence.some((e) => e.method === 'ground_distance_tri')) return [f];
  return [
    { ...f, data: { ...f.data, evidence: f.data.evidence.map((e) => ({ ...e, method: 'ground_distance' })) } },
    { event: 'ui', data: { run_id: f.data.run_id, cmd: 'highlight', args: { ids: ['P1', 'F1'], mode: 'outline' } } },
  ];
}) });

// wh-vss: wh-main on site, where VSS's own /search result arrives after status, before done.
const VSS_SQL = '-- Text branch\nSELECT source, similarity FROM "vss-collection" ORDER BY similarity DESC LIMIT 100';
ASKS.push({ ...MAIN, ask_id: 'wh-vss', events: MAIN.events.flatMap((f) => f.event !== 'status' ? [f] : [
  f,
  { event: 'vss_native', data: { run_id: f.data.run_id, status: 'ok', elapsed_ms: 3812, sql_query: VSS_SQL, hits: [
    { rank: 1, source: 's3://team-38-vss-chunks-segments/segments/a_segment_001_of_006.mp4', scene_key: 'wh-sdg-fixture-01', cam_id: 'ceiling_04', t_ms: 4000, similarity: 0.412, caption: 'A worker steps back as a forklift approaches.' },
    { rank: 2, source: 's3://team-38-vss-chunks-segments/segments/b_segment_002_of_006.mp4', scene_key: null, cam_id: null, t_ms: null, similarity: 0.398, caption: 'Pallets are stacked along an aisle.' },
  ] } },
]) });

// wh-steer: wh-main where the agent also moves the camera (ui focus) before its seek and highlight.
ASKS.push({ ...MAIN, ask_id: 'wh-steer', events: MAIN.events.flatMap((f, i) => f.event === 'ui' && MAIN.events.findIndex((x) => x.event === 'ui') === i
  ? [{ event: 'ui', data: { run_id: f.data.run_id, cmd: 'focus', args: { entity_id: 'P1', ms: 600 } } }, f] : [f]) });

// The trigger's hazard zone: event participants at t_min_ms (else t0_ms) averaged and buffered by 3 m.
function hazardZone(req, n) {
  const key = req.scene_key, id = req.event_id.split(':').pop();
  const ev = read(`${key}/events.json`).events.find((e) => e.id === id);
  const t = ev.params.t_min_ms ?? ev.t0_ms;
  const pts = read(`${key}/tracks.json`).tracks.filter((tr) => ev.actor_ids.includes(tr.entity_id))
    .map((tr) => tr.pts[nearest(tr.pts.map((p) => p[0]), t)]);
  const [cx, cy] = [1, 2].map((k) => pts.reduce((s, p) => s + p[k], 0) / pts.length);
  const polygon_world = Array.from({ length: 24 }, (_, i) => [cx + 3 * Math.cos((Math.PI * i) / 12), cy + 3 * Math.sin((Math.PI * i) / 12)]);
  return { zone_id: `Z${n}`, scene_key: key, kind: 'hazard', polygon_world, source_event_id: req.event_id, action_id: `a${n}`, t_range: [ev.t0_ms, ev.t1_ms], created: '2026-10-09T00:00:00Z' };
}

function sseBody(askId, from = 0, to = undefined) {
  const ask = ASKS.find((a) => a.ask_id === askId);
  if (!ask) throw new Error(`unknown ask ${askId}`);
  return ask.events.slice(from, to).map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');
}

function nearest(ts, t) {
  let best = -1, bd = Infinity;
  ts.forEach((v, i) => { const d = Math.abs(v - t); if (d < bd) { bd = d; best = i; } });
  return best;
}

function moment(key, t) {
  const m = read(`${key}/manifest.json`);
  const persons = [];
  if (m.assets.skeleton) {
    for (const [id, p] of Object.entries(read(`${key}/${m.assets.skeleton}`).persons)) {
      const i = nearest(p.t_ms, t);
      if (i < 0 || Math.abs(p.t_ms[i] - t) > 150) continue;
      persons.push({ entity_id: id, root_world: p.root_world[i], root_method: p.root_method[i], sigma_m: p.root_sigma_m[i],
        xyz17: p.xyz[i], seen_by: [], observed_in: [] });
    }
  }
  const forklift = [];
  if (m.assets.forklift) {
    for (const f of read(`${key}/${m.assets.forklift}`).forklifts) {
      const i = nearest(f.track.map((r) => r.t_ms), t);
      if (i >= 0) { const r = f.track[i]; forklift.push({ entity_id: f.entity_id, x: r.x, y: r.y, heading: r.heading, speed: r.speed, footprint: r.footprint, sigma_m: r.sigma_m, source: r.source }); }
    }
  }
  const s = Math.floor(t / 1000).toString().padStart(5, '0');
  const cams = m.cams.map((c) => ({ id: c.id, frame_url: c.thumbs && fs.existsSync(path.join(OUT, key, c.thumbs, `${s}.jpg`)) ? `api/assets/${key}/${c.thumbs}/${s}.jpg` : null }));
  return { t_ms: t, persons, forklift, floor_visibility_ref: m.assets.visibility ? `api/assets/${key}/${m.assets.visibility}` : null, cams };
}

async function mockApi(page) {
  if (LIVE) return;
  await page.route(/\/api\//, async (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname.replace(/^.*?\/api\//, '');
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    let m;
    if (p === 'workspaces') {
      const ws = { warehouse: [], fleet: [] };
      for (const k of scenes()) { const mf = read(`${k}/manifest.json`); ws[mf.workspace].push({ scene_id: mf.scene_id, scene_key: k, title: mf.title, t0_ms: mf.t0_ms, t1_ms: mf.t1_ms, thumb: null, n_events: mf.assets.events ? read(`${k}/${mf.assets.events}`).events.length : 0 }); }
      return json([{ id: 'warehouse', title: 'Warehouse', scenes: ws.warehouse }, { id: 'fleet', title: 'Fleet', scenes: ws.fleet }]);
    }
    if ((m = p.match(/^scene\/([a-z0-9_-]+)\/(manifest|timeline|entities|moment)$/))) {
      const [, key, what] = m;
      const mf = read(`${key}/manifest.json`);
      if (what === 'manifest') return json(mf);
      if (what === 'timeline') return json({ ...read(`${key}/${mf.assets.timeline}`), events: read(`${key}/${mf.assets.events}`).events });
      if (what === 'entities') return json(read(`${key}/${mf.assets.entities}`));
      return json(moment(key, Number(url.searchParams.get('t') || 0)));
    }
    if (p === 'model-results') {
      const key = url.searchParams.get('scene_key'), t = Number(url.searchParams.get('t_ms')), w = Number(url.searchParams.get('window_ms') || 3000);
      const mf = read(`${key}/manifest.json`);
      return json(mf.assets.cosmos ? read(`${key}/${mf.assets.cosmos}`).records.filter((r) => Math.abs(r.t_ms - t) <= w) : []);
    }
    if (p === 'actions' && route.request().method() === 'GET') {
      const k = url.searchParams.get('scene_key');
      return json({ actions: actions.filter((a) => a.scene_key === k), zones: zones.filter((z) => z.scene_key === k) });
    }
    if (p === 'actions/trigger') {
      const req = route.request().postDataJSON();
      const hit = actions.find((a) => a.scene_key === req.scene_key && a.event_id && a.event_id === req.event_id);
      if (hit) return json({ action_id: hit.action_id, delivered: true, review_task_id: hit.review_task_id, zone_id: hit.zone_id, existing: true });
      const n = actions.length + 1;
      actions.push({ ...req, action_id: `a${n}`, review_task_id: `RT-${n}`, zone_id: `Z${n}`, delivered: true });
      if (req.event_id && req.actions.includes('hazard_zone')) zones.push(hazardZone(req, n));
      return json({ action_id: `a${n}`, delivered: true, review_task_id: `RT-${n}`, zone_id: `Z${n}`, existing: false });
    }
    if (p === 'fleet/trips') {
      return json(scenes().map((k) => read(`${k}/manifest.json`)).filter((mf) => mf.workspace === 'fleet').map((mf) => ({
        trip_id: mf.scene_key, title: mf.title, t0_ms: mf.t0_ms, t1_ms: mf.t1_ms, n_events: mf.assets.events ? read(`${mf.scene_key}/${mf.assets.events}`).events.length : 0,
        geojson_url: `api/assets/${mf.scene_key}/${mf.assets.trip}`, full: mf.cams.length > 0 })));
    }
    if ((m = p.match(/^fleet\/trip\/([a-z0-9_-]+)\/moment$/))) {
      const t = Number(url.searchParams.get('t') || 0);
      const frames = read(`${m[1]}/ego/${Math.floor(t / 10000).toString().padStart(5, '0')}.json`).frames;
      const f = frames[nearest(frames.map((x) => x.t_ms), t)];
      return json({ ...f, place: null, gps_sigma_m: f.ego.sigma_m });
    }
    if ((m = p.match(/^fleet\/building\/([a-z0-9_]+)$/))) {
      for (const k of scenes()) { const mf = read(`${k}/manifest.json`); if (mf.assets.buildings) { const b = read(`${k}/${mf.assets.buildings}`).buildings[m[1]]; if (b) return json({ ...b, bid: m[1], facade_note: null }); } }
      return json({ detail: 'not found' }, 404);
    }
    if ((m = p.match(/^agent\/runs\/(r_[0-9a-z]+)$/))) {
      const f = path.join(FX, 'runs', `${m[1]}.json`);
      return fs.existsSync(f) ? json(JSON.parse(fs.readFileSync(f, 'utf8'))) : json({ detail: 'not found' }, 404);
    }
    if (p === 'agent/ask') return route.fallback();
    if ((m = p.match(/^assets\/(.+)$/))) {
      const f = path.join(OUT, m[1]);
      if (!f.startsWith(OUT) || !fs.existsSync(f)) return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ status: 200, contentType: MIME[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
    }
    return json({ detail: `mockapi: no route for ${p}` }, 404);
  });
}

async function mockAsk(page, askId) {
  if (LIVE) return;
  await page.route(/\/api\/agent\/ask$/, (route) =>
    route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' }, body: sseBody(askId) }));
}

// Streams the ask in the page (route.fulfill cannot hold back part of a body): frames from the first `held` event on
// are sent only after window.__releaseAsk() is called.
async function mockAskHeld(page, askId, held) {
  if (LIVE) return;
  const k = ASKS.find((a) => a.ask_id === askId).events.findIndex((f) => f.event === held);
  await page.addInitScript(([head, tail]) => {
    const fetch0 = window.fetch;
    const gate = new Promise((resolve) => (window.__releaseAsk = resolve));
    window.fetch = (url, init) => {
      if (!String(url).endsWith('api/agent/ask')) return fetch0(url, init);
      const enc = new TextEncoder();
      const body = new ReadableStream({ async start(c) { c.enqueue(enc.encode(head)); await gate; c.enqueue(enc.encode(tail)); c.close(); } });
      return Promise.resolve(new Response(body, { headers: { 'content-type': 'text/event-stream' } }));
    };
  }, [sseBody(askId, 0, k), sseBody(askId, k)]);
}

module.exports = { ASKS, sseBody, mockApi, mockAsk, mockAskHeld, hazardZone };
