// Reasoning page (#runpage, routes #/run/<run_id> and #/runs): a persisted run on its own page over the workspace,
// which stays mounted underneath. A sticky section nav beside: verdict and answer, the step that changed the verdict,
// the steps (kernel/trace.js), evidence, model notes and VSS's own search for comparison.
import { api } from '../kernel/api.js';
import { icon, say, source, verdict } from '../kernel/badges.js';
import { count, esc, sec, shortEid, tc } from '../kernel/format.js';
import { atOf, frame, rangeOf, trace } from '../kernel/trace.js';

const el = () => document.getElementById('runpage');
const OPS = { lt: '<', le: '≤', gt: '>', ge: '≥', eq: '=' };
const STATUS = { running: 'Running', done: 'Done', error: 'Error', aborted: 'Aborted' };
const NATIVE = { ok: 'done', timeout: 'timed out', error: 'failed' };
const CHECK = { verified: 'yes', unverified: 'no' };
const distance = (c) => (c?.a && c?.b ? `Distance ${c.a.entity_id} to ${c.b.entity_id}` : 'Ground distance');
// method -> label for a person reading the answer (a function gets the input of the step that read it); others fall
// back to the method in words. Same labels as the workspace Evidence tab.
const METHOD_LABEL = {
  ground_distance: distance,
  ground_distance_tri: distance,
  ground_dist: distance,
  closest_approach: 'Closest approach',
  floor_cell_visibility: (c) => (c?.target && c?.cam ? `Floor under ${c.target} as seen from ${c.cam}` : 'Floor cell visibility'),
  caption_embed1_rrf: 'Caption and visual search rank',
  event_gt_dodge: 'Dodge event (ground truth)',
  event_rule_ped_crossing: 'Pedestrian crossing event (rule)',
  geocode: 'Place name match',
  geocode_rapidfuzz: 'Place name match',
  gps_pass: 'GPS pass near the place',
  searched_passes: 'Passes checked',
  obd_speed: 'Speed (OBD)',
  obd_over_limit: 'Over the speed limit (OBD)',
  osm_maxspeed: 'Speed limit (map)',
};
// Model record task -> row title and what it said.
const NOTES = {
  C1: ['Situation C1', (p) => `<b class="mono">${esc(p.label)}</b> <span class="rp-dim">${esc(p.reason)}</span>`],
  C2: ['Next C2', (p) => `predicts <b class="mono">${esc(p.actions.join(' / '))}</b> <span class="rp-dim">within ${esc(p.horizon_s)} s · actual ${p.actual ? esc(p.actual.actions.join(' / ')) : 'unknown'}</span>`],
  C4: ['Timing C4', (p) => p.segments.map((s) => `<span class="mono">model says ≈${tc(s.start_s * 1000)}–${tc(s.end_s * 1000)}</span> <span class="rp-dim">not measured</span> <span class="rp-dim">"${esc(s.text)}"</span>`).join('; ') || `<span class="rp-dim">"${esc(p.query)}" not found</span>`],
};
const css = Object.assign(document.createElement('link'), { rel: 'stylesheet', href: new URL('./run.css', import.meta.url).href });
let observer = null, pinned = false, generation = 0, refreshTimer = null;

const ms = (n) => (n == null ? '–' : n < 1000 ? `${n} ms` : sec(n));
const when = (iso) => iso.slice(0, 16).replace('T', ' ');
const meta = (r) => `${esc(r.run_id)} · ${when(r.created)} · ${count(r.steps.length, 'step')} · ${ms(r.elapsed_ms)}`;
const verdictOf = (r) => (r.final ? say(r.final) : `<span class="verdict">${STATUS[r.status]}</span>`);
const camsOf = (e) => [...new Set(e.observation_ids?.length ? e.observation_ids.map((o) => o.cam_id) : e.cam_id ? [e.cam_id] : [])];
const top = (back, weave) => `
  <header class="rp-top"><span class="brand-mark mono">SVA</span>
    <button class="rp-back">← ${esc(back)}<kbd class="mono">Esc</kbd></button>
    <a class="ghost" href="#/runs">${icon('workflow')}All runs</a>
    ${weave ? `<a class="ghost rp-weave" href="${esc(weave)}" target="_blank" rel="noopener">Open in Weave ↗</a>` : ''}
  </header>`;

function cite(e, eid) {
  const label = e ? `${e.cam_id ?? (camsOf(e).length > 1 ? 'multi-cam' : camsOf(e)[0] ?? e.method)} · ${(e.t0_ms / 1000).toFixed(1)}s` : shortEid(eid);
  return `<button class="cite" data-cite data-eid="${esc(eid)}" title="${esc(eid)}">${esc(label)}</button>`;
}

function value(e) {
  if (e.kind === 'model') return '<span class="rp-dim">describes only</span>';
  if (typeof e.value === 'number') return esc([e.value, e.unit, e.sigma != null && `±${e.sigma}`].filter((x) => x != null && x !== false).join(' '));
  if (e.value && typeof e.value === 'object') {
    const strs = Object.values(e.value).filter((v) => typeof v === 'string');
    return esc(strs.length ? strs.join(' · ') : Object.entries(e.value).filter(([, v]) => typeof v === 'number').slice(0, 2).map(([k, v]) => `${k} ${v}`).join(' · '));
  }
  return esc(e.value ?? '');
}

function raw(e) {
  const rows = [['eid', e.eid], ['method', e.method], ['value', e.value], ['unit', e.unit], ['sigma', e.sigma], ['cams', camsOf(e).join(', ') || null], ['t', `${e.t0_ms}–${e.t1_ms} ms`]];
  return rows.map(([k, v]) => `${k.padEnd(8)}${esc(typeof v === 'string' ? v : JSON.stringify(v))}`).join('\n');
}

function header(r, src, ev) {
  const sentence = (a) => (a.cites?.length
    ? `<span class="rp-sent">${esc(a.text)}${a.cites.map((id) => cite(ev.get(id), id)).join('')}</span>`
    : `<span class="rp-sent uncited">${esc(a.text)}<span class="rp-ns">Not supported</span></span>`);
  const answers = src.steps.filter((s) => s.name === 'answer').flatMap((s) => Array.isArray(s.output) ? s.output : [s.output]).filter((a) => a?.text);
  return `
    <h1 class="rp-q">${esc(r.q)}</h1>
    <div class="rp-meta">${verdictOf(r)}<span class="mono">${meta(r)}</span>
      ${r.source_run_id ? `<span>Cached replay; the steps below are from the original run <a class="mono" href="#/run/${esc(src.run_id)}">${esc(src.run_id)}</a></span>` : ''}</div>
    ${answers.length ? `<p class="rp-answer">${answers.map(sentence).join(' ')}</p>` : ''}`;
}

// The step that changed the verdict: the last one that read evidence a changed claim's predicates rest on.
function changedAt(src) {
  return src.claims.filter((c) => c.after !== (c.before ?? 'UNRESOLVED')).flatMap((claim) => {
    const eids = new Set(claim.predicates.flatMap((p) => p.eids));
    const step = src.steps.findLast((s) => s.evidence_ids.some((e) => eids.has(e)));
    return step ? [{ step, claims: [claim] }] : [];
  });
}

function changed({ step, claims }, src, ev) {
  const ids = new Set(claims[0].predicates.flatMap((p) => p.eids));
  const scene = claims[0].scene_key ?? [...ids].map((eid) => ev.get(eid)?.scene_key).find(Boolean);
  const before = src.steps.findLast((s) => s.i < step.i && rangeOf(s) && (!scene || rangeOf(s).scene_key === scene));
  const moment = (src.hits ?? []).find((m) => claims[0].moment_id ? m.moment_id === claims[0].moment_id : m.scene_key === scene && m.evidence_ids?.some((id) => ids.has(id)));
  const afterFrame = moment?.thumb ? { output: { thumb: moment.thumb } } : step;
  const decided = (p) => p.eids.map((id) => ev.get(id)).find((e) => typeof e?.value === 'number');
  const pred = (p, e = decided(p)) => `<span class="mono rp-pred">${p.subject_refs.map((s) => esc(s.entity_id)).join('–')} ${esc(OPS[p.operator] ?? p.operator)} ${esc(p.value)} ${esc(p.unit ?? '')}${e ? ` → <span class="pv-${e.kind}">${value(e)}</span>` : ''}</span>`;
  return `
    <div class="rp-changed">
      <span class="p-label">WHAT CHANGED THE VERDICT · STEP ${step.i + 1}</span>
      <div class="rp-cg">
        <div class="rp-fr">${frame(before, before ? atOf(before) : 'No earlier frame')}${verdict(claims[0].before ?? 'UNRESOLVED')}</div>
        <span class="rp-arrow">→</span>
        <div class="rp-fr after">${frame(afterFrame, atOf(step))}${verdict(claims[0].after)}</div>
        <div class="rp-claims">${claims.map((c) => `<span class="rp-ctext" data-claim="${esc(c.claim_id)}">${esc(c.text)}</span>${c.predicates.map((p) => pred(p)).join('')}`).join('')}</div>
      </div>
    </div>`;
}

function evidence(src, ev) {
  if (!src.evidence.length) return '<p class="muted" data-empty>No evidence was read in this run.</p>';
  const input = new Map(src.steps.flatMap((s) => s.evidence_ids.map((id) => [id, s.input])));
  const label = (e) => {
    const l = METHOD_LABEL[e.method];
    return typeof l === 'function' ? l(input.get(e.eid)) : l ?? e.method.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
  };
  return `<div class="rp-ev">${[...ev.values()].map((e) => `
    <div class="rp-evrow" data-eid="${esc(e.eid)}">
      ${e.method.startsWith('event_gt') ? '<span aria-hidden="true"></span>' : source(e.kind)}<span class="rp-evlabel">${esc(label(e))}</span>
      <span class="mono rp-evwhere">${esc(camsOf(e).join(' + ') || e.method)} · ${tc(e.t0_ms)}</span>
      <span class="mono rp-evval">${value(e)}</span>
      <button type="button" class="rp-raw-btn" aria-expanded="false">raw</button>
      <pre class="rp-raw" hidden>${raw(e)}</pre>
    </div>`).join('')}</div>`;
}

// C1 / C2 / C4 records the run's model steps returned (vlm.<task> outputs, or a tool envelope's output.record).
function notes(src) {
  const recs = new Map();
  for (const s of src.steps) {
    const rec = s.name.startsWith('vlm.') ? s.output : s.output?.output?.record;
    if (rec?.record_id && NOTES[rec.task]) recs.set(rec.record_id, rec);
  }
  return [...recs.values()].sort((a, b) => a.task.localeCompare(b.task));
}

function note(rec) {
  const [title, body] = NOTES[rec.task], v = rec.verification;
  return `<div class="rp-note"><span class="rp-note-k">${title}</span><span class="rp-note-v">${body(rec.payload)}</span>
    ${v.status === 'conflict' ? `<span class="rp-check conflict">Conflict · ${esc(v.method)}</span>` : `<span class="rp-check" title="${esc(v.method)}">Checked by measure: ${CHECK[v.status]}</span>`}</div>`;
}

function native(d) {
  const hits = (d.hits ?? []).slice(0, 5).map((h) => `
    <li>${esc(h.caption)} <span class="mono rp-dim">${h.scene_key ? `${esc(h.cam_id)} · ${esc(h.scene_key)} · ${sec(h.t_ms)} · ` : ''}similarity ${h.similarity.toFixed(3)}</span></li>`).join('');
  return `<details class="rp-vss">
    <summary>${icon('chevron-right')}${icon('database')}<span>VSS native search</span>
      <span class="mono rp-dim">${NATIVE[d.status] ?? esc(d.status)} · ${sec(d.elapsed_ms)} · ${count(d.hits?.length ?? 0, 'hit')}</span>
      <span class="rp-cmp">For comparison only · not used in the answer</span></summary>
    ${hits ? `<ol class="rp-hits">${hits}</ol>` : '<p class="rp-dim">No results returned</p>'}
    ${d.sql_query ? `<details class="jt ans-sql"><summary>SQL</summary><pre>${esc(d.sql_query)}</pre></details>` : ''}
  </details>`;
}

function activate(id) {
  el().querySelectorAll('.rp-nav button').forEach((b) => b.classList.toggle('on', b.dataset.go === id));
}

// The active nav item is the last section whose top has passed below the top bar; a nav click pins its item until
// the user scrolls by hand, since a short last section may never reach that line.
function spy() {
  observer?.disconnect();
  const secs = [...el().querySelectorAll('.rp-sec')];
  observer = new IntersectionObserver(() => {
    if (pinned) return;
    const line = el().getBoundingClientRect().top + 140;
    activate((secs.findLast((s) => s.getBoundingClientRect().top <= line) ?? secs[0]).dataset.sec);
  }, { root: el(), rootMargin: '-52px 0px -50% 0px', threshold: [0, 1] });
  secs.forEach((s) => observer.observe(s));
  el().onwheel = el().ontouchmove = el().onkeydown = () => (pinned = false);
}

function go(id) {
  pinned = true;
  activate(id);
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  el().querySelector(`.rp-sec[data-sec="${id}"]`).scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
}

// r: the opened run; src: the run whose steps it shows (r itself, or the source of a cache replay).
function typedObservations(src) {
  const records = src.steps.map(s=>s.output?.observation).filter(o=>o?.observation_id && Array.isArray(o.facts));
  const unique = [...new Map(records.map(o=>[o.observation_id,o])).values()];
  return unique.length ? `<span class="p-label">MODEL OBSERVATIONS · NOT MEASURED</span>${unique.map(o=>`<article class="rp-observation"><p class="mono muted">${esc(o.cam_id)} · ${tc(o.t0_ms)} · ${esc(o.verification)}</p>${o.facts.map(f=>`<p>${esc(f.text)}</p><p class="muted">Uncertainty ${esc(f.uncertainty)}</p>${(f.frame_refs?.length?f.frame_refs:o.frame_refs).map(frame=>`<button type="button" data-observed-frame data-scene="${esc(o.scene_key)}" data-cam="${esc(frame.cam_id)}" data-time="${frame.t_ms}">View ${esc(frame.cam_id)} · ${tc(frame.t_ms)}</button>`).join('')}`).join('')}<p class="muted">${esc(o.model_rev)} · ${esc(o.prompt_rev)}</p></article>`).join('')}` : '';
}

function renderRun(r, src, summaries, keepView = false) {
  const scroll = el().scrollTop, active = keepView && el().querySelector('.rp-nav .on')?.dataset.go;
  const expanded = keepView ? [...el().querySelectorAll('.trace-step.open')].map((s) => +s.dataset.i) : [];
  const raws = keepView ? [...el().querySelectorAll('.rp-raw-btn[aria-expanded="true"]')].map((b) => b.closest('[data-eid]').dataset.eid) : [];
  const ev = new Map(src.evidence.map((e) => [e.eid, e]));
  const ch = changedAt(src), recs = notes(src), vss = src.steps.findLast((s) => s.name === 'vss.search' && s.output);
  const secs = [
    ['verdict', 'Verdict', header(r, src, ev)],
    ch.length && ['changed', 'What changed', ch.map((change) => changed(change, src, ev)).join('')],
    ['steps', `Steps · ${src.steps.length}`, '<span class="p-label">STEPS</span><div class="rp-steps"></div>'],
    ['evidence', `Evidence · ${src.evidence.length}`, `<span class="p-label">EVIDENCE</span>${evidence(src, ev)}`],
    typedObservations(src) && ['observations', 'Model observations', typedObservations(src)],
    recs.length && ['models', `Model notes · ${recs.length}`, `<span class="p-label">MODEL NOTES · NOT USED FOR NUMBERS</span><div class="rp-notes">${recs.map(note).join('')}</div>`],
    vss && ['vss', 'VSS comparison', native(vss.output)],
  ].filter(Boolean);
  el().innerHTML = `
    ${top(r.context?.scene_key ?? r.context?.trip_id ?? 'Back', r.weave_url ?? src.weave_url)}
    <div class="rp">
      <nav class="rp-nav">${secs.map(([id, label]) => `<button type="button" data-go="${id}">${label}</button>`).join('')}</nav>
      <div class="rp-main">${secs.map(([id, , html]) => `<section class="rp-sec" data-sec="${id}">${html}</section>`).join('')}</div>
    </div>`;
  trace.renderRun(src, el().querySelector('.rp-steps'), summaries, ch.at(-1)?.step.i);
  for (const i of expanded) if (src.steps.some((s) => s.i === i) && !el().querySelector(`.trace-step[data-i="${i}"].open`)) trace.expandStep(i);
  for (const eid of raws) { const b = el().querySelector(`.rp-evrow[data-eid="${CSS.escape(eid)}"] .rp-raw-btn`); if (b) { b.setAttribute('aria-expanded', 'true'); b.nextElementSibling.hidden = false; } }
  activate(active ?? 'verdict');
  spy();
  if (keepView) el().scrollTop = scroll;
}

function renderList(runs) {
  el().innerHTML = `
    ${top('Back', null)}
    <div class="rp-col">
      <h1 class="rp-title">Recent runs</h1>
      <ol class="rp-list">${runs.map((r) => `
        <li><a href="#/run/${esc(r.run_id)}">${verdictOf(r)}<span class="rp-lq">${esc(r.q)}</span>
          <span class="mono rp-lmeta">${meta(r)}${r.source_run_id ? ' · cached replay' : ''}</span></a></li>`).join('')}
      </ol>
      ${runs.length ? '' : '<p class="muted" data-empty>No runs yet.</p>'}
    </div>`;
}

// Nav items scroll to their section, raw toggles an evidence row's fields, a cite flashes its evidence row.
function onClick(e) {
  const observed=e.target.closest('[data-observed-frame]');
  if(observed){window.dispatchEvent(new CustomEvent('sva:observation-frame',{detail:{scene_key:observed.dataset.scene,cam_id:observed.dataset.cam,t_ms:Number(observed.dataset.time)}}));return;}
  const nav = e.target.closest('.rp-nav [data-go]');
  if (nav) return go(nav.dataset.go);
  const b = e.target.closest('.rp-raw-btn');
  if (b) {
    const open = b.getAttribute('aria-expanded') !== 'true';
    b.setAttribute('aria-expanded', open);
    b.nextElementSibling.hidden = !open;
    return;
  }
  const c = e.target.closest('.cite');
  const row = c && el().querySelector(`.rp-evrow[data-eid="${CSS.escape(c.dataset.eid)}"]`);
  if (!row) return;
  row.scrollIntoView({ block: 'nearest' });
  row.classList.remove('flash');
  void row.offsetWidth;
  row.classList.add('flash');
}

export const runpage = {
  isOpen: () => !el().hidden,
  // runId null lists the recent runs; summaries: see trace.renderRun.
  async open(runId, summaries) {
    const current = ++generation;
    if (!css.isConnected) document.head.append(css);
    observer?.disconnect();
    pinned = false;
    el().hidden = false;
    el().onclick = onClick;
    clearTimeout(refreshTimer);
    const owns = () => current === generation && !el().hidden;
    let previous = null;
    const load = async () => {
      try {
        if (!runId) {
          const runs = await api.get('api/agent/runs');
          if (owns()) renderList(runs);
          return;
        }
        const r = await api.get(`api/agent/runs/${runId}`);
        if (!owns()) return;
        const src = r.source_run_id ? await api.get(`api/agent/runs/${r.source_run_id}`) : r;
        if (!owns()) return;
        const snapshot = JSON.stringify([r, src]);
        if (snapshot !== previous) renderRun(r, src, summaries, previous !== null);
        previous = snapshot;
        el().querySelector('.rp-refresh-error')?.remove();
        if (r.status === 'running' || src.status === 'running') refreshTimer = setTimeout(load, 1000);
      } catch {
        if (!owns()) return;
        if (previous !== null) {
          el().querySelector('.rp-refresh-error')?.remove();
          el().querySelector('.rp-main').insertAdjacentHTML('afterbegin', '<p class="rp-refresh-error muted" role="status">Run updates could not load. Retrying…</p>');
          refreshTimer = setTimeout(load, 2000);
        } else {
          el().innerHTML = `${top('Back', null)}<div class="rp-col"><p class="muted">Could not read run ${esc(runId ?? 'list')}.</p></div>`;
        }
      }
    };
    await load();
  },
  close() {
    generation++;
    clearTimeout(refreshTimer);
    observer?.disconnect();
    el().hidden = true;
  },
};
