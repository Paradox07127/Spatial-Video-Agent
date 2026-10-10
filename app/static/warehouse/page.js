// Warehouse workspace page (spec §9.2): full-bleed 3D stage with a camera rail, Explore | Evidence modes, a Layers popover
// and the stage video dock (one camera on the master clock); synced videos of every hit camera in the panel's Videos tab.
// Page interface: mount(root, ctx) → dispose, applyHits, applyUI, applyModelResults, loadScene, showMeasure.
import { icon, source, srcIcon } from '../kernel/badges.js';
import { esc, tc } from '../kernel/format.js';
import { setLayers, state } from '../kernel/state.js';
import { createStage } from './stage3d.js';
import { createVideos, owners } from './videos.js';
import { shortZone } from './hazard.js';

const AUTO_ACTIONS = ['create_review_task', 'hazard_zone', 'webhook'];
const ZONE_ICON = '<svg class="i" aria-hidden="true"><path d="M12 3l8 6-3 11H7L4 9z"/></svg>';
// [id, label, manifest asset the layer needs]
const LAYERS = [['people', 'People + skeletons'], ['forklift', 'Forklift', 'forklift'], ['measures', 'Agent measures'], ['blind', 'Blind spots', 'visibility'], ['cloud', 'Point cloud', 'points'], ['frusta', 'Camera frustums']];
const SWATCH = { forklift: '<i class="sw sw-lift" title="Bright face is the front"></i>', measures: '<i class="sw sw-measure"></i>', blind: '<i class="sw sw-blind" title="Seen by no camera"></i><i class="sw sw-single" title="Seen by one camera only"></i>' };
const OFF_BY_DEFAULT = new Set(['cloud']);
const TOAST_MS = 8000;
const DOCK_KEY = 'sva.whVideo'; // 'open' | 'collapsed'

let P = null; // the mounted page

const eventId = (e) => `${P.key}:${e.id}`;
// Incident point: mean position of the event's actors at its closest-approach time (or start).
function eventPoint(e) {
  const t = e.params?.t_min_ms ?? e.t0_ms;
  const ps = e.actor_ids.map((id) => P.stage.position(id, t)).filter(Boolean);
  return [ps.reduce((s, p) => s + p[0], 0) / ps.length, ps.reduce((s, p) => s + p[1], 0) / ps.length];
}

// state.layers is shared with other pages; 'wh' marks that the warehouse layers were chosen, so turning all off survives a reload.
const layerOn = (id) => (state.layers.has('wh') ? state.layers.has(`wh.${id}`) : !OFF_BY_DEFAULT.has(id));
// The camera an evidence was read from: its own, else that of its first observation.
const evCam = (e) => e.cam_id ?? e.observation_ids?.[0]?.cam_id ?? null;

function dockOpen() {
  try {
    return localStorage.getItem(DOCK_KEY) !== 'collapsed';
  } catch {
    return true;
  }
}
function storeDockOpen(open) {
  try {
    localStorage.setItem(DOCK_KEY, open ? 'open' : 'collapsed');
  } catch {
    // storage may be unavailable; the dock state still applies to this page
  }
}

export async function mount(root, ctx) {
  const css = Object.assign(document.createElement('link'), { rel: 'stylesheet', href: new URL('./warehouse.css', import.meta.url).href });
  document.head.append(css);
  root.innerHTML = `
    <div class="wh">
      <canvas class="wh-canvas"></canvas>
      <p class="stage-msg wh-loading">Loading scene…</p>
      <div class="wh-hud">
        <nav class="wh-rail" aria-label="Cameras"></nav>
        <div class="wh-bar">
          <nav class="seg wh-mode" aria-label="Stage mode"><button type="button" data-mode="explore" class="on">Explore</button><button type="button" data-mode="evidence" disabled>Evidence</button></nav>
          <span class="wh-focus" hidden></span>
          <nav class="seg wh-views" aria-label="View"></nav>
          <button class="ghost wh-zone" title="Click the floor to add points, double-click to close">${ZONE_ICON}Draw zone</button>
          <span class="wh-zone-chip" hidden></span>
        </div>
        <div class="wh-layers">
          <button type="button" class="ghost wh-layers-btn" aria-expanded="false">Layers${icon('chevron-down')}</button>
          <div class="wh-layers-pop" hidden>
            ${LAYERS.map(([id, label, asset]) => `<label class="wh-layer"${asset ? ` data-asset="${asset}"` : ''}><input type="checkbox" data-layer="${id}">${label}<span class="wh-layer-sw">${SWATCH[id] ?? ''}</span></label>`).join('')}
            <div class="wh-src-legend">${['measure', 'map', 'model', 'human'].map(source).join('')}</div>
          </div>
        </div>
        <section class="wh-stage-video" aria-label="Camera video" hidden><header class="wh-sv-head mono"></header><div class="wh-sv-body"></div></section>
        <button type="button" class="wh-sv-open" hidden></button>
        <div class="wh-toast" role="status" hidden></div>
      </div>
    </div>`;
  const host = root.querySelector('.wh');
  const stage = createStage(host.querySelector('.wh-canvas'));
  P = {
    ctx, host, stage, key: null, ready: null, events: [], triggered: new Set(), pins: [], videos: null, dock: null, dockCam: null, dockOnly: null, hitCams: null, hitIds: null, hitPts: null,
    used: new Set(), focus: null, inEvidence: false, measure: null, toastTimer: 0, models: new Map(), lastT: ctx.clock.t, second: -1, modelLanes: false,
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
    sub('evidence:hover', (eid) => P?.mf && P.stage.setMeasureHot(!!eid && eid === P.measure?.eid)),
    sub('stage:mode', onStageMode),
    sub('panel:tab', () => P?.mf && renderVideos()),
    stage.onPick((id) => id && ctx.selection.set([id], { source: 'user' })),
    () => document.removeEventListener('pointerdown', closeLayers),
  ];
  host.addEventListener('click', onClick);
  host.addEventListener('change', (e) => e.target.matches('input[data-layer]') && setLayer(e.target.dataset.layer, e.target.checked));
  const key = ctx.route.scene_key ?? (await ctx.api.get('api/workspaces')).find((w) => w.id === 'warehouse').scenes[0].scene_key;
  await loadScene(key);
  return () => {
    offs.forEach((off) => off());
    clearTimeout(P.toastTimer);
    players().forEach((v) => v.dispose());
    document.getElementById('tab-videos')?.replaceChildren();
    stage.dispose();
    css.remove();
    P = null;
  };
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
      key, mf, entities, owner: owners(entities), lanes: tl.lanes, events: tl.events, triggered: new Set(acts.actions.map((a) => a.event_id)), pins: [], models: new Map(),
      second: -1, modelLanes: false, used: new Set(), focus: null, measure: null, hitCams: null, hitIds: null, hitPts: null,
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
    setDock();
    renderViews();
    renderRail();
    renderMode();
    renderPins();
    setHazards(acts.zones, null);
    P.host.querySelector('.wh-zone-chip').hidden = true;
    hideToast();
    onTick(clock.t);
    // a selection made while the scene loaded was skipped by onSelection
    onSelection(P.ctx.selection);
    P.host.dataset.scene = key;
    msg.hidden = true;
    // the point cloud (several MB) comes in after the scene shows; a scene loaded meanwhile drops it
    if (mf.assets.points) {
      fetch(api.asset(`${key}/${mf.assets.points}`)).then((r) => r.arrayBuffer()).then((buf) => P?.mf === mf && stage.setPoints(buf));
    }
    return { scene_key: key };
  })();
  P.ready.catch(() => (msg.textContent = 'Scene failed to load'));
  return P.ready;
}

// Players of the scene's videos: the Videos tab and the stage video dock.
const players = () => [P.videos, P.dock].filter(Boolean);

function renderViews() {
  P.host.querySelector('.wh-views').innerHTML = [['free', 'Free'], ['top', 'Top']]
    .map(([v, l]) => `<button data-view="${v}" class="${v === P.stage.view ? 'on' : ''}">${l}</button>`).join('');
}

// Camera rail: a strip of dots that expands to one thumbnail per camera on hover or keyboard focus.
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
  }));
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
  }
  players().forEach((v) => v.sync(t));
  // Watch mode: playback crossing an untriggered severity ≥ 2 event while nobody is asking triggers it once.
  const crossed = clock.playing && state.view === 'default'
    ? P.events.filter((e) => e.severity >= 2 && !P.triggered.has(eventId(e)) && P.lastT < e.t0_ms && t >= e.t0_ms) : [];
  P.lastT = t;
  crossed.forEach(trigger);
}

async function trigger(e) {
  const { api, clock, timeline } = P.ctx, key = P.key;
  P.triggered.add(eventId(e));
  renderPins();
  timeline.highlight(e.id);
  clock.seek(e.t0_ms);
  const r = await api.post('api/actions/trigger', { scene_key: key, event_id: eventId(e), auto: true, actions: AUTO_ACTIONS });
  if (P?.key !== key) return;
  showToast(e, r);
  setHazards((await api.get(`api/actions?scene_key=${key}`)).zones, r.zone_id);
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
  if (P.modelLanes) ['cosmos', 'predict'].forEach((id) => P.ctx.timeline.setLaneData(id, []));
}

// ---- stage modes
// The shell's setStage (app.js), for the stage's own Explore | Evidence switch and focus chip.
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
  if (mode === 'evidence' && P.focus?.eid === eid) enterEvidence();
  else exitEvidence();
}

// Actors an evidence cites: the measured pair, else the owners of its observations.
function cited(e) {
  if (P.measure?.eid === e.eid) return P.measure.ids;
  return [...new Set((e.observation_ids ?? []).map((o) => P.owner.get(`${o.cam_id}:${o.local_track_id}`)).filter(Boolean))];
}

function focusLabel(e, ids) {
  if (e.method.startsWith('ground_dist') && ids.length === 2) return `${ids[0]}–${ids[1]} distance`;
  return e.method.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

// Evidence mode: the cited actors at full strength and framed, the stage video dock on the evidence's camera boxing only them.
function enterEvidence() {
  const e = P.focus, ids = cited(e), cam = P.mf.cams.find((c) => c.id === evCam(e));
  P.inEvidence = true;
  P.stage.setFocus(ids);
  const ps = ids.map((id) => P.stage.position(id, e.t0_ms)).filter(Boolean);
  if (ps.length) P.stage.fit(ps);
  if (cam) setDock(cam, new Set(ids));
  else setDock();
  const chip = P.host.querySelector('.wh-focus');
  chip.innerHTML = `${srcIcon(e.kind)}<span>${esc(focusLabel(e, ids))}</span><button type="button" class="icon-btn wh-focus-clear" title="Back to Explore">${icon('x')}</button>`;
  chip.hidden = false;
  updateRail();
  renderMode();
}

function exitEvidence() {
  if (!P.inEvidence) return;
  P.inEvidence = false;
  P.stage.setFocus(null);
  // back to the hit's framing, else to the view before the evidence
  if (P.hitPts) P.stage.fit(P.hitPts);
  else P.stage.unfit();
  setDock();
  P.host.querySelector('.wh-focus').hidden = true;
  updateRail();
  renderMode();
}

function renderMode() {
  P.host.querySelectorAll('.wh-mode button').forEach((b) => b.classList.toggle('on', (b.dataset.mode === 'evidence') === P.inEvidence));
  P.host.querySelector('.wh-mode [data-mode="evidence"]').disabled = !P.focus;
}

// ---- Videos tab and the stage video dock
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

// "Pin to stage" in the Videos tab: the stage dock opens on that camera.
function pin(id) {
  storeDockOpen(true);
  setDock(P.mf.cams.find((c) => c.id === id));
}

// Cameras the dock offers: the hit's (primary first), else the scene's in manifest order.
const dockCams = () => P.hitCams ?? P.mf.cams;
// Bold boxes in the dock: the evidence's cited actors, else the selection plus the hit's actors.
const dockBold = () => (P.dockOnly ? [...P.dockOnly] : [...P.ctx.selection.ids, ...(P.hitIds ?? [])]);

// Shows camera cam in the stage dock; only: the actor ids an evidence cites (the only ones boxed), null outside Evidence mode.
function setDock(cam = dockCams()[0], only = null) {
  if (cam !== P.dockCam || only || P.dockOnly) {
    P.dock?.dispose();
    P.dock = null;
  }
  P.dockCam = cam;
  P.dockOnly = only;
  renderDock();
}

// The dock is one player while open and a "Video · <cam>" tab while collapsed (no <video> then).
function renderDock() {
  const { api, clock } = P.ctx, cam = P.dockCam, open = dockOpen(), box = P.host.querySelector('.wh-stage-video');
  if (!open) {
    P.dock?.dispose();
    P.dock = null;
  } else if (!P.dock) {
    P.dock = createVideos({ api, clock, key: P.key, mf: P.mf, cams: [cam], lanes: P.lanes, entities: P.entities, only: P.dockOnly });
    box.querySelector('.wh-sv-body').append(P.dock.el);
    P.dock.sync(clock.t);
  }
  P.dock?.highlight(dockBold());
  const fold = `<button type="button" class="icon-btn wh-sv-fold" title="Collapse video">${icon('chevron-down')}</button>`;
  box.querySelector('.wh-sv-head').innerHTML = P.dockOnly
    ? `<span class="wh-sv-src">Source</span><span>· ${esc(cam.id)} · ${tc(P.focus.t0_ms)}</span>
      <button type="button" class="wh-clip" title="Play from 2.5 s before">${icon('play')}clip</button>${fold}`
    : `<nav class="wh-sv-tabs" aria-label="Video camera">${dockCams().map((c) => `<button type="button" data-cam="${esc(c.id)}" class="${c === cam ? 'on' : ''}">${esc(c.id)}</button>`).join('')}</nav>${fold}`;
  box.hidden = !open;
  const tab = P.host.querySelector('.wh-sv-open');
  tab.innerHTML = `${icon('play')}Video · ${esc(cam.id)}`;
  tab.hidden = open;
  P.stage.setActiveCam(cam.id);
  updateRail();
}

function foldDock(open) {
  storeDockOpen(open);
  renderDock();
}

async function drawZone(btn) {
  const { api, timeline } = P.ctx;
  if (btn.classList.contains('on')) return P.stage.drawZone(false);
  btn.classList.add('on');
  const poly = await P.stage.drawZone(true);
  btn.classList.remove('on');
  if (!poly) return;
  const r = await api.post(`api/scene/${P.key}/zone`, { polygon_world: poly, t_range: [P.mf.t0_ms, P.mf.t1_ms] });
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
  else if (t.closest('.wh-mode button')) {
    const b = t.closest('button');
    setStageMode(b.dataset.mode, b.dataset.mode === 'evidence' ? P.focus.eid : null);
  } else if (t.closest('.wh-focus-clear')) setStageMode('explore', null);
  else if (t.closest('.wh-layers-btn')) toggleLayers(P.host.querySelector('.wh-layers-pop').hidden);
  else if (t.closest('.wh-clip')) {
    clock.seek(Math.max(P.mf.t0_ms, P.focus.t0_ms - 2500));
    clock.play();
  } else if (t.closest('.wh-sv-tabs button')) setDock(P.mf.cams.find((c) => c.id === t.closest('button').dataset.cam));
  else if (t.closest('.wh-sv-fold')) foldDock(false);
  else if (t.closest('.wh-sv-open')) foldDock(true);
  else if (t.closest('.wh-toast-view')) {
    const ev = P.events.find((x) => x.id === t.closest('.wh-toast-view').dataset.ev);
    clock.seek(ev.t0_ms);
    timeline.highlight(ev.id);
    P.stage.focus(eventPoint(ev));
    hideToast();
  } else if (t.closest('.wh-zone')) drawZone(t.closest('.wh-zone'));
  else if (t.closest('.wh-zone-clear')) {
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

export async function applyHits(moments) {
  await P.ready;
  const { api, clock } = P.ctx, m = moments[0];
  P.hitCams = [m.primary_cam, ...m.cams.filter((c) => c !== m.primary_cam)].map((id) => P.mf.cams.find((c) => c.id === id));
  P.hitIds = m.actor_refs.map((a) => a.entity_id);
  for (const x of moments) x.cams.forEach((c) => P.used.add(c));
  if (!P.inEvidence) setDock();
  updateRail();
  renderVideos();
  api.get(`api/model-results?scene_key=${P.key}&t_ms=${m.t0_ms}&window_ms=3000`).then((rs) => state.view === 'hit' && addModels(rs));
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
  timeline.addLane('cosmos', { label: 'Cosmos C4', kind: 'model' });
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
  P.measure = { eid: e.eid, ids };
  if (evCam(e)) P.used.add(evCam(e));
  updateRail();
  P.stage.setMeasure({ ids, t0: moment.t0_ms, t1: moment.t1_ms, text: `${+e.value.toPrecision(4)} m ±${+e.sigma.toPrecision(4)}`, sub: `ground distance · ${tc(e.t0_ms)}` }, P.ctx.clock.t);
}

// e2e hooks: client pixels of world points under the active view, and what the stage currently shows.
export const project = (points) => P.stage.project(points);
export const probe = () => P.stage.probe();
