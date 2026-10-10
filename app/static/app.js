// Shell: routing, Ask, the right-hand answer panel, the running narration and the timeline. Pages draw the stage (spec §9.1).
// Bus events the shell emits, besides the kernel's clock:* and selection:change:
//   route (route)               a route was mounted (home has workspace null)
//   view:state (view)           home | default | searching | hit | abstained
//   evidence:hover (eid|null)   the pointer entered / left an answer number, cite chip or evidence row
//   evidence:focus (evidence)   one of those was clicked; the clock is already at it
//   stage:mode ({mode, eid})    explore | evidence; Esc goes back to {mode: 'explore', eid: null}
//   agent:camera (bool)         false once the user takes over during a run; agent focus / setView commands are then dropped
//   panel:tab (name)            answer | evidence | steps | videos | trip
import { api } from './kernel/api.js';
import { exec, icon, level, say, source, srcIcon, srcLabel, transition, verdict } from './kernel/badges.js';
import { bus } from './kernel/bus.js';
import { clock } from './kernel/clock.js';
import { cmdk } from './kernel/cmdk.js';
import { count, esc, int, sec, span, tc } from './kernel/format.js';
import { modelpanel } from './kernel/modelpanel.js';
import { selection } from './kernel/selection.js';
import { askSSE } from './kernel/sse.js';
import { setState, state } from './kernel/state.js';
import { createTimeline } from './kernel/timeline.js';
import { trace } from './kernel/trace.js';
import { runpage } from './run/page.js';

const $ = (s) => document.querySelector(s);
const PAGES = { home: './home/page.js', warehouse: './warehouse/page.js', fleet: './fleet/page.js', street: './street/page.js', test: './kernel/_testpage.js' };
// Each one was answered (not abstained) on the deployed site; the first two fleet ones are place questions.
const PRESETS = {
  warehouse: ['When did the worker start to dodge the forklift, and how far was it?', 'How close did the forklift come to a person?', 'Was anyone where ceiling_00 cannot see in these 10 seconds?'],
  fleet: ['At Queen and Spadina, what is the building on the right?', 'At Avenue Road and Cumberland Street, what is the building on the right?', 'Was this car speeding?'],
  street: ['When does a pedestrian cross the street?', 'How many people walk past in this clip?', 'When is the street busiest in this clip?'],
};
// Presets asked from a chip on a route of their own: the fleet place questions search every trip. Others keep the current route.
const PRESET_ROUTE = new Map(PRESETS.fleet.slice(0, 2).map((q) => [q, '#/fleet']));
// Abstention code -> [why, what can be asked instead] (spec §8.3).
const ABSTAIN = {
  UNSUPPORTED: ['The question is of a kind that cannot be answered (identity, intent, blame).', 'Ask what can be measured instead: when, where, how far apart, whether it appears. For example'],
  NO_MATCH: ['No matching clip was found. That does not mean it did not happen.', 'Widen the time or place, or ask another way. For example'],
  OUT_OF_RANGE: ['The time or place asked about is outside the footage.', 'Press ⌘K to see the scenes and trips, then ask about a time or place in them. For example'],
};
const OPS = { lt: '<', le: '≤', gt: '>', ge: '≥', eq: '=' };
// The narration's six segments and the step states each one covers.
const SEGMENTS = [['Parse', ['parse']], ['Search', ['plan', 'retrieve']], ['Inspect', ['inspect']], ['Measure', ['measure']], ['Check', ['assess']], ['Answer', ['answer']]];
const segOf = (s) => SEGMENTS.findIndex(([, states]) => states.includes(s));
const CAMERA_CMDS = new Set(['focus', 'setView']);
const KINDS = ['measure', 'map', 'model', 'human'];
const LAST_ROUTE = 'sva.lastRoute';

const AGENT_LANE = { label: 'Agent looked', kind: 'agent' };
const timeline = createTimeline($('#timeline'), clock.range);
timeline.addLane('agent', AGENT_LANE);
const ctx = { clock, selection, bus, api, timeline, route: null, presets: PRESETS, workspaces: () => workspaces, ask: async (q, { ws, hash, preset } = {}) => {
  const corpus = !hash && !preset && !ws;
  ask(q, { home: corpus ? null : hash ?? (preset ? PRESET_ROUTE.get(q) : null) ?? await defaultRoute(ws), corpus });
} };
// Only workspaces that have a page template.
const sceneWorkspaces = new Map();
const workspaces = api.get('api/workspaces').then((ws) => {
  for (const w of ws) for (const s of w.scenes) sceneWorkspaces.set(s.scene_key, w.id);
  return ws.filter((w) => PAGES[w.id]);
});
let page = null, pageKind = null, dispose = null, stream = null, run = null;
let navigation = 0, runGeneration = 0, evidenceGeneration = 0, pageQueue = Promise.resolve(), mountQueue = Promise.resolve(), applying = Promise.resolve(), pageContext = null;
const narration = $('#narration'), feedback = $('#ask-status');
const STALE = new Error('Superseded page command');
let wsHash = null; // the route the run page opens over
// Agent frames for the page wait, in arrival order, until the page they are for has mounted (null if it failed to load).
// hold() makes later frames wait for the next mount; a hold still pending follows the newer one.
let mounted = Promise.resolve(null), settle = null;
function hold() {
  const prev = settle;
  let done;
  mounted = new Promise((r) => (done = settle = r));
  prev?.(mounted);
  return done;
}
const toPage = (fn) => {
  const r = run, generation = runGeneration, ready = mounted;
  const owns = () => r === run && generation === runGeneration;
  pageQueue = pageQueue.then(async () => {
    const p = await ready;
    if (!p || p !== page || pageContext?.generation !== navigation || !owns()) return;
    const generation = navigation;
    const valid = () => owns() && generation === navigation && p === page;
    const c = pageContext;
    c.owner = valid;
    try {
      applying = Promise.resolve(fn(p, valid));
      await applying;
    } finally {
      c.owner = null;
    }
  }).catch((e) => {
    if (e !== STALE && owns()) statusLine(`Evidence could not load: ${e.message}. Please retry.`);
  });
  return pageQueue;
};
// A fleet overview accepts trips; a scene route owns exactly its scene. Legacy frames stay with the ask's source.
const here = (key) => ctx.route.page === 'test' || (ctx.route.page === 'fleet' && !ctx.route.scene_key
  ? !key || sceneWorkspaces.get(key) === 'fleet' : key ? key === ctx.route.scene_key : !ctx.route.scene_key);

// Page reads started by a command retain its ownership even when the page does not await the read itself.
function mountContext(route, generation) {
  const c = { ...ctx, route, generation, owner: null };
  c.api = { ...api, get(path) {
    const owner = c.owner;
    if (!owner) return api.get(path);
    return api.get(path).then((value) => owner() ? value : Promise.reject(STALE));
  } };
  return c;
}
// e2e hook: the current run.
export const currentRun = () => run;

const stored = (k) => {
  try {
    return localStorage.getItem(k);
  } catch {
    return null;
  }
};
const store = (k, v) => {
  try {
    localStorage.setItem(k, v);
  } catch {
    // not remembered across reloads; the session still has state.sourceTag
  }
};
const PANEL_W_KEY = 'sva.panelWidth';
const clampPanelWidth = (w) => Math.min(Math.round(window.innerWidth * (2 / 3)), Math.max(Math.round(window.innerWidth / 3), w));

function initPanelResizer() {
  const handle = $('#panel-resizer');
  if (!handle) return;
  const saved = stored(PANEL_W_KEY);
  if (saved) {
    const num = parseFloat(saved);
    if (!Number.isNaN(num)) {
      const w = clampPanelWidth(num);
      document.documentElement.style.setProperty('--panel-w', `${w}px`);
    }
  }

  let dragging = false;
  handle.addEventListener('pointerdown', (e) => {
    dragging = true;
    handle.setPointerCapture(e.pointerId);
  });
  handle.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const w = clampPanelWidth(window.innerWidth - e.clientX);
    document.documentElement.style.setProperty('--panel-w', `${w}px`);
    store(PANEL_W_KEY, String(w));
  });
  const stopDrag = (e) => {
    if (!dragging) return;
    dragging = false;
    try { handle.releasePointerCapture(e.pointerId); } catch {}
  };
  handle.addEventListener('pointerup', stopDrag);
  handle.addEventListener('pointercancel', stopDrag);

  handle.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      const cur = $('#panel').getBoundingClientRect().width;
      const delta = e.key === 'ArrowLeft' ? 16 : -16;
      const w = clampPanelWidth(cur + delta);
      document.documentElement.style.setProperty('--panel-w', `${w}px`);
      store(PANEL_W_KEY, String(w));
    }
  });

  window.addEventListener('resize', () => {
    const cur = $('#panel')?.getBoundingClientRect().width;
    if (cur) {
      const w = clampPanelWidth(cur);
      document.documentElement.style.setProperty('--panel-w', `${w}px`);
    }
  });
}
const shortKey = (key) => key.replace(/^fl-pie-(set\d+)_video_(\d+)$/, '$1·$2');
const mmss = (ms) => tc(ms).slice(0, 5);

// ---- routing: #/ home; #/warehouse/<scene_key>, #/fleet, #/fleet/<trip_id>@<t_ms>, #/street/<scene_key>[@<t_ms>], #/test;
// #/run/<run_id> and #/runs open over them
function parseRoute(hash) {
  if (hash == null) return null;
  let m;
  if (hash === '' || hash === '#/') return { workspace: null, page: 'home', scene_key: null };
  if ((m = hash.match(/^#\/warehouse\/([a-z0-9_-]+)$/))) return { workspace: 'warehouse', page: 'warehouse', scene_key: m[1] };
  if ((m = hash.match(/^#\/street\/([a-z0-9_-]+)(?:@(\d+))?$/))) return { workspace: 'street', page: 'street', scene_key: m[1], t_ms: m[2] ? +m[2] : null };
  if ((m = hash.match(/^#\/fleet\/([a-z0-9_-]+)@(\d+)$/))) return { workspace: 'fleet', page: 'fleet', scene_key: m[1], trip_id: m[1], t_ms: +m[2] };
  if (hash === '#/fleet') return { workspace: 'fleet', page: 'fleet', scene_key: null };
  if (hash === '#/test') return { workspace: 'warehouse', page: 'test', scene_key: null };
  return null;
}

async function firstWarehouse() {
  const s = (await workspaces).find((w) => w.id === 'warehouse').scenes[0];
  return s ? `#/warehouse/${s.scene_key}` : '#/fleet';
}

async function firstStreet() {
  const s = (await workspaces).find((w) => w.id === 'street')?.scenes[0];
  return s ? `#/street/${s.scene_key}` : firstWarehouse();
}

// The route a question without a chosen source runs against: the last workspace route (of workspace ws, if given), else the first one.
async function defaultRoute(ws) {
  const last = state.sourceTag?.hash ?? stored(LAST_ROUTE);
  const r = last && parseRoute(last);
  if (r?.workspace && (!ws || r.workspace === ws)) return last;
  return ws === 'fleet' ? '#/fleet' : ws === 'street' ? firstStreet() : firstWarehouse();
}

async function resetTimeline(sceneKey, ws, generation) {
  const street = ws === 'street';
  const [mf, tl] = sceneKey ? await Promise.all([api.get(`api/scene/${sceneKey}/manifest`), street ? null : api.get(`api/scene/${sceneKey}/timeline`)]) : [];
  // A street scene has no timeline asset: one coverage lane per camera from its video chunks, and no events.
  const lanes = street
    ? Object.fromEntries(await Promise.all(mf.cams.map(async (c) => [c.id, (await api.get(`api/street/${sceneKey}/chunks?cam=${encodeURIComponent(c.id)}`)).map((k) => [k.t0_ms, k.t1_ms, 'ok'])])))
    : tl?.lanes;
  if (generation !== navigation) return;
  timeline.clear();
  if (sceneKey) {
    clock.bind({ kind: 'scene' });
    clock.setRange(mf.t0_ms, mf.t1_ms);
    for (const [cam, items] of Object.entries(lanes)) {
      timeline.addLane(`cam:${cam}`, { label: cam, kind: 'coverage' });
      timeline.setLaneData(`cam:${cam}`, items);
    }
    timeline.setEvents(tl?.events ?? []);
  }
  timeline.addLane('agent', AGENT_LANE);
  timeline.setLaneData('agent', run?.lane ?? []);
}

async function onRoute() {
  const m = location.hash.match(/^#\/run(?:s|\/(r_[0-9a-z]+))$/);
  if (m) {
    if (!ctx.route) await mountWorkspace(await defaultRoute());
    return runpage.open(m[1] ?? null, m[1] && m[1] === run?.id ? run.summaries : undefined);
  }
  const back = runpage.isOpen() && location.hash === wsHash;
  runpage.close();
  if (back) return;
  if (!parseRoute(location.hash)) return location.replace('#/');
  await mountWorkspace(location.hash);
}

// Opens a route without waiting for hashchange, so the frames handled right after it already wait for the new page.
function go(hash, evidence = false) {
  history.pushState(null, '', hash);
  runpage.close();
  return mountWorkspace(hash, evidence);
}

async function mountWorkspace(hash, evidence = false) {
  const route = parseRoute(hash), generation = ++navigation;
  wsHash = hash;
  const changed = ctx.route?.page !== route.page || ctx.route?.scene_key !== route.scene_key;
  if (changed && !evidence) {
    $('#ask-input').value = '';
    toDefault();
    selection.clear();
  }
  ctx.route = route;
  if (evidence && run) {
    run.activeScene = route.scene_key;
    renderModels();
  }
  document.body.dataset.ws = route.workspace ?? '';
  if (route.page === 'home') setState('home');
  else {
    if (state.view === 'home') setState(run ? 'searching' : 'default');
    state.sourceTag = { hash, workspace: route.workspace, scene_key: route.scene_key, trip_id: route.trip_id ?? null };
    store(LAST_ROUTE, hash);
    renderSourceTag();
    $('#presets').innerHTML = PRESETS[route.workspace].map((q) => `<button class="preset">${esc(q)}</button>`).join('');
  }
  bus.emit('route', route);
  const done = hold();
  restoreFeedback();
  $('#stage').style.visibility = 'hidden';
  // Page modules keep module-local state, so mounts and commands must finish before another mount starts.
  mountQueue = mountQueue.then(async () => {
    await applying.catch(() => {});
    if (generation !== navigation) return;
    restoreFeedback();
    dispose?.();
    page = dispose = pageKind = pageContext = null;
    const stage = $('#stage');
    const host = document.createElement('div');
    host.style.display = 'contents';
    stage.replaceChildren(host);
    stage.style.visibility = '';
    let off = null;
    try {
      await resetTimeline(route.page === 'warehouse' || route.page === 'street' ? route.scene_key : null, route.workspace, generation);
      if (generation !== navigation) return;
      const p = await import(PAGES[route.page]);
      if (generation !== navigation) return;
      const c = mountContext(route, generation);
      off = await p.mount(host, c);
      if (generation !== navigation) {
        off?.();
        off = null;
        return;
      }
      page = p;
      pageKind = route.page;
      pageContext = c;
      dispose = off;
      done(p);
      if (route.page === 'home') homeFeedback();
      else if (run) {
        timeline.setEvidence(evMarks());
        renderModels();
      }
    } catch (e) {
      off?.();
      host.replaceChildren();
      if (generation !== navigation) return;
      page = dispose = pageKind = pageContext = null;
      done(null);
      stage.innerHTML = `<p class="stage-msg"><span>Page not loaded: ${esc(e.message)}. <button type="button" class="preset page-retry">Retry</button></span></p>`;
      stage.querySelector('.page-retry').onclick = () => mountWorkspace(hash, true);
      statusLine(`Page not loaded: ${e.message}. Please retry.`);
    }
  });
  await mountQueue;
  return generation === navigation ? page : null;
}

function restoreFeedback() {
  $('.stage-wrap').append(narration);
  $('#panel').append(feedback);
}

function homeFeedback() {
  const out = $('.home-out');
  if (out) out.append(feedback, narration);
}

function completion(d) {
  const label = d.stop_reason === 'error' ? `Investigation failed${d.error ? `: ${typeof d.error === 'string' ? d.error : d.error.message ?? 'Backend error'}` : ''}. Please retry.`
    : d.stop_reason === 'budget_exhausted' ? 'Budget exhausted; investigation incomplete. Ask a narrower question or a follow-up to continue.'
    : d.stop_reason === 'abstained' || ABSTAIN[d.final] ? 'Done · Abstained. Try another question or source.' : 'Done';
  return `${label} · ${count(run.steps.length, 'step')} · ${sec(took())}${d.cached ? ' · cached replay' : ''}`;
}

function renderModels() {
  const records = ctx.route.page === 'test' ? [...(run?.models.values() ?? [])].flatMap((rs) => [...rs.values()])
    : [...(run?.models.get(run?.activeScene ?? ctx.route.scene_key ?? run?.source)?.values() ?? [])];
  if (records.length) modelpanel.render(records, 'summary');
  else $('#modelpanel').replaceChildren();
}

function queueEvidence(key, fn) {
  const item = { key, fn, applied: null };
  run.queue.push(item);
  return replayEvidence([item]);
}

function replayEvidence(items = run.queue) {
  for (const item of items) toPage(async (p, owns) => {
    if (!here(item.key) || item.applied === navigation) return;
    await item.fn(p, owns);
    if (owns()) item.applied = navigation;
  });
  return pageQueue;
}

async function activateEvidence(e, after = null, automatic = false) {
  const r = run, generation = runGeneration, activation = ++evidenceGeneration;
  const ws = (await workspaces).find((w) => w.scenes.some((s) => s.scene_key === e.scene_key));
  if (r !== run || generation !== runGeneration || activation !== evidenceGeneration || !ws) return;
  const hash = ws.id === 'fleet' ? `#/fleet/${e.scene_key}@${e.t0_ms}` : `#/${ws.id}/${e.scene_key}`;
  const changed = ctx.route.page !== 'test' && (ctx.route.workspace !== ws.id || ctx.route.scene_key !== e.scene_key);
  if (changed) {
    selection.clear();
    setStage('explore', null);
    r.home = hash;
    const p = await go(hash, true);
    if (!p || r !== run || generation !== runGeneration || activation !== evidenceGeneration) return;
  }
  if (!page) return;
  r.activeScene = e.scene_key;
  setState('hit');
  document.body.dataset.candidates = '';
  renderModels();
  if (document.body.dataset.run === 'done') { renderTabs(); renderFollowup(); }
  if (automatic) return replayEvidence();
  const m = run.moments.find((m) => m === e || m.scene_key === e.scene_key && m.t0_ms <= e.t0_ms && e.t0_ms < m.t1_ms);
  await toPage((p, owns) => m && p.applyHits([m], owns));
  await replayEvidence();
  await toPage(() => {
    if (activation !== evidenceGeneration) return;
    clock.seek(clock.toWall(e.scene_key, e.t0_ms));
    after?.();
  });
}

function renderSourceTag() {
  const r = ctx.route;
  const label = r.trip_id ? shortKey(r.trip_id) : r.scene_key ?? (r.page === 'fleet' ? 'all trips' : r.page);
  $('#source-tag').innerHTML = `${icon({ fleet: 'truck', street: 'camera' }[r.workspace] ?? 'warehouse')}${esc(label)}${icon('chevron-down')}`;
}

// ---- layer state
function setTab(name) {
  const changed = state.panelTab !== name;
  state.panelTab = name;
  document.querySelectorAll('#tabs [data-tab]').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  document.querySelectorAll('.tab-pane').forEach((p) => (p.hidden = p.id !== `tab-${name}`));
  if (changed) bus.emit('panel:tab', name);
}

function setStage(mode, eid) {
  if (state.stageMode === mode && state.focusEid === eid) return;
  state.stageMode = mode;
  state.focusEid = eid;
  document.body.dataset.stage = mode;
  document.querySelectorAll('.ev').forEach((r) => r.classList.toggle('sel', r.dataset.eid === eid));
  bus.emit('stage:mode', { mode, eid });
}

function setAgentCamera(on) {
  $('#takeover').hidden = !on;
  if (state.agentCamera === on) return;
  state.agentCamera = on;
  bus.emit('agent:camera', on);
}

function hover(eid) {
  if (state.hoverEid === eid) return;
  document.querySelectorAll('.hover[data-eid]').forEach((el) => el.classList.remove('hover'));
  state.hoverEid = eid;
  if (eid) document.querySelectorAll(`[data-eid="${CSS.escape(eid)}"]`).forEach((el) => el.classList.add('hover'));
  bus.emit('evidence:hover', eid);
}

// ---- ask
function toDefault() {
  runGeneration++;
  run?.held?.(null);
  stream?.abort();
  stream = run = null;
  if (state.view !== 'home') setState('default');
  document.body.dataset.run = '';
  document.body.dataset.candidates = '';
  setStage('explore', null);
  setTab('answer');
  for (const s of ['#answer', '#tabs', '#keyframe', '#claims', '#modelpanel', '#moments', '#tab-evidence', '#tab-steps', '#narration', '.p-moments-head']) $(s).replaceChildren();
  $('.p-moments').classList.remove('open');
  statusLine('');
  timeline.setLaneData('agent', []);
  timeline.setEvidence([]);
}

// Evidence without a camera is multi-camera in the warehouse and trip-level (GPS, OBD, map) in the fleet.
const TRIP_SOURCE = { obd_speed: 'OBD', obd_over_limit: 'OBD', osm_maxspeed: 'map limit', gps_pass: 'GPS pass', searched_passes: 'checked span', geocode: 'place name' };
const SCENE_SOURCE = { closest_approach: 'track scan' };
const camLabel = (e) => e.cam_id ?? (ctx.route?.workspace === 'fleet' ? TRIP_SOURCE[e.method] ?? 'trip' : SCENE_SOURCE[e.method] ?? 'multi-cam');
const camsOf = (e) => [...new Set(e.observation_ids?.length ? e.observation_ids.map((o) => o.cam_id) : e.cam_id ? [e.cam_id] : [])];

function statusLine(text) {
  $('#ask-status').textContent = text;
}

const took = () => run.steps.at(-1)?.elapsed_ms ?? 0;

// home: the route a question asked from the home page runs against; it opens on the first hits (or an answer that is not a refusal).
function ask(q, { home = null, t_ms = null, corpus = false } = {}) {
  q = q.trim();
  if (!q) return;
  toDefault();
  const route = home ? parseRoute(home) : ctx.route;
  $('#ask-input').value = q;
  run = {
    id: null, q, home, source: route.scene_key, held: ctx.route.page === 'home' ? hold() : null, steps: [], lastStep: null, reached: -1, items: [], changes: [], summaries: new Map(),
    evidence: new Map(), evCall: new Map(), claims: new Map(), cites: [], models: new Map(), queue: [], lane: [], answers: [], rawAnswers: [], calls: new Map(), moments: [], native: null, final: null,
    skippedUi: 0, corpus,
  };
  document.body.dataset.run = 'running';
  setAgentCamera(true);
  if (ctx.route.page !== 'home') setState('searching');
  else homeFeedback();
  statusLine('Sent, waiting for the agent…');
  renderSlot();
  renderNarration();
  const context = corpus ? { scope: 'corpus' } : { workspace: route.workspace, scene_key: route.scene_key };
  if (!corpus && selection.ids.size) context.selection = [...selection.ids];
  if (t_ms != null) context.t_ms = t_ms;
  const r = run, generation = runGeneration;
  stream = askSSE({ q, context }, Object.fromEntries(Object.entries(handlers).map(([event, fn]) => [event, (data) => {
    if (r === run && generation === runGeneration) fn(data);
  }])));
}

const sceneT = () => clock.map(clock.t)?.t_ms ?? clock.t;
const followUp = (q) => ask(q, ctx.route.page === 'home' ? { corpus: true } : { t_ms: sceneT() });

function stop() {
  runGeneration++;
  stream?.abort();
  stream = null;
  if (ctx.route.page === 'home') {
    run.held?.(null);
    document.body.dataset.run = '';
    statusLine('Stopped. Ask again to continue.');
    return;
  }
  finish();
  statusLine(`Stopped · ${count(run.steps.length, 'step')} · ${sec(took())}`);
  if (state.view === 'searching') setState('default');
}

const handlers = {
  step(d) {
    run.id = d.run_id;
    run.lastStep = d;
    run.steps.push(d);
    run.items.push({ step: d });
    run.summaries.set(d.i, d.summary);
    run.reached = Math.max(run.reached, segOf(d.state));
    statusLine(`${d.state} ${d.summary} ${sec(d.elapsed_ms)}`);
    renderNarration();
    if (!run.answers.length) renderSlot();
  },
  tool_call(d) {
    run.calls.set(d.call_id, d.args);
    run.items.push({ call: d, result: null });
  },
  tool_result(d) {
    for (const e of d.evidence) {
      run.evidence.set(e.eid, e);
      run.evCall.set(e.eid, run.calls.get(d.call_id));
    }
    for (const e of d.evidence.filter((x) => x.method === 'ground_distance')) {
      const { a, b } = run.calls.get(d.call_id);
      const moment = run.moments.find((m) => m.scene_key === e.scene_key && m.t0_ms <= e.t0_ms && e.t0_ms < m.t1_ms);
      queueEvidence(e.scene_key, (p) => p.showMeasure?.(e, [a.entity_id, b.entity_id], moment));
    }
    run.items.find((it) => it.call?.call_id === d.call_id).result = d;
    const r = d.envelope.input_range;
    run.lane.push([r.t0_ms, r.t1_ms, d.ok ? 'done' : 'failed']);
    timeline.setLaneData('agent', run.lane);
  },
  hits(d) {
    if (!d.moments.length) return;
    run.moments = d.moments;
    run.activeScene = d.moments.find((m) => here(m.scene_key))?.scene_key ?? run.activeScene;
    renderMoments(d.moments);
    run.candidates = d.auto_open === null;
    if (ctx.route.page === 'home' && run.candidates) document.body.dataset.candidates = 'true';
    if (ctx.route.page !== 'home') setState('hit');
    const key = d.auto_open ?? (d.auto_open === undefined && ctx.route.page === 'home' ? parseRoute(run.home)?.scene_key ?? (run.home ? null : d.moments[0].scene_key) : null);
    if (key) run.opening = activateEvidence({ scene_key: key, t0_ms: d.moments.find((m) => m.scene_key === key)?.t0_ms ?? 0 }, null, true);
    else if (ctx.route.page === 'home' && d.auto_open === undefined && run.home) go(run.home, true);
    for (const key of new Set(d.moments.map((m) => m.scene_key))) {
      queueEvidence(key, (p, owns) => p.applyHits(d.moments.filter((m) => m.scene_key === key), owns));
    }
  },
  model_result(d) {
    const key = d.scene_key ?? run.source;
    if (!run.models.has(key)) run.models.set(key, new Map());
    for (const r of d.results) run.models.get(key).set(r.record_id, r);
    renderModels();
    const records = [...run.models.get(key).values()];
    queueEvidence(key, (p, owns) => p.applyModelResults(records, owns));
  },
  ui(d) {
    if (!state.agentCamera && CAMERA_CMDS.has(d.cmd)) return;
    const key = d.scene_key ?? d.args.scene_key ?? run.source;
    if (!here(key)) run.skippedUi++;
    queueEvidence(key, (p, owns) => p.applyUI(d, owns));
  },
  claim(d) {
    run.claims.set(d.claim_id, d);
    // A new claim starts unresolved; any other state change is pinned to the step it happened in.
    if (d.after !== (d.before ?? 'UNRESOLVED') && run.lastStep) run.changes.push({ claim_id: d.claim_id, step: run.lastStep, before: d.before, after: d.after });
    renderClaims();
    if (!run.answers.length) renderSlot();
  },
  answer(d) {
    run.cites.push(...d.cites);
    run.rawAnswers.push(d);
    run.answers.push(numbered(d));
    renderSlot();
    timeline.setEvidence(evMarks());
  },
  vss_native(d) {
    run.native = d;
    $('#tab-steps').insertAdjacentHTML('beforeend', nativeBlock(d));
  },
  status(d) {
    run.final = d.final;
    run.stopReason = d.stop_reason;
    run.failure = d.error;
    trace.linkWeave(d.weave_url);
    if (ctx.route.page === 'home') {
      if (d.final === 'UNSUPPORTED') return refuseAtHome(d);
      if (run.opening) {
        finish();
        statusLine(completion(d));
        return;
      }
      if (d.stop_reason === 'error' || d.stop_reason === 'budget_exhausted') {
        run.held?.(null);
        document.body.dataset.run = 'failed';
        statusLine(completion(d));
        return;
      }
      if (run.candidates) {
        finish();
        statusLine(completion(d));
        return;
      }
      if (run.home && !run.corpus) go(run.home, true);
      else {
        run.held?.(null);
        document.body.dataset.run = 'done';
        statusLine(completion(d));
        page.showAbstain(d.final, ABSTAIN[d.final]?.[0] ?? 'The investigation finished without a resolved answer. Try another question or source.', []);
        return;
      }
    }
    finish();
    timeline.setEvidence(evMarks());
    statusLine(completion(d));
    if (ABSTAIN[d.final]) setState('abstained');
    else if (state.view === 'searching') setState('default');
  },
  done() {
    stream = null;
  },
  comparison_error(e) {
    stream = null;
    statusLine(`Answer complete. Comparison connection failed: ${e.message}`);
    $('#tab-steps').insertAdjacentHTML('beforeend', `<p class="muted">Comparison connection failed: ${esc(e.message)}</p>`);
  },
  error(e) {
    runGeneration++;
    run.held?.(null);
    stream = null;
    document.body.dataset.run = 'failed';
    if (state.view !== 'home') setState('default');
    $('#answer').innerHTML = `<p class="abstain">${icon('ban')}Agent connection failed: ${esc(e.message)}</p>`;
    statusLine(`Agent connection failed: ${e.message}`);
  },
};

// A refusal asked from home stays on the home page; frames held for a workspace page are dropped.
function refuseAtHome(d) {
  run.held?.(null);
  document.body.dataset.run = '';
  statusLine(completion(d));
  const ws = parseRoute(run.home)?.workspace ?? 'warehouse';
  page.showAbstain(d.final, ABSTAIN.UNSUPPORTED[0], PRESETS[ws].slice(0, 2).filter((q) => q !== run.q).map((q) => ({ q, ws })));
}

// After `status` (or Stop): pinned answer header, tabs and their panes.
function finish() {
  document.body.dataset.run = 'done';
  renderAnswer();
  renderTabs();
  renderKeyframe();
  renderClaims();
  renderMomentsHead();
  renderEvidence();
  renderSteps();
  renderFollowup();
}

// ---- running: narration card and the reserved answer slot
function renderNarration() {
  const s = run.lastStep, cur = s ? segOf(s.state) : -1;
  const cams = (t) => esc(t).replace(/\b[a-z]+_\d{2}\b/g, '<span class="mono">$&</span>');
  $('#narration').innerHTML = `
    <div class="nr-row">
      <span class="live-dot"></span><span class="nr-step mono">STEP ${cur + 1 || '–'} OF ${SEGMENTS.length}</span><span class="mono muted">${s ? sec(s.elapsed_ms) : ''}</span>
      <button type="button" class="nr-toggle" aria-expanded="${!!run.narrOpen}">Steps${icon(run.narrOpen ? 'chevron-down' : 'chevron-right')}</button>
      <button type="button" class="ghost nr-stop">${icon('x')}Stop</button>
    </div>
    <p class="nr-sum">${s ? cams(s.summary) : 'Sent, waiting for the agent…'}</p>
    ${run.narrOpen ? `<ol class="nr-list">${run.steps.map((x) => `<li><span class="st-ok">✓</span>${cams(x.summary)}</li>`).join('')}</ol>` : ''}
    <div class="nr-segs">${SEGMENTS.map((_, i) => `<span class="nr-seg${i === cur ? ' on' : i <= run.reached ? ' done' : ''}"></span>`).join('')}</div>
    <div class="nr-labels">${SEGMENTS.map(([label], i) => `<span${i === cur ? ' class="on"' : ''}>${label}</span>`).join('')}</div>`;
}

function slotPlaceholder(r) {
  const preds = [...(r?.claims?.values() ?? [])].flatMap((c) => c.predicates ?? []);
  const units = preds.map((p) => p.unit).filter(Boolean);
  const ops = preds.map((p) => (p.operator ?? '').toLowerCase());
  const parseSummary = (r?.steps?.find((s) => s.state === 'parse')?.summary ?? r?.steps?.[0]?.summary ?? '').toLowerCase();
  const q = (r?.q ?? '').toLowerCase();

  // Decide from predicate unit first: "m" means distance; "km/h" or "m/s" means speed
  if (units.some((u) => u === 'km/h' || u === 'm/s')) {
    return 'Appears here once the speed is measured.';
  }
  if (units.some((u) => u === 'm')) {
    return 'Appears here once the distance is measured.';
  }

  // A measured quantity named in the question wins over count and timing words ("when … and how far").
  if (/\b(speed|speeding)\b/.test(parseSummary) || /\b(speed|speeding)\b/.test(q)) {
    return 'Appears here once the speed is measured.';
  }
  if (/\b(distance|how far|how close|close to)\b/.test(parseSummary) || /\b(how far|how close|distance)\b/.test(q)) {
    return 'Appears here once the distance is measured.';
  }

  // Use operator only for count and timing
  const isCount = ops.some((op) => ['count', 'counts', 'num'].includes(op))
    || /\b(count|how many)\b/.test(parseSummary)
    || /\b(how many|count)\b/.test(q);
  if (isCount) return 'Appears here once objects are counted.';

  const isTiming = ops.some((op) => ['time', 'timing', 'when', 'localize'].includes(op))
    || /\b(timing|when|time)\b/.test(parseSummary)
    || /\b(when|what time)\b/.test(q);
  if (isTiming) return 'Appears here once the timing is located.';

  return 'Appears here once evidence is verified.';
}

function renderSlot() {
  $('#answer').innerHTML = `
    <div class="ans-slot">
      <span class="p-label">ANSWER</span>
      ${run.answers.length ? `<p class="ans-text">${run.answers.join('')}</p>` : `<i class="skel" style="width:92%"></i><i class="skel" style="width:70%"></i><span class="muted">${esc(slotPlaceholder(run))}</span>`}
    </div>`;
}

// ---- answer header
// The answer's numbers come from evidence fields (measure_refs, in text order, formatted like Python's '.4g');
// each found in the text becomes a button to its evidence. A time field (*_ms, *_s) is written in seconds ("4.6 s", "3.0 s")
// and marked as a time, not a measured number.
function numbered(d) {
  let html = '', rest = d.text;
  for (const r of d.measure_refs) {
    const v = r.field.split('.').reduce((o, k) => o?.[k], run.evidence.get(r.eid)), ms = r.field.endsWith('_ms'), time = ms || r.field.endsWith('_s');
    const n = typeof v === 'number' && String(+(ms ? v / 1000 : v).toPrecision(4)).replace('.', '\\.');
    const pat = time ? `${n}${n.includes('.') ? '0*' : '(?:\\.0+)?'}(?:\\s?s\\b)?` : n;
    const m = n && rest.match(new RegExp(`(?<![\\d.])${pat}(?!\\d)`));
    if (!m) continue;
    html += `${esc(rest.slice(0, m.index))}<button class="${time ? 'ans-time' : 'ans-num'} mono" data-cite data-eid="${esc(r.eid)}" title="${esc(r.eid)} · ${esc(r.field)}">${m[0]}</button>`;
    rest = rest.slice(m.index + m[0].length);
  }
  return html + esc(rest);
}

const OP_WORDS = {
  located: 'position',
  detected: 'presence',
  same_entity: 'same person',
  closest_approach: 'closest distance',
};
const ABSTAIN_QTY = {
  no_geometry: 'distance',
  no_gps: 'speed',
  no_visibility: 'blind spot',
  no_track_ids: 'same person',
};

function notMeasuredLine() {
  const claims = [...run.claims.values()];
  const items = [];
  const pushItem = (quantity, reason) => {
    if (!items.some((it) => it.quantity === quantity)) {
      items.push({ quantity, reason });
    }
  };

  for (const c of claims) {
    if (c.explain?.abstain) {
      const q = ABSTAIN_QTY[c.explain.abstain.reason] ?? 'measurement';
      pushItem(q, c.explain.abstain.text);
    }
    for (const p of c.predicates ?? []) {
      if (p.status === 'unknown') {
        const q = p.unit === 'm' ? 'distance'
          : p.unit === 'm/s' || p.unit === 'km/h' ? 'speed'
          : OP_WORDS[p.operator] ?? 'measurement';
        const reason = p.eids && p.eids.length > 0 ? 'checked but unresolved' : 'not checked';
        pushItem(q, reason);
      }
    }
  }

  if (!items.length) return '';
  return `<p class="ans-not-measured">Not measured: ${items.map((it) => `${esc(it.quantity)} — ${esc(it.reason)}`).join('; ')}</p>`;
}

function renderAnswer() {
  const abstain = ABSTAIN[run.final];
  const hasMeasureRef = run.rawAnswers.some((a) => a.measure_refs?.length > 0);
  const kinds = [...new Set(run.cites.filter((e) => run.evidence.has(e)).map((e) => run.evidence.get(e).kind))]
    .filter((k) => k !== 'measure' || hasMeasureRef);
  const text = run.answers.join('') || (abstain ? `${abstain[0]}<span class="muted"> (${esc(run.lastStep?.summary)})</span>` : run.final ? '' : 'Stopped before an answer.');
  // The first two presets of each workspace are answerable ones.
  const instead = (PRESETS[ctx.route.workspace] ?? PRESETS.warehouse).slice(0, 2).filter((q) => q !== run.q);
  const knownLevel = run.rawAnswers.find((a) => a.evidence_level)?.evidence_level
    ?? [...run.claims.values()].find((c) => c.evidence_level)?.evidence_level
    ?? run.moments.find((m) => m.evidence_level)?.evidence_level
    ?? null;
  const reasoningLine = `<a class="ans-run" href="${run.id ? `#/run/${esc(run.id)}` : '#/runs'}"><span class="ans-meta mono muted">${count(run.steps.length, 'step')} · ${sec(took())}</span> · Reasoning <span class="mono">›</span></a>`;
  $('#answer').innerHTML = `
    <div class="ans-head">${run.final ? say(run.final) : '<span class="verdict say v-stopped">Stopped</span>'}
      ${knownLevel ? level(knownLevel) : ''}
      ${reasoningLine}</div>
    <p class="ans-text">${text}</p>
    ${notMeasuredLine()}
    ${run.stopReason === 'error' || run.stopReason === 'budget_exhausted' ? `<p class="ans-instead">${esc(completion({ stop_reason: run.stopReason, error: run.failure }))}</p>` : ''}
    ${kinds.length ? `<div class="ans-src">${kinds.map(source).join('')}</div>` : ''}
    ${abstain ? `<p class="ans-instead">${abstain[1]}${instead.map((q) => `<button class="preset">${esc(q)}</button>`).join('')}</p>` : ''}`;
}

function evIds() {
  const claims = [...run.claims.values()];
  return [...new Set([...claims.flatMap((c) => [...c.examined_eids, ...c.predicates.flatMap((p) => p.eids)]), ...run.cites])].filter((e) => run.evidence.has(e));
}
// Evidence the answer cites or a predicate decides on.
const usedIds = () => [...new Set([...[...run.claims.values()].flatMap((c) => c.predicates.flatMap((p) => p.eids)), ...run.cites])].filter((e) => run.evidence.has(e));
// The timeline's Evidence lane, in wall time.
const evMarks = () => evIds().map((eid) => {
  const e = run.evidence.get(eid);
  return { eid, kind: e.kind, t0_ms: clock.toWall(e.scene_key, e.t0_ms), t1_ms: clock.toWall(e.scene_key, e.t1_ms) };
});

function renderTabs() {
  const last = ctx.route.workspace === 'fleet' ? ['trip', 'Trip'] : ['videos', 'Videos'];
  const tabs = [['answer', 'Answer'], ['evidence', 'Evidence', run.evidence.size], ['steps', 'Steps', run.steps.length], last];
  $('#tabs').innerHTML = tabs.map(([id, label, n]) => `<button type="button" data-tab="${id}">${label}${n != null ? ` <span class="mono">${n}</span>` : ''}</button>`).join('');
  setTab(state.panelTab);
}

// ---- Answer tab
function renderKeyframe() {
  const m = run.moments[0];
  if (!m) return;
  $('#keyframe').innerHTML = `
    <figure class="keyframe">
      ${m.thumb ? `<img src="${esc(api.url(m.thumb))}" alt="">` : '<span class="thumb-empty">No thumbnail</span>'}
      <figcaption class="mono"><span class="kf-at">${esc(m.primary_cam)} · ${tc(m.t0_ms)}</span>
        <button type="button" class="kf-play">${icon('play')}Play ${mmss(Math.max(0, m.t0_ms - 2500))}–${mmss(m.t0_ms + 2500)}</button></figcaption>
    </figure>`;
  $('#keyframe .kf-play').onclick = () => activateEvidence(m, () => { clock.seek(clock.toWall(m.scene_key, Math.max(0, m.t0_ms - 2500))); clock.play(); });
}

const predText = (p) => `${p.subject_refs.map((s) => esc(s.entity_id)).join('–')} ${esc(OPS[p.operator] ?? p.operator)} ${esc(p.value)} ${esc(p.unit ?? '')}`;
// The value a predicate was decided on: its first evidence with a number.
function predValue(p) {
  const e = p.eids.map((id) => run.evidence.get(id)).find((x) => typeof x?.value === 'number');
  return e ? `<span class="mono pv pv-${e.kind}">${value(e)}</span>` : '<span class="mono pv muted">–</span>';
}

// A verdict that did not change shows once.
const change = (before, after) => (before === after ? verdict(after) : transition(before, after));

function renderClaims() {
  const claims = [...run.claims.values()];
  if (!claims.length) return;
  if (document.body.dataset.run === 'running') {
    $('#claims').innerHTML = `<h2 class="p-label">CLAIMS SO FAR</h2>${claims.map((c) => {
      const open = c.predicates.filter((p) => p.status !== 'supported' && p.status !== 'contradicted');
      return `<div class="claim-card claim v-${c.after.toLowerCase()}" data-claim="${esc(c.claim_id)}">${verdict(c.after)}<p class="claim-text">${esc(c.text)}</p>
        ${open.length ? `<p class="claim-needs">Needs a measure of ${open.map(predText).join('; ')}</p>` : ''}</div>`;
    }).join('')}`;
    return;
  }

  const byMoment = new Map();
  for (const c of claims) {
    const key = c.moment_id || c.claim_id;
    if (!byMoment.has(key)) byMoment.set(key, []);
    byMoment.get(key).push(c);
  }

  const allMeasureEids = new Set(run.rawAnswers.flatMap((a) => a.measure_refs?.map((mr) => mr.eid) ?? []));

  $('#claims').innerHTML = [...byMoment.entries()].map(([key, groupClaims]) => {
    const firstClaim = groupClaims[0];
    const cardVerdict = groupClaims.some((c) => c.after === 'CONTRADICTED')
      ? 'CONTRADICTED'
      : groupClaims.every((c) => c.after === 'SUPPORTED')
      ? 'SUPPORTED'
      : 'UNRESOLVED';
    const cardLevel = groupClaims.find((c) => c.evidence_level)?.evidence_level
      ?? run.moments.find((m) => m.moment_id === key)?.evidence_level
      ?? null;
    const allPreds = groupClaims.flatMap((c) => c.predicates);
    const hasMeasure = allPreds.some((p) => p.eids.some((eid) => allMeasureEids.has(eid)))
      || groupClaims.some((c) => run.rawAnswers.some((a) => a.claim_ids?.includes(c.claim_id) && a.measure_refs?.length > 0));

    const counts = new Map();
    const order = [];
    for (const p of allPreds) {
      const sym = p.status === 'supported' ? '✓' : p.status === 'contradicted' ? '✗' : (p.eids && p.eids.length > 0 ? '?' : '—');
      const statusKey = p.status === 'supported' ? 'supported' : p.status === 'contradicted' ? 'contradicted' : (p.eids && p.eids.length > 0 ? 'unresolved' : 'not-checked');
      const desc = predText(p);
      const val = predValue(p);
      const k = `${sym}|${desc}|${val}|${statusKey}`;
      if (!counts.has(k)) {
        counts.set(k, { sym, desc, val, statusKey, count: 1 });
        order.push(k);
      } else {
        counts.get(k).count++;
      }
    }

    const chips = order.map((k) => {
      const it = counts.get(k);
      const countLabel = it.count > 1 ? `<span class="chip-count mono">×${it.count}</span>` : '';
      const valHtml = it.val && it.val !== '<span class="mono pv muted">–</span>' ? ` ${it.val}` : '';
      return `<span class="pred-chip chip-${it.statusKey}"><span class="chip-sym">${it.sym}</span> <span class="pt">${it.desc}</span>${valHtml}${countLabel ? ` ${countLabel}` : ''}</span>`;
    }).join('');

    const at = run.changes.findLast((x) => groupClaims.some((c) => c.claim_id === x.claim_id))?.step;

    return `<div class="claim verdict-card v-${cardVerdict.toLowerCase()}" data-claim="${esc(firstClaim.claim_id)}">
      <div class="claim-row">
        <span class="claim-head">${change(firstClaim.before, cardVerdict)}</span>
        ${cardLevel ? level(cardLevel) : ''}
        ${hasMeasure ? source('measure') : ''}
        ${at ? `<span class="claim-once">at ${esc(at.summary)}</span>` : ''}
      </div>
      <div class="preds">${chips}</div>
    </div>`;
  }).join('');
}

function renderMoments(moments) {
  renderMomentsHead();
  $('#moments').innerHTML = moments.map((m, i) => `
    <button class="moment" data-i="${i}">
      ${m.thumb ? `<img src="${esc(api.url(m.thumb))}" alt="">` : '<span class="thumb-empty">No thumbnail</span>'}
      <span class="m-body">
        <span class="m-head"><span class="mono">${span(m.t0_ms, m.t1_ms)}</span>${m.exec_state ? exec(m.exec_state) : ''}</span>
        <span class="m-meta mono">${m.actor_refs.length ? `${m.actor_refs.map((a) => esc(a.entity_id)).join(' ')} · ` : ''}main cam ${esc(m.primary_cam)} · ${count(m.cams.length, 'cam')}</span>
        <span class="m-why">${m.why.map(esc).join('<br>')}</span>
      </span>
    </button>`).join('');
  $('#moments').onclick = (e) => {
    const b = e.target.closest('.moment');
    if (!b) return;
    const m = moments[+b.dataset.i];
    document.querySelectorAll('.moment').forEach((x) => x.classList.toggle('on', x === b));
    activateEvidence(m, () => selection.set(m.actor_refs.map((a) => a.entity_id), { source: 'user' }));
  };
}

// Running: every moment found; done: the top one is the key frame, the rest fold into one row.
function renderMomentsHead() {
  const n = run.moments.length;
  $('.p-moments-head').innerHTML = document.body.dataset.run === 'done'
    ? n > 1 ? `<button type="button" class="p-more">Other moments · ${n - 1}${icon('chevron-right')}</button>` : ''
    : n ? `MOMENTS FOUND · ${n}` : '';
}

function renderFollowup() {
  const ws = ctx.route.workspace;
  $('#followup .fu-chips').innerHTML = (PRESETS[ws] ?? []).filter((q) => q !== run.q).slice(0, 2).map((q) => `<button type="button" class="preset">${esc(q)}</button>`).join('');
  $('#followup-input').value = '';
  renderContext();
}

function renderContext() {
  if (document.body.dataset.run !== 'done') return;
  $('#followup .fu-ctx').textContent = [...selection.ids, tc(sceneT())].join(' · ');
}

// ---- Evidence tab
const distance = (c) => (c?.a && c?.b ? `Distance ${c.a.entity_id} to ${c.b.entity_id}` : 'Ground distance');
// method -> label for a person reading the answer (a function gets the tool call's args); others fall back to the method in words.
const METHOD_LABEL = {
  ground_distance: distance,
  ground_distance_tri: distance,
  ground_dist: distance,
  closest_approach: 'Closest approach',
  floor_cell_visibility: (c) => (c?.target && c?.cam ? `Floor under ${c.target} as seen from ${c.cam}` : 'Floor cell visibility'),
  caption_embed1_rrf: 'Caption and visual search rank',
  event_gt_dodge: 'Dodge event (ground truth)',
  event_rule_ped_crossing: 'Pedestrian crossing event (rule)',
  geocode: 'Place name match',
  geocode_rapidfuzz: 'Place name match',
  gps_pass: 'GPS pass near the place',
  searched_passes: 'Passes checked',
  obd_speed: 'Speed (OBD)',
  obd_over_limit: 'Over the speed limit (OBD)',
  osm_maxspeed: 'Speed limit (map)',
};
function evLabel(e) {
  const l = METHOD_LABEL[e.method];
  if (typeof l === 'function') return l(run.evCall.get(e.eid));
  return l ?? e.method.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

function value(e) {
  if (typeof e.value === 'number') return `${+e.value.toPrecision(4)} ${esc(e.unit ?? '')}${e.sigma != null ? ` ±${+e.sigma.toPrecision(2)}` : ''}`;
  if (e.value && typeof e.value === 'object') return esc(Object.values(e.value).filter((v) => typeof v === 'string').join(' · '));
  return esc(e.value ?? '');
}

// JSON with its numbers marked; numbers inside strings are left alone.
const json = (v) => JSON.stringify(v ?? null).replace(/"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[^"\d-]+|./g, (t) => (/^-?\d/.test(t) ? `<span class="num">${t}</span>` : esc(t)));
function raw(e) {
  const rows = [['eid', e.eid], ['method', e.method], ['value', e.value], ['unit', e.unit], ['sigma', e.sigma], ['cams', camsOf(e).join(', ') || null]];
  return rows.map(([k, v]) => `${k.padEnd(8)}${typeof v === 'string' ? esc(v) : json(v)}`).join('\n');
}

function evRow(eid) {
  const e = run.evidence.get(eid);
  return `<div class="ev ev-k-${e.kind}${state.focusEid === eid ? ' sel' : ''}" data-eid="${esc(eid)}">
    <span class="ev-label">${esc(evLabel(e))}</span><span class="mono ev-val">${value(e)}</span>
    <span class="mono muted ev-where">${esc(camsOf(e).join(' + ') || camLabel(e))} · ${tc(e.t0_ms)}</span>
    <button type="button" class="ev-raw-btn" aria-expanded="false">raw${icon('chevron-right')}</button>
    <pre class="ev-raw" hidden>${raw(e)}</pre>
  </div>`;
}

// Used evidence first; the rest the run read folds into one row, or is the whole list when nothing was claimed or cited.
function renderEvidence() {
  const used = usedIds(), rest = [...run.evidence.keys()].filter((e) => !used.includes(e));
  const groups = (eids) => KINDS.map((k) => {
    const rows = eids.filter((e) => run.evidence.get(e).kind === k);
    return rows.length ? `<section class="ev-group"><h3 class="ev-head ev-${k}">${srcIcon(k)}${srcLabel(k).toUpperCase()} · ${rows.length}</h3>${rows.map(evRow).join('')}</section>` : '';
  }).join('');
  $('#tab-evidence').innerHTML = !run.claims.size && !run.cites.length
    ? `<h3 class="p-label ev-read">Read, not used in an answer</h3>${groups(rest)}`
    : `${groups(used)}${rest.length ? `<details class="ev-rest"><summary class="p-more ev-more">Other evidence read · ${rest.length}${icon('chevron-right')}</summary>${groups(rest)}</details>` : ''}`;
}

// ---- Steps tab
function renderSteps() {
  const dur = run.steps.map((s, k) => s.elapsed_ms - (k ? run.steps[k - 1].elapsed_ms : 0));
  const changedAt = new Set(run.changes.map((x) => x.step.i));
  const row = (it) => {
    if (it.step) {
      const s = it.step, changed = changedAt.has(s.i);
      return `<li class="st-step${changed ? ' changed' : ''}" data-step="${esc(s.step_id)}"><span class="st-ok">✓</span><span class="st-sum">${esc(s.summary)}</span>
        <span class="mono muted st-ms">${dur[run.steps.indexOf(s)]} ms</span>
        ${changed ? `<span class="st-claims">${run.changes.filter((x) => x.step === s).map((x) => change(x.before, x.after)).join('')}</span>` : ''}</li>`;
    }
    const { call, result } = it;
    return `<li class="st-tool${result && !result.ok ? ' failed' : ''}" data-call="${esc(call.call_id)}"><span></span>
      <span class="st-sum"><span class="mono tool-name">tool.${esc(call.name)}</span> ${esc(call.why)}</span>
      <span class="mono muted st-ms">${result ? `${result.envelope.elapsed_ms} ms · ${result.evidence.length} evidence ${exec(result.envelope.exec_state)}` : '…'}</span></li>`;
  };
  $('#tab-steps').innerHTML = `
    <ol class="st-list">${run.items.map(row).join('')}</ol>
    <div class="st-bar">${run.steps.map((s, k) => `<i class="${changedAt.has(s.i) ? 'on' : ''}" style="flex:${Math.max(dur[k], 0)}"></i>`).join('')}</div>
    ${run.id ? `<a class="st-link" href="#/run/${esc(run.id)}">${icon('workflow')}Full reasoning, tool calls and VSS comparison</a>` : ''}
    ${run.native ? nativeBlock(run.native) : ''}`;
}

// Site VSS /search for the same question, for comparison only; the answer above does not use it.
const NATIVE = { ok: 'done', timeout: 'timed out', error: 'failed' };
function nativeBlock(d) {
  const hits = d.hits.slice(0, 5).map((h) => `
    <li>${esc(h.caption)} <span class="mono muted">${h.scene_key ? `${esc(h.cam_id)} · ${esc(h.scene_key)} · ${sec(h.t_ms)} · ` : ''}similarity ${h.similarity.toFixed(3)}</span></li>`).join('');
  return `<details class="ans-vss">
    <summary class="muted">VSS native search · <span class="mono">${NATIVE[d.status]} · ${sec(d.elapsed_ms)}</span></summary>
    ${hits ? `<ol>${hits}</ol>` : '<p>No results returned</p>'}
    ${d.sql_query ? `<details class="jt ans-sql"><summary>SQL</summary><pre style="white-space: pre-wrap">${esc(d.sql_query)}</pre></details>` : ''}
  </details>`;
}

// Citation click: seek to the evidence, enter evidence mode and flash every row that shows it (1.4 s × 2).
async function flash(eid) {
  const e = run.evidence.get(eid);
  await activateEvidence(e, () => focusEvidence(eid, e));
}

function focusEvidence(eid, e) {
  bus.emit('evidence:focus', e);
  setStage('evidence', eid);
  document.querySelectorAll(`.ev[data-eid="${CSS.escape(eid)}"]`).forEach((row) => {
    row.classList.remove('flash');
    void row.offsetWidth;
    row.classList.add('flash');
  });
}

// ---- Esc steps back one layer
async function closeRunpage() {
  location.hash = wsHash ?? (await defaultRoute());
}

function stepBack() {
  if (cmdk.isOpen()) return cmdk.close();
  if (runpage.isOpen()) return closeRunpage();
  if (state.stageMode === 'evidence') return setStage('explore', null);
  if (state.panelTab !== 'answer') return setTab('answer');
  if (!ctx.route || ctx.route.page === 'home') return;
  if (!run) return (location.hash = '#/');
  toDefault();
  selection.clear();
}

// ---- wiring
bus.on('view:state', (s) => (document.body.dataset.view = s));
selection.onChange(renderContext);
clock.onTick(renderContext);
window.addEventListener('hashchange', onRoute);
$('#ask-form').addEventListener('submit', (e) => {
  e.preventDefault();
  ask($('#ask-input').value);
});
$('#source-tag').addEventListener('click', () => openCmdk());
// A preset chip in a workspace; one with a route of its own opens that route first.
function askPreset(q) {
  const hash = PRESET_ROUTE.get(q);
  if (hash && location.hash !== hash) go(hash);
  ask(q);
}
$('#presets').addEventListener('click', (e) => e.target.closest('.preset') && askPreset(e.target.closest('.preset').textContent));
$('#answer').addEventListener('click', (e) => e.target.closest('.preset') && askPreset(e.target.closest('.preset').textContent));
$('#tabs').addEventListener('click', (e) => e.target.closest('[data-tab]') && setTab(e.target.closest('[data-tab]').dataset.tab));
$('.p-moments').addEventListener('click', (e) => e.target.closest('.p-more') && $('.p-moments').classList.toggle('open'));
$('#tab-evidence').addEventListener('click', (e) => {
  const b = e.target.closest('.ev-raw-btn');
  if (!b) return;
  const open = b.getAttribute('aria-expanded') !== 'true';
  b.setAttribute('aria-expanded', open);
  b.innerHTML = `raw${icon(open ? 'chevron-down' : 'chevron-right')}`;
  b.nextElementSibling.hidden = !open;
});
$('#followup').addEventListener('submit', (e) => {
  e.preventDefault();
  followUp($('#followup-input').value);
});
$('#followup .fu-chips').addEventListener('click', (e) => e.target.closest('.preset') && followUp(e.target.closest('.preset').textContent));
$('#narration').addEventListener('click', (e) => {
  if (e.target.closest('.nr-stop')) return stop();
  if (!e.target.closest('.nr-toggle')) return;
  run.narrOpen = !run.narrOpen;
  renderNarration();
});
$('#takeover button').addEventListener('click', () => setAgentCamera(false));
$('#stage').addEventListener('pointerdown', () => document.body.dataset.run === 'running' && setAgentCamera(false));
$('#panel').addEventListener('click', (e) => {
  const c = e.target.closest('.cite, .ans-num, .ans-time, .ev');
  if (c && !e.target.closest('.ev-raw-btn, .ev-raw') && run?.evidence.has(c.dataset.eid)) flash(c.dataset.eid);
});
timeline.onEvidence((eid) => run?.evidence.has(eid) && flash(eid));
document.addEventListener('pointerover', (e) => hover(e.target.closest('.ans-num, .ans-time, .cite, .ev')?.dataset.eid ?? null));
document.addEventListener('pointerout', (e) => !e.relatedTarget && hover(null));
$('#runpage').addEventListener('click', (e) => e.target.closest('.rp-back') && closeRunpage());

async function openCmdk() {
  const items = (await workspaces).flatMap((w) => w.id === 'warehouse'
    ? w.scenes.map((s) => ({ icon: 'warehouse', label: s.title, hint: s.scene_key, run: () => (location.hash = `#/warehouse/${s.scene_key}`) }))
    : w.id === 'street'
    ? w.scenes.map((s) => ({ icon: 'camera', label: s.title, hint: s.scene_key, run: () => (location.hash = `#/street/${s.scene_key}`) }))
    : [{ icon: 'map', label: 'All trips', hint: count(w.scenes.length, 'trip'), run: () => (location.hash = '#/fleet') },
      ...w.scenes.map((s) => ({ icon: 'truck', label: `${shortKey(s.scene_key)} · ${s.title}`, hint: s.scene_key, run: () => (location.hash = `#/fleet/${s.scene_key}@${s.t0_ms ?? 0}`) }))]);
  cmdk.open([...items, { icon: 'workflow', label: 'Recent runs', hint: '#/runs', run: () => (location.hash = '#/runs') }], (q) => (ctx.route?.page === 'home' ? ctx.ask(q) : ask(q)));
}
$('#cmdk-btn').addEventListener('click', openCmdk);
document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    openCmdk();
  } else if (e.key === 'Escape') stepBack();
});

api.get('health').then(
  (h) => {
    const rows = Object.values(h.index_rows).reduce((a, b) => a + b, 0);
    const badge = $('#data-badge');
    badge.innerHTML = `${icon('database')}Data · <span class="mono">${esc(h.backend ?? h.mode)} · ${int(rows)} rows</span>`;
    badge.title = Object.entries(h.index_rows).map(([k, v]) => `${k}: ${int(v)}`).join('\n');
  },
  () => ($('#data-badge').innerHTML = `${icon('database')}Data · backend unreachable`),
);

initPanelResizer();
onRoute();

