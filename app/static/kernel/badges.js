// Source badges (line style + icon), exec_state chips and verdict chips, as HTML strings.
import { esc } from './format.js';

const SRC = { measure: ['Measured', 'ruler'], map: ['Map', 'map'], model: ['Model', 'cpu'], human: ['Human', 'user-check'] };
const EXEC = { cached: 'cached', precomputed: 'precomputed', live: 'live', waiting: 'waiting', failed: 'failed' };
const SAY = { SUPPORTED: 'Supported', CONTRADICTED: 'Contradicted', UNRESOLVED: 'Unresolved', NO_MATCH: 'No match', OUT_OF_RANGE: 'Out of range', UNSUPPORTED: 'Out of scope' };

export const icon = (id) => `<svg class="i" aria-hidden="true"><use href="icons.svg#${id}"/></svg>`;
export const source = (kind) => `<span class="src src-${kind}" title="Source: ${SRC[kind][0]}">${icon(SRC[kind][1])}${SRC[kind][0]}</span>`;
export const srcLabel = (kind) => SRC[kind][0];
export const srcIcon = (kind) => icon(SRC[kind][1]);
export const exec = (s) => `<span class="exec exec-${s}" title="exec_state=${s}">${EXEC[s]}</span>`;
export const verdict = (v) => `<span class="verdict v-${v.toLowerCase()}">${esc(v)}</span>`;
// The final verdict in words for a non-engineer; the code stays in the title.
export const say = (v) => `<span class="verdict say v-${v.toLowerCase()}" title="${esc(v)}">${v === 'UNSUPPORTED' ? icon('ban') : ''}${SAY[v]}</span>`;
// Evidence level badge (L1 / L2 / L3)
export const level = (lvl) => (lvl ? `<span class="level evidence-level level-${esc(lvl).toLowerCase()} mono" title="Evidence level ${esc(lvl)}">${esc(lvl)}</span>` : '');
// Claim before -> after; before is null on the first assessment.
export const transition = (before, after) =>
  before ? `${verdict(before)}<span class="arrow">→</span>${verdict(after)}` : verdict(after);
