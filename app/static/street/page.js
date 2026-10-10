// Street workspace page (evidence level L1): one 2D camera's video, chunk by chunk, slaved to the scene clock, with its
// detection boxes (class and score; no track ids, no geometry) drawn over it. A chip names the level and one muted line
// says what the scene cannot measure, from its caps in api/workspaces. The agent's moments are thin time bands under the video.
import { esc, span } from '../kernel/format.js';
import { state } from '../kernel/state.js';

const WINDOW_MS = 10000;
const MEASURE = '#56B4E9';
// Commands on objects or the 3D camera; an L1 scene has neither track ids nor geometry.
const IGNORED = new Set(['highlight', 'annotate', 'focus', 'setView', 'showTrack']);

let ctx, root, video, ovl, S = null, ready = null, offs = [];

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

function draw(rows) {
  const { w, h } = S.cam;
  ovl.clearRect(0, 0, w, h);
  ovl.lineWidth = 1;
  ovl.strokeStyle = ovl.fillStyle = MEASURE;
  ovl.font = '500 11px "IBM Plex Mono", monospace';
  const allowed = S.answerLabels;
  if (state.stageMode === 'evidence') rows = rows.filter((r) => allowed.has(r[1]));
  for (const [, label, x1, y1, x2, y2, conf] of rows) {
    ovl.strokeRect(x1, y1, x2 - x1, y2 - y1);
    ovl.fillText(`${label} ${conf.toFixed(2)}`, x1, Math.max(11, y1 - 3));
  }
  S.boxes = rows.length;
}

// The chunk holding t plays at t − its start. Re-seek when the video drifts more than a frame (paused) or 0.25 s (playing).
function sync(t) {
  const { chunks } = S, i = chunks.findIndex((c) => t < c.t1_ms), k = i < 0 ? chunks.length - 1 : i, c = chunks[k];
  if (k !== S.cur) {
    S.cur = k;
    S.presented = null;
    video.src = ctx.api.url(c.url);
    // play() rejects when a pause() interrupts it; that is expected here.
    if (ctx.clock.playing) video.play().catch(() => {});
  }
  const target = Math.max(0, (t - c.t0_ms) / 1000);
  if (Math.abs(video.currentTime - target) > (ctx.clock.playing ? 0.25 : 1 / S.mf.fps)) video.currentTime = target;
}

const pct = (t) => `${(100 * (t - S.mf.t0_ms)) / (S.mf.t1_ms - S.mf.t0_ms)}%`;

async function tick(t) {
  const s = S;
  if (!s) return;
  sync(t);
  root.querySelector('.st-now').style.left = pct(t);
  draw([]);
  const presented = s.presented;
  if (presented == null || Math.abs(presented - t) > (ctx.clock.playing ? 250 : 1000 / s.mf.fps)) return;
  try {
    const d = await dets(s, presented);
    if (s !== S || t !== ctx.clock.t || presented !== s.presented) return;
    draw(rowsAt(d, presented));
    root.querySelector('.st-chip').textContent = 'L1 · 2D video';
  } catch (err) {
    if (s === S && t === ctx.clock.t && err?.message !== 'Superseded page command') root.querySelector('.st-chip').textContent = 'L1 · 2D video · Detections unavailable';
  }
}

function renderBands() {
  root.querySelector('.st-marks').innerHTML = S.moments
    .map((m) => `<i class="st-band" style="left:${pct(m.t0_ms)};width:calc(${pct(m.t1_ms)} - ${pct(m.t0_ms)})" title="${esc(span(m.t0_ms, m.t1_ms))}"></i>`)
    .join('');
}

export async function mount(el, c) {
  ctx = c;
  const css = Object.assign(document.createElement('link'), { rel: 'stylesheet', href: new URL('./street.css', import.meta.url).href });
  document.head.append(css);
  el.innerHTML = `
    <div class="st-root">
      <div class="st-fit"><div class="st-vbox">
        <video muted playsinline preload="auto"></video>
        <canvas class="st-ovl"></canvas>
        <div class="st-bands"><div class="st-marks"></div><i class="st-now"></i></div>
      </div></div>
      <div class="st-head"><span class="st-chip mono">L1 · 2D video</span><p class="st-caps muted" hidden></p></div>
    </div>`;
  root = el.firstElementChild;
  video = root.querySelector('video');
  ovl = root.querySelector('.st-ovl').getContext('2d');
  let alive = true;
  const onFrame = (now, md) => {
    if (!alive) return;
    if (S && !video.seeking) {
      S.presented = Math.round(md.mediaTime * 1000) + S.chunks[S.cur].t0_ms;
      tick(ctx.clock.t);
    }
    video.requestVideoFrameCallback(onFrame);
  };
  video.requestVideoFrameCallback(onFrame);
  // A seek while paused need not present a new frame callback; the seeked position is the presented frame.
  video.addEventListener('seeked', () => {
    if (!alive || !S || ctx.clock.playing) return;
    S.presented = Math.round(video.currentTime * 1000) + S.chunks[S.cur].t0_ms;
    tick(ctx.clock.t);
  });
  const onPlay = (on) => (on ? video.play().catch(() => {}) : video.pause());
  ctx.bus.on('clock:play', onPlay);
  const onFocus = (e) => { if (!S || e.scene_key !== S.key) return; S.answerLabels = new Set(Object.keys(e.value?.counts ?? {})); if (/people|person|pedestrian|bytetrack|yolo_people/.test(e.method)) S.answerLabels.add('person'); tick(ctx.clock.t); };
  const onMode = () => tick(ctx.clock.t);
  ctx.bus.on('evidence:focus', onFocus); ctx.bus.on('stage:mode', onMode);
  offs = [() => ctx.bus.off('evidence:focus', onFocus), () => ctx.bus.off('stage:mode', onMode), ctx.clock.onTick(tick), () => ctx.bus.off('clock:play', onPlay), () => css.remove()];
  const dispose = () => {
    alive = false;
    offs.forEach((off) => off());
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
  const line = root.querySelector('.st-caps');
  line.textContent = `Can't measure here: ${texts.join(' ')}`;
  line.hidden = !texts.length;
  const box = root.querySelector('.st-vbox'), canvas = root.querySelector('.st-ovl');
  box.style.setProperty('--ar', cam.w / cam.h);
  Object.assign(canvas, { width: cam.w, height: cam.h });
  S = { key, mf, cam, chunks, cur: -1, presented: null, dets: new Map(), moments: [], boxes: 0, answerLabels: new Set() };
  renderBands();
  if (ctx.route.t_ms != null) ctx.clock.seek(ctx.route.t_ms);
  else tick(ctx.clock.t);
}

// The agent's moments of this scene: seek to the first, one time band each.
export async function applyHits(moments) {
  await ready;
  S.moments = moments;
  renderBands();
  ctx.clock.seek(moments[0].t0_ms);
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

// e2e hook: the scene shown, the clock and the boxes drawn.
export const probe = () => ({ scene_key: S?.key ?? null, t_ms: ctx.clock.t, boxes: S?.boxes ?? 0 });
