// Synchronised videos (spec §9.2): one <video> per camera slaved to the master clock,
// currentTime = (t − offset_ms)/1000, re-seeked while playing when off by more than a frame; boxes are drawn at the
// presented frame's mediaTime (requestVideoFrameCallback). video.dataset.sceneMs = scene time of the presented frame.
// The same element serves the Videos tab, the stage PiP and the source frame dock; the container styles its layout.
import { esc, tc } from '../kernel/format.js';

const css = getComputedStyle(document.documentElement);

// cam_id:local_track_id -> entity_id
export const owners = (entities) => new Map(entities.entities.flatMap((e) => e.observations.map((o) => [`${o.cam_id}:${o.local_track_id}`, e.entity_id])));

// cams: manifest cams, primary first; lanes: timeline.json lanes; entities: entities.json; only: entity ids to box (all when null).
export function createVideos({ api, clock, key, mf, cams, lanes, entities, only = null }) {
  const el = document.createElement('section');
  el.className = 'wh-videos';
  const p0 = cams[0].offset_ms;
  const cell = (c, i) => {
    const off = i ? c.offset_ms - p0 : c.offset_ms;
    const meta = [i ? '' : 'primary', off ? `${off > 0 ? '+' : '−'}${Math.abs(off)} ms` : ''].filter(Boolean).join(' · ');
    return `
    <figure class="wh-vcell${i ? '' : ' primary'}" data-cam="${esc(c.id)}">
      <div class="wh-vframe"><video muted playsinline preload="auto" src="${esc(api.asset(`${key}/${c.video}`))}"></video><canvas></canvas><span class="wh-nov"></span></div>
      <figcaption><span class="mono">${esc(c.id)}</span>${meta ? `<span class="mono muted">${meta}</span>` : ''}<button type="button" class="wh-pip-btn" data-cam="${esc(c.id)}" title="Pin to stage">${i ? 'Pin' : 'Pin to stage'}</button></figcaption>
    </figure>`;
  };
  el.innerHTML = `${cell(cams[0], 0)}${cams.length > 1 ? `<div class="wh-vgrid">${cams.slice(1).map((c, i) => cell(c, i + 1)).join('')}</div>` : ''}`;
  const owner = owners(entities);
  const chunks = new Map();
  const frame = 1 / mf.fps;
  let selected = new Set(), alive = true;

  const vids = cams.map((c) => {
    const fig = el.querySelector(`.wh-vcell[data-cam="${CSS.escape(c.id)}"]`);
    const x = { c, fig, v: fig.querySelector('video'), cv: fig.querySelector('canvas'), nov: fig.querySelector('.wh-nov'), ms: null };
    const onFrame = (now, md) => {
      if (!alive) return;
      x.ms = md.mediaTime * 1000 + c.offset_ms;
      x.v.dataset.sceneMs = Math.round(x.ms);
      draw(x);
      x.v.requestVideoFrameCallback(onFrame);
    };
    x.v.requestVideoFrameCallback(onFrame);
    x.v.addEventListener('loadedmetadata', () => sync(clock.t));
    return x;
  });

  // Box rows of the chunk holding t; undefined until loaded (then the cell is redrawn).
  function rows(x, t) {
    const name = `${x.c.id}/${String(Math.floor(t / 10000)).padStart(5, '0')}`;
    if (!chunks.has(name)) {
      chunks.set(name, null);
      api.get(`api/assets/${key}/${mf.assets.boxes_dir}/${name}.json`).then((b) => {
        chunks.set(name, b.rows);
        if (alive) draw(x);
      });
    }
    return chunks.get(name);
  }

  // The video is letterboxed into its frame (object-fit: contain); boxes follow the same fit.
  function draw(x) {
    const { cv, c } = x, dpr = devicePixelRatio, w = cv.clientWidth, h = cv.clientHeight;
    cv.width = w * dpr;
    cv.height = h * dpr;
    const g = cv.getContext('2d');
    const all = x.ms == null ? null : rows(x, x.ms);
    if (!all?.length) return;
    let best = Infinity;
    for (const r of all) best = Math.min(best, Math.abs(r[0] - x.ms));
    const s = dpr * Math.min(w / c.w, h / c.h), ox = (dpr * w - s * c.w) / 2, oy = (dpr * h - s * c.h) / 2;
    g.font = `${11 * dpr}px ${css.getPropertyValue('--font-mono')}`;
    for (const [t, ltid, , x1, y1, x2, y2] of all) {
      if (Math.abs(t - x.ms) !== best || best > 100) continue;
      const id = owner.get(`${c.id}:${ltid}`), on = selected.has(id);
      if (only && !only.has(id)) continue;
      g.strokeStyle = g.fillStyle = css.getPropertyValue(on ? '--ink' : '--ink-2');
      g.lineWidth = (on ? 2 : 1) * dpr;
      g.strokeRect(ox + x1 * s, oy + y1 * s, (x2 - x1) * s, (y2 - y1) * s);
      g.fillText(id ?? `#${ltid}`, ox + x1 * s + 3 * dpr, Math.max(oy + y1 * s - 4 * dpr, 12 * dpr));
    }
  }

  function sync(t) {
    for (const x of vids) {
      const want = Math.max(0, (t - x.c.offset_ms) / 1000);
      const seg = lanes[x.c.id]?.find(([a, b]) => t >= a && t < b);
      const nov = !seg || seg[2] === 'no_video';
      x.fig.classList.toggle('nov', nov);
      if (nov) x.nov.textContent = `No video at ${tc(t)}`;
      if (clock.playing) {
        x.v.playbackRate = clock.rate;
        if (x.v.paused) x.v.play().catch(() => {}); // rejected when a pause() interrupts it
        if (!x.v.seeking && Math.abs(x.v.currentTime - want) > frame) x.v.currentTime = want;
      } else {
        if (!x.v.paused) x.v.pause();
        if (Math.abs(x.v.currentTime - want) > 0.001) x.v.currentTime = want;
      }
    }
  }

  return {
    el,
    sync,
    highlight(ids) {
      selected = new Set(ids);
      vids.forEach(draw);
    },
    dispose() {
      alive = false;
      for (const { v } of vids) {
        v.pause();
        v.removeAttribute('src');
        v.load();
      }
      el.remove();
    },
  };
}
