// Model line in the Answer tab (#modelpanel, spec §2.5): one summary line by default (C1 label · C2 predicted actions · C4 interval)
// with whether a measurement checked it, expanded to four rows. Every row says whether a measurement checked it.
import { icon, source } from './badges.js';
import { esc, sec, span } from './format.js';

const el = () => document.getElementById('modelpanel');
let last = [];

const CHECK = { verified: 'yes', unverified: 'no', conflict: 'conflict' };
const check = (r) => `<span class="mp-check ck-${r.verification.status}" title="${esc(r.verification.method)}">Checked by measure: ${CHECK[r.verification.status]}</span>`;
const seg = (s) => `${span(s.start_s * 1000, s.end_s * 1000)} ${esc(s.text)}`;

function rows(by) {
  const { C1: c1, C2: c2, C3: c3, C4: c4, C5: c5 } = by;
  const none = '<span class="mp-none">No record this run</span>';
  const row = (title, r, body) => `
    <div class="mp-row">
      <div class="mp-title">${title}</div>
      <div class="mp-body">${body}</div>
      <div class="mp-meta">${source('model')}${r ? check(r) : ''}</div>
    </div>`;
  return [
    row('Situation C1', c1, c1 ? `<b class="mono">${esc(c1.payload.label)}</b> ${esc(c1.payload.reason)}` : none),
    row('Next C2', c2, c2 ? `predicts <b class="mono">${esc(c2.payload.actions.join(' / '))}</b> (frames ≤ ${sec(c2.payload.max_frame_ms)} only, within ${c2.payload.horizon_s} s) · actual <b class="mono">${c2.payload.actual ? esc(c2.payload.actual.actions.join(' / ')) : 'unknown'}</b>` : none),
    row('Timing C4', c4, c4 ? `"${esc(c4.payload.query)}" ${c4.payload.segments.map(seg).join('; ') || 'not found'}` : none),
    row('Objects C3/C5', c3 || c5, [
      c3 && c3.payload.objects.map((o) => `${esc(o.label)} (depth ${o.geometric_support ? 'checked' : 'unchecked'})`).join(', '),
      c5 && `${esc(c5.payload.text)}${c5.payload.sign_text ? ` "${esc(c5.payload.sign_text)}"` : ''}`,
    ].filter(Boolean).join(' · ') || none),
  ].join('');
}

// One verdict for the summary line: any conflict wins, then any unchecked record.
function checked(rs) {
  const st = rs.map((r) => r.verification.status);
  return CHECK[st.includes('conflict') ? 'conflict' : st.every((s) => s === 'verified') ? 'verified' : 'unverified'];
}

export const modelpanel = {
  render(results, mode) {
    last = results;
    const by = Object.fromEntries(results.map((r) => [r.task, r]));
    const parts = [
      by.C1 && `Situation <b class="mono">${esc(by.C1.payload.label)}</b>`,
      by.C2 && `predicts <b class="mono">${esc(by.C2.payload.actions.join('/'))}</b>`,
      by.C4?.payload.segments[0] && `located ${span(by.C4.payload.segments[0].start_s * 1000, by.C4.payload.segments[0].end_s * 1000)}`,
    ].filter(Boolean);
    const shown = [by.C1, by.C2, by.C4?.payload.segments[0] && by.C4].filter(Boolean);
    el().dataset.mode = mode;
    el().innerHTML = `
      <button class="mp-summary" aria-expanded="${mode === 'full'}">
        ${icon('cpu')}<span class="mp-text"><span class="mp-line">${parts.join(' · ') || 'No model record this run'}</span>
        ${shown.length ? `<span class="mp-sub">Checked by measure: ${checked(shown)} · describes only, no numbers</span>` : ''}</span>${icon(mode === 'full' ? 'chevron-down' : 'chevron-right')}
      </button>
      ${mode === 'full' ? `<div class="mp-rows">${rows(by)}</div>` : ''}`;
    el().querySelector('.mp-summary').addEventListener('click', () => (mode === 'full' ? modelpanel.collapse() : modelpanel.expand()));
  },
  expand() {
    modelpanel.render(last, 'full');
  },
  collapse() {
    modelpanel.render(last, 'summary');
  },
};
