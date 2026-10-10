// Bottom timeline: ruler (drag = clock.seek, no animation), lanes, the run's Evidence lane; drag across lanes brushes a range.
// The Events row and camera coverage lanes sit behind one Cameras toggle; with several trip lanes, only the current one shows.
// Lane items are [t0_ms, t1_ms, state] like timeline.json lanes; state becomes the segment's CSS class.
import { bus } from './bus.js';
import { clock } from './clock.js';
import { icon } from './badges.js';
import { esc, span, tc } from './format.js';
import { state } from './state.js';

const TICK_STEPS = [100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000, 1800000, 3600000];

export function createTimeline(el, { t0, t1 }) {
  document.head.append(Object.assign(document.createElement('link'), { rel: 'stylesheet', href: new URL('./timeline.css', import.meta.url).href }));
  el.classList.add('tl');
  el.innerHTML = `
    <div class="tl-row tl-ruler"><div class="tl-label"><button class="tl-play" title="Play/pause">${icon('play')}</button><span class="tl-now mono"></span></div><div class="tl-track"></div></div>
    <div class="tl-lanes">
      <div class="tl-row tl-evidence" hidden><div class="tl-label"></div><div class="tl-track"></div></div>
      <div class="tl-row tl-others" hidden><button type="button" class="tl-label"></button><div class="tl-others-line"></div></div>
    </div>
    <div class="tl-cams" hidden>
      <div class="tl-row tl-events"><div class="tl-label">Events</div><div class="tl-track"></div></div>
      <div class="tl-cam-lanes"></div>
    </div>
    <button type="button" class="tl-cams-btn" hidden></button>
    <div class="tl-over"><div class="tl-brush" hidden></div><div class="tl-head"></div></div>`;
  const ruler = el.querySelector('.tl-ruler .tl-track');
  Object.assign(ruler, { tabIndex: 0 });
  ruler.setAttribute('role', 'slider'); ruler.setAttribute('aria-label', 'Scene time');
  const evTrack = el.querySelector('.tl-events .tl-track');
  const lanesEl = el.querySelector('.tl-lanes');
  const evidenceRow = el.querySelector('.tl-evidence');
  const evidenceTrack = evidenceRow.querySelector('.tl-track');
  const others = el.querySelector('.tl-others');
  const camsEl = el.querySelector('.tl-cams');
  const camLanes = el.querySelector('.tl-cam-lanes');
  const camsBtn = el.querySelector('.tl-cams-btn');
  const head = el.querySelector('.tl-head');
  const brush = el.querySelector('.tl-brush');
  const now = el.querySelector('.tl-now');
  const play = el.querySelector('.tl-play');
  const lanes = new Map();
  const seekFns = new Set(), brushFns = new Set(), evidenceFns = new Set();
  let range = { t0, t1 }, events = [], highlighted = null, evidence = [];
  let hoverEid = null, focusEid = null, overEid = null, curTrip = null, othersOpen = false, eventsShown = false;

  const pct = (t) => `${(100 * (t - range.t0)) / (range.t1 - range.t0)}%`;
  const width = (a, b) => `${(100 * (b - a)) / (range.t1 - range.t0)}%`;
  const tAt = (x) => {
    const r = ruler.getBoundingClientRect();
    return range.t0 + Math.min(Math.max((x - r.left) / r.width, 0), 1) * (range.t1 - range.t0);
  };
  const seek = (t) => {
    clock.seek(t);
    for (const fn of seekFns) fn(clock.t);
  };

  function renderRuler() {
    const step = TICK_STEPS.find((s) => (range.t1 - range.t0) / s <= 12) ?? TICK_STEPS.at(-1);
    let html = '';
    for (let t = Math.ceil(range.t0 / step) * step; t < range.t1; t += step) html += `<span class="tick" style="left:${pct(t)}">${esc(tc(t))}</span>`;
    ruler.innerHTML = html;
    ruler.setAttribute('aria-valuemin', String(range.t0)); ruler.setAttribute('aria-valuemax', String(range.t1 - 1));
  }
  function renderLane(lane) {
    lane.track.innerHTML = lane.items
      .map(([a, b, s]) => `<i class="tl-seg s-${esc(s)}" style="left:${pct(a)};width:${width(a, b)}" title="${esc(s)} · ${esc(span(a, b))}"></i>`)
      .join('');
  }
  function renderEvents() {
    evTrack.innerHTML = events
      .map((e) => `<i class="ev-mark sev-${e.severity}${e.id === highlighted || eventsShown ? ' on' : ''}" data-ev="${esc(e.id)}" role="button" tabindex="0" aria-label="${esc(e.type)} at ${tc(e.t0_ms)}" style="left:${pct(e.t0_ms)};width:${width(e.t0_ms, e.t1_ms)}" title="${esc(e.id)} ${esc(e.type)} · severity ${e.severity} · ${esc(span(e.t0_ms, e.t1_ms))}"></i>`)
      .join('');
    renderCams();
  }
  // Measured and human evidence is a tick at t0; map and model evidence covers its span.
  function renderEvidence() {
    evidenceRow.hidden = !evidence.length;
    el.classList.toggle('has-ev', evidence.length > 0);
    evidenceRow.querySelector('.tl-label').textContent = `Evidence · ${evidence.length}`;
    evidenceTrack.innerHTML = evidence
      .map((e) => {
        const at = e.kind === 'measure' || e.kind === 'human' ? `left:${pct(e.t0_ms)}` : `left:${pct(e.t0_ms)};width:${width(e.t0_ms, e.t1_ms)}`;
        return `<i class="tl-evm k-${esc(e.kind)}" data-eid="${esc(e.eid)}" role="button" tabindex="0" aria-label="${esc(e.kind)} evidence at ${tc(e.t0_ms)}" style="${at}" title="${esc(e.eid)} · ${esc(e.kind)} · ${esc(span(e.t0_ms, e.t1_ms))}"></i>`;
      })
      .join('');
    outlineEvidence();
  }
  function outlineEvidence() {
    for (const m of evidenceTrack.children) m.classList.toggle('on', m.dataset.eid === hoverEid || m.dataset.eid === focusEid);
  }
  function renderCams() {
    const n = [...lanes.values()].filter((l) => l.kind === 'coverage').length;
    camsBtn.hidden = !n && !events.length;
    camsBtn.innerHTML = `${n ? `Cameras · ${n}` : 'Events'}${icon(state.cameraLanesOpen ? 'chevron-down' : 'chevron-right')}`;
    camsEl.hidden = !state.cameraLanesOpen && !eventsShown;
    camLanes.hidden = !state.cameraLanesOpen;
    el.classList.toggle('cams-open', state.cameraLanesOpen);
  }
  // Trip lanes: the route's trip (else the first) stays; the others fold into one line until it is clicked.
  function renderTrips() {
    const trips = [...lanes.entries()].filter(([, l]) => l.kind === 'trip');
    const cur = (trips.find(([id]) => id === `trip:${curTrip}`) ?? trips[0])?.[1];
    const rest = trips.length > 1 ? trips.map(([, l]) => l).filter((l) => l !== cur) : [];
    for (const [, l] of trips) {
      const other = rest.includes(l);
      l.row.classList.toggle('tl-other', other);
      l.row.hidden = other && !othersOpen;
    }
    others.hidden = !rest.length;
    others.querySelector('.tl-label').innerHTML = `Other trips · ${rest.length}${icon(othersOpen ? 'chevron-down' : 'chevron-right')}`;
    others.querySelector('.tl-others-line').innerHTML = rest
      .filter((l) => l.items.length)
      .map((l) => `<i style="left:${pct(l.items[0][0])};width:${width(l.items[0][0], l.items.at(-1)[1])}"></i>`)
      .join('');
  }
  function renderAll() {
    renderRuler();
    renderEvents();
    for (const lane of lanes.values()) renderLane(lane);
    renderEvidence();
    renderTrips();
    brush.hidden = true;
    head.style.left = pct(clock.t);
  }

  ruler.addEventListener('keydown', (e) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    e.preventDefault(); seek(e.key === 'Home' ? range.t0 : e.key === 'End' ? range.t1 - 1 : clock.t + (e.key === 'ArrowLeft' ? -100 : 100));
  });
  el.addEventListener('keydown', (e) => {
    if (!['Enter', ' '].includes(e.key)) return;
    const mark = e.target.closest('[data-ev], [data-eid]'); if (!mark) return; e.preventDefault();
    if (mark.dataset.eid) for (const fn of evidenceFns) fn(mark.dataset.eid);
    else { const event = events.find((x) => x.id === mark.dataset.ev); if (event) { tl.highlight(event.id); seek(event.t0_ms); } }
  });
  el.addEventListener('pointerdown', (e) => {
    const track = e.target.closest('.tl-track');
    if (!track) return;
    el.setPointerCapture(e.pointerId);
    const x0 = e.clientX, a = tAt(x0), scrub = track === ruler, evId = e.target.closest('[data-ev]')?.dataset.ev;
    const eid = e.target.closest('.tl-evm')?.dataset.eid;
    let moved = false;
    if (scrub) seek(a);
    const move = (m) => {
      moved ||= Math.abs(m.clientX - x0) > 3;
      if (scrub) seek(tAt(m.clientX));
      else if (moved) {
        const b = tAt(m.clientX);
        brush.hidden = false;
        brush.style.left = pct(Math.min(a, b));
        brush.style.width = width(Math.min(a, b), Math.max(a, b));
      }
    };
    const up = (u) => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      if (scrub) return;
      if (moved) {
        const b = tAt(u.clientX);
        for (const fn of brushFns) fn({ t0: Math.round(Math.min(a, b)), t1: Math.round(Math.max(a, b)) });
        return;
      }
      brush.hidden = true;
      if (eid) {
        for (const fn of evidenceFns) fn(eid);
        return;
      }
      const ev = events.find((x) => x.id === evId);
      if (ev) tl.highlight(ev.id);
      seek(ev ? ev.t0_ms : a);
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
  });
  el.addEventListener('pointerover', (e) => {
    const eid = e.target.closest('.tl-evm')?.dataset.eid ?? null;
    if (eid !== overEid) bus.emit('evidence:hover', (overEid = eid));
  });
  el.addEventListener('pointerleave', () => overEid && bus.emit('evidence:hover', (overEid = null)));
  camsBtn.addEventListener('click', () => {
    state.cameraLanesOpen = !state.cameraLanesOpen;
    renderCams();
  });
  others.addEventListener('click', () => {
    othersOpen = !othersOpen;
    renderTrips();
  });
  play.addEventListener('click', () => (clock.playing ? clock.pause() : clock.play()));
  bus.on('clock:play', (on) => (play.innerHTML = icon(on ? 'pause' : 'play')));
  bus.on('clock:tick', (t) => {
    head.style.left = pct(t);
    now.textContent = tc(t);
    ruler.setAttribute('aria-valuenow', String(t)); ruler.setAttribute('aria-valuetext', tc(t));
  });
  bus.on('clock:range', (r) => {
    range = { ...r };
    renderAll();
  });
  bus.on('fleet:active', ({ scene_key }) => { curTrip = scene_key; renderTrips(); });
  bus.on('route', (r) => {
    curTrip = r.trip_id ?? null;
    renderTrips();
  });
  bus.on('evidence:hover', (eid) => {
    hoverEid = eid;
    outlineEvidence();
  });
  bus.on('evidence:focus', (e) => {
    focusEid = e.eid;
    outlineEvidence();
  });
  bus.on('stage:mode', ({ eid }) => {
    focusEid = eid;
    outlineEvidence();
  });

  const tl = {
    addLane(id, { label, kind }) {
      if (lanes.has(id)) return;
      const row = document.createElement('div');
      row.className = `tl-row lane-${kind}`;
      row.dataset.lane = id;
      row.innerHTML = `<div class="tl-label" title="${esc(label)}">${esc(label)}</div><div class="tl-track"></div>`;
      (kind === 'coverage' ? camLanes : lanesEl).append(row);
      lanes.set(id, { row, kind, track: row.querySelector('.tl-track'), items: [] });
      renderCams();
      renderTrips();
    },
    setLaneData(id, items) {
      const lane = lanes.get(id);
      lane.items = items;
      renderLane(lane);
      if (lane.kind === 'trip') renderTrips();
    },
    setEvents(evs) {
      events = evs;
      renderEvents();
    },
    highlight(eventId) {
      highlighted = eventId;
      renderEvents();
    },
    // Shows the Events row without the camera lanes, every mark highlighted (a zone's events); false folds it back behind the toggle.
    showEvents(on) {
      eventsShown = on;
      renderEvents();
    },
    // items: [{eid, kind, t0_ms, t1_ms}] in timeline time; [] hides the lane.
    setEvidence(items) {
      evidence = items;
      renderEvidence();
    },
    // Removes every lane and event (scene change); the Evidence lane belongs to the run and stays.
    clear() {
      for (const lane of lanes.values()) lane.row.remove();
      lanes.clear();
      events = [];
      eventsShown = false;
      renderAll();
    },
    clearBrush() { brush.hidden = true; },
    onSeek(fn) {
      seekFns.add(fn);
      return () => seekFns.delete(fn);
    },
    onBrush(fn) {
      brushFns.add(fn);
      return () => brushFns.delete(fn);
    },
    // fn(eid) runs when an Evidence mark is clicked.
    onEvidence(fn) {
      evidenceFns.add(fn);
      return () => evidenceFns.delete(fn);
    },
  };
  renderAll();
  now.textContent = tc(clock.t);
  return tl;
}
