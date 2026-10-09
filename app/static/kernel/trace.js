// Agent workflow steps of a persisted run (schemas/agent_run.json) on a Gantt; a step expands inline into why / camera /
// span / entities / exec, its output frame, Request / Response JSON trees and a link to the workspace at that moment.
import { api } from './api.js';
import { exec } from './badges.js';
import { esc, sec, shortEid, tc } from './format.js';

let root = null, run = null, weaveUrl = null;
const ENTITY = /^[PFV][0-9]+$/;

function tree(v, key, depth) {
  const k = key === undefined ? '' : `<span class="jk">${esc(key)}</span>: `;
  if (v === null || typeof v !== 'object') return `<div class="jl">${k}<span class="jv jv-${v === null ? 'null' : typeof v}">${esc(JSON.stringify(v))}</span></div>`;
  const entries = Array.isArray(v) ? v.map((x, i) => [i, x]) : Object.entries(v);
  const size = Array.isArray(v) ? `[${v.length}]` : `{${entries.length}}`;
  return `<details class="jt"${depth < 2 ? ' open' : ''}><summary>${k}<span class="jm">${size}</span></summary>${entries.map(([kk, x]) => tree(x, kk, depth + 1)).join('')}</details>`;
}

// The first thumbnail anywhere in a step output (moment cards carry `thumb`, moments `frame_url`).
function thumbOf(v) {
  if (!v || typeof v !== 'object') return null;
  for (const [k, x] of Object.entries(v)) {
    if ((k === 'thumb' || k === 'frame_url') && typeof x === 'string') return x;
    const t = thumbOf(x);
    if (t) return t;
  }
  return null;
}

// The scene range a tool step read: its envelope's input_range.
export const rangeOf = (s) => s?.output?.input_range ?? null;
// `cam · mm:ss.d` of the range a step read.
export const atOf = (s) => {
  const r = rangeOf(s);
  return r ? `${r.cam_id ?? r.scene_key} · ${tc(r.t0_ms)}` : '';
};
// The step's thumbnail, else a hatched placeholder labelled with what it would show.
export const frame = (s, label) => {
  const url = s && thumbOf(s.output);
  return `<div class="t-frame">${url ? `<img src="${esc(api.url(url))}" alt="">` : `<span class="mono">${esc(label)}</span>`}</div>`;
};

function entities(v, out = new Set()) {
  if (typeof v === 'string' && ENTITY.test(v)) out.add(v);
  else if (v && typeof v === 'object') Object.values(v).forEach((x) => entities(x, out));
  return out;
}

// The workspace route at the moment a step read; warehouse routes carry no time.
function where(s) {
  const r = rangeOf(s), key = r?.scene_key ?? s.input?.scene_key ?? run.context?.scene_key;
  if (!key) return null;
  return run.context?.workspace === 'fleet' ? `#/fleet/${key}@${r?.t0_ms ?? 0}` : `#/warehouse/${key}`;
}

function weave(step) {
  return weaveUrl
    ? `<a class="weave" href="${esc(weaveUrl)}" target="_blank" rel="noopener">Weave ${esc(step.weave_call_id ?? '')}</a>`
    : `<span class="weave off">Weave: ${esc(step.trace_delivery_state)}</span>`;
}

function detail(s) {
  const r = rangeOf(s), ids = [...entities(s.input)], href = where(s);
  const pts = s.output?.pts_read;
  const kv = [
    ['why', s.note && esc(s.note)],
    ['camera', esc(r?.cam_id ?? s.input?.cam ?? s.input?.cam_id ?? '')],
    ['span', r && `${tc(r.t0_ms)} – ${tc(r.t1_ms)}`],
    ['entities', esc(ids.join(', '))],
    ['exec', `${esc(s.exec_state)}${pts != null ? ` · ${pts} read` : ''}`],
    ['evidence', s.evidence_ids.map((e) => `<button class="cite" data-eid="${esc(e)}" title="${esc(e)}">${esc(shortEid(e))}</button>`).join('')],
    ['raw', s.blob_ref && `${esc(s.blob_ref.uri)} · ${s.blob_ref.size} B`],
    ['trace', weave(s)],
  ].filter(([, v]) => v);
  return `
    <div class="trace-detail">
      <div class="td-kv mono">${kv.map(([k, v]) => `<span class="td-k">${k}</span><span class="td-v td-${k}">${v}</span>`).join('')}</div>
      <div class="td-out">
        ${frame(s, `OUTPUT FRAME${r ? ` · ${tc(r.t0_ms)}` : ''}`)}
        <div class="td-links">
          <details class="jt"><summary>Request</summary>${tree(s.input, undefined, 0)}</details>
          <details class="jt"><summary title="${s.name.startsWith('tool.') ? 'Tool envelope' : 'Output'}">Response</summary>${tree(s.output, undefined, 0)}</details>
          ${href ? `<a class="td-3d" href="${esc(href)}">Show in 3D ↗</a>` : ''}
        </div>
      </div>
    </div>`;
}

const ms = (n) => (n < 1000 ? `${n} ms` : sec(n));

export const trace = {
  // el: the container; summaries: step i -> the one-line summary the live stream gave (persisted steps keep only `note`);
  // changed: the i of the step that changed the verdict, shown expanded.
  renderRun(r, el, summaries = new Map(), changed = null) {
    root = el;
    run = r;
    weaveUrl = r.weave_url;
    // Each step's bar sits at its start within the run and is as wide as its share of the run's wall time.
    const t0 = Math.min(...r.steps.map((s) => s.started_ms));
    const total = Math.max(1, ...r.steps.map((s) => s.started_ms + s.elapsed_ms - t0));
    root.innerHTML = `
      <ol class="trace-steps">${r.steps.map((s) => `
        <li class="trace-step${s.parent_step_id ? ' child' : ''}${s.i === changed ? ' changed' : ''}" data-i="${s.i}">
          <button class="trace-row">
            <span class="mono ts-i">${s.i + 1}</span>
            <span class="ts-note">${esc(summaries.get(s.i) ?? s.note ?? s.name)}</span>
            <span class="ts-meta"><span class="mono ts-name">${esc(s.name)}</span>${s.evidence_ids.length ? ` · ${s.evidence_ids.length} evidence` : ''}${s.i === changed ? ' · verdict changed' : ''}${exec(s.exec_state)}</span>
            <span class="ts-bar"><i style="left:${(100 * (s.started_ms - t0)) / total}%;width:${(100 * s.elapsed_ms) / total}%"></i></span>
            <span class="mono ts-ms">${ms(s.elapsed_ms)}</span>
          </button>
        </li>`).join('')}
      </ol>`;
    root.querySelectorAll('.trace-row').forEach((b) => b.addEventListener('click', () => trace.expandStep(+b.parentElement.dataset.i)));
    if (changed != null) trace.expandStep(changed);
  },
  // Toggles the detail of step i.
  expandStep(i) {
    const li = root.querySelector(`.trace-step[data-i="${i}"]`);
    const open = li.querySelector('.trace-detail');
    li.classList.toggle('open', !open);
    if (open) return open.remove();
    li.insertAdjacentHTML('beforeend', detail(run.steps.find((s) => s.i === i)));
  },
  linkWeave(url) {
    weaveUrl = url;
  },
};
