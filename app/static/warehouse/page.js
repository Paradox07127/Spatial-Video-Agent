// Warehouse workspace page (spec §9.2), layout W-A: a stage header (Answer | Explore, scene, facts, view presets, Layers,
// Draw zone), the 3D stage with its camera rail full width on top, and a spatial row below: the source video (one camera on
// the master clock) and the evidence cell. Synced videos of every hit camera stay in the panel's Videos tab.
// Page interface: mount(root, ctx) → dispose, applyHits, applyUI, applyModelResults, loadScene, showMeasure.
import { icon, level, source } from '../kernel/badges.js';
import { esc, tc } from '../kernel/format.js';
import { setLayers, state } from '../kernel/state.js';
import { createStage } from './stage3d.js';
import { createVideos, owners } from './videos.js';
import { shortZone } from './hazard.js';

const AUTO_ACTIONS = ['create_review_task', 'hazard_zone', 'wandb_alert'];
const ZONE_ICON = '<svg class="i" aria-hidden="true"><path d="M12 3l8 6-3 11H7L4 9z"/></svg>';
// [id, label, manifest asset the layer needs]
const LAYERS = [['people', 'People + skeletons'], ['forklift', 'Forklift', 'forklift'], ['measures', 'Agent measures'], ['blind', 'Blind spots', 'visibility'], ['cloud', 'Point cloud', 'points'], ['frusta', 'Camera frustums']];
const SWATCH = { forklift: '<i class="sw sw-lift" title="Bright face is the front"></i>', measures: '<i class="sw sw-measure"></i>', blind: '<i class="sw sw-blind" title="Seen by no camera"></i><i class="sw sw-single" title="Seen by one camera only"></i>' };
const OFF_BY_DEFAULT = new Set(['cloud']);
const TOAST_MS = 8000;
const CLIP_MS = 2500; // the source clip plays from this long before the evidence to this long after it
const LEVEL_KIND = { L3: 'SDG', L2: 'SmartSpaces' };
const CELL_STATE = { visible: 'sees it', occluded: 'occluded', out_of_fov: 'out of view', no_video: 'no video', unknown: 'unknown' };

let P = null; // the mounted page

const eventId = (e) => `${P.key}:${e.id}`;
// Incident point: mean position of the event's actors at its closest-approach time (or start).
function eventPoint(e) {
  const t = e.params?.t_min_ms ?? e.t0_ms;
  const ps = e.actor_ids.map((id) => P.stage.position(id, t)).filter(Boolean);
  return ps.length ? [ps.reduce((s, p) => s + p[0], 0) / ps.length, ps.reduce((s, p) => s + p[1], 0) / ps.length] : null;
}

// state.layers is shared with other pages; 'wh' marks that the warehouse layers were chosen, so turning all off survives a reload.
const layerOn = (id) => (state.layers.has('wh') ? state.layers.has(`wh.${id}`) : !OFF_BY_DEFAULT.has(id));
// The camera an evidence was read from: its own, else that of its first observation.
const evCam = (e) => e.cam_id ?? e.observation_ids?.[0]?.cam_id ?? null;
// Value and σ of a numeric measure as the answer prints them.
const sigmaText = (e) => (e.sigma == null ? '±σ n/a' : `±${+e.sigma.toPrecision(4)}`);
const measureText = (e) => `${+e.value.toPrecision(4)} ${e.unit ?? 'm'} ${sigmaText(e)}`;

export async function mount(root, ctx) {
  const css = Object.assign(document.createElement('link'), { rel: 'stylesheet', href: new URL('./warehouse.css', import.meta.url).href });
  document.head.append(css);
  root.innerHTML = `
    <div class="wh" data-show="3d">
      <header class="wh-head">
        <span class="wh-scene-chip"></span>
        <span class="wh-fact-time mono" title="Scene time"></span><span class="wh-fact-cams mono"></span><span class="wh-fact-warn" hidden></span>
        <span class="wh-focus" hidden></span>
        <nav class="seg wh-views" aria-label="View"></nav>
        <div class="wh-layers">
          <button type="button" class="ghost wh-layers-btn" aria-expanded="false">Layers${icon('chevron-down')}</button>
          <div class="wh-layers-pop" hidden>
            ${LAYERS.map(([id, label, asset]) => `<label class="wh-layer"${asset ? ` data-asset="${asset}"` : ''}><input type="checkbox" data-layer="${id}">${label}<span class="wh-layer-sw">${SWATCH[id] ?? ''}</span></label>`).join('')}
            <div class="wh-src-legend">${['measure', 'map', 'model', 'human'].map(source).join('')}</div>
          </div>
        </div>
        <button class="ghost wh-zone" title="Click the floor to add points, double-click to close, Esc to cancel">${ZONE_ICON}<span class="wh-zone-label">Draw zone</span></button>
        <span class="wh-zone-chip" hidden></span>
      </header>
      <div class="wh-top-slot">
        <div class="wh-stage-3d">
          <nav class="wh-rail" aria-label="Cameras"></nav>
          <div class="wh-view"><canvas class="wh-canvas"></canvas><p class="stage-msg wh-loading">Loading scene…</p></div>
        </div>
      </div>
      <nav class="seg wh-mobile-seg" aria-label="Stage view"><button type="button" data-show="3d" class="on">3D</button><button type="button" data-show="video">Video</button><button type="button" data-show="evidence">Evidence</button></nav>
      <div class="wh-spatial" data-show="video">
        <nav class="seg wh-spatial-tabs" aria-label="Spatial view"><button type="button" data-show="video" class="on">Source video</button><button type="button" data-show="measure">Evidence</button><button type="button" data-show="cameras">Cameras</button><button type="button" data-show="visibility">Visibility</button></nav>
        <div class="wh-spatial-cell wh-spatial-left">
          <section class="wh-stage-video" aria-label="Source video"><header class="wh-sv-head mono"></header><div class="wh-sv-body"></div></section>
        </div>
        <section class="wh-spatial-cell wh-spatial-right" aria-label="Evidence">
          <nav class="seg wh-evidence-tabs" aria-label="Evidence view"><button type="button" data-tab="measure" class="on">Measure</button><button type="button" data-tab="cameras">Cameras</button><button type="button" data-tab="visibility">Visibility</button></nav>
          <div class="wh-evidence-panes">
            <div class="wh-pane wh-pane-measure"></div>
            <div class="wh-pane wh-pane-cameras" hidden></div>
            <div class="wh-pane wh-pane-visibility" hidden></div>
          </div>
        </section>
      </div>
      <div class="wh-toast" role="status" hidden></div>
    </div>`;
  const host = root.querySelector('.wh');
  // The shell's Answer | Explore toggle sits in the stage header while the page is mounted (as on the fleet page).
  const controls = document.querySelector('.stage-controls'), [home, next] = [controls.parentElement, controls.nextSibling];
  controls.classList.add('wh-mode');
  host.querySelector('.wh-head').prepend(controls);
  const stage = createStage(host.querySelector('.wh-canvas'));
  const page = P = {
    ctx, host, stage, controls, key: null, ready: null, events: [], triggered: new Set(), pending: new Set(), pins: [], videos: null, dock: null, dockCam: null, dockOnly: null, grid: null,
    hitCams: null, hitIds: null, hitPts: null, used: new Set(), focus: null, inEvidence: false, measure: null, toastTimer: 0, models: new Map(), lastT: ctx.clock.t, second: -1,
    modelLanes: false, swapped: false, tab: 'measure', clipEnd: null, hover: null, disposed: false,
  };
  const sub = (type, fn) => (ctx.bus.on(type, fn), () => ctx.bus.off(type, fn));
  const closeLayers = (e) => !e.target.closest?.('.wh-layers') && toggleLayers(false);
  document.addEventListener('pointerdown', closeLayers);
  const offs = [
    ctx.clock.onTick(onTick),
    sub('clock:play', () => players().forEach((v) => v.sync(ctx.clock.t))),
    ctx.selection.onChange(onSelection),
    sub('view:state', onView),
    sub('evidence:focus', onFocus),
    sub('observation:frame', ({scene_key,cam_id,t_ms}) => { if(P?.key===scene_key && P.mf.cams.some(c=>c.id===cam_id)) { pin(cam_id); ctx.clock.seek(t_ms); } }),
    sub('evidence:hover', (eid) => P?.mf && P.stage.setMeasureHot(!!eid && eid === P.measure?.eid)),
    sub('stage:mode', onStageMode),
    sub('context:zone-clear', () => P.host.querySelector('.wh-zone-clear')?.click()),
    sub('panel:tab', () => P?.mf && renderVideos()),
    stage.onPick((id) => id && ctx.selection.set([id], { source: 'user' })),
    stage.onHover(onHover),
    () => document.removeEventListener('pointerdown', closeLayers),
  ];
  host.addEventListener('click', onClick);
  host.addEventListener('change', (e) => e.target.matches('input[data-layer]') && setLayer(e.target.dataset.layer, e.target.checked));
  const dispose = () => {
    if (page.disposed) return;
    page.disposed = true;
    offs.forEach((off) => off());
    clearTimeout(page.toastTimer);
    players(page).forEach((v) => v.dispose());
    document.getElementById('tab-videos')?.replaceChildren();
    controls.classList.remove('wh-mode');
    controls.querySelector('[data-mode="evidence"]').disabled = false;
    controls.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.mode === state.stageMode));
    home.insertBefore(controls, next);
    stage.dispose();
    css.remove();
    if (P === page) P = null;
  };
  try {
    const key = ctx.route.scene_key ?? (await ctx.api.get('api/workspaces')).find((w) => w.id === 'warehouse').scenes[0].scene_key;
    await loadScene(key);
  } catch (e) {
    dispose();
    throw e;
  }
  return dispose;
}

export function loadScene(key) {
  const { api, clock } = P.ctx, msg = P.host.querySelector('.wh-loading');
  msg.textContent = 'Loading scene…';
  msg.hidden = false;
  P.ready = (async () => {
    const [mf, entities, tl, acts] = await Promise.all(
      [`api/scene/${key}/manifest`, `api/scene/${key}/entities`, `api/scene/${key}/timeline`, `api/actions?scene_key=${key}`].map((p) => api.get(p)));
    // points, forklift and visibility are optional warehouse assets (CONTRACT §4.2).
    const asset = (k) => mf.assets[k] && api.get(`api/assets/${key}/${mf.assets[k]}`);
    const [skeleton, forklift, visibility, tracks] = await Promise.all([asset('skeleton'), asset('forklift'), asset('visibility'), asset('tracks')]);
    Object.assign(P, {
      key, mf, entities, owner: owners(entities), lanes: tl.lanes, events: tl.events, triggered: new Set(acts.actions.map((a) => a.event_id)), pending: new Set(), pins: [], models: new Map(),
      second: -1, modelLanes: false, used: new Set(), focus: null, measure: null, hitCams: null, hitIds: null, hitPts: null, clipEnd: null, hover: null,
    });
    const { stage } = P;
    stage.setSkeleton(skeleton);
    stage.setForklift(forklift);
    stage.setTracks(tracks);
    stage.loadManifest(mf);
    stage.setPoints(null);
    // layer toggles and their swatches for optional assets the scene lacks
    P.host.querySelectorAll('[data-asset]').forEach((el) => (el.hidden = !mf.assets[el.dataset.asset]));
    for (const [id] of LAYERS) {
      stage.setLayer(id, layerOn(id));
      P.host.querySelector(`input[data-layer="${id}"]`).checked = layerOn(id);
    }
    stage.setVisibility(visibility);
    stage.drawZone(false);
    stage.setMeasure(null);
    exitEvidence();
    P.dock?.dispose();
    P.dock = P.dockCam = null;
    P.grid?.dispose();
    P.grid = null;
    setDock();
    renderScene();
    renderViews();
    renderRail();
    renderMode();
    renderPins();
    setTab(P.tab);
    setHazards(acts.zones, null);
    P.host.querySelector('.wh-zone-chip').hidden = true;
    P.host.querySelector('.wh-fact-warn').hidden = true;
    P.host.querySelector('input[data-layer="cloud"]').disabled = false;
    hideToast();
    onTick(clock.t);
    // a selection made while the scene loaded was skipped by onSelection
    onSelection(P.ctx.selection);
    P.host.dataset.scene = key;
    msg.hidden = true;
    const url = new URL(location.href), t = url.searchParams.get('t_ms');
    if (t != null && url.hash === `#/warehouse/${key}`) {
      clock.seek(+t);
      url.searchParams.delete('t_ms');
      history.replaceState(null, '', url);
    }
    // the point cloud (several MB) comes in after the scene shows; a scene loaded meanwhile drops it
    if (mf.assets.points) {
      fetch(api.asset(`${key}/${mf.assets.points}`))
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.arrayBuffer();
        })
        .then((buf) => P?.mf === mf && stage.setPoints(buf))
        .catch(() => P?.mf === mf && cloudUnavailable());
    }
    return { scene_key: key };
  })();
  P.ready.catch(() => (msg.textContent = 'Scene failed to load'));
  return P.ready;
}

// The stage keeps its floor, people and cameras; only the cloud layer and the block massing are missing.
function cloudUnavailable() {
  const warn = P.host.querySelector('.wh-fact-warn');
  warn.textContent = 'Point cloud unavailable';
  warn.hidden = false;
  P.host.querySelector('input[data-layer="cloud"]').disabled = true;
}

// Players of the scene's videos: the Videos tab, the source video and the Cameras grid.
const players = (page = P) => [page.videos, page.dock, page.grid].filter(Boolean);

// Scene chip: key, evidence level and what the level stands for here.
function renderScene() {
  const lvl = P.mf.evidence_level ?? (P.mf.metric ? 'L3' : 'L2');
  P.host.querySelector('.wh-scene-chip').innerHTML = `<span class="wh-scene-key mono">${esc(P.key)}</span>${level(lvl)}${LEVEL_KIND[lvl] ? `<span class="wh-level-kind">${LEVEL_KIND[lvl]}</span>` : ''}`;
}

// Scene time and how many cameras have video at t.
function renderFacts(t) {
  P.host.querySelector('.wh-fact-time').textContent = tc(t);
  const missing = P.mf.cams.filter((c) => (P.lanes[c.id]?.find(([a, b]) => t >= a && t < b)?.[2] ?? 'no_video') === 'no_video').map((c) => c.id);
  const n = P.mf.cams.length, cams = P.host.querySelector('.wh-fact-cams');
  cams.textContent = missing.length ? `${n - missing.length}/${n} cams ok` : `${n} cams ok`;
  cams.title = missing.length ? `No video: ${missing.join(', ')}` : 'Every camera has video';
}

function renderViews() {
  P.host.querySelector('.wh-views').innerHTML = [['free', 'Free'], ['top', 'Top']]
    .map(([v, l]) => `<button data-view="${v}" class="${v === P.stage.view ? 'on' : ''}">${l}</button>`).join('');
}

// Camera rail: a strip of dots beside the stage that expands to one thumbnail per camera on hover or keyboard focus.
function renderRail() {
  P.host.querySelector('.wh-rail').innerHTML = `<span class="wh-rail-head mono">CAMERAS</span>${P.mf.cams.map((c) => `
    <button type="button" class="wh-rcam" data-view="cam:${esc(c.id)}" title="View from ${esc(c.id)}">
      <i class="wh-rdot"></i><span class="wh-rthumb"><img alt="" hidden></span><span class="wh-rid mono">${esc(c.id)}</span>
    </button>`).join('')}<span class="wh-rail-foot">● used by agent</span>`;
  updateRail();
}

function updateRail() {
  const sel = P.dockCam?.id;
  P.host.querySelectorAll('.wh-rcam').forEach((b) => {
    const id = b.dataset.view.slice(4);
    b.classList.toggle('used', P.used.has(id));
    b.classList.toggle('sel', id === sel);
    b.classList.toggle('on', b.dataset.view === P.stage.view);
  });
}

// frames: moment.cams [{id, frame_url|null}] of the current second.
function railThumbs(frames) {
  for (const { id, frame_url } of frames) {
    const img = P.host.querySelector(`.wh-rcam[data-view="cam:${CSS.escape(id)}"] img`);
    img.hidden = !frame_url;
    if (frame_url && img.getAttribute('src') !== frame_url) img.src = frame_url;
  }
}

function setView(v) {
  const shown = P.stage.setView(v);
  if (shown?.startsWith('cam:')) P.ctx.bus.emit('source:camera', {scene_key:P.key,cam_id:shown.slice(4)});
  P.host.querySelectorAll('.wh-views button').forEach((b) => b.classList.toggle('on', b.dataset.view === shown));
  updateRail();
  return shown;
}

function toggleLayers(open) {
  if (!P?.mf) return;
  P.host.querySelector('.wh-layers-pop').hidden = !open;
  P.host.querySelector('.wh-layers-btn').setAttribute('aria-expanded', open);
}

function setLayer(id, on) {
  const others = [...state.layers].filter((l) => l !== 'wh' && !l.startsWith('wh.'));
  setLayers([...others, 'wh', ...LAYERS.map(([l]) => l).filter((l) => (l === id ? on : layerOn(l))).map((l) => `wh.${l}`)]);
  P.stage.setLayer(id, on);
}

// "Worth a look" pins: rule events of severity ≥ 2 plus agent annotations.
function renderPins() {
  const events = P.events.filter((e) => e.severity >= 2).map((e) => ({
    id: e.id, kind: 'event', xy: eventPoint(e), t_ms: e.t0_ms, text: ` · ${e.id} ${e.type}`, on: P.triggered.has(eventId(e)),
  })).filter((e) => e.xy);
  P.stage.setPins([...events, ...P.pins]);
}

// zones: Zone rows of GET /api/actions; the label and the spread animation sit at the vertex mean.
function setHazards(zones, fresh) {
  P.stage.setHazards(zones.map((z) => ({
    zone_id: z.zone_id, polygon: z.polygon_world,
    center: [0, 1].map((k) => z.polygon_world.reduce((s, p) => s + p[k], 0) / z.polygon_world.length),
  })), fresh);
}

function onTick(t) {
  if (!P.mf) return;
  const { api, clock } = P.ctx;
  P.stage.setTime(t);
  // the clock may sit on t1 itself (end of playback or a time carried over), which is past the last second
  const s = Math.min(Math.floor(t / 1000), Math.ceil(P.mf.t1_ms / 1000) - 1), key = P.key;
  if (s !== P.second) {
    P.second = s;
    api.get(`api/scene/${key}/moment?t=${s * 1000}`).then((m) => {
      if (P?.key !== key || P.second !== s) return;
      const cams = m.cams.map((c) => ({ ...c, frame_url: c.frame_url && api.url(c.frame_url) }));
      P.stage.setFrustumThumbs(cams);
      railThumbs(cams);
    });
    if (P.tab === 'visibility') renderVisibility();
  }
  renderFacts(t);
  players().forEach((v) => v.sync(t));
  if (P.clipEnd !== null && t >= P.clipEnd) {
    P.clipEnd = null;
    if (clock.playing) clock.pause();
  }
  // Watch mode: playback crossing an untriggered severity ≥ 2 event while nobody is asking triggers it once.
  const crossed = clock.playing && state.view === 'default'
    ? P.events.filter((e) => e.severity >= 2 && !P.triggered.has(eventId(e)) && !P.pending.has(eventId(e)) && P.lastT < e.t0_ms && t >= e.t0_ms) : [];
  P.lastT = t;
  crossed.forEach(trigger);
}

async function trigger(e) {
  const page = P, { api, clock, timeline } = page.ctx, key = page.key, id = eventId(e);
  page.pending.add(id);
  timeline.highlight(e.id);
  clock.seek(e.t0_ms);
  try {
    const r = await api.post('api/actions/trigger', { scene_key: key, event_id: id, auto: true, actions: AUTO_ACTIONS });
    if (P !== page || P.key !== key) return;
    P.triggered.add(id);
    renderPins();
    showToast(e, r);
    const acts = await api.get(`api/actions?scene_key=${key}`);
    if (P === page && P.key === key) setHazards(acts.zones, r.zone_id);
  } catch (err) {
    if (P !== page || P.key !== key) return;
    toast(`Action unavailable for ${e.id}: ${err.message}`);
  } finally {
    page.pending.delete(id);
  }
}

// Auto-action toast; no Undo: the backend cannot delete a review task or zone yet.
function showToast(e, r) {
  const box = P.host.querySelector('.wh-toast');
  clearTimeout(P.toastTimer);
  box.innerHTML = `${icon('user-check')}<span>Review task <span class="mono">#${esc(r.review_task_id)}</span> and hazard zone <span class="mono" title="${esc(r.zone_id)}">#${esc(shortZone(r.zone_id))}</span> created</span>
    <span class="mono muted">${esc(e.id)} ${esc(e.type)} · ${tc(e.t0_ms)}</span><button type="button" class="wh-toast-view" data-ev="${esc(e.id)}">View</button>`;
  box.hidden = false;
  P.toastTimer = setTimeout(hideToast, TOAST_MS);
}

// A plain-text toast (a failed action or zone).
function toast(text) {
  const box = P.host.querySelector('.wh-toast');
  clearTimeout(P.toastTimer);
  box.textContent = text;
  box.hidden = false;
  P.toastTimer = setTimeout(hideToast, TOAST_MS);
}

function hideToast() {
  clearTimeout(P.toastTimer);
  P.host.querySelector('.wh-toast').hidden = true;
}

function onSelection({ ids, source }) {
  if (!P?.mf) return;
  P.stage.highlight([...ids], source === 'user');
  P.videos?.highlight(ids);
  P.dock?.highlight(dockBold());
}

function onView(s) {
  if (s === 'hit' || !P?.mf) return;
  P.hitCams = P.hitIds = P.hitPts = null;
  renderVideos();
  if (!P.inEvidence) setDock();
  P.models.clear();
  P.stage.setModelResults([]);
  P.stage.setMeasure(null);
  P.measure = P.focus = null;
  P.used.clear();
  exitEvidence();
  P.stage.unfit();
  updateRail();
  renderMode();
  renderMeasure();
  if (P.modelLanes) ['cosmos', 'predict'].forEach((id) => P.ctx.timeline.setLaneData(id, []));
}

// ---- stage modes
// The shell's setStage (app.js), for the page's focus chip and for Answer on the last focused evidence.
function setStageMode(mode, eid) {
  if (state.stageMode === mode && state.focusEid === eid) return;
  state.stageMode = mode;
  state.focusEid = eid;
  document.body.dataset.stage = mode;
  document.querySelectorAll('.ev').forEach((r) => r.classList.toggle('sel', r.dataset.eid === eid));
  P.ctx.bus.emit('stage:mode', { mode, eid });
}

function onFocus(e) {
  if (!P?.mf || e.scene_key !== P.key) return;
  P.focus = e;
  if (evCam(e)) P.used.add(evCam(e));
  if (state.stageMode === 'evidence' && state.focusEid === e.eid) enterEvidence();
  else setStageMode('evidence', e.eid);
}

function onStageMode({ mode, eid }) {
  if (!P?.mf) return;
  P.stage.setAnswerMode(mode === 'evidence');
  if (mode === 'evidence' && P.focus?.eid === eid) enterEvidence();
  else exitEvidence();
  renderMode();
}

// An actor reference of a request or value: an entity_id, or {scene_key, entity_id}; null unless it names an actor of this scene.
function actorOf(ref) {
  const id = typeof ref === 'string' ? ref : ref?.entity_id;
  if (typeof id !== 'string' || ref?.scene_key && ref.scene_key !== P.key) return null;
  return P.entities.entities.some((actor) => actor.entity_id === id) ? id : null;
}

// Actors an evidence cites: a measured pair, observed owners, or the actors named by an explicit measured tool.
function cited(e) {
  if (P.measure?.eid === e.eid) return P.measure.ids;
  const observed = [...new Set((e.observation_ids ?? []).map((o) => P.owner.get(`${o.cam_id}:${o.local_track_id}`, e.t0_ms)).filter(Boolean))];
  if (observed.length || e.kind !== 'measure') return observed;
  // a distance names its pair in the request (params) or, for closest_approach, in its value
  const refs = e.method.startsWith('ground_dist') || e.method === 'closest_approach' ? [(e.params ?? e.value)?.a, (e.params ?? e.value)?.b]
    : e.method === 'ground_position' ? [e.value?.entity_id ?? e.params?.target] : e.method === 'track_speed' ? [e.params?.a] : [];
  const ids = refs.map(actorOf);
  return ids.every(Boolean) ? ids : [];
}

function focusLabel(e, ids) {
  if (e.method.startsWith('ground_dist') && ids.length === 2) return `${ids[0]}–${ids[1]} distance`;
  return e.method.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

// Answer mode: the cited actors at full strength and framed, the source video on the evidence's camera boxing only them,
// the evidence cell on the evidence's detail.
function enterEvidence() {
  const e = P.focus, ids = cited(e), cam = P.mf.cams.find((c) => c.id === evCam(e));
  P.inEvidence = true;
  P.stage.setFocus(ids);
  const ps = ids.map((id) => P.stage.position(id, e.t0_ms)).filter(Boolean);
  if (ps.length) P.stage.fit(ps);
  if (cam) setDock(cam, new Set(ids));
  else setDock();
  const chip = P.host.querySelector('.wh-focus');
  // the focus chip carries the cited measurement: what, value ±σ, its source
  chip.innerHTML = `<span class="wh-focus-label">${esc(focusLabel(e, ids))}</span>${typeof e.value === 'number' ? `<span class="wh-fact-measure mono">${esc(measureText(e))}</span>` : ''}${source(e.kind)}
    <button type="button" class="icon-btn wh-focus-clear" title="Back to Explore">${icon('x')}</button>`;
  chip.hidden = false;
  P.host.classList.add('wh-answer');
  setTab('measure');
  updateRail();
  renderMode();
}

function exitEvidence() {
  if (!P.inEvidence) return;
  P.inEvidence = false;
  P.clipEnd = null;
  P.stage.setFocus(null);
  // back to the hit's framing, else to the view before the evidence
  if (P.hitPts) P.stage.fit(P.hitPts);
  else P.stage.unfit();
  setDock();
  P.host.querySelector('.wh-focus').hidden = true;
  P.host.classList.remove('wh-answer');
  renderMeasure();
  updateRail();
  renderMode();
}

// The header toggle follows the page: Answer is on while an evidence is shown and offered once one was focused.
function renderMode() {
  P.controls.querySelectorAll('button').forEach((b) => b.classList.toggle('on', (b.dataset.mode === 'evidence') === P.inEvidence));
  P.controls.querySelector('[data-mode="evidence"]').disabled = !P.focus;
}

// ---- Videos tab and the source video
function renderVideos() {
  const pane = document.getElementById('tab-videos');
  P.videos?.dispose();
  P.videos = null;
  pane.replaceChildren();
  if (!P.hitCams || state.panelTab !== 'videos') return;
  const { api, clock, selection } = P.ctx;
  P.videos = createVideos({ api, clock, key: P.key, mf: P.mf, cams: P.hitCams, lanes: P.lanes, entities: P.entities });
  P.videos.el.addEventListener('click', (e) => e.target.closest('.wh-pip-btn') && pin(e.target.closest('.wh-pip-btn').dataset.cam));
  pane.append(P.videos.el);
  pane.insertAdjacentHTML('beforeend', '<p class="wh-vnote">All videos follow the timeline. Selected people are drawn bold in every view.</p>');
  P.videos.highlight(selection.ids);
  P.videos.sync(clock.t);
}

// "Pin to stage" in the Videos tab: the source video switches to that camera.
function pin(id) {
  setDock(P.mf.cams.find((c) => c.id === id));
}

// Cameras the source video offers: the hit's (primary first), else the scene's in manifest order.
const dockCams = () => P.hitCams ?? P.mf.cams;
// Bold boxes in the source video: the evidence's cited actors, else the selection plus the hit's actors.
const dockBold = () => (P.dockOnly ? [...P.dockOnly] : [...P.ctx.selection.ids, ...(P.hitIds ?? [])]);

// Shows camera cam in the source video; only: the actor ids an evidence cites (the only ones boxed), null outside Answer mode.
function setDock(cam = dockCams()[0], only = null) {
  if (cam !== P.dockCam || only || P.dockOnly) {
    P.dock?.dispose();
    P.dock = null;
  }
  P.dockCam = cam;
  P.ctx.bus.emit('source:camera', {scene_key:P.key,cam_id:cam?.id ?? null});
  P.dockOnly = only;
  renderDock();
}

function renderDock() {
  const { api, clock } = P.ctx, cam = P.dockCam, box = P.host.querySelector('.wh-stage-video');
  if (!P.dock) {
    P.dock = createVideos({ api, clock, key: P.key, mf: P.mf, cams: [cam], lanes: P.lanes, entities: P.entities, only: P.dockOnly });
    box.querySelector('.wh-sv-body').append(P.dock.el);
    P.dock.sync(clock.t);
  }
  P.dock.highlight(dockBold());
  const e = P.focus, swap = '<button type="button" class="ghost wh-swap-btn">Swap with 3D</button>';
  box.querySelector('.wh-sv-head').innerHTML = P.dockOnly
    ? `<span class="wh-sv-src">Source</span><span>· ${esc(cam.id)} · ${tc(e.t0_ms)}</span>${typeof e.value === 'number' ? `<span class="wh-sv-tag">${esc(measureText(e))}</span>` : ''}
      <button type="button" class="wh-clip" title="Play from 2.5 s before to 2.5 s after">${icon('play')}clip</button>${swap}`
    : `<nav class="wh-sv-tabs" aria-label="Video camera">${dockCams().map((c) => `<button type="button" data-cam="${esc(c.id)}" class="${c === cam ? 'on' : ''}">${esc(c.id)}</button>`).join('')}</nav>${swap}`;
  P.stage.setActiveCam(cam.id);
  updateRail();
}

// Swaps the 3D stage and the source video between the top slot and the left cell.
function swap() {
  P.swapped = !P.swapped;
  const top = P.host.querySelector('.wh-top-slot'), left = P.host.querySelector('.wh-spatial-left');
  const stage = P.host.querySelector('.wh-stage-3d'), video = P.host.querySelector('.wh-stage-video');
  top.append(P.swapped ? video : stage);
  left.append(P.swapped ? stage : video);
}

// ---- evidence cell: Measure | Cameras | Visibility; in Answer mode the Measure pane is the cited evidence's detail
function setTab(tab) {
  P.tab = tab;
  P.host.querySelectorAll('.wh-evidence-tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
  for (const t of ['measure', 'cameras', 'visibility']) P.host.querySelector(`.wh-pane-${t}`).hidden = t !== tab;
  if (tab !== 'cameras') {
    P.grid?.dispose();
    P.grid = null;
  } else if (!P.grid) {
    const { api, clock } = P.ctx;
    P.grid = createVideos({ api, clock, key: P.key, mf: P.mf, cams: P.mf.cams, lanes: P.lanes, entities: P.entities });
    P.host.querySelector('.wh-pane-cameras').append(P.grid.el);
    P.grid.highlight(P.ctx.selection.ids);
    P.grid.sync(clock.t);
  }
  if (tab === 'measure') renderMeasure();
  if (tab === 'visibility') renderVisibility();
  renderSpatialTabs();
}

// 851–1200 px: the spatial row is one cell, showing the source video or one evidence tab.
function setSpatial(show) {
  P.host.querySelector('.wh-spatial').dataset.show = show === 'video' ? 'video' : 'evidence';
  if (show === 'video') renderSpatialTabs();
  else setTab(show);
}

function renderSpatialTabs() {
  const show = P.host.querySelector('.wh-spatial').dataset.show === 'video' ? 'video' : P.tab;
  P.host.querySelectorAll('.wh-spatial-tabs button').forEach((b) => b.classList.toggle('on', b.dataset.show === show));
}

// ≤ 850 px: one slot under the header shows the 3D stage, the source video or the evidence cell.
function setMobile(show) {
  if (P.swapped) swap();
  P.host.dataset.show = show;
  P.host.querySelectorAll('.wh-mobile-seg button').forEach((b) => b.classList.toggle('on', b.dataset.show === show));
}

const row = (label, value) => `<dt>${label}</dt><dd>${value}</dd>`;
const point = (id, t) => {
  const p = P.stage.position(id, t);
  return row(`${esc(id)} ground point`, p ? `(${p[0].toFixed(2)}, ${p[1].toFixed(2)}) m` : 'no track');
};

// Measure pane: in Answer mode the focused evidence; in Explore the agent's measured distance, if any.
function renderMeasure() {
  const pane = P.host.querySelector('.wh-pane-measure'), e = P.inEvidence ? P.focus : P.measure?.e;
  if (!e) {
    pane.innerHTML = '<p class="wh-pane-note">No measurement yet. Ask about a distance, or click Answer on a cited number.</p>';
    return;
  }
  if (e.method === 'floor_cell_visibility') {
    pane.innerHTML = cellDetail(e);
    return;
  }
  const ids = P.inEvidence ? cited(e) : P.measure.ids;
  pane.innerHTML = `
    <p class="wh-pane-head">${source(e.kind)}<b>${esc(focusLabel(e, ids))}</b>${typeof e.value === 'number' ? `<span class="mono">${esc(measureText(e))}</span>` : ''}</p>
    <dl class="wh-measure-detail mono">
      ${ids.map((id) => point(id, e.t0_ms)).join('')}
      ${row('Sample time', tc(e.t0_ms))}
      ${row('Sigma', e.sigma == null ? '±σ n/a' : `±${+e.sigma.toPrecision(4)} ${esc(e.unit ?? 'm')}`)}
      ${row('Method', esc(e.method))}
      ${row('Camera', esc(evCam(e) ?? 'multi-cam'))}
    </dl>`;
}

// A blind-spot evidence: the floor cell, and which cameras see it at the sample time and which do not.
function cellDetail(e) {
  const cell = e.value?.cell, states = cell && P.stage.cellStates(cell, e.t0_ms);
  return `
    <p class="wh-pane-head">${source(e.kind)}<b>Floor cell ${cell ? `(${cell.join(', ')})` : ''}</b><span class="mono">${tc(e.t0_ms)}</span></p>
    ${states ? camStates(states) : '<p class="wh-pane-note">No visibility data for this cell.</p>'}
    <dl class="wh-measure-detail mono">${row('Method', esc(e.method))}${row('Camera', esc(evCam(e) ?? 'multi-cam'))}</dl>`;
}

const camStates = (states) => `<ul class="wh-cam-states">${states.map(({ cam, state: s }) => `<li class="${s === 'visible' ? 'seen' : ''}"><span class="mono">${esc(cam)}</span><span>${CELL_STATE[s] ?? esc(s)}</span></li>`).join('')}</ul>`;

// Visibility pane: blind cells now, and the cameras that see the floor cell under the pointer.
function renderVisibility() {
  const pane = P.host.querySelector('.wh-pane-visibility');
  if (!P.mf.assets.visibility) {
    pane.innerHTML = '<p class="wh-pane-note">This scene has no camera visibility data.</p>';
    return;
  }
  const t = P.ctx.clock.t, cell = P.hover && P.stage.cellOf(P.hover), states = cell && P.stage.cellStates(cell, t);
  pane.innerHTML = `
    <p class="wh-pane-head">${source('measure')}<b><span class="mono">${P.stage.probe().blind_cells}</span> floor cells seen by no camera</b><span class="mono">${tc(t)}</span></p>
    ${states ? `<p class="wh-pane-note">Cell under the pointer <span class="mono">(${cell.join(', ')})</span></p>${camStates(states)}` : '<p class="wh-pane-note">Point at the floor to see which cameras see that cell.</p>'}`;
}

function onHover(xy) {
  const cell = (xy && P?.mf && P.stage.cellOf(xy)) ?? null, was = P.hover && P.stage.cellOf(P.hover);
  P.hover = xy;
  if (P.tab === 'visibility' && String(cell) !== String(was)) renderVisibility();
}

async function drawZone(btn) {
  const { api, timeline, bus } = P.ctx;
  if (btn.classList.contains('on')) return P.stage.drawZone(false);
  btn.classList.add('on');
  P.host.classList.add('wh-drawing');
  const poly = await P.stage.drawZone(true);
  P.host.classList.remove('wh-drawing');
  btn.classList.remove('on');
  if (!poly) return;
  bus.emit('context:zone', poly);
  let r;
  try {
    r = await api.post(`api/scene/${P.key}/zone`, { polygon_world: poly, t_range: [P.mf.t0_ms, P.mf.t1_ms] });
  } catch (err) {
    bus.emit('context:zone', null);
    P.stage.drawZone(false);
    toast(`Zone not saved: ${err.message}`);
    return;
  }
  const inZone = new Set(r.events_in_zone.map((e) => e.event_id.slice(e.event_id.indexOf(':') + 1)));
  const events = P.events.filter((e) => inZone.has(e.id));
  timeline.setEvents(events);
  timeline.showEvents(true);
  const chip = P.host.querySelector('.wh-zone-chip');
  chip.innerHTML = `${events.length ? `<span class="mono">${events.length}</span> event${events.length === 1 ? '' : 's'} in zone` : 'No events in zone'}<button class="icon-btn wh-zone-clear" title="Clear zone"><svg class="i"><use href="icons.svg#x"/></svg></button>`;
  chip.hidden = false;
}

function onClick(e) {
  const { clock, timeline } = P.ctx;
  const t = e.target;
  if (t.closest('.wh-views button, .wh-rcam')) setView(t.closest('[data-view]').dataset.view);
  // the shell has set its Answer stage already; Answer here returns to the last focused evidence
  else if (t.closest('.wh-mode [data-mode="evidence"]')) setStageMode('evidence', P.focus.eid);
  else if (t.closest('.wh-focus-clear')) setStageMode('explore', null);
  else if (t.closest('.wh-layers-btn')) toggleLayers(P.host.querySelector('.wh-layers-pop').hidden);
  else if (t.closest('.wh-clip')) {
    clock.seek(Math.max(P.mf.t0_ms, P.focus.t0_ms - CLIP_MS));
    P.clipEnd = Math.min(P.mf.t1_ms, P.focus.t1_ms + CLIP_MS);
    clock.play();
  } else if (t.closest('.wh-swap-btn')) swap();
  else if (t.closest('.wh-sv-tabs button')) setDock(P.mf.cams.find((c) => c.id === t.closest('button').dataset.cam));
  else if (t.closest('.wh-evidence-tabs button')) setTab(t.closest('button').dataset.tab);
  else if (t.closest('.wh-spatial-tabs button')) setSpatial(t.closest('button').dataset.show);
  else if (t.closest('.wh-mobile-seg button')) setMobile(t.closest('button').dataset.show);
  else if (t.closest('.wh-toast-view')) {
    const ev = P.events.find((x) => x.id === t.closest('.wh-toast-view').dataset.ev);
    clock.seek(ev.t0_ms);
    timeline.highlight(ev.id);
    P.stage.focus(eventPoint(ev));
    hideToast();
  } else if (t.closest('.wh-zone')) drawZone(t.closest('.wh-zone'));
  else if (t.closest('.wh-zone-clear')) {
    P.ctx.bus.emit('context:zone', null);
    P.stage.drawZone(false);
    timeline.setEvents(P.events);
    timeline.showEvents(false);
    P.host.querySelector('.wh-zone-chip').hidden = true;
  } else if (t.closest('.wh-pin')) {
    const pin = t.closest('.wh-pin');
    clock.seek(+pin.dataset.t);
    if (P.events.some((x) => x.id === pin.dataset.pin)) timeline.highlight(pin.dataset.pin);
  } else if (t.closest('.wh-ask-this')) {
    // The kernel selection already holds the object; the question goes through the shell's Ask box.
    const input = document.getElementById('ask-input');
    input.value = `About ${t.closest('.wh-ask-this').dataset.id}: `;
    input.focus();
  }
}

export async function applyHits(moments, owns = () => true) {
  await P.ready;
  const { api } = P.ctx, m = moments[0];
  // a moment without a known primary camera shows its first camera with video
  const known = (id) => P.mf.cams.find((c) => c.id === id);
  const primary = known(m.primary_cam) ?? m.cams.map(known).find((c) => c?.video) ?? P.mf.cams.find((c) => c.video) ?? P.mf.cams[0];
  P.hitCams = [primary, ...m.cams.map(known).filter((c) => c && c !== primary)];
  P.hitIds = m.actor_refs.map((a) => a.entity_id);
  const measure = P.measure, match = measure && moments.find((x) => x.t0_ms <= measure.e.t0_ms && measure.e.t0_ms < x.t1_ms);
  if (match) showMeasure(measure.e, measure.ids, match);
  for (const x of moments) x.cams.forEach((c) => P.used.add(c));
  if (!P.inEvidence) setDock();
  updateRail();
  renderVideos();
  api.get(`api/model-results?scene_key=${P.key}&t_ms=${m.t0_ms}&window_ms=3000`).then((rs) => state.view === 'hit' && addModels(rs)).catch((e) => {
    if (owns() && P && e?.message !== 'Superseded page command') toast(`Model results unavailable: ${e.message}`);
  });
  const ps = m.actor_refs.map((a) => P.stage.position(a.entity_id, m.t0_ms)).filter(Boolean);
  if (ps.length && state.agentCamera) P.stage.fit((P.hitPts = ps));
}

// Executes an agent ui command (schemas/ui_cmd.json) and returns the state actually reached.
export function applyUI({ cmd, args }) {
  const { clock, selection } = P.ctx;
  // the user has taken the camera over
  if (!state.agentCamera && (cmd === 'focus' || cmd === 'setView')) return { view: P.stage.view };
  switch (cmd) {
    case 'seek':
      clock.seek(args.t_ms);
      return { t_ms: clock.t };
    case 'highlight':
      selection.set(args.ids, { source: 'agent' });
      return { ids: [...selection.ids] };
    case 'focus': {
      if (args.cam_id) return { view: setView(`cam:${args.cam_id}`) };
      const target = args.entity_id ? P.stage.position(args.entity_id, clock.t) : args.point;
      return { target: target && P.stage.focus(target, args.ms) };
    }
    case 'showTrack':
      return { entity_id: args.entity_id, points: P.stage.showTrack(args.entity_id, args.t0_ms, args.t1_ms) };
    case 'setView':
      return { view: setView(args.view) };
    case 'annotate':
      if (args.kind === 'zone') {
        P.ctx.api.get(`api/actions?scene_key=${P.key}`).then((a) => setHazards(a.zones, args.zone_id));
        return { kind: 'zone', zone_id: args.zone_id };
      }
      if (!args.xy) return { kind: args.kind, shown: false };
      P.pins.push({ id: `a${P.pins.length}`, kind: 'label', xy: args.xy, t_ms: args.t_ms, text: args.text, on: false });
      renderPins();
      return { kind: args.kind, shown: true };
    case 'loadScene':
      location.hash = `#/warehouse/${args.scene_key}`;
      return { scene_key: args.scene_key };
  }
  return null;
}

// Model primitives of the hit: the agent's records plus the scene's records around the moment (GET /api/model-results).
function addModels(results) {
  for (const r of results) P.models.set(r.record_id, r);
  P.stage.setModelResults([...P.models.values()]);
}

export function applyModelResults(results) {
  const { timeline } = P.ctx;
  addModels(results);
  const c4 = results.filter((r) => r.task === 'C4').flatMap((r) => r.payload.segments.map((s) => [s.start_s * 1000, s.end_s * 1000, 'model']));
  const c2 = results.filter((r) => r.task === 'C2').map((r) => [r.payload.max_frame_ms, r.payload.max_frame_ms + r.payload.horizon_s * 1000, 'model']);
  timeline.addLane('cosmos', { label: 'Model says ≈ · unmeasured', kind: 'model' });
  timeline.addLane('predict', { label: 'Prediction', kind: 'model' });
  timeline.setLaneData('cosmos', c4);
  timeline.setLaneData('predict', c2);
  P.modelLanes = true;
}

// A ground_distance measure evidence between actors ids [a, b], drawn over the hit moment that holds it, labelled like the
// answer; hovering the evidence lights it, focusing it adds what was measured and when.
export async function showMeasure(e, ids, moment) {
  await P.ready;
  if (e.scene_key !== P.key) return;
  P.measure = { eid: e.eid, e, ids };
  if (evCam(e)) P.used.add(evCam(e));
  updateRail();
  P.stage.setMeasure({ ids, t0: moment?.t0_ms ?? e.t0_ms, t1: moment?.t1_ms ?? e.t1_ms, text: measureText(e), sub: `ground distance · ${tc(e.t0_ms)}` }, P.ctx.clock.t);
  if (!P.inEvidence) renderMeasure();
}

// e2e hooks: client pixels of world points under the active view, and what the stage currently shows.
export const project = (points) => P.stage.project(points);
export const probe = () => P.stage.probe();
