// Street workspace page (evidence level L1, layout S-A). A stage header holds the Answer/Explore toggle, the scene, its time
// and what an L1 scene cannot measure. Under it one 2D camera's video, chunk by chunk, slaved to the scene clock, with its
// detection boxes (class and score; no track ids, no geometry), the agent's moments as buttons, and a spatial row: per-second
// person and vehicle counts (Activity) and the Evidence cell.
import { esc, tc, count } from '../kernel/format.js';
import { state } from '../kernel/state.js';
import { createMediaStatus } from '../kernel/media.js';
import { source, level } from '../kernel/badges.js';

const WINDOW_MS = 10000, ACTIVITY_MS = 60000;
const MEASURE = '#56B4E9';
// gen_fixture.py street_clips: a frame's vehicle count is its boxes of these labels.
const VEHICLES = new Set(['bicycle', 'car', 'motorcycle', 'bus', 'truck']);
// Commands on objects or the 3D camera; an L1 scene has neither track ids nor geometry.
const IGNORED = new Set(['highlight', 'annotate', 'focus', 'setView', 'showTrack']);

let ctx, root, video, media, ovl, S = null, ready = null, offs = [], resetFrames = () => {};

// Detection rows of the 10 s window holding t, fetched once per window, as {times (sorted), at: Map(t_ms -> rows)}.
function dets(s, t) {
  const w = Math.floor(t / WINDOW_MS);
  if (!s.dets.has(w)) {
    const q = `cam=${encodeURIComponent(s.cam.id)}&t0=${w * WINDOW_MS}&t1=${(w + 1) * WINDOW_MS}`;
    s.dets.set(w, ctx.api.get(`api/street/${s.key}/detections?${q}`).then(({ rows }) => {
      // The API omits rows for empty frames; retain the manifest's sampled frame times too.
      const at = new Map(), dt = 1000 / s.mf.fps;
      for (let i = Math.ceil(w * WINDOW_MS / dt); i * dt < Math.min((w + 1) * WINDOW_MS, s.mf.t1_ms); i++) at.set(Math.round(i * dt), []);
      for (const r of rows) at.set(r[0], [...(at.get(r[0]) ?? []), r]);
      return { times: [...at.keys()].sort((a, b) => a - b), at };
    }).catch((err) => {
      s.dets.delete(w);
      throw err;
    }));
  }
  return s.dets.get(w);
}

function rowsAt({ times, at }, t) {
  const ft = times.reduce((best, x) => (best == null || Math.abs(x - t) < Math.abs(best - t) ? x : best), null);
  return ft != null && Math.abs(ft - t) <= 500 / S.mf.fps ? at.get(ft) : [];
}

// Boxes come in source pixels; the canvas backing store follows the displayed video (fitOverlay).
function draw(rows) {
  const c = ovl.canvas, k = c.width / S.cam.w, px = c.width / Math.max(1, c.clientWidth);
  ovl.clearRect(0, 0, c.width, c.height);
  const shown = S.boxMode === 'off' ? [] : S.boxMode === 'cited' ? rows.filter((r) => S.cited.has(r[1])) : rows;
  ovl.lineWidth = 1.5 * px;
  ovl.strokeStyle = ovl.fillStyle = MEASURE;
  ovl.font = `500 ${11 * px}px "IBM Plex Mono", monospace`;
  for (const [, label, x1, y1, x2, y2, conf] of shown) {
    ovl.strokeRect(x1 * k, y1 * k, (x2 - x1) * k, (y2 - y1) * k);
    ovl.fillText(`${label} ${conf.toFixed(2)}`, x1 * k, Math.max(11 * px, y1 * k - 3 * px));
  }
  S.rows = rows;
  S.boxes = shown.length;
}

function fitOverlay() {
  const c = ovl.canvas, w = Math.max(1, Math.round(c.clientWidth * devicePixelRatio)), h = Math.max(1, Math.round(c.clientHeight * devicePixelRatio));
  if (c.width === w && c.height === h) return;
  Object.assign(c, { width: w, height: h });
  if (S) draw(S.rows);
}

// The chunk holding t plays at t − its start. Re-seek when the video drifts more than a frame (paused) or 0.25 s (playing).
function sync(t) {
  const { chunks } = S, k = chunks.findIndex((c) => c.t0_ms <= t && t < c.t1_ms), c = chunks[k];
  if (!c) return false;
  if (k !== S.cur) {
    S.cur = k;
    S.presented = null;
    S.blind = false;
    video.src = ctx.api.url(c.url);
    resetFrames();
    // play() rejects when a pause() interrupts it; that is expected here.
    if (ctx.clock.playing) video.play().catch(() => {});
  }
  video.playbackRate = ctx.clock.rate;
  const target = Math.max(0, (t - c.t0_ms) / 1000);
  if (Math.abs(video.currentTime - target) > (ctx.clock.playing ? 0.25 : 1 / S.mf.fps)) video.currentTime = target;
  return true;
}

const pct = (t) => `${(100 * (t - S.mf.t0_ms)) / (S.mf.t1_ms - S.mf.t0_ms)}%`;

// Text over the video box when it cannot show the camera's own frames; `still` = time of the server still frame to show.
function note(text, still = null) {
  const img = root.querySelector('.st-still'), label = root.querySelector('.st-vnote');
  root.querySelector('.st-vid').dataset.view = !text ? '' : still == null ? 'novid' : 'still';
  label.hidden = !text;
  label.textContent = text ?? '';
  img.hidden = still == null;
  if (still == null) return;
  const src = ctx.api.url(`api/street/${S.key}/thumb?cam=${encodeURIComponent(S.cam.id)}&t=${still}`);
  if (img.getAttribute('src') !== src) img.src = src;
}

async function tick(t) {
  const s = S;
  if (!s) return;
  const covered = sync(t);
  root.querySelector('.st-time').textContent = tc(t);
  root.querySelector('.st-now').style.left = pct(t);
  drawActivity();
  draw([]);
  // A browser without the chunk's video decoder may still play its audio: ready, no error, no picture. The server then
  // decodes the first frame of each second (the thumbnails of the answer panel's key frame).
  const still = covered && s.blind ? Math.floor(t / 1000) * 1000 : null;
  note(!covered ? 'No video at this time' : still != null ? "Still frame each second · this browser can't decode this video" : null, still);
  if (!covered || !media.available) return;
  const at = still ?? s.presented;
  if (at == null || (still == null && Math.abs(at - t) > (ctx.clock.playing ? 250 : 1000 / s.mf.fps))) return;
  try {
    const d = await dets(s, at);
    if (s !== S || t !== ctx.clock.t || (still == null && at !== s.presented)) return;
    // A still is the first frame of its second.
    const rows = still == null ? rowsAt(d, at) : d.at.get(d.times.find((x) => x >= at)) ?? [];
    draw(rows);
    listDetections(rows, at);
    root.querySelector('.st-chip').textContent = 'L1 · 2D video';
  } catch (err) {
    if (s === S && t === ctx.clock.t && err?.message !== 'Superseded page command') root.querySelector('.st-chip').textContent = 'L1 · 2D video · Detections unavailable';
  }
}

function renderBands() {
  root.querySelector('.st-marks').innerHTML = S.moments
    .map((m) => `<button type="button" class="st-band" data-t="${m.t0_ms}" style="left:${pct(m.t0_ms)};width:calc(${pct(m.t1_ms)} - ${pct(m.t0_ms)})" title="Moment ${tc(m.t0_ms)}–${tc(m.t1_ms)}" aria-label="Go to moment at ${tc(m.t0_ms)}"></button>`)
    .join('');
}

// ---- Activity: per second, the most persons and the most vehicles in one frame (null: no detections read).
async function loadActivity(s) {
  const { mf } = s, n = Math.ceil((mf.t1_ms - mf.t0_ms) / 1000);
  s.act = { person: new Array(n).fill(null), vehicle: new Array(n).fill(null) };
  const windows = [];
  for (let a = mf.t0_ms; a < mf.t1_ms; a += ACTIVITY_MS) windows.push([a, Math.min(a + ACTIVITY_MS, mf.t1_ms)]);
  await Promise.all(windows.map(async ([a, b]) => {
    const d = await ctx.api.get(`api/street/${s.key}/detections?cam=${encodeURIComponent(s.cam.id)}&t0=${a}&t1=${b}`).catch(() => null);
    if (!d || s !== S) return;
    for (let t = a; t < b; t += 1000) {
      const i = (t - mf.t0_ms) / 1000;
      if (!(d.gaps ?? []).some(([g0, g1]) => g0 <= t && t < g1)) s.act.person[i] = s.act.vehicle[i] = 0;
    }
    const frames = new Map();
    for (const [t, label] of d.rows) {
      const f = frames.get(t) ?? [0, 0];
      if (label === 'person') f[0]++;
      else if (VEHICLES.has(label)) f[1]++;
      frames.set(t, f);
    }
    for (const [t, [p, v]] of frames) {
      const i = Math.floor((t - mf.t0_ms) / 1000);
      s.act.person[i] = Math.max(s.act.person[i] ?? 0, p);
      s.act.vehicle[i] = Math.max(s.act.vehicle[i] ?? 0, v);
    }
    drawActivity();
  }));
}

function drawActivity() {
  const c = root.querySelector('.st-act canvas'), g = c.getContext('2d'), px = devicePixelRatio;
  const W = Math.max(1, Math.round(c.clientWidth * px)), H = Math.max(1, Math.round(c.clientHeight * px));
  if (c.width !== W || c.height !== H) Object.assign(c, { width: W, height: H });
  g.clearRect(0, 0, W, H);
  if (!S?.act) return;
  const { mf, act } = S, css = getComputedStyle(root), dur = mf.t1_ms - mf.t0_ms;
  const peak = Math.max(1, ...act.person.map((n) => n ?? 0), ...act.vehicle.map((n) => n ?? 0));
  const x = (t) => ((t - mf.t0_ms) / dur) * W, y = (n) => H - px - (n / peak) * (H - 6 * px);
  root.querySelector('.st-act-max').textContent = `max ${peak}`;
  if (state.stageMode === 'evidence' && S.focus) {
    g.fillStyle = 'rgb(86 180 233 / 0.16)';
    g.fillRect(x(S.focus.t0_ms), 0, Math.max(2 * px, x(S.focus.t1_ms) - x(S.focus.t0_ms)), H);
  }
  const series = (vals, color, dash) => {
    g.beginPath();
    let pen = false;
    vals.forEach((n, i) => {
      if (n == null) return (pen = false);
      const t = mf.t0_ms + i * 1000;
      if (pen) g.lineTo(x(t), y(n));
      else g.moveTo(x(t), y(n));
      g.lineTo(x(Math.min(t + 1000, mf.t1_ms)), y(n));
      pen = true;
    });
    g.setLineDash(dash);
    g.strokeStyle = color;
    g.lineWidth = 1.5 * px;
    g.stroke();
    g.setLineDash([]);
  };
  series(act.vehicle, css.getPropertyValue('--ink-2'), [4 * px, 3 * px]);
  series(act.person, css.getPropertyValue('--ink'), []);
  const cursor = (t, color, dash) => {
    g.beginPath();
    g.setLineDash(dash);
    g.moveTo(Math.round(x(t)) + 0.5, 0);
    g.lineTo(Math.round(x(t)) + 0.5, H);
    g.strokeStyle = color;
    g.lineWidth = px;
    g.stroke();
    g.setLineDash([]);
  };
  cursor(ctx.clock.t, css.getPropertyValue('--ink-2'), []);
  if (S.hover != null) cursor(S.hover, css.getPropertyValue('--thread'), [3 * px, 3 * px]);
}

// ---- Evidence cell: tabs in Explore, the cited evidence in Answer mode.
const unverified = `${source('model')}<span class="muted">unverified</span>`;

function listDetections(rows, at) {
  const pane = root.querySelector('.st-pane[data-pane="detections"]'), key = `${at}|${rows.length}`;
  if (pane.dataset.key === key) return;
  pane.dataset.key = key;
  pane.innerHTML = `<p class="st-pane-head">${count(rows.length, 'box')} at <span class="mono">${tc(at)}</span>${source('measure')}<span class="muted">2D, no track ids</span></p>
    ${rows.length ? `<ul class="st-dets">${[...rows].sort((a, b) => b[6] - a[6]).map((r) => `<li class="mono">${esc(r[1])} ${r[6].toFixed(2)}</li>`).join('')}</ul>` : ''}`;
}

function renderSegments() {
  const segs = S.segments;
  root.querySelector('.st-pane[data-pane="captions"]').innerHTML = !segs ? '<p class="muted">Loading captions…</p>'
    : !segs.length ? '<p class="muted">No caption segments for this camera.</p>'
    : segs.map((g) => `<button type="button" class="st-seg" data-t="${g.t0_ms}"><span class="st-seg-head"><span class="mono">${tc(g.t0_ms)}–${tc(g.t1_ms)}</span>${unverified}</span><span>${esc(g.caption ?? '')}</span></button>`).join('');
}

async function renderCard(s) {
  const e = s.focus, cell = root.querySelector('.st-answer-card');
  if (!e) return (cell.innerHTML = '<p class="muted">Select a cited number in the answer to see its evidence here.</p>');
  const when = `<span class="mono muted">${tc(e.t0_ms)}–${tc(e.t1_ms)}</span>`;
  if (e.value?.counts) {
    const labels = Object.keys(e.value.counts);
    cell.innerHTML = `<div class="st-card">
      <p class="st-card-head"><b>2D count</b>${source('measure')}${when}</p>
      <p class="mono">${Object.entries(e.value.counts).map(([k, n]) => `${esc(k)} ${n}`).join(' · ')}</p>
      <p class="muted">Most boxes of a class in one frame${e.value.frames_read != null ? ` · ${count(e.value.frames_read, 'frame')} read` : ''} · <span class="st-conf mono">conf …</span></p>
      <p class="muted">No track ids: one person in two frames is counted in each frame.</p></div>`;
    const q = `cam=${encodeURIComponent(s.cam.id)}&t0=${e.t0_ms}&t1=${e.t1_ms}`;
    const d = await ctx.api.get(`api/street/${s.key}/detections?${q}`).catch(() => null);
    if (s !== S || s.focus !== e || !cell.querySelector('.st-conf')) return;
    const conf = d?.rows.filter((r) => labels.includes(r[1])).map((r) => r[6]) ?? [];
    cell.querySelector('.st-conf').textContent = conf.length ? `conf ${Math.min(...conf).toFixed(2)}–${Math.max(...conf).toFixed(2)}` : 'conf not read';
    return;
  }
  if (e.kind === 'model') {
    const seg = s.segments?.find((g) => g.t0_ms < e.t1_ms && e.t0_ms < g.t1_ms);
    cell.innerHTML = `<div class="st-card"><p class="st-card-head"><b>Caption segment</b>${unverified}${when}</p>
      <p>${seg ? esc(seg.caption ?? '') : '<span class="muted">Caption not loaded.</span>'}</p></div>`;
    return;
  }
  cell.innerHTML = `<div class="st-card"><p class="st-card-head"><b class="mono">${esc(e.method)}</b>${when}</p></div>`;
}

function setBoxes(mode) {
  S.boxMode = mode;
  root.querySelectorAll('.st-boxes button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.boxes === mode)));
}

// Answer mode: the cited count in the header, the cited card in the Evidence cell, the cited span on Activity.
function renderMode() {
  const answer = state.stageMode === 'evidence', counts = answer && S.focus?.value?.counts;
  const fact = root.querySelector('.st-fact'), ev = root.querySelector('.st-evidence');
  fact.hidden = !counts;
  if (counts) fact.innerHTML = `Peak ${esc(Object.entries(counts).map(([k, n]) => count(n, k)).join(' · '))}${source('measure')}<span class="muted">2D</span>`;
  ev.querySelector('[role="tablist"]').hidden = answer;
  ev.querySelector('.st-answer-card').hidden = !answer;
  ev.querySelectorAll('.st-pane').forEach((p) => (p.hidden = answer || p.dataset.pane !== S.tab));
  if (answer) renderCard(S);
  drawActivity();
}

function setTab(tab) {
  S.tab = tab;
  root.querySelectorAll('.st-evtabs [role="tab"]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
  renderMode();
}

function setSpatial(show) {
  root.querySelector('.st-spatial').dataset.show = show;
  root.querySelectorAll('.st-spatial-tabs [role="tab"]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.show === show)));
  drawActivity();
}

export async function mount(el, c) {
  ctx = c;
  const css = Object.assign(document.createElement('link'), { rel: 'stylesheet', href: new URL('./street.css', import.meta.url).href });
  document.head.append(css);
  el.innerHTML = `
    <div class="st-root">
      <header class="st-head">
        <span class="st-scene"><span class="st-key mono"></span>${level('L1')}</span>
        <span class="st-time mono">00:00.0</span>
        <span class="st-chip mono">L1 · 2D video</span>
        <button type="button" class="st-limits ghost" popovertarget="st-limits-pop" aria-expanded="false" hidden>Can't measure</button>
        <div id="st-limits-pop" class="st-pop" popover><p class="st-caps"></p></div>
        <span class="st-fact" hidden></span>
      </header>
      <div class="st-top"><div class="st-vid">
        <div class="st-vbox">
          <video muted playsinline preload="auto"></video>
          <img class="st-still" alt="Still frame from the camera" hidden>
          <canvas class="st-ovl"></canvas>
        </div>
        <span class="st-vnote" role="status" hidden></span>
        <div class="seg st-boxes" role="group" aria-label="Detection boxes">
          <button type="button" data-boxes="cited" aria-pressed="false">Cited</button>
          <button type="button" data-boxes="all" aria-pressed="true">All</button>
          <button type="button" data-boxes="off" aria-pressed="false">Off</button>
        </div>
      </div></div>
      <div class="st-bands"><div class="st-marks"></div><i class="st-now"></i></div>
      <nav class="seg st-spatial-tabs" role="tablist" aria-label="Spatial view">
        <button type="button" role="tab" data-show="activity" aria-selected="true">Activity</button>
        <button type="button" role="tab" data-show="evidence" aria-selected="false">Evidence</button>
      </nav>
      <div class="st-spatial" data-show="activity">
        <section class="st-cell st-activity" aria-label="Activity">
          <p class="st-cell-head"><b>Activity</b><span class="st-legend"><i class="lg-person"></i>Persons<i class="lg-vehicle"></i>Vehicles</span><span class="st-act-max mono"></span></p>
          <div class="st-act"><canvas aria-label="Most persons and vehicles in one frame, per second; click to seek"></canvas><span class="st-act-tip mono" hidden></span></div>
        </section>
        <section class="st-cell st-evidence" aria-label="Evidence">
          <nav class="seg st-evtabs" role="tablist" aria-label="Evidence view">
            <button type="button" role="tab" data-tab="detections" aria-selected="true">Detections</button>
            <button type="button" role="tab" data-tab="captions" aria-selected="false">Captions · Model</button>
            <button type="button" role="tab" data-tab="limits" aria-selected="false">Limits</button>
          </nav>
          <div class="st-panes">
            <div class="st-pane" data-pane="detections"><p class="muted">No frame shown yet.</p></div>
            <div class="st-pane" data-pane="captions" hidden></div>
            <div class="st-pane" data-pane="limits" hidden></div>
            <div class="st-answer-card" hidden></div>
          </div>
        </section>
      </div>
    </div>`;
  root = el.firstElementChild;
  video = root.querySelector('video');
  ovl = root.querySelector('.st-ovl').getContext('2d');
  // The shell's Answer/Explore toggle sits in this header while the page is mounted, as on the fleet page.
  const toggle = document.querySelector('.stage-controls');
  root.querySelector('.st-head').prepend(toggle);
  const pop = root.querySelector('.st-pop'), limits = root.querySelector('.st-limits');
  // The popover sits in the top layer: place it under its button, inside the viewport.
  pop.addEventListener('beforetoggle', (e) => {
    limits.setAttribute('aria-expanded', String(e.newState === 'open'));
    if (e.newState !== 'open') return;
    const b = limits.getBoundingClientRect();
    pop.style.top = `${b.bottom + 4}px`;
    pop.style.left = `${Math.max(8, Math.min(b.left, innerWidth - 8 - Math.min(440, innerWidth - 16)))}px`;
  });
  root.addEventListener('click', (e) => {
    const t = e.target.closest('.st-band, .st-seg')?.dataset.t;
    if (t != null) return ctx.clock.seek(Number(t));
    if (!S) return;
    const boxes = e.target.closest('.st-boxes button')?.dataset.boxes;
    if (boxes) {
      setBoxes(boxes);
      return tick(ctx.clock.t);
    }
    const tab = e.target.closest('.st-evtabs [role="tab"]')?.dataset.tab;
    if (tab) return setTab(tab);
    const show = e.target.closest('.st-spatial-tabs [role="tab"]')?.dataset.show;
    if (show) setSpatial(show);
  });
  // The shell leaves Answer mode on any pointerdown in the stage; these controls only change the view.
  root.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.st-head, .st-boxes, .st-spatial-tabs')) e.stopPropagation();
  });
  const chart = root.querySelector('.st-act canvas'), tip = root.querySelector('.st-act-tip');
  const timeAt = (e) => {
    const b = chart.getBoundingClientRect(), f = Math.min(1, Math.max(0, (e.clientX - b.left) / b.width));
    return Math.round(S.mf.t0_ms + f * (S.mf.t1_ms - S.mf.t0_ms));
  };
  chart.addEventListener('pointermove', (e) => {
    if (!S?.act) return;
    S.hover = timeAt(e);
    const i = Math.min(S.act.person.length - 1, Math.floor((S.hover - S.mf.t0_ms) / 1000)), p = S.act.person[i], v = S.act.vehicle[i];
    tip.textContent = `${tc(S.hover)} · ${p == null ? 'no detections read' : `${count(p, 'person')} · ${count(v, 'vehicle')}`}`;
    tip.style.left = `${e.clientX - chart.getBoundingClientRect().left}px`;
    tip.hidden = false;
    drawActivity();
  });
  chart.addEventListener('pointerleave', () => {
    tip.hidden = true;
    if (!S) return;
    S.hover = null;
    drawActivity();
  });
  chart.addEventListener('click', (e) => S && ctx.clock.seek(timeAt(e)));
  const resized = new ResizeObserver(() => {
    fitOverlay();
    if (S) drawActivity();
  });
  resized.observe(root.querySelector('.st-vbox'));
  resized.observe(chart);
  let alive = true, generation = 0, frameHandle = null, pending = null;
  const v = video;
  const publish = (deferred = false) => {
    const p = pending;
    if (!p || !S || !media.available || v.seeking || v.readyState < 2 || v.error) return;
    pending = null;
    if (p.scene !== S || p.chunk !== S.cur || p.generation !== generation || p.source !== v.src) return;
    if (deferred && Math.abs(p.mediaTime - v.currentTime) > 1 / S.mf.fps) return;
    S.presented = Math.round(p.mediaTime * 1000) + S.chunks[S.cur].t0_ms;
  };
  const requestFrame = () => {
    const source = v.src, epoch = generation;
    frameHandle = v.requestVideoFrameCallback((now, md) => onFrame(now, md, source, epoch));
  };
  const invalidate = () => {
    generation++; pending = null;
    if (frameHandle != null) v.cancelVideoFrameCallback(frameHandle);
    frameHandle = null;
    if (alive) requestFrame();
  };
  resetFrames = invalidate;
  const onFrame = (now, md, source, epoch) => {
    if (!alive || epoch !== generation || source !== v.src) return;
    frameHandle = null;
    if (S) pending = { scene: S, chunk: S.cur, generation, source, mediaTime: md.mediaTime, early: v.seeking && !seekSeen };
    if (media.present()) publish();
    requestFrame();
    if (S) tick(ctx.clock.t);
  };
  // Metadata without a picture size: the browser has no decoder for this chunk's video track.
  const onMetadata = () => {
    if (!S) return;
    S.blind = v.videoWidth === 0;
    tick(ctx.clock.t);
  };
  // A paused seek can present its frame before the seeking event; that frame is the seek target's, so it survives.
  let seekSeen = false;
  const onSeeking = () => {
    const p = pending?.early && S && Math.abs(pending.mediaTime - v.currentTime) <= 1 / S.mf.fps ? pending : null;
    seekSeen = true;
    invalidate();
    if (p) pending = { ...p, generation, early: false };
  };
  const onSeeked = () => { seekSeen = false; };
  v.addEventListener('seeking', onSeeking);
  v.addEventListener('seeked', onSeeked);
  v.addEventListener('loadstart', invalidate);
  v.addEventListener('loadedmetadata', onMetadata);
  requestFrame();
  media = createMediaStatus(video, {
    host: root.querySelector('.st-vbox'),
    onUnavailable() { if (S) { S.presented = null; draw([]); } else ovl.clearRect(0, 0, ovl.canvas.width, ovl.canvas.height); },
    // A real frame callback can precede seeked/canplay. Readiness must publish
    // that pending metadata rather than substitute the requested currentTime.
    onReady() { if (alive && S) queueMicrotask(() => { if (alive && S) { publish(true); tick(ctx.clock.t); } }); },
  });
  const onPlay = (on) => (on ? video.play().catch(() => {}) : video.pause());
  ctx.bus.on('clock:play', onPlay);
  const onFocus = (e) => {
    if (!S || e.scene_key !== S.key) return;
    S.focus = e;
    S.cited = new Set([...Object.keys(e.value?.counts ?? {}), ...(e.value?.classes ?? [])]);
    if (/people|person|pedestrian|bytetrack|yolo_people/.test(e.method)) S.cited.add('person');
    setSpatial('evidence');
    renderMode();
    tick(ctx.clock.t);
  };
  const onMode = () => {
    if (!S) return;
    setBoxes(state.stageMode === 'evidence' ? 'cited' : 'all');
    renderMode();
    tick(ctx.clock.t);
  };
  ctx.bus.on('evidence:focus', onFocus); ctx.bus.on('stage:mode', onMode);
  offs = [() => ctx.bus.off('evidence:focus', onFocus), () => ctx.bus.off('stage:mode', onMode), ctx.clock.onTick(tick), () => ctx.bus.off('clock:play', onPlay), () => css.remove(), () => resized.disconnect()];
  const dispose = () => {
    alive = false;
    invalidate();
    v.removeEventListener('seeking', onSeeking);
    v.removeEventListener('seeked', onSeeked);
    v.removeEventListener('loadstart', invalidate);
    v.removeEventListener('loadedmetadata', onMetadata);
    if (resetFrames === invalidate) resetFrames = () => {};
    media.dispose();
    offs.forEach((off) => off());
    if (pop.matches(':popover-open')) pop.hidePopover();
    document.getElementById('stage').after(toggle);
    video.pause();
    video.removeAttribute('src');
    video.load();
    S = null;
  };
  try {
    await loadScene(ctx.route.scene_key);
  } catch (e) {
    dispose();
    throw e;
  }
  return dispose;
}

// #/street/<scene_key>[@<t_ms>]; the shell has set the clock range and the coverage lanes before.
export function loadScene(key) {
  return (ready = load(key));
}

async function load(key) {
  const { api } = ctx;
  const [mf, ws] = await Promise.all([api.get(`api/scene/${key}/manifest`), ctx.workspaces()]);
  const cam = mf.cams[0];
  const chunks = await api.get(`api/street/${key}/chunks?cam=${encodeURIComponent(cam.id)}`);
  const caps = ws.flatMap((w) => w.scenes).find((s) => s.scene_key === key)?.caps ?? {};
  const texts = [...new Set(Object.values(caps).filter((x) => !x.ok && x.text).map((x) => x.text))];
  root.querySelector('.st-key').textContent = key;
  root.querySelector('.st-caps').textContent = `Can't measure here: ${texts.join(' ')}`;
  root.querySelector('.st-limits').hidden = !texts.length;
  root.querySelector('.st-pane[data-pane="limits"]').innerHTML = `<p class="st-pane-head">${level('L1')}One 2D camera: detections per frame, no geometry, no GPS, no track ids.</p>
    ${texts.length ? `<ul class="st-limit-list">${texts.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}`;
  root.querySelector('.st-vbox').style.setProperty('--ar', cam.w / cam.h);
  ctx.bus.emit('source:camera',{scene_key:key,cam_id:cam.id});
  const s = S = { key, mf, cam, chunks, cur: -1, presented: null, blind: false, dets: new Map(), moments: [], boxes: 0, rows: [],
    cited: new Set(), focus: null, boxMode: 'all', tab: 'detections', act: null, hover: null, segments: null };
  setBoxes(state.stageMode === 'evidence' ? 'cited' : 'all');
  fitOverlay();
  renderBands();
  renderSegments();
  renderMode();
  loadActivity(s);
  api.get(`api/street/${key}/segments`).then((segs) => {
    if (s !== S) return;
    s.segments = segs.filter((g) => g.cam_id === cam.id).sort((a, b) => a.t0_ms - b.t0_ms);
    renderSegments();
    if (state.stageMode === 'evidence') renderCard(s);
  }).catch(() => {
    if (s === S) root.querySelector('.st-pane[data-pane="captions"]').innerHTML = '<p class="muted">Captions unavailable.</p>';
  });
  if (ctx.route.t_ms != null) ctx.clock.seek(ctx.route.t_ms);
  else tick(ctx.clock.t);
}

// The agent's moments of this scene: seek to the first, one button each under the video.
export async function applyHits(moments) {
  await ready;
  S.moments = moments;
  renderBands();
  if (moments.length) ctx.clock.seek(moments[0].t0_ms);
}

export async function applyUI({ cmd, args }) {
  await ready;
  if (cmd === 'seek') {
    ctx.clock.seek(args.t_ms);
    return { t_ms: ctx.clock.t };
  }
  return IGNORED.has(cmd) ? { ignored: true, reason: 'no_track_ids' } : null;
}

// Model results are shown by the shell's model panel; an L1 scene has no ground distance to draw.
export function applyModelResults() {}
export function showMeasure() {}

// e2e hook: the scene shown, the clock, the boxes drawn and the Activity counts.
export const probe = () => ({ scene_key: S?.key ?? null, t_ms: ctx.clock.t, boxes: S?.boxes ?? 0, activity: S?.act ?? null });
