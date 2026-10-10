// Dashcam video of the intersection view, slaved to the replay clock, with overlays in source pixels: the cited actor's
// tracked box with its ground distance, other detection boxes, pedestrian skeletons (ego peds kpts2d), clickable map facade
// outlines, and a legend of the element types drawn. "Pinhole crop" remaps the fisheye frame (cv2.fisheye, K and dist from
// the manifest) to a pinhole image with the same K, i.e. the central crop.
import { esc } from '../kernel/format.js';
import { state } from '../kernel/state.js';
import { selection } from '../kernel/selection.js';

// COCO-17 limbs.
const LIMBS = [[0, 1], [0, 2], [1, 3], [2, 4], [3, 5], [4, 6], [5, 6], [5, 7], [7, 9], [6, 8], [8, 10], [5, 11], [6, 12], [11, 12], [11, 13], [13, 15], [12, 14], [14, 16]];
const MEASURE = '#56B4E9', INK = '#EDF0F4', PAPER = '#0E1218';
const LEGEND = { tracked: 'tracked', detected: 'detected', map: 'map' };

const distort = (k, th) => {
  const t2 = th * th;
  return th * (1 + t2 * (k[0] + t2 * (k[1] + t2 * (k[2] + t2 * k[3]))));
};

// Camera-frame point (OpenCV axes: x right, y down, z forward) -> fisheye source pixel; null behind the camera.
export function fisheye(cam, [x, y, z]) {
  if (z <= 0) return null;
  const [fx, fy, cx, cy] = cam.K, r = Math.hypot(x, y);
  const s = r ? distort(cam.dist, Math.atan2(r, z)) / r : 0;
  return [fx * s * x + cx, fy * s * y + cy];
}

// Fisheye source pixel -> pixel of the pinhole image with the same K (θd(θ) inverted by Newton).
export function undistortPx(cam, [u, v]) {
  const [fx, fy, cx, cy] = cam.K, [k1, k2, k3, k4] = cam.dist;
  const xd = (u - cx) / fx, yd = (v - cy) / fy, td = Math.hypot(xd, yd);
  let th = td;
  for (let i = 0; i < 6; i++) {
    const t2 = th * th;
    th -= (distort(cam.dist, th) - td) / (1 + t2 * (3 * k1 + t2 * (5 * k2 + t2 * (7 * k3 + t2 * 9 * k4))));
  }
  const s = td ? Math.tan(th) / td : 1;
  return [fx * s * xd + cx, fy * s * yd + cy];
}

// names: Map of local_track_id -> entity_id for this camera; bname(bid) -> building name; onPick(bid); cite: entity_id of
// the actor the answer cites, or null.
export function createVideo(el, { cam, src, fps, names, bname, onPick, cite, onPresented = () => {} }) {
  const { w, h } = cam;
  el.innerHTML = `
    <div class="fl-vbox" style="aspect-ratio:${w}/${h}">
      <video muted playsinline preload="auto" crossorigin="anonymous" src="${esc(src)}"></video>
      <canvas class="fl-remap" width="${w}" height="${h}" hidden></canvas>
      <canvas class="fl-ovl" width="${w}" height="${h}"></canvas>
      <div class="fl-cols"></div>
    </div>
    <div class="fl-vhead"><span class="mono">${esc(cam.id)}</span><span class="muted">fisheye · ${w}×${h}</span>
      <button class="ghost fl-undist" aria-pressed="false">Pinhole crop</button></div>
    <div class="fl-legend"></div><span class="fl-media-status muted" role="status" hidden></span>`;
  const video = el.querySelector('video'), remapEl = el.querySelector('.fl-remap'), ovl = el.querySelector('.fl-ovl').getContext('2d');
  const btn = el.querySelector('.fl-undist'), cols = el.querySelector('.fl-cols'), legend = el.querySelector('.fl-legend');
  const grab = new OffscreenCanvas(w, h).getContext('2d', { willReadFrequently: true });
  const out = remapEl.getContext('2d');
  let undist = false, lut = null, last = null, alive = true, time = 0, playing = false;
  video.addEventListener('loadedmetadata', () => alive && player.sync(time, playing));

  // lut[i] = source pixel index for pinhole pixel i, −1 outside the fisheye frame.
  function buildLut() {
    const [fx, fy, cx, cy] = cam.K;
    lut = new Int32Array(w * h);
    for (let v = 0; v < h; v++) {
      for (let u = 0; u < w; u++) {
        const [su, sv] = fisheye(cam, [(u - cx) / fx, (v - cy) / fy, 1]).map(Math.round);
        lut[v * w + u] = su >= 0 && su < w && sv >= 0 && sv < h ? sv * w + su : -1;
      }
    }
  }
  function remap() {
    grab.drawImage(video, 0, 0, w, h);
    const src32 = new Uint32Array(grab.getImageData(0, 0, w, h).data.buffer);
    const img = out.createImageData(w, h), dst32 = new Uint32Array(img.data.buffer);
    for (let i = 0; i < lut.length; i++) dst32[i] = lut[i] >= 0 ? src32[lut[i]] : 0xff000000;
    out.putImageData(img, 0, 0);
  }
  const onFrame = (now, md) => {
    if (!alive) return;
    if (undist) remap();
    if (!video.seeking) onPresented(Math.round(md.mediaTime * 1000) + cam.offset_ms);
    video.requestVideoFrameCallback(onFrame);
  };
  video.requestVideoFrameCallback(onFrame);
  video.addEventListener('seeked', () => { if (alive && !playing) onPresented(Math.round(Math.min(video.currentTime, Number.isFinite(video.duration) ? Math.max(0, video.duration - 1 / fps) : video.currentTime) * 1000) + cam.offset_ms); });
  for (const event of ['waiting', 'stalled', 'error', 'playing', 'canplay']) video.addEventListener(event, () => {
    el.dataset.media = event === 'error' ? 'error' : ['waiting', 'stalled'].includes(event) ? 'waiting' : 'ready';
    const status = el.querySelector('.fl-media-status');
    status.hidden = el.dataset.media === 'ready';
    status.textContent = event === 'error' ? 'Video unavailable · reopen this evidence to retry' : 'Waiting for video…';
    el.querySelector('.fl-vhead').title = event === 'error' ? 'Video failed to load. Reopen this evidence to retry.' : event === 'waiting' || event === 'stalled' ? 'Waiting for video; overlays follow presented frames.' : '';
  });

  function draw(frame, boxes) {
    last = [frame, boxes];
    const P = undist ? (p) => undistortPx(cam, p) : (p) => p;
    ovl.clearRect(0, 0, w, h);
    ovl.lineWidth = 2;
    ovl.strokeStyle = ovl.fillStyle = MEASURE;
    ovl.font = '500 13px "IBM Plex Mono", monospace';
    const drawn = new Set();
    let tracked = null;
    for (const [, tid, cls, x1, y1, x2, y2] of boxes) {
      const [a, b] = [P([x1, y1]), P([x2, y2])];
      if (cite && names.get(tid) === cite) {
        tracked = [a, b];
        continue;
      }
      if (state.stageMode === 'evidence') continue;
      drawn.add('detected');
      ovl.strokeRect(a[0], a[1], b[0] - a[0], b[1] - a[1]);
      ovl.fillText(`${names.get(tid) ?? `#${tid}`} ${cls}`, a[0], a[1] - 4);
    }
    for (const p of frame.peds) {
      if (state.stageMode === 'evidence' && p.entity_id !== cite && !selection.ids.has(p.entity_id)) continue;
      const k = p.kpts2d.map(([u, v, c]) => [...P([u, v]), c]);
      ovl.lineWidth = 1.5;
      for (const [i, j] of LIMBS) {
        ovl.globalAlpha = Math.min(k[i][2], k[j][2]);
        ovl.beginPath();
        ovl.moveTo(k[i][0], k[i][1]);
        ovl.lineTo(k[j][0], k[j][1]);
        ovl.stroke();
      }
      ovl.globalAlpha = 1;
    }
    if (tracked) {
      // Tag: entity id and its ground distance from the ego when the frame places it.
      const [a, b] = tracked, o = [...frame.peds, ...frame.vehs].find((x) => x.entity_id === cite);
      const tag = o ? `${cite} · ${Math.hypot(o.x, o.y).toFixed(1)} m` : cite;
      ovl.lineWidth = 2;
      ovl.strokeStyle = ovl.fillStyle = INK;
      ovl.strokeRect(a[0], a[1], b[0] - a[0], b[1] - a[1]);
      ovl.fillRect(a[0] - 1, a[1] - 22, ovl.measureText(tag).width + 10, 18);
      ovl.fillStyle = PAPER;
      ovl.fillText(tag, a[0] + 4, a[1] - 8);
      drawn.add('tracked');
    }
    cols.innerHTML = frame.buildings.map((b) => {
      if (state.stageMode === 'evidence' && b.bid !== cite && !selection.ids.has(b.bid)) return '';
      const u0 = Math.max(0, P([b.u0, cam.K[3]])[0]), u1 = Math.min(w, P([b.u1, cam.K[3]])[0]);
      if (u1 <= u0) return '';
      drawn.add('map');
      return `<button class="fl-col" data-bid="${esc(b.bid)}" title="${state.stageMode === 'explore' ? esc(b.bid) : ''}" style="left:${(100 * u0) / w}%;width:${(100 * (u1 - u0)) / w}%"><span>${esc(bname(b.bid) === b.bid ? 'Map' : bname(b.bid))} facade · map</span></button>`;
    }).join('');
    legend.innerHTML = Object.entries(LEGEND).filter(([k]) => drawn.has(k)).map(([k, v]) => `<span class="lg-${k}"><i></i>${v}</span>`).join('');
  }

  btn.addEventListener('click', () => {
    undist = !undist;
    btn.setAttribute('aria-pressed', undist);
    btn.classList.toggle('on', undist);
    if (undist && !lut) buildLut();
    remapEl.hidden = !undist;
    if (undist) remap();
    if (last) draw(...last);
  });
  cols.addEventListener('click', (e) => {
    const c = e.target.closest('.fl-col');
    if (c) onPick(c.dataset.bid);
  });

  const player = {
    draw,
    redraw() { if (last) draw(...last); },
    clear() {
      last = null;
      ovl.clearRect(0, 0, w, h);
      cols.replaceChildren();
      legend.textContent = 'Detections unavailable';
    },
    // t: trip t_ms. Re-seek when the video drifts more than a frame (paused) or 0.25 s (playing).
    sync(t, on) {
      time = t;
      playing = on;
      const target = Math.max(0, (t - cam.offset_ms) / 1000);
      if (Math.abs(video.currentTime - target) > (playing ? 0.25 : 1 / fps)) video.currentTime = target;
      if (playing && video.paused) video.play().catch(() => {});
      else if (!playing && !video.paused) video.pause();
    },
    play(on) {
      playing = on;
      // play() rejects when a pause() interrupts it; that is expected here.
      on ? video.play().catch(() => {}) : video.pause();
    },
    dispose() {
      alive = false;
      video.pause();
      video.removeAttribute('src');
      video.load();
    },
  };
  return player;
}
