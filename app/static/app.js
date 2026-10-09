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
import { exec, icon, say, source, srcIcon, srcLabel, transition, verdict } from './kernel/badges.js';
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
const PAGES = { home: './home/page.js', warehouse: './warehouse/page.js', fleet: './fleet/page.js', test: './kernel/_testpage.js' };
// Spec §8.5; the last warehouse question must be refused (identity).
const PRESETS = {
  warehouse: ['When did the worker start to dodge the forklift, and how far was it?', 'Was anyone where ceiling_00 cannot see in these 10 seconds?', 'Who is the person in eye_00?'],
  fleet: ['Near the ROM, did a pedestrian cross in front of the car?', 'At Queen and Spadina, what is the building on the right?', 'Was this car speeding?'],
};
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
const ctx = { clock, selection, bus, api, timeline, route: null, presets: PRESETS, workspaces: () => workspaces, ask: async (q, { ws, hash } = {}) => ask(q, { home: hash ?? await defaultRoute(ws) }) };
// Only workspaces that have a page template; /api/workspaces also lists street scenes, which have none yet.
const workspaces = api.get('api/workspaces').then((ws) => ws.filter((w) => PAGES[w.id]));
let page = null, pageKind = null, dispose = null, stream = null, run = null;
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
  const r = run;
  mounted.then((p) => p && p === page && r === run && fn(p));
};

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
const shortKey = (key) => key.replace(/^fl-pie-(set\d+)_video_(\d+)$/, '$1·$2');
const mmss = (ms) => tc(ms).slice(0, 5);

// ---- routing: #/ home; #/warehouse/<scene_key>, #/fleet, #/fleet/<trip_id>@<t_ms>, #/test; #/run/<run_id> and #/runs open over them
function parseRoute(hash) {
  let m;
  if (hash === '' || hash === '#/') return { workspace: null, page: 'home', scene_key: null };
  if ((m = hash.match(/^#\/warehouse\/([a-z0-9_-]+)$/))) return { workspace: 'warehouse', page: 'warehouse', scene_key: m[1] };
  if ((m = hash.match(/^#\/fleet\/([a-z0-9_-]+)@(\d+)$/))) return { workspace: 'fleet', page: 'fleet', scene_key: m[1], trip_id: m[1], t_ms: +m[2] };
  if (hash === '#/fleet') return { workspace: 'fleet', page: 'fleet', scene_key: null };
  if (hash === '#/test') return { workspace: 'warehouse', page: 'test', scene_key: null };
  return null;
}

async function firstWarehouse() {
  const s = (await workspaces).find((w) => w.id === 'warehouse').scenes[0];
  return s ? `#/warehouse/${s.scene_key}` : '#/fleet';
}

// The route a question without a chosen source runs against: the last workspace route (of workspace ws, if given), else the first one.
async function defaultRoute(ws) {
  const last = state.sourceTag?.hash ?? stored(LAST_ROUTE);
  const r = last && parseRoute(last);
  if (r?.workspace && (!ws || r.workspace === ws)) return last;
  return ws === 'fleet' ? '#/fleet' : firstWarehouse();
}

async function resetTimeline(sceneKey) {
  const [mf, tl] = sceneKey ? await Promise.all([api.get(`api/scene/${sceneKey}/manifest`), api.get(`api/scene/${sceneKey}/timeline`)]) : [];
  timeline.clear();
  if (sceneKey) {
    clock.bind({ kind: 'scene' });
    clock.setRange(mf.t0_ms, mf.t1_ms);
    for (const [cam, items] of Object.entries(tl.lanes)) {
      timeline.addLane(`cam:${cam}`, { label: cam, kind: 'coverage' });
      timeline.setLaneData(`cam:${cam}`, items);
    }
    timeline.setEvents(tl.events);
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
function go(hash) {
  history.pushState(null, '', hash);
  onRoute();
}

async function mountWorkspace(hash) {
  const route = parseRoute(hash);
  wsHash = hash;
  // A question asked from home keeps running when its route opens.
  if (ctx.route?.workspace !== route.workspace && !(run && run.home === hash)) {
    toDefault();
    selection.clear();
  }
  ctx.route = route;
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
  const same = route.page === pageKind;
  const done = same ? null : hold();
  pageKind = route.page;
  // Lanes are reset before the page mounts so that lanes a page adds survive.
  await resetTimeline(route.page === 'warehouse' ? route.scene_key : null);
  if (same) return (route.scene_key || route.page === 'fleet') && page?.loadScene(route.scene_key);

  dispose?.();
  page = dispose = null;
  const stage = $('#stage');
  stage.replaceChildren();
  try {
    page = await import(PAGES[route.page]);
  } catch {
    stage.innerHTML = '<p class="stage-msg">Page not loaded</p>';
    return done(null);
  }
  dispose = await page.mount(stage, ctx);
  done(page);
}

function renderSourceTag() {
  const r = ctx.route;
  const label = r.trip_id ? shortKey(r.trip_id) : r.scene_key ?? (r.page === 'fleet' ? 'all trips' : r.page);
  $('#source-tag').innerHTML = `${icon(r.workspace === 'fleet' ? 'truck' : 'warehouse')}${esc(label)}${icon('chevron-down')}`;
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
  stream?.abort();
  stream = run = null;
  if (state.view !== 'home') setState('default');
  document.body.dataset.run = '';
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
function ask(q, { home = null, t_ms = null } = {}) {
  q = q.trim();
  if (!q) return;
  toDefault();
  const route = home ? parseRoute(home) : ctx.route;
  $('#ask-input').value = q;
  run = {
    id: null, q, home, held: home ? hold() : null, steps: [], lastStep: null, reached: -1, items: [], changes: [], summaries: new Map(),
    evidence: new Map(), evCall: new Map(), claims: new Map(), cites: [], models: [], lane: [], answers: [], calls: new Map(), moments: [], native: null, final: null,
  };
  document.body.dataset.run = 'running';
  setAgentCamera(true);
  if (!home) setState('searching');
  statusLine('Sent, waiting for the agent…');
  renderSlot();
  renderNarration();
  const context = { workspace: route.workspace, scene_key: route.scene_key };
  if (selection.ids.size) context.selection = [...selection.ids];
  if (t_ms != null) context.t_ms = t_ms;
  stream = askSSE({ q, context }, handlers);
}

const sceneT = () => clock.map(clock.t)?.t_ms ?? clock.t;
const followUp = (q) => ask(q, { t_ms: sceneT() });

function stop() {
  stream?.abort();
  stream = null;
  if (ctx.route.page === 'home') {
    run.held(null);
    document.body.dataset.run = '';
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
      toPage((p) => p.showMeasure?.(e, [a.entity_id, b.entity_id], moment));
    }
    run.items.find((it) => it.call?.call_id === d.call_id).result = d;
    const r = d.envelope.input_range;
    run.lane.push([r.t0_ms, r.t1_ms, d.ok ? 'done' : 'failed']);
    timeline.setLaneData('agent', run.lane);
  },
  hits(d) {
    if (!d.moments.length) return;
    if (ctx.route.page === 'home') go(run.home);
    run.moments = d.moments;
    renderMoments(d.moments);
    setState('hit');
    toPage((p) => p.applyHits(d.moments));
  },
  model_result(d) {
    run.models = d.results;
    modelpanel.render(d.results, 'summary');
    toPage((p) => p.applyModelResults(d.results));
  },
  ui(d) {
    if (!state.agentCamera && CAMERA_CMDS.has(d.cmd)) return;
    toPage((p) => p.applyUI(d));
  },
  claim(d) {
    run.claims.set(d.claim_id, d);
    // A new claim starts unresolved; any other state change is pinned to the step it happened in.
    if (d.after !== (d.before ?? 'UNRESOLVED') && run.lastStep) run.changes.push({ claim_id: d.claim_id, step: run.lastStep, before: d.before, after: d.after });
    renderClaims();
  },
  answer(d) {
    run.cites.push(...d.cites);
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
    trace.linkWeave(d.weave_url);
    if (ctx.route.page === 'home') {
      if (d.final === 'UNSUPPORTED') return refuseAtHome(d);
      go(run.home);
    }
    finish();
    timeline.setEvidence(evMarks());
    statusLine(`Done · ${count(run.steps.length, 'step')} · ${sec(took())}${d.cached ? ' · cached replay' : ''}`);
    if (ABSTAIN[d.final]) setState('abstained');
    else if (state.view === 'searching') setState('default');
  },
  done() {
    stream = null;
  },
  error(e) {
    stream = null;
    document.body.dataset.run = 'failed';
    if (state.view !== 'home') setState('default');
    $('#answer').innerHTML = `<p class="abstain">${icon('ban')}Agent connection failed: ${esc(e.message)}</p>`;
    statusLine(`Agent connection failed: ${e.message}`);
  },
};

// A refusal asked from home stays on the home page; frames held for a workspace page are dropped.
function refuseAtHome(d) {
  run.held(null);
  document.body.dataset.run = '';
  const ws = parseRoute(run.home).workspace;
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

function renderSlot() {
  $('#answer').innerHTML = `
    <div class="ans-slot">
      <span class="p-label">ANSWER</span>
      ${run.answers.length ? `<p class="ans-text">${run.answers.join('')}</p>` : '<i class="skel" style="width:92%"></i><i class="skel" style="width:70%"></i><span class="muted">Appears here once the distance is measured.</span>'}
    </div>`;
}

// ---- answer header
// The answer's numbers come from evidence fields (measure_refs, in text order, formatted like Python's '.4g');
// each found in the text becomes a button to its evidence.
function numbered(d) {
  let html = '', rest = d.text;
  for (const r of d.measure_refs) {
    const v = r.field.split('.').reduce((o, k) => o?.[k], run.evidence.get(r.eid));
    const m = typeof v === 'number' && rest.match(new RegExp(`(?<![\\d.])${String(+v.toPrecision(4)).replace('.', '\\.')}(?!\\d)`));
    if (!m) continue;
    html += `${esc(rest.slice(0, m.index))}<button class="ans-num mono" data-cite data-eid="${esc(r.eid)}" title="${esc(r.eid)} · ${esc(r.field)}">${m[0]}</button>`;
    rest = rest.slice(m.index + m[0].length);
  }
  return html + esc(rest);
}

function renderAnswer() {
  const abstain = ABSTAIN[run.final];
  const kinds = [...new Set(run.cites.filter((e) => run.evidence.has(e)).map((e) => run.evidence.get(e).kind))];
  const text = run.answers.join('') || (abstain ? `${abstain[0]}<span class="muted"> (${esc(run.lastStep.summary)})</span>` : run.final ? '' : 'Stopped before an answer.');
  // The first two presets of each workspace are answerable ones.
  const instead = PRESETS[ctx.route.workspace].slice(0, 2).filter((q) => q !== run.q);
  $('#answer').innerHTML = `
    <div class="ans-head">${run.final ? say(run.final) : '<span class="verdict say v-stopped">Stopped</span>'}
      <span class="mono muted ans-meta">${count(run.steps.length, 'step')} · ${sec(took())}</span>
      ${run.id ? `<a class="ans-run" href="#/run/${esc(run.id)}">${icon('workflow')}Reasoning</a>` : ''}</div>
    <p class="ans-text">${text}</p>
    ${kinds.length ? `<div class="ans-src">${kinds.map(source).join('')}</div>` : ''}
    ${abstain ? `<p class="ans-instead">${abstain[1]}${instead.map((q) => `<button class="preset">${esc(q)}</button>`).join('')}</p>` : ''}`;
}

function evIds() {
  const claims = [...run.claims.values()];
  return [...new Set([...claims.flatMap((c) => [...c.examined_eids, ...c.predicates.flatMap((p) => p.eids)]), ...run.cites])].filter((e) => run.evidence.has(e));
}
// The timeline's Evidence lane, in wall time.
const evMarks = () => evIds().map((eid) => {
  const e = run.evidence.get(eid);
  return { eid, kind: e.kind, t0_ms: clock.toWall(e.scene_key, e.t0_ms), t1_ms: clock.toWall(e.scene_key, e.t1_ms) };
});

function renderTabs() {
  const last = ctx.route.workspace === 'fleet' ? ['trip', 'Trip'] : ['videos', 'Videos'];
  const tabs = [['answer', 'Answer'], ['evidence', 'Evidence', evIds().length], ['steps', 'Steps', run.steps.length], last];
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
  $('#keyframe .kf-play').onclick = () => clock.seek(clock.toWall(m.scene_key, Math.max(0, m.t0_ms - 2500)));
}

const predText = (p) => `${p.subject_refs.map((s) => esc(s.entity_id)).join('–')} ${esc(OPS[p.operator] ?? p.operator)} ${esc(p.value)} ${esc(p.unit ?? '')}`;
// The value a predicate was decided on: its first evidence with a number.
function predValue(p) {
  const e = p.eids.map((id) => run.evidence.get(id)).find((x) => typeof x?.value === 'number');
  return e ? `<span class="mono pv pv-${e.kind}">${value(e)}</span>` : '<span class="mono pv muted">–</span>';
}

function renderClaims() {
  const claims = [...run.claims.values()];
  if (!claims.length) return;
  if (document.body.dataset.run === 'running') {
    $('#claims').innerHTML = `<h2 class="p-label">CLAIMS SO FAR</h2>${claims.map((c) => {
      const open = c.predicates.filter((p) => p.status !== 'supported' && p.status !== 'contradicted');
      return `<div class="claim-card" data-claim="${esc(c.claim_id)}">${verdict(c.after)}<p class="claim-text">${esc(c.text)}</p>
        ${open.length ? `<p class="claim-needs">Needs a measure of ${open.map(predText).join('; ')}</p>` : ''}</div>`;
    }).join('')}`;
    return;
  }
  $('#claims').innerHTML = claims.map((c) => {
    const at = run.changes.findLast((x) => x.claim_id === c.claim_id)?.step;
    return `<div class="claim" data-claim="${esc(c.claim_id)}">
      <div class="claim-row"><span class="claim-head">${transition(c.before, c.after)}</span>${at ? `<span class="claim-once">at ${esc(at.summary)}</span>` : ''}</div>
      <div class="preds">${c.predicates.map((p) => `<span class="pt">${predText(p)}</span>${predValue(p)}<span class="pst pst-${p.status}">${p.status}</span>`).join('')}</div>
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
    clock.seek(clock.toWall(m.scene_key, m.t0_ms));
    selection.set(m.actor_refs.map((a) => a.entity_id), { source: 'user' });
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
  $('#followup .fu-chips').innerHTML = PRESETS[ws].filter((q) => q !== run.q).slice(0, 2).map((q) => `<button type="button" class="preset">${esc(q)}</button>`).join('');
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
const json = (v) => JSON.stringify(v).replace(/"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[^"\d-]+|./g, (t) => (/^-?\d/.test(t) ? `<span class="num">${t}</span>` : esc(t)));
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

function renderEvidence() {
  const eids = evIds();
  $('#tab-evidence').innerHTML = KINDS.map((k) => {
    const rows = eids.filter((e) => run.evidence.get(e).kind === k);
    return rows.length ? `<section class="ev-group"><h3 class="ev-head ev-${k}">${srcIcon(k)}${srcLabel(k).toUpperCase()} · ${rows.length}</h3>${rows.map(evRow).join('')}</section>` : '';
  }).join('');
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
        ${changed ? `<span class="st-claims">${run.changes.filter((x) => x.step === s).map((x) => transition(x.before, x.after)).join('')}</span>` : ''}</li>`;
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
function flash(eid) {
  const e = run.evidence.get(eid);
  clock.seek(clock.toWall(e.scene_key, e.t0_ms));
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
$('#presets').addEventListener('click', (e) => e.target.closest('.preset') && ask(e.target.closest('.preset').textContent));
$('#answer').addEventListener('click', (e) => e.target.closest('.preset') && ask(e.target.closest('.preset').textContent));
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
document.addEventListener('click', (e) => {
  const c = e.target.closest('.cite, .ans-num, .ev');
  if (c && !e.target.closest('.ev-raw-btn, .ev-raw') && run?.evidence.has(c.dataset.eid)) flash(c.dataset.eid);
});
timeline.onEvidence((eid) => run?.evidence.has(eid) && flash(eid));
document.addEventListener('pointerover', (e) => hover(e.target.closest('.ans-num, .cite, .ev')?.dataset.eid ?? null));
document.addEventListener('pointerout', (e) => !e.relatedTarget && hover(null));
$('#runpage').addEventListener('click', (e) => e.target.closest('.rp-back') && closeRunpage());

async function openCmdk() {
  const items = (await workspaces).flatMap((w) => w.id === 'warehouse'
    ? w.scenes.map((s) => ({ icon: 'warehouse', label: s.title, hint: s.scene_key, run: () => (location.hash = `#/warehouse/${s.scene_key}`) }))
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

onRoute();
