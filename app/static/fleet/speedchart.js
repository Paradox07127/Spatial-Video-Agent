// Speed chart (spec §9.3, layout A): OBD speed vs. OSM speed limit for the moment ±10 s.
// Numbers carry source badges (OBD: Measured, Limit: Map).
import { icon, source } from '../kernel/badges.js';
import { esc, sec, tc } from '../kernel/format.js';
import { posAt } from './geo.js';

const W = 460, H = 190, PAD = { top: 28, right: 16, bottom: 26, left: 42 };

export function createSpeedChart(el, { trip, t_ms, moment, onSeek }) {
  el.classList.add('fl-speed-cell');
  el.innerHTML = `
    <header class="fl-speed-head">
      <span class="p-label">${icon('ruler')}SPEED OVER TIME</span>
      <span class="fl-speed-badges"></span>
    </header>
    <div class="fl-speed-chart-wrap">
      <svg class="fl-speed-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-label="Speed chart"></svg>
    </div>`;

  const headBadges = el.querySelector('.fl-speed-badges');
  const svg = el.querySelector('.fl-speed-svg');
  let curTrip = trip, curMoment = moment, curT = t_ms;

  function render() {
    if (!curTrip || !curTrip.pts.length) {
      svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle" fill="var(--ink-2)" font-size="12">Speed data unavailable</text>`;
      headBadges.innerHTML = '';
      return;
    }

    const t0 = (curMoment?.t0_ms ?? curT) - 10000;
    const t1 = (curMoment?.t1_ms ?? curT) + 10000;
    const span = Math.max(1000, t1 - t0);

    const pts = curTrip.pts.filter((p) => p.t_ms >= t0 - 1000 && p.t_ms <= t1 + 1000);
    const curP = posAt(curTrip.pts, curT);
    const curSpeed = curP ? Math.round(curP.speed * 3.6) : null;
    const curLimit = curP?.maxspeed_kmh ?? null;

    headBadges.innerHTML = `
      <span class="src src-measure" title="Source: Measured (OBD speed)">${icon('ruler')}OBD ${curSpeed != null ? `${curSpeed} km/h` : '—'}</span>
      <span class="src src-map" title="Source: Map (OSM speed limit)">${icon('map')}Limit ${curLimit != null ? `${curLimit} km/h` : '—'}</span>`;

    const maxSpeed = Math.max(40, ...pts.map((p) => Math.max((p.speed ?? 0) * 3.6, p.maxspeed_kmh ?? 0)));
    const yMax = Math.ceil((maxSpeed * 1.15) / 10) * 10;
    const chartW = W - PAD.left - PAD.right;
    const chartH = H - PAD.top - PAD.bottom;

    const xOf = (t) => PAD.left + Math.max(0, Math.min(chartW, ((t - t0) / span) * chartW));
    const yOf = (v) => PAD.top + chartH - (Math.max(0, Math.min(yMax, v)) / yMax) * chartH;

    let gridHtml = '';
    // Horizontal ticks at 0, 20, 40...
    const step = yMax <= 60 ? 20 : yMax <= 120 ? 30 : 40;
    for (let v = 0; v <= yMax; v += step) {
      const y = yOf(v);
      gridHtml += `<line x1="${PAD.left}" y1="${y}" x2="${W - PAD.right}" y2="${y}" stroke="var(--rule)" stroke-width="1"/>
        <text x="${PAD.left - 6}" y="${y + 4}" text-anchor="end" fill="var(--ink-3)" font-size="10" font-family="var(--font-mono)">${v}</text>`;
    }

    // Moment span highlight background
    let momentHtml = '';
    if (curMoment && curMoment.t0_ms != null) {
      const mx0 = xOf(curMoment.t0_ms), mx1 = xOf(curMoment.t1_ms ?? curMoment.t0_ms + 1000);
      momentHtml = `<rect x="${mx0}" y="${PAD.top}" width="${Math.max(2, mx1 - mx0)}" height="${chartH}" fill="rgba(86, 180, 233, 0.08)"/>`;
    }

    // GPS gap bands
    let gapHtml = '';
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i];
      if (p.state === 'last_known') {
        const nextP = pts[i + 1] ?? { t_ms: p.t_ms + (p.age_ms || 1000) };
        const gx0 = xOf(p.t_ms), gx1 = xOf(nextP.t_ms);
        gapHtml += `<rect x="${gx0}" y="${PAD.top}" width="${Math.max(1, gx1 - gx0)}" height="${chartH}" fill="rgba(248, 147, 188, 0.12)" stroke="var(--neutral)" stroke-width="0.5" stroke-dasharray="2 2"/>`;
      }
    }

    // Limit line (OSM maxspeed)
    let limitPath = '';
    for (const p of pts) {
      if (p.maxspeed_kmh != null) {
        const x = xOf(p.t_ms), y = yOf(p.maxspeed_kmh);
        limitPath += (limitPath ? ` L ${x} ${y}` : `M ${x} ${y}`);
      }
    }

    // Speed curve (OBD)
    let speedPath = '', areaPath = '';
    if (pts.length) {
      speedPath = `M ${xOf(pts[0].t_ms)} ${yOf(pts[0].speed * 3.6)}`;
      areaPath = `M ${xOf(pts[0].t_ms)} ${PAD.top + chartH} L ${xOf(pts[0].t_ms)} ${yOf(pts[0].speed * 3.6)}`;
      for (let i = 1; i < pts.length; i++) {
        const x = xOf(pts[i].t_ms), y = yOf(pts[i].speed * 3.6);
        speedPath += ` L ${x} ${y}`;
        areaPath += ` L ${x} ${y}`;
      }
      areaPath += ` L ${xOf(pts.at(-1).t_ms)} ${PAD.top + chartH} Z`;
    }

    // Time cursor
    const cx = xOf(curT);
    const cy = curSpeed != null ? yOf(curSpeed) : null;
    const cursorHtml = `
      <line x1="${cx}" y1="${PAD.top}" x2="${cx}" y2="${PAD.top + chartH}" stroke="var(--ink)" stroke-width="1.5" stroke-dasharray="3 2"/>
      ${cy != null ? `<circle cx="${cx}" cy="${cy}" r="4" fill="var(--src-measure)" stroke="var(--paper)" stroke-width="2"/>` : ''}
      <text x="${cx}" y="${PAD.top - 6}" text-anchor="middle" fill="var(--ink)" font-size="10" font-family="var(--font-mono)">${tc(curT)}</text>`;

    // X axis labels
    const tStart = Math.ceil(t0 / 5000) * 5000;
    let xLabelsHtml = '';
    for (let t = tStart; t <= t1; t += 5000) {
      const x = xOf(t);
      xLabelsHtml += `<text x="${x}" y="${PAD.top + chartH + 16}" text-anchor="middle" fill="var(--ink-3)" font-size="10" font-family="var(--font-mono)">${tc(t)}</text>`;
    }

    svg.innerHTML = `
      <defs>
        <linearGradient id="fl-speed-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--src-measure)" stop-opacity="0.25"/>
          <stop offset="100%" stop-color="var(--src-measure)" stop-opacity="0.0"/>
        </linearGradient>
      </defs>
      ${gridHtml}
      ${momentHtml}
      ${gapHtml}
      ${areaPath ? `<path d="${areaPath}" fill="url(#fl-speed-fill)"/>` : ''}
      ${limitPath ? `<path d="${limitPath}" fill="none" stroke="var(--src-map)" stroke-width="2" stroke-dasharray="4 3"/>` : ''}
      ${speedPath ? `<path d="${speedPath}" fill="none" stroke="var(--src-measure)" stroke-width="2.5" stroke-linecap="round"/>` : ''}
      ${xLabelsHtml}
      ${cursorHtml}`;
  }

  svg.addEventListener('click', (e) => {
    if (!curTrip) return;
    const rect = svg.getBoundingClientRect();
    const clickX = ((e.clientX - rect.left) / rect.width) * W;
    const chartW = W - PAD.left - PAD.right;
    const frac = Math.max(0, Math.min(1, (clickX - PAD.left) / chartW));
    const t0 = (curMoment?.t0_ms ?? curT) - 10000;
    const t1 = (curMoment?.t1_ms ?? curT) + 10000;
    const targetT = Math.round(t0 + frac * (t1 - t0));
    onSeek(targetT);
  });

  render();

  return {
    update(t, trip = curTrip) {
      curT = t;
      curTrip = trip;
      render();
    },
    setMoment(m) {
      curMoment = m;
      render();
    },
    dispose() {
      svg.replaceChildren();
    },
  };
}
