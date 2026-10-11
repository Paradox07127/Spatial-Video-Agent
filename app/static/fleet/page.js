// Fleet workspace page (spec §9.3, board v2-4a). Default: the result layout on the trip with video and the most events.
// Result layout: one big panel on the left (dashcam, map or ego-frame 3D), the map as a small inset in its corner, the
// other panel over the place card in the right column; the overview map moves into its panel. Esc (view state back to
// default) closes it to the full-bleed overview map under the simulated replay clock and restores the map camera.
import { icon } from '../kernel/badges.js';
import { esc, sec, tc } from '../kernel/format.js';
import { state } from '../kernel/state.js';
import { createBuildings } from './buildings.js';
import { en, posAt, tripT, wallOf } from './geo.js';
import { createIntersection } from './intersection.js';
import { createOverview } from './overview.js';
import { createVideo } from './videooverlay.js';
import { owners } from '../warehouse/videos.js';

const FLY_MS = 800;
const STATE = { observed: 'fix', interpolated: 'interpolated', last_known: 'last fix', unknown: 'unknown' };
const CHECKED = { verified: 'yes', conflict: 'conflict', unverified: 'no' };
const PANELS = { video: '.fl-vid', map: '.fl-map', '3d': '.fl-3d' };
// Click-catcher over a small panel; a click makes the panel big.
const GROW = (panel) => `<div class="fl-grow" data-panel="${panel}" title="Enlarge"></div>`;
// Read by the e2e spec: {overview, ix, big}.
export const view = {};
let ctx, root, trips, places, ov, card, ix = null, saved = null, offs = [];
// Resolves once mount has the trips, the map and the building card; the agent's hits can arrive before that.
let ready, read, pendingTrip = null, tripLoadGeneration = 0;

const byId = (id) => trips.find((t) => t.id === id);
const asset = (trip, rel) => read(`api/assets/${trip.id}/${rel}`);

// Cap all route/detail requests, rather than multiplying a trip cap by each trip's asset fan-out.
function boundedReads(api, limit = 6) {
  let active = 0;
  const queue = [];
  const pump = () => {
    while (active < limit && queue.length) {
      const { path, resolve, reject } = queue.shift();
      active++;
      Promise.resolve().then(() => api.get(path)).then(resolve, reject).finally(() => { active--; pump(); });
    }
  };
  return (path) => new Promise((resolve, reject) => { queue.push({ path, resolve, reject }); pump(); });
}

async function loadCore(trip) {
  const result = await Promise.allSettled([read(trip.summary.geojson_url), read(`api/scene/${trip.id}/manifest`)]);
  for (const [i, name] of ['route', 'camera information'].entries()) {
    if (result[i].status === 'rejected') trip.failedAssets.set(name, result[i].reason);
    else trip.failedAssets.delete(name);
  }
  if (result[0].status === 'fulfilled') {
    try {
      const gj = result[0].value, first = gj.features[0];
      if (!Array.isArray(first.properties.replay.coverage_intervals) || !Array.isArray(first.geometry.coordinates)) throw new Error('Invalid trip route');
      Object.assign(trip, { replay: first.properties.replay, line: first.geometry.coordinates,
        pts: gj.features.slice(1).map((f) => f.properties).filter((p) => Number.isFinite(p.lon) && Number.isFinite(p.lat)) });
    } catch (e) { trip.failedAssets.set('route', e); }
  }
  if (result[1].status === 'fulfilled') {
    const mf = result[1].value;
    if (!mf || !Array.isArray(mf.cams) || !mf.assets) trip.failedAssets.set('camera information', new Error('Invalid trip manifest'));
    else { trip.mf = mf; trip.cam = mf.cams[0] ?? null; }
  }
  trip.coreReady = !trip.failedAssets.has('route') && !trip.failedAssets.has('camera information');
  // Events support the overview pins. The larger video/map/entity details remain on demand.
  if (trip.mf.assets.events) {
    try { trip.events = (await read(`api/assets/${trip.id}/${trip.mf.assets.events}`)).events; trip.failedAssets.delete('events'); }
    catch (e) { trip.events = []; trip.failedAssets.set('events', e); }
  }
  return trip;
}

async function loadTrips() {
  read = boundedReads(ctx.api);
  const list = await ctx.api.get('api/fleet/trips');
  return Promise.all(list.map(async (summary) => {
    const id = summary.trip_id, t0 = summary.t0_ms ?? 0, t1 = summary.t1_ms ?? t0 + 1;
    const trip = { id, summary, title: summary.title, mf: { t0_ms: t0, t1_ms: t1, fps: 30, assets: {}, cams: [] },
      replay: { trip_id: id, source_start_ms: t0, replay_start_ms: t0, rate: 1, coverage_intervals: [] },
      line: [], pts: [], cam: null, chunks: new Map(), names: new Map(), events: [], buildings: {}, timeline: null, places: [],
      failedAssets: new Map(), detailsLoaded: false, detailsPromise: null, coreReady: false };
    return loadCore(trip);
  }));
}

async function loadDetails(trip, retry = false) {
  if (!trip.coreReady || trip.detailsLoaded && !retry) return;
  if (trip.detailsPromise) return trip.detailsPromise;
  trip.detailsPromise = (async () => {
    const kinds = ['buildings', 'entities', 'timeline', 'places'].filter((kind) => trip.mf.assets[kind] && (!trip.detailsLoaded || trip.failedAssets.has(kind)));
    const results = await Promise.allSettled(kinds.map((kind) => read(`api/assets/${trip.id}/${trip.mf.assets[kind]}`)));
    results.forEach((result, i) => {
      const kind = kinds[i];
      if (result.status === 'rejected') return trip.failedAssets.set(kind, result.reason);
      try {
        if (!result.value || typeof result.value !== 'object') throw new Error('Invalid trip detail');
        trip.failedAssets.delete(kind);
        if (kind === 'buildings') trip.buildings = result.value.buildings ?? {};
        if (kind === 'timeline') trip.timeline = result.value;
        if (kind === 'places') trip.places = result.value.places ?? [];
        if (kind === 'entities') {
          trip.names = owners(result.value);
        }
      } catch (e) { trip.failedAssets.set(kind, e); }
    });
    trip.detailsLoaded = true;
  })().finally(() => { trip.detailsPromise = null; });
  return trip.detailsPromise;
}

function refreshPlaces() {
  places = new Map(trips.flatMap((t) => t.places.map((p) => [p.place_id, p])).reverse());
}

function renderLoadStatus() {
  const el = root.querySelector('.fl-load-status');
  const failed = trips?.filter((t) => t.failedAssets.size) ?? [];
  el.hidden = !pendingTrip && !failed.length;
  el.innerHTML = `${pendingTrip ? `<p>Loading ${esc(pendingTrip.title ?? pendingTrip.id)}…</p>` : ''}${failed.map((t) =>
    `<p data-trip-error="${esc(t.id)}"><span>${esc(shortId(t.id))}: ${esc([...t.failedAssets.keys()].join(', '))} unavailable.</span> <button class="ghost" type="button" data-retry-trip="${esc(t.id)}">Retry trip</button></p>`).join('')}`;
}

async function retryTrip(trip) {
  const mount = ctx, current = ix?.trip === trip ? ix.moment : null, generation = tripLoadGeneration;
  pendingTrip = trip; renderLoadStatus();
  try {
    if (!trip.coreReady) await loadCore(trip);
    else if (trip.failedAssets.has('events')) {
      try { trip.events = (await read(`api/assets/${trip.id}/${trip.mf.assets.events}`)).events; trip.failedAssets.delete('events'); }
      catch (e) { trip.failedAssets.set('events', e); }
    }
    if (trip.detailsLoaded || current || ctx.route.trip_id === trip.id) await loadDetails(trip, true);
    if (ctx !== mount) return;
    refreshPlaces(); ov.refresh(); addLanes();
    if (generation !== tripLoadGeneration) return;
    if (current) await enterIx(trip, current, true);
    else if (ctx.route.trip_id === trip.id && trip.coreReady) await loadScene(trip.id);
  } finally {
    if (ctx === mount) { pendingTrip = null; renderLoadStatus(); }
  }
}

// fl-pie-set04_video_0007 → set04·0007; other ids unchanged.
const shortId = (id) => id.replace(/^fl-pie-(set\d+)_video_(\d+)$/, '$1·$2');

// Timeline: one lane per trip in replay wall time (GPS fix vs. last_known gap), trips with video first, and every trip's events.
function addLanes() {
  const tl = ctx.timeline;
  for (const t of [...trips].sort((a, b) => !!b.cam - !!a.cam)) {
    const items = [];
    t.pts.forEach((p, i) => {
      const s = p.state === 'last_known' ? 'gap' : 'ok', a = wallOf(t.replay, p.t_ms);
      const b = wallOf(t.replay, t.pts[i + 1]?.t_ms ?? t.replay.coverage_intervals.at(-1)[1]);
      if (items.at(-1)?.[2] === s) items.at(-1)[1] = b;
      else items.push([a, b, s]);
    });
    tl.addLane(`trip:${t.id}`, { label: shortId(t.id), kind: 'trip' });
    tl.setLaneData(`trip:${t.id}`, items);
    document.querySelector(`#timeline [data-lane="trip:${t.id}"]`).style.setProperty('--trip', t.color);
  }
  tl.setEvents(trips.flatMap((t) => t.events.map((e) => ({ ...e, id: `${t.id}:${e.id}`, t0_ms: wallOf(t.replay, e.t0_ms), t1_ms: wallOf(t.replay, e.t1_ms) }))));
}

function chunk(trip, kind, t) {
  const n = String(Math.floor(t / 10000)).padStart(5, '0'), key = `${kind}:${n}`;
  if (!trip.chunks.has(key)) {
    trip.chunks.set(key, kind === 'ego'
      ? asset(trip, `${trip.mf.assets.ego_dir}/${n}.json`).then((j) => j.frames)
      // A 10 s chunk with no detections may have no boxes file.
      : asset(trip, `${trip.mf.assets.boxes_dir}/${trip.cam.id}/${n}.json`).then((j) => j.rows, (err) => {
        if (err.message.startsWith('404 ')) return [];
        throw err;
      }));
    trip.chunks.set(key, trip.chunks.get(key).catch((err) => {
      trip.chunks.delete(key);
      throw err;
    }));
  }
  return trip.chunks.get(key);
}

// Place card: the place under the car (map data), its side and distance from the car, and the C5 reading nearest in time.
function placeHtml(p, t, c5) {
  const pl = places.get(p.place_id);
  if (!pl) return '';
  let meta = [];
  if (pl.lat != null && pl.lon != null) {
    const [e, n] = en([p.lon, p.lat], [pl.lon, pl.lat]);
    const rel = ((((Math.atan2(e, n) * 180) / Math.PI - p.heading) % 360) + 540) % 360 - 180;
    meta = [Math.abs(rel) <= 30 ? 'Ahead' : Math.abs(rel) >= 150 ? 'Behind' : rel > 0 ? 'Right side' : 'Left side', `${Math.round(Math.hypot(e, n))} m`];
  }
  if (pl.geom) meta.push('OSM footprint');
  const r = c5.reduce((best, x) => (!best || Math.abs(x.t_ms - t) < Math.abs(best.t_ms - t) ? x : best), null);
  return `<span class="p-label fl-place-h">${icon('map')}PLACE</span>
    <span class="fl-place-name">${esc(pl.name)}</span>
    ${meta.length ? `<span class="fl-place-meta muted">${meta.join(' · ')}</span>` : ''}
    ${r ? `<div class="fl-model">${icon('cpu')}<div><span>${esc(r.payload.text)}</span>
      <span class="muted" title="${esc(r.verification.method)}">Cosmos C5 · checked by map: ${CHECKED[r.verification.status]}</span></div></div>` : ''}`;
}

async function ixTick(wall) {
  const cur = ix, current = ++cur.tick, t = tripT(cur.trip.replay, wall);
  cur.el.querySelector('.fl-vid').classList.toggle('novid', t == null);
  if (t == null) {
    cur.video.suspend(); cur.frame = null; cur.place.replaceChildren(); cur.placeHtml = null; cur.placeId = undefined;
    cur.facts.textContent = 'No source coverage at this time'; cur.el.querySelector('.fl-3d').style.visibility = 'hidden'; ov.gps(null); ov.place(null); return;
  }
  cur.video.sync(t, ctx.clock.playing, ctx.clock.rate * cur.trip.replay.rate);
  if (cur.frame && Math.floor(cur.frame.t_ms / 10000) !== Math.floor(t / 10000)) cur.video.clear();
  let frames, rows;
  try {
    [frames, rows] = await Promise.all([chunk(cur.trip, 'ego', t), chunk(cur.trip, 'boxes', t)]);
  } catch (err) {
    if (ix !== cur || current !== cur.tick || err?.message === 'Superseded page command') return;
    cur.frame = null;
    cur.video.clear();
    cur.el.querySelector('.fl-3d').style.visibility = 'hidden';
    cur.place.replaceChildren();
    cur.placeHtml = null;
    cur.placeId = undefined;
    cur.facts.textContent = 'Detections unavailable';
    ov.gps(null);
    ov.place(null);
    return;
  }
  const f = frames.findLast((x) => x.t_ms <= t) ?? frames[0];
  if (ix !== cur || current !== cur.tick || wall !== ctx.clock.t || !f || f === cur.frame) return;
  cur.frame = f;
  cur.el.querySelector('.fl-3d').style.visibility = '';
  cur.scene.setFrame(f, ctx.selection.ids);
  if (cur.frames !== frames) cur.scene.setPath((cur.frames = frames));

  const p = posAt(cur.trip.pts, t);
  if (!p) {
    ov.gps(null);
    ov.place(null);
    cur.place.replaceChildren();
    cur.placeHtml = null;
    cur.placeId = undefined;
    cur.facts.textContent = 'GPS unavailable';
    return;
  }
  const pl = places.get(p.place_id), stale = p.state === 'last_known';
  ov.gps([p.lon, p.lat], f.ego.sigma_m);
  if (cur.placeId !== p.place_id) ov.place(places.get((cur.placeId = p.place_id))?.geom);
  cur.facts.innerHTML = `<span class="fl-ft">${tc(t)}</span>
    <span class="fl-fgps${stale ? ' stale' : ''}"><i></i>GPS ${STATE[p.state]}${stale ? ` ${sec(p.age_ms)} ago` : ''} ±${f.ego.sigma_m.toFixed(1)} m</span>
    <span>${p.speed.toFixed(1)} m/s</span>${p.place_id ? `<span>${esc(pl?.name ?? p.place_id)}</span>` : ''}
    <span class="fl-rate">${icon('play')}${ctx.clock.rate}×</span>`;
  const html = placeHtml(p, t, cur.c5);
  if (html !== cur.placeHtml) cur.place.innerHTML = cur.placeHtml = html;
}

// Video annotations follow the decoded source frame; map/GPS and 3D retain the master clock.
async function presented(cur, t, epoch) {
  if (ix !== cur) return;
  const current = cur.videoTick = (cur.videoTick ?? 0) + 1;
  try {
    const [frames, rows] = await Promise.all([chunk(cur.trip, 'ego', t), chunk(cur.trip, 'boxes', t)]);
    if (ix !== cur || current !== cur.videoTick || epoch !== cur.video.epoch) return;
    const f = frames.reduce((best, x) => !best || Math.abs(x.t_ms - t) < Math.abs(best.t_ms - t) ? x : best, null);
    // Ego/box sidecars can be sampled below the video's frame rate. Never bridge a gap over 50 ms.
    if (!f || Math.abs(f.t_ms - t) > 50) return cur.video.clear();
    cur.presented = t;
    cur.video.draw(f, rows.filter((r) => Math.abs(r[0] - t) <= 50));
  } catch { if (ix === cur && current === cur.videoTick && epoch === cur.video.epoch) cur.video.clear(); }
}

function tick(wall) {
  root.querySelector('.fl-wall').textContent = tc(wall);
  ov.update(wall);
  if (ix) ixTick(wall);
}

// Trip tab: the result's trip, its GPS quality over the moment, other trips passing the same place, duration and distance.
async function renderTrip() {
  const el = document.getElementById('tab-trip'), cur = ix;
  if (!cur) return el.replaceChildren();
  const { trip } = cur, m = { ...cur.moment, t1_ms: cur.moment.t1_ms ?? cur.moment.t0_ms + 1 };
  const n = [Math.floor(m.t0_ms / 10000), Math.floor((m.t1_ms - 1) / 10000)];
  let frames;
  try {
    frames = (await Promise.all(Array.from({ length: n[1] - n[0] + 1 }, (_, i) => chunk(trip, 'ego', (n[0] + i) * 10000)))).flat()
      .filter((f) => f.t_ms >= m.t0_ms && f.t_ms < m.t1_ms);
  } catch (err) {
    if (ix === cur && err?.message !== 'Superseded page command') el.textContent = 'Detections unavailable';
    return;
  }
  if (ix !== cur) return;
  const spans = trip.pts.map((q, i) => ({ ...q, end: trip.pts[i + 1]?.t_ms ?? trip.mf.t1_ms }))
    .filter((q) => q.t_ms < m.t1_ms && q.end > m.t0_ms);
  const covered = spans.length && spans[0].t_ms <= m.t0_ms && spans.at(-1).end >= m.t1_ms;
  const stale = spans.some((q) => q.state === 'last_known');
  const quality = stale ? 'GPS used a last-known fix during this interval' : !covered || spans.some((q) => !['observed', 'interpolated'].includes(q.state)) ? 'GPS quality is unknown for this interval' : 'GPS had observed or interpolated fixes throughout this interval';
  const sigma = frames.length ? Math.max(...frames.map((f) => f.ego.sigma_m)) : null;
  const placeId = trip.events.find((e) => e.place_id && e.t0_ms < m.t1_ms && e.t1_ms > m.t0_ms)?.place_id ?? posAt(trip.pts, m.t0_ms)?.place_id;
  const pl = places.get(placeId);
  const others = placeId ? trips.filter((t) => t !== trip && t.pts.some((q) => q.place_id === placeId)) : [];
  const withVideo = others.filter((t) => t.cam).length;
  const meta = [withVideo && `${withVideo} with video`, others.length - withVideo && `${others.length - withVideo} GPS only`].filter(Boolean).join(' · ');
  const dist = trip.line.reduce((s, ll, i) => (i ? s + Math.hypot(...en(trip.line[i - 1], ll)) : 0), 0);
  el.innerHTML = `
    <div class="tt-head"><span class="mono">${esc(trip.id)}</span><b>${esc(trip.title)}</b></div>
    <div class="tt-gps${stale ? ' warn' : ''}"><i></i><span>${quality}</span>${sigma != null ? `<span class="mono muted">±${sigma.toFixed(1)} m</span>` : ''}</div>
    ${placeId ? `<details class="tt-other"><summary>${icon('chevron-right')}<b>Other passes near ${esc(pl?.name ?? placeId)}</b><span class="mono muted">${others.length}</span><span class="muted tt-meta">${meta}</span></summary>
      <ul>${others.map((t) => `<li><span class="mono">${esc(shortId(t.id))}</span> ${esc(t.title)} <span class="muted">${t.cam ? 'video' : 'GPS only'} · ${tc(t.pts.find((q) => q.place_id === placeId).t_ms)}</span></li>`).join('')}</ul></details>` : ''}
    <dl class="tt-facts"><dt>Duration</dt><dd class="mono">${sec(trip.mf.t1_ms - trip.mf.t0_ms)}</dd><dt>Distance</dt><dd class="mono">${dist >= 1000 ? `${(dist / 1000).toFixed(1)} km` : `${Math.round(dist)} m`}</dd></dl>`;
}

async function enterIx(trip, m, force = false) {
  if (!force && ix?.moment === m) return;
  const mount = ctx, owns = ctx.owner ?? (() => true);
  ctx.bus.emit('fleet:active', { scene_key: trip.id, cam_id: trip.cam?.id ?? null, t_ms: m.t0_ms, commit: m.commit !== false });
  leaveIx();
  const token = ++tripLoadGeneration;
  if (!trip.coreReady) return renderLoadStatus();
  pendingTrip = trip; renderLoadStatus();
  await loadDetails(trip);
  if (ctx !== mount || token !== tripLoadGeneration || !owns()) return;
  pendingTrip = null; refreshPlaces(); ov.refreshBuildings(); renderLoadStatus();
  const p = posAt(trip.pts, m.t0_ms);
  if (!p) {
    root.querySelector('.fl-cap').textContent = 'GPS unavailable';
    return;
  }
  root.querySelector('.fl-cap').textContent = 'Dashed ring = GPS uncertainty';
  saved ??= ov.camera();
  root.classList.add('ix');
  const el = document.createElement('div');
  el.className = 'fl-ix';
  el.innerHTML = `<div class="fl-vid"><div class="fl-vwrap"></div><div class="fl-facts mono"></div><span class="fl-novid">No video at this time</span>${GROW('video')}</div>
    <div class="fl-3d"></div><div class="fl-place"></div>`;
  root.querySelector('.fl-map').after(el);
  view.big = 'video';
  const slots = { video: 'big', map: 'inset', '3d': 'side' };
  setSlots(slots);
  ov.frame([p.lon, p.lat]);
  ov.result(trip.id);
  const pick = (bid) => card.focus(bid);
  const cite = m.actor_refs?.[0]?.entity_id ?? null;
  const scene = createIntersection(el.querySelector('.fl-3d'), { cam: trip.cam, origin: trip.line[0], buildings: trip.buildings, onPick: pick, cite });
  el.querySelector('.fl-3d').insertAdjacentHTML('beforeend', GROW('3d'));
  const cur = view.ix = ix = {
    trip, moment: m, el, scene, slots, tick: 0, frame: null, frames: null, placeId: undefined, placeHtml: null, c5: [],
    facts: el.querySelector('.fl-facts'), place: el.querySelector('.fl-place'),
    video: createVideo(el.querySelector('.fl-vwrap'), {
      cam: trip.cam, fps: trip.mf.fps, names: trip.names, onPick: pick, cite,
      onPresented: (t, epoch) => ix && ix.trip === trip && presented(ix, t, epoch),
      onInvalidate: () => { if (ix?.trip === trip) { ix.videoTick = (ix.videoTick ?? 0) + 1; ix.presented = null; } },
      src: ctx.api.url(`api/assets/${trip.id}/${trip.cam.video}`), bname: (bid) => trip.buildings[bid]?.name ?? bid,
    }),
  };
  if (trip.referenceBid) { cur.video.setReferences([trip.referenceBid]); cur.scene.focus(trip.referenceBid); }
  const q = `scene_key=${trip.id}&t_ms=${trip.mf.t0_ms}&window_ms=${trip.mf.t1_ms - trip.mf.t0_ms}`;
  read(`api/model-results?${q}`).then((rs) => {
    if (ix !== cur) return;
    cur.c5 = rs.filter((r) => r.task === 'C5');
    trip.failedAssets.delete('model descriptions'); renderLoadStatus();
    cur.frame = null;
    ixTick(ctx.clock.t);
  }).catch((e) => {
    if (ix === cur && e?.message !== 'Superseded page command') { trip.failedAssets.set('model descriptions', e); renderLoadStatus(); }
  });
  ixTick(ctx.clock.t);
  if (!document.getElementById('tab-trip').hidden) renderTrip();
}

function leaveIx() {
  tripLoadGeneration++;
  pendingTrip = null;
  if (root?.querySelector('.fl-load-status')) renderLoadStatus();
  if (!ix) return;
  ix.scene.dispose();
  ix.video.dispose();
  ix.el.remove();
  view.ix = ix = view.big = null;
  delete root.querySelector('.fl-map').dataset.slot;
  root.classList.remove('ix');
  ov.gps(null);
  ov.place(null);
  ov.result(null);
  document.getElementById('tab-trip').replaceChildren();
}

// slots: panel name -> 'big' | 'inset' | 'side'.
function setSlots(slots) {
  for (const [k, sel] of Object.entries(PANELS)) root.querySelector(sel).dataset.slot = slots[k];
}

// A click on a small panel swaps it with the big one.
function setBig(name) {
  const s = ix.slots;
  [s[view.big], s[name]] = [s[name], 'big'];
  view.big = name;
  setSlots(s);
  ov.map.resize();
}

// Dashcam view of a trip with video at its trip time t_ms (the result layout without an agent moment).
const openTrip = (trip, t0_ms, commit = true) => enterIx(trip, { scene_key: trip.id, t0_ms, commit });

// Default view: the trip with video and the most events, at its first event.
function openDefault() {
  const trip = trips.filter((t) => t.cam && t.coreReady).sort((a, b) => (b.summary.n_events ?? b.events.length) - (a.summary.n_events ?? a.events.length))[0];
  if (!trip) return tick(ctx.clock.t);
  const t0 = Math.min(...trip.events.map((e) => e.t0_ms), Infinity);
  const t = t0 === Infinity ? trip.replay.coverage_intervals[0]?.[0] ?? trip.mf.t0_ms : t0;
  ctx.clock.seek(wallOf(trip.replay, t));
  return openTrip(trip, t, false);
}

function toOverview(resetSource = false) {
  if (resetSource) ctx.bus.emit('fleet:active', { scene_key: null });
  leaveIx();
  card.close();
  ov.hit(null);
  if (saved) ov.restore(saved);
  saved = null;
}

// The result layout keeps its map on the car; only the overview flies.
function flyTo(lngLat) {
  saved ??= ov.camera();
  ov.hit(lngLat);
  if (!ix) ov.fly(lngLat, FLY_MS);
}

export async function mount(el, c) {
  let loaded;
  ready = new Promise((r) => (loaded = r));
  ctx = c;
  pendingTrip = null; tripLoadGeneration++;
  root = el;
  const links = [ctx.api.url('lib/maplibre/maplibre-gl.css'), new URL('./fleet.css', import.meta.url).href].map((href) =>
    document.head.appendChild(Object.assign(document.createElement('link'), { rel: 'stylesheet', href })));
  root.innerHTML = `
    <div class="fl-root">
      <div class="fl-map"></div>
      <div class="fl-clock"><span class="fl-sim" title="Simulated replay clock: trips were not filmed at the same time">Simulated clock</span><span class="mono fl-wall"></span></div>
      <div class="fl-card" hidden></div><div class="fl-load-status" role="status">Loading fleet routes…</div>
    </div>`;
  root = root.firstElementChild;
  [trips] = await Promise.all([loadTrips(), new Promise((r) => {
    const done = () => { clearTimeout(timer); r(); };
    const timer = setTimeout(done, 3000);
    links[1].onload = links[1].onerror = done;
    if (links[1].sheet) done();
  })]);
  view.trips = trips;
  const initial = byId(ctx.route.trip_id) ?? (!['searching', 'hit'].includes(state.view) ? trips.filter((t) => t.cam && t.coreReady).sort((a, b) => (b.summary.n_events ?? b.events.length) - (a.summary.n_events ?? a.events.length))[0] : null);
  if (initial) await loadDetails(initial);
  refreshPlaces();
  renderLoadStatus();

  // fleet.css's --route-1…8 by order in the trip list; MapLibre paint cannot read CSS variables.
  const css = getComputedStyle(root);
  trips.forEach((t, i) => (t.color = css.getPropertyValue(`--route-${(i % 8) + 1}`).trim() || css.getPropertyValue('--ink-2').trim()));
  ctx.clock.bind({ kind: 'fleet', replay: trips.map((t) => t.replay) });
  const ranges = trips.flatMap((t) => t.replay.coverage_intervals.map(([a, b]) => [wallOf(t.replay, a), wallOf(t.replay, b)]));
  ctx.clock.setRange(ranges.length ? Math.min(...ranges.map(([a]) => a)) : 0, ranges.length ? Math.max(...ranges.map(([, b]) => b)) : 1);
  addLanes();
  view.overview = ov = await createOverview(root.querySelector('.fl-map'), {
    trips, api: ctx.api,
    onCar(id) {
      const trip = byId(id), t = tripT(trip.replay, ctx.clock.t);
      ctx.bus.emit('fleet:active', { scene_key: trip.id, cam_id: trip.cam?.id ?? null, t_ms: t });
      if (trip.cam && t != null) openTrip(trip, t);
      else {
        leaveIx();
        ov.select(id);
      }
    },
    onEvent(trip, e) {
      ctx.bus.emit('fleet:active', { scene_key: trip.id, cam_id: trip.cam?.id ?? null, t_ms: e.t0_ms });
      if (trip.cam) openTrip(trip, e.t0_ms); else leaveIx();
      ov.select(trip.id);
      ctx.timeline.highlight(`${trip.id}:${e.id}`);
      ctx.clock.seek(wallOf(trip.replay, e.t0_ms));
    },
    onBuilding: (bid) => card.focus(bid),
  });
  // Added after the map is created: MapLibre wants an empty container.
  root.querySelector('.fl-map').append(Object.assign(document.createElement('span'), { className: 'fl-cap', textContent: trips.some((t) => t.pts.length) ? 'Dashed ring = GPS uncertainty' : 'GPS unavailable' }));
  root.querySelector('.fl-map').insertAdjacentHTML('beforeend', GROW('map'));
  root.addEventListener('click', (e) => {
    const retry = e.target.closest('[data-retry-trip]');
    if (retry) { retry.disabled = true; retryTrip(byId(retry.dataset.retryTrip)).catch(() => {}); return; }
    const g = e.target.closest('.fl-grow');
    if (g && ix) setBig(g.dataset.panel);
  });
  card = createBuildings(root.querySelector('.fl-card'), {
    api: ctx.api,
    tripOf: (bid) => trips.find((t) => bid in t.buildings),
    onFocus(bid, on) {
      ov.focusBuilding(bid, on);
      if (ix) ix.scene.focus(on ? bid : null);
    },
  });
  loaded();
  const onView = (s) => s === 'default' && toOverview();
  const onPlay = (on) => ix?.video.play(on);
  const onTab = (name) => name === 'trip' && renderTrip();
  // Without an agent result the shell's Esc leaves the page; here it first closes the dashcam view to the overview.
  const onKey = (e) => {
    if (e.key !== 'Escape' || !ix || state.view !== 'default' || !document.getElementById('cmdk').hidden || !document.getElementById('runpage').hidden) return;
    e.stopPropagation();
    toOverview(true);
  };
  ctx.bus.on('view:state', onView);
  ctx.bus.on('clock:play', onPlay);
  ctx.bus.on('panel:tab', onTab);
  const onMode = () => { ix?.video.redraw(); if (ix?.frame) ix.scene.setFrame(ix.frame, ctx.selection.ids); };
  ctx.bus.on('stage:mode', onMode);
  const onEvidence = (e) => {
    if (!ix || e.scene_key !== ix.trip.id) return;
    const bid = e.value?.bid;
    ix.trip.referenceBid = bid ?? null;
    ix.video.setReferences(bid ? [bid] : []);
    ix.scene.focus(bid ?? null);
  };
  ctx.bus.on('evidence:focus', onEvidence);
  window.addEventListener('keydown', onKey, true);
  offs = [
    () => window.removeEventListener('keydown', onKey, true),
    ctx.clock.onTick(tick),
    ctx.selection.onChange(() => { ix?.video.redraw(); if (ix?.frame) ix.scene.setFrame(ix.frame, ctx.selection.ids); }),
    () => ctx.bus.off('view:state', onView),
    () => ctx.bus.off('clock:play', onPlay),
    () => ctx.bus.off('panel:tab', onTab),
    () => ctx.bus.off('stage:mode', onMode),
    () => ctx.bus.off('evidence:focus', onEvidence),
    () => links.forEach((l) => l.remove()),
  ];
  if (ctx.route.trip_id) await loadScene(ctx.route.trip_id);
  // A question still running (or its hit) replaces the view once mount returns.
  else if (!['searching', 'hit'].includes(state.view)) await openDefault();
  else tick(ctx.clock.t);
  return () => {
    tripLoadGeneration++;
    leaveIx();
    ov.dispose();
    offs.forEach((off) => off());
    view.overview = null;
  };
}

// The agent's moment: select its trip, seek to it and open the result layout at once (a GPS-only trip: fly the map to it).
export async function applyHits(moments) {
  await ready;
  const m = moments.find((x) => byId(x.scene_key));
  if (!m) return;
  const trip = byId(m.scene_key), p = posAt(trip.pts, m.t0_ms);
  ctx.bus.emit('fleet:active', { scene_key: trip.id, cam_id: trip.cam?.id ?? null, t_ms: m.t0_ms });
  ov.select(trip.id);
  ctx.clock.seek(wallOf(trip.replay, m.t0_ms));
  if (trip.cam) await enterIx(trip, m);
  else {
    leaveIx(); renderLoadStatus();
    if (p) flyTo([p.lon, p.lat]);
    else root.querySelector('.fl-cap').textContent = 'GPS unavailable';
  }
}

export async function applyUI({ cmd, args }) {
  await ready;
  if (cmd === 'seek') {
    const trip = byId(args.trip_id ?? args.scene_key) ?? ix?.trip;
    if (trip) ctx.bus.emit('fleet:active', { scene_key: trip.id, cam_id: trip.cam?.id ?? null, t_ms: args.t_ms, commit: false });
    ctx.clock.seek(trip ? wallOf(trip.replay, args.t_ms) : args.t_ms);
  } else if (cmd === 'focus' && args.latlon) flyTo([args.latlon[1], args.latlon[0]]);
  else if (cmd === 'highlight') {
    const buildings = args.ids.filter((id) => trips.some((t) => id in t.buildings));
    ctx.selection.set(args.ids.filter((id) => !buildings.includes(id)), { source: 'agent' });
    if (buildings.length) await card.focus(buildings[0]);
  } else if (cmd === 'setView') ix?.scene.setView(args.view);
  else if (cmd === 'loadScene') return loadScene(args.scene_key);
}

// Model results are shown by the shell's model panel; the building and place cards fetch their own C5 records.
export function applyModelResults() {}

// #/fleet/<trip_id>@<t_ms>: select the trip, seek to t_ms and open its dashcam view (a GPS-only trip: fly to the car).
// The shell cleared the timeline before calling.
export function loadScene(id) {
  const trip = byId(id);
  addLanes();
  if (!trip) return;
  ov.select(id);
  if (ctx.route.trip_id === id) ctx.clock.seek(wallOf(trip.replay, ctx.route.t_ms));
  const t = tripT(trip.replay, ctx.clock.t) ?? trip.pts[0]?.t_ms ?? trip.mf.t0_ms;
  ctx.bus.emit('fleet:active', { scene_key: trip.id, cam_id: trip.cam?.id ?? null, t_ms: t });
  if (trip.cam) return openTrip(trip, t);
  leaveIx(); renderLoadStatus();
  const p = posAt(trip.pts, t);
  if (p) ov.fly([p.lon, p.lat], FLY_MS);
  else root.querySelector('.fl-cap').textContent = 'GPS unavailable';
}
