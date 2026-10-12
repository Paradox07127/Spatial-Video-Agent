// Bottom timeline: ruler (drag = clock.seek, no animation), lanes, the run's Evidence lane; drag across lanes brushes a range.
// The Events row and camera coverage lanes sit behind one Cameras toggle; with several trip lanes, only the current one shows.
// Lane items are [t0_ms, t1_ms, state] like timeline.json lanes; state becomes the segment's CSS class.
// A 'speed' lane takes samples [t_ms, km/h, limit km/h | null] and draws a sparkline instead.
// setZoom(true) (opt-in, Fleet) adds the overview bar and zoom controls; the ruler then shows a window of the clock range.
import { bus } from './bus.js';
import { clock } from './clock.js';
import { icon } from './badges.js';
import { esc, span, tc } from './format.js';
import { state } from './state.js';

const TICK_STEPS = [100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000, 1800000, 3600000, 7200000, 10800000, 21600000, 43200000, 86400000];
const MIN_SPAN = 1000;
const STATE_WORDS = { fix: 'GPS fix', interpolated: 'interpolated', last_known: 'last-known fix', ok: 'GPS fix', gap: 'last-known fix' };
// ped_crossing -> PC
const glyph = (type) => String(type).split('_').map((w) => w[0] ?? '').join('').toUpperCase();

export function createTimeline(el, { t0, t1 }) {
  document.head.append(Object.assign(document.createElement('link'), { rel: 'stylesheet', href: new URL('./timeline.css', import.meta.url).href }));
  el.classList.add('tl');
  el.innerHTML = `
    <div class="tl-zoombar" hidden>
      <span class="tl-clock"></span>
      <div class="tl-overview" title="Whole range: drag the window to pan"><i class="tl-window"></i></div>
      <span class="tl-zoom"><button type="button" data-zoom="out" title="Zoom out" aria-label="Zoom out">−</button><button type="button" data-zoom="in" title="Zoom in (Ctrl or ⌘ + wheel)" aria-label="Zoom in">+</button><button type="button" data-zoom="reset" title="Show the whole range">Reset</button><button type="button" data-zoom="undo" title="Previous view">Undo</button></span>
    </div>
    <div class="tl-row tl-ruler"><div class="tl-label"><button type="button" class="tl-play" title="Play/pause" aria-label="Play timeline" aria-pressed="false">${icon('play')}</button><span class="tl-now mono"></span></div><div class="tl-track"></div></div>
    <div class="tl-lanes">
      <div class="tl-row tl-evidence" hidden><div class="tl-label"></div><div class="tl-track"></div></div>
      <div class="tl-row tl-others" hidden><button type="button" class="tl-label"></button><div class="tl-others-line"></div></div>
    </div>
    <div class="tl-cams" hidden>
      <div class="tl-row tl-events"><div class="tl-label">Events</div><div class="tl-track"></div></div>
      <div class="tl-cam-lanes"></div>
    </div>
    <button type="button" class="tl-cams-btn" hidden></button>
    <div class="tl-over"><div class="tl-brush" hidden></div><div class="tl-head"></div><div class="tl-ghost" hidden></div><div class="tl-tip mono" hidden></div></div>`;
  const ruler = el.querySelector('.tl-ruler .tl-track');
  Object.assign(ruler, { tabIndex: 0 });
  ruler.setAttribute('role', 'slider'); ruler.setAttribute('aria-label', 'Scene time');
  ruler.setAttribute('aria-orientation', 'horizontal');
  const zoombar = el.querySelector('.tl-zoombar');
  const clockLabel = el.querySelector('.tl-clock');
  const overview = el.querySelector('.tl-overview');
  const win = el.querySelector('.tl-window');
  const zoomBtns = Object.fromEntries([...el.querySelectorAll('[data-zoom]')].map((b) => [b.dataset.zoom, b]));
  const evTrack = el.querySelector('.tl-events .tl-track');
  const lanesEl = el.querySelector('.tl-lanes');
  const evidenceRow = el.querySelector('.tl-evidence');
  const evidenceTrack = evidenceRow.querySelector('.tl-track');
  const others = el.querySelector('.tl-others');
  const camsEl = el.querySelector('.tl-cams');
  const camLanes = el.querySelector('.tl-cam-lanes');
  const camsBtn = el.querySelector('.tl-cams-btn');
  const over = el.querySelector('.tl-over');
  const head = el.querySelector('.tl-head');
  const brush = el.querySelector('.tl-brush');
  const ghost = el.querySelector('.tl-ghost');
  const tip = el.querySelector('.tl-tip');
  const now = el.querySelector('.tl-now');
  const play = el.querySelector('.tl-play');
  const lanes = new Map();
  const seekFns = new Set(), brushFns = new Set(), evidenceFns = new Set();
  // range: the clock's whole range; view: the part the ruler shows (equal unless zoomed).
  let range = { t0, t1 }, view = { t0, t1 }, zoomable = false, views = [], wheelAt = 0;
  let events = [], highlighted = null, evidence = [];
  let hoverEid = null, focusEid = null, overEid = null, curTrip = null, othersOpen = false, eventsShown = false, eventsLane = false;

  const pct = (t) => `${(100 * (t - view.t0)) / (view.t1 - view.t0)}%`;
  const width = (a, b) => `${(100 * (b - a)) / (view.t1 - view.t0)}%`;
  const tAt = (x) => {
    const r = ruler.getBoundingClientRect();
    return view.t0 + Math.min(Math.max((x - r.left) / r.width, 0), 1) * (view.t1 - view.t0);
  };
  const seek = (t) => {
    clock.seek(t);
    for (const fn of seekFns) fn(clock.t);
  };

  function rulerValue(t) {
    const value = Math.min(Math.max(Math.round(t), range.t0), range.t1 - 1);
    ruler.setAttribute('aria-valuenow', String(value));
    ruler.setAttribute('aria-valuetext', `${(value / 1000).toFixed(1)} seconds`);
  }
  function renderRuler() {
    ruler.setAttribute('aria-valuemin', String(range.t0)); ruler.setAttribute('aria-valuemax', String(range.t1 - 1));
    rulerValue(clock.t);
    const pixels = ruler.getBoundingClientRect().width, duration = view.t1 - view.t0;
    if (!pixels || duration <= 0) return ruler.replaceChildren();
    // Measure the actual tick font, padding and longest minute field; Fleet replay can span many hours.
    const probe = document.createElement('span');
    probe.className = 'tick'; probe.textContent = tc(view.t1 - 1); probe.style.visibility = 'hidden';
    ruler.replaceChildren(probe);
    const labelWidth = probe.getBoundingClientRect().width, needed = duration * (labelWidth + 12) / pixels;
    let step = TICK_STEPS.find((s) => s >= needed);
    if (!step) {
      const days = needed / 86400000, scale = 10 ** Math.floor(Math.log10(days));
      step = [1, 2, 5, 10].find((n) => n * scale >= days) * scale * 86400000;
    }
    let html = '';
    for (let t = Math.ceil(view.t0 / step) * step; t < view.t1; t += step) {
      // Labels grow to the right of their true tick; don't clip the final label at the ruler edge.
      if ((t - view.t0) / duration * pixels + labelWidth > pixels) continue;
      html += `<span class="tick" style="left:${pct(t)}">${esc(tc(t))}</span>`;
    }
    ruler.innerHTML = html || `<span class="tick" style="left:0">${esc(tc(view.t0))}</span>`;
  }
  function renderOverview() {
    const full = range.t1 - range.t0;
    win.style.left = `${(100 * (view.t0 - range.t0)) / full}%`;
    win.style.width = `${(100 * (view.t1 - view.t0)) / full}%`;
    const whole = view.t0 === range.t0 && view.t1 === range.t1;
    zoomBtns.reset.disabled = whole;
    zoomBtns.out.disabled = whole;
    zoomBtns.in.disabled = view.t1 - view.t0 <= MIN_SPAN;
    zoomBtns.undo.disabled = !views.length;
  }
  // Speed sparkline: OBD km/h solid, the map limit dotted, over the limit shaded.
  function drawSpeed(lane) {
    const c = lane.track.firstElementChild, w = lane.track.clientWidth, h = lane.track.clientHeight, dpr = devicePixelRatio || 1;
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
    const g = c.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const s = lane.items;
    if (!s.length || !w) return;
    const css = getComputedStyle(el), color = (v) => css.getPropertyValue(v).trim();
    const top = Math.max(10, ...s.map(([, v, l]) => Math.max(v ?? 0, l ?? 0))) * 1.1;
    const x = (t) => ((t - view.t0) / (view.t1 - view.t0)) * w, y = (v) => h - 2 - (v / top) * (h - 4);
    g.fillStyle = color('--danger');
    g.globalAlpha = 0.35;
    for (let i = 0; i + 1 < s.length; i++) {
      const [a, va, l] = s[i], [b, vb] = s[i + 1];
      if (l == null || va == null || vb == null || Math.max(va, vb) <= l) continue;
      g.beginPath();
      g.moveTo(x(a), y(Math.max(va, l))); g.lineTo(x(b), y(Math.max(vb, l))); g.lineTo(x(b), y(l)); g.lineTo(x(a), y(l));
      g.fill();
    }
    g.globalAlpha = 1;
    g.lineWidth = 1;
    g.strokeStyle = color('--src-map');
    g.setLineDash([2, 2]);
    g.beginPath();
    s.forEach(([t, , l], i) => {
      if (l == null) return;
      const next = s[i + 1]?.[0] ?? t;
      g.moveTo(x(t), y(l)); g.lineTo(x(next), y(l));
    });
    g.stroke();
    g.setLineDash([]);
    g.lineWidth = 1.5;
    g.strokeStyle = color('--src-measure');
    g.beginPath();
    let pen = false;
    for (const [t, v] of s) {
      if (v == null) { pen = false; continue; }
      if (pen) g.lineTo(x(t), y(v)); else g.moveTo(x(t), y(v));
      pen = true;
    }
    g.stroke();
  }
  function renderLane(lane) {
    if (lane.kind === 'speed') return drawSpeed(lane);
    lane.track.innerHTML = lane.items
      .map(([a, b, s]) => `<i class="tl-seg s-${esc(s)}" style="left:${pct(a)};width:${width(a, b)}" title="${esc(s)} · ${esc(span(a, b))}"></i>`)
      .join('');
  }
  function renderEvents() {
    const focused = evTrack.contains(document.activeElement) ? document.activeElement.dataset.ev : null;
    const answer = state.stageMode === 'evidence';
    evTrack.innerHTML = events
      .filter((e) => !(e.other && answer))
      .map((e) => `<i class="ev-mark sev-${e.severity}${e.id === highlighted || eventsShown ? ' on' : ''}${e.other ? ' ev-other' : ''}" data-ev="${esc(e.id)}" role="button" tabindex="0" aria-label="${esc(e.type)} at ${tc(e.t0_ms)}" style="left:${pct(e.t0_ms)};width:${width(e.t0_ms, e.t1_ms)}" title="${esc(e.id)} ${esc(e.type)} · severity ${e.severity} · ${esc(span(e.t0_ms, e.t1_ms))}">${eventsLane ? `<b>${esc(glyph(e.type))}</b>${esc(e.severity)}` : ''}</i>`)
      .join('');
    renderCams();
    if (focused) [...evTrack.children].find((node) => node.dataset.ev === focused)?.focus({ preventScroll: true });
  }
  // Measured and human evidence is a tick at t0; map and model evidence covers its span.
  function renderEvidence() {
    const focused = evidenceTrack.contains(document.activeElement) ? document.activeElement.dataset.eid : null;
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
    if (focused) [...evidenceTrack.children].find((node) => node.dataset.eid === focused)?.focus({ preventScroll: true });
  }
  function outlineEvidence() {
    for (const m of evidenceTrack.children) m.classList.toggle('on', m.dataset.eid === hoverEid || m.dataset.eid === focusEid);
  }
  function renderCams() {
    const n = [...lanes.values()].filter((l) => l.kind === 'coverage').length;
    camsBtn.hidden = !n && !events.length;
    camsBtn.innerHTML = `${n ? `Cameras · ${n}` : 'Events'}${icon(state.cameraLanesOpen ? 'chevron-down' : 'chevron-right')}`;
    camsEl.hidden = !state.cameraLanesOpen && !eventsShown && !eventsLane;
    camLanes.hidden = !state.cameraLanesOpen;
    el.classList.toggle('cams-open', state.cameraLanesOpen);
    camsBtn.setAttribute('aria-expanded', String(!camsEl.hidden));
  }
  // Trip lanes: the route's trip (else the first) stays; the others fold into one faded line until it is clicked.
  // Answer mode hides the other trips.
  function renderTrips() {
    const trips = [...lanes.entries()].filter(([, l]) => l.kind === 'trip');
    const cur = (trips.find(([id]) => id === `trip:${curTrip}`) ?? trips[0])?.[1];
    const rest = trips.length > 1 ? trips.map(([, l]) => l).filter((l) => l !== cur) : [];
    const answer = state.stageMode === 'evidence';
    for (const [, l] of trips) {
      const other = rest.includes(l);
      l.row.classList.toggle('tl-other', other);
      l.row.hidden = other && (!othersOpen || answer);
    }
    others.hidden = !rest.length || answer;
    others.querySelector('.tl-label').innerHTML = `Other trips · ${rest.length}${icon(othersOpen ? 'chevron-down' : 'chevron-right')}`;
    others.querySelector('.tl-others-line').innerHTML = rest
      .filter((l) => l.items.length)
      .map((l) => `<i style="left:${pct(l.items[0][0])};width:${width(l.items[0][0], l.items.at(-1)[1])}"></i>`)
      .join('');
  }
  function renderHead(t) {
    head.style.left = pct(t);
    head.hidden = t < view.t0 || t > view.t1;
  }
  function renderAll() {
    renderOverview();
    renderRuler();
    renderEvents();
    for (const lane of lanes.values()) renderLane(lane);
    renderEvidence();
    renderTrips();
    brush.hidden = true;
    renderHead(clock.t);
  }

  // Shows [a, b) clamped into the clock range (at least MIN_SPAN wide); remember = Undo can return to the previous view.
  function setView(a, b, remember = true) {
    const s = Math.min(range.t1 - range.t0, Math.max(MIN_SPAN, b - a));
    const lo = Math.min(Math.max(a - (s - (b - a)) / 2, range.t0), range.t1 - s);
    const next = { t0: Math.round(lo), t1: Math.round(lo + s) };
    if (next.t0 === view.t0 && next.t1 === view.t1) return;
    if (remember) views = [...views.slice(-49), view];
    view = next;
    renderAll();
  }
  const zoomBy = (f, at = (view.t0 + view.t1) / 2, remember = true) => setView(at - (at - view.t0) * f, at + (view.t1 - at) * f, remember);

  // − and + keep the playhead in place while it is in view.
  for (const [name, b] of Object.entries(zoomBtns)) b.addEventListener('click', () => {
    const at = clock.t >= view.t0 && clock.t < view.t1 ? clock.t : undefined;
    if (name === 'in') zoomBy(0.5, at);
    else if (name === 'out') zoomBy(2, at);
    else if (name === 'reset') tl.resetZoom();
    else tl.undoZoom();
  });
  el.addEventListener('wheel', (e) => {
    if (!zoomable || !(e.ctrlKey || e.metaKey) || !e.target.closest('.tl-track, .tl-overview')) return;
    e.preventDefault();
    // One Undo step per wheel gesture.
    const remember = e.timeStamp - wheelAt > 400;
    wheelAt = e.timeStamp;
    zoomBy(e.deltaY < 0 ? 0.8 : 1.25, tAt(e.clientX), remember);
  }, { passive: false });
  // Overview bar: a click centres the window there; dragging pans it.
  overview.addEventListener('pointerdown', (e) => {
    e.preventDefault(); e.stopPropagation();
    const before = view, r = overview.getBoundingClientRect(), full = range.t1 - range.t0, s = view.t1 - view.t0;
    const at = (x) => range.t0 + ((x - r.left) / r.width) * full;
    if (e.target !== win) setView(at(e.clientX) - s / 2, at(e.clientX) + s / 2, false);
    const x0 = e.clientX, v0 = view.t0;
    overview.setPointerCapture(e.pointerId);
    const move = (m) => setView(v0 + ((m.clientX - x0) / r.width) * full, v0 + ((m.clientX - x0) / r.width) * full + s, false);
    const up = () => {
      overview.removeEventListener('pointermove', move);
      overview.removeEventListener('pointerup', up);
      overview.removeEventListener('pointercancel', up);
      if (view !== before) { views = [...views.slice(-49), before]; renderOverview(); }
    };
    overview.addEventListener('pointermove', move);
    overview.addEventListener('pointerup', up);
    overview.addEventListener('pointercancel', up);
  });

  ruler.addEventListener('keydown', (e) => {
    const steps = { ArrowLeft: -100, ArrowDown: -100, ArrowRight: 100, ArrowUp: 100,
      PageDown: -Math.max(1000, Math.round((view.t1 - view.t0) / 10)),
      PageUp: Math.max(1000, Math.round((view.t1 - view.t0) / 10)) };
    if (!(e.key in steps) && !['Home', 'End'].includes(e.key)) return;
    e.preventDefault(); e.stopPropagation();
    const t = e.key === 'Home' ? range.t0 : e.key === 'End' ? range.t1 - 1 : clock.t + steps[e.key] * (e.shiftKey ? 10 : 1);
    seek(Math.min(Math.max(t, range.t0), range.t1 - 1));
  });
  el.addEventListener('keydown', (e) => {
    if (!['Enter', ' '].includes(e.key)) return;
    const mark = e.target.closest('[data-ev], [data-eid]'); if (!mark) return; e.preventDefault(); e.stopPropagation();
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
      el.removeEventListener('pointercancel', up);
      if (scrub) return;
      if (u.type === 'pointercancel') {
        brush.hidden = true;
        return;
      }
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
    el.addEventListener('pointercancel', up);
  });
  // Hover: a ghost cursor over every lane and the value of the lane under the pointer.
  function valueAt(row, t) {
    if (row.classList.contains('tl-events')) {
      const e = events.find((x) => x.t0_ms <= t && t < x.t1_ms);
      return e ? `${e.type.replace(/_/g, ' ')} · severity ${e.severity}` : 'no event';
    }
    const lane = lanes.get(row.dataset.lane);
    if (!lane) return null;
    if (lane.kind === 'speed') {
      const s = lane.items, i = s.findLastIndex(([at]) => at <= t);
      if (i < 0 || s[i][1] == null) return 'no speed';
      const [a, va, l] = s[i], [b, vb] = s[i + 1] ?? s[i];
      const v = vb == null || b === a ? va : va + ((t - a) / (b - a)) * (vb - va);
      return `${Math.round(v)} km/h · OBD${l != null ? ` · limit ${Math.round(l)} km/h · Map` : ''}`;
    }
    const item = lane.items.find(([a, b]) => a <= t && t < b);
    return item ? STATE_WORDS[item[2]] ?? item[2].replace(/_/g, ' ') : 'no data';
  }
  el.addEventListener('pointermove', (e) => {
    const row = e.target.closest('.tl-row');
    if (!row || !e.target.closest('.tl-track')) {
      ghost.hidden = tip.hidden = true;
      return;
    }
    const t = tAt(e.clientX), box = over.getBoundingClientRect(), r = row.getBoundingClientRect(), x = e.clientX - box.left;
    const value = row === ruler.parentElement ? null : valueAt(row, t);
    ghost.hidden = tip.hidden = false;
    ghost.style.left = `${x}px`;
    tip.textContent = value ? `${tc(t)} · ${value}` : tc(t);
    const above = r.top - box.top - tip.offsetHeight - 2;
    tip.style.top = `${above >= 0 ? above : r.bottom - box.top + 2}px`;
    tip.style.left = `${Math.min(Math.max(x - tip.offsetWidth / 2, 0), box.width - tip.offsetWidth)}px`;
  });
  el.addEventListener('pointerleave', () => { ghost.hidden = tip.hidden = true; });
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
  bus.on('clock:play', (on) => {
    play.innerHTML = icon(on ? 'pause' : 'play');
    play.setAttribute('aria-label', on ? 'Pause timeline' : 'Play timeline');
    play.setAttribute('aria-pressed', String(on));
  });
  bus.on('clock:tick', (t) => {
    // A zoomed ruler follows the clock when it leaves the window (playback, seeks).
    if (zoomable && (t < view.t0 || t >= view.t1)) setView(t - (view.t1 - view.t0) / 2, t + (view.t1 - view.t0) / 2, false);
    renderHead(t);
    now.textContent = tc(t);
    rulerValue(t);
  });
  bus.on('clock:range', (r) => {
    range = { ...r };
    view = { ...r };
    views = [];
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
    renderTrips();
    renderEvents();
  });

  const tl = {
    // sub: a second, smaller label line (e.g. the source of a reference line).
    addLane(id, { label, kind, sub = null }) {
      if (lanes.has(id)) return;
      const row = document.createElement('div');
      row.className = `tl-row lane-${kind}`;
      row.dataset.lane = id;
      row.innerHTML = `<div class="tl-label" title="${esc(sub ? `${label} · ${sub}` : label)}"><span>${esc(label)}</span>${sub ? `<span class="tl-sub">${esc(sub)}</span>` : ''}</div><div class="tl-track">${kind === 'speed' ? '<canvas></canvas>' : ''}</div>`;
      (kind === 'coverage' ? camLanes : lanesEl).append(row);
      lanes.set(id, { row, kind, track: row.querySelector('.tl-track'), items: [] });
      renderCams();
      renderTrips();
    },
    removeLane(id) {
      lanes.get(id)?.row.remove();
      lanes.delete(id);
      renderCams();
      renderTrips();
    },
    setLaneData(id, items) {
      const lane = lanes.get(id);
      lane.items = items;
      renderLane(lane);
      if (lane.kind === 'trip') renderTrips();
    },
    // evs: [{id, type, severity, t0_ms, t1_ms, other?}]; other = another trip's event (faded, hidden in Answer mode).
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
    // Keeps the Events row open as a lane with each event's type glyph and severity (narrow layouts fold it behind the toggle).
    setEventsLane(on) {
      eventsLane = on;
      el.classList.toggle('ev-lane', on);
      renderEvents();
    },
    // Seeks to the previous (dir -1) or next (dir 1) event of the shown trip; returns it.
    stepEvent(dir) {
      const own = events.filter((e) => !e.other).sort((a, b) => a.t0_ms - b.t0_ms);
      const e = dir > 0 ? own.find((x) => x.t0_ms > clock.t) : own.findLast((x) => x.t0_ms < clock.t);
      if (e) { tl.highlight(e.id); seek(e.t0_ms); }
      return e ?? null;
    },
    // The previous / next Evidence mark from the clock time (eid or null).
    stepEvidence(dir) {
      const marks = [...evidence].sort((a, b) => a.t0_ms - b.t0_ms);
      return (dir > 0 ? marks.find((x) => x.t0_ms > clock.t) : marks.findLast((x) => x.t0_ms < clock.t))?.eid ?? null;
    },
    // items: [{eid, kind, t0_ms, t1_ms}] in timeline time; [] hides the lane.
    setEvidence(items) {
      evidence = items;
      renderEvidence();
    },
    // Removes every lane and event (scene change), and the page options (zoom, clock name, events lane);
    // the Evidence lane belongs to the run and stays.
    clear() {
      for (const lane of lanes.values()) lane.row.remove();
      lanes.clear();
      events = [];
      eventsShown = false;
      tl.setEventsLane(false);
      tl.setClock('');
      tl.setZoom(false);
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
    // Names the clock the ruler shows (e.g. Trip time, Fleet replay clock).
    setClock(label) {
      clockLabel.textContent = label;
      now.title = label;
    },
    setZoom(on) {
      zoomable = on;
      zoombar.hidden = !on;
      el.classList.toggle('zoom', on);
      view = { ...range };
      views = [];
      renderAll();
    },
    get view() { return { ...view }; },
    get zoomable() { return zoomable; },
    // Shows [t0, t1) of timeline time; no-op unless zoom is on.
    zoomTo(a, b) {
      if (zoomable) setView(a, b);
    },
    resetZoom() {
      if (zoomable) setView(range.t0, range.t1);
    },
    undoZoom() {
      if (!views.length) return;
      view = views.at(-1);
      views = views.slice(0, -1);
      renderAll();
    },
  };
  renderAll();
  // Resize only redraws the ruler's ticks and the canvases. Paused clocks, evidence marks and an active brush stay intact.
  const resized = () => {
    renderRuler();
    for (const lane of lanes.values()) if (lane.kind === 'speed') drawSpeed(lane);
  };
  new ResizeObserver(resized).observe(ruler);
  document.fonts.ready.then(resized);
  now.textContent = tc(clock.t);
  return tl;
}
