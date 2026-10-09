// Text helpers for innerHTML templates and numbers.
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

export const sec = (ms) => `${(ms / 1000).toFixed(1)} s`;
export const span = (t0, t1) => `${(t0 / 1000).toFixed(2)}–${(t1 / 1000).toFixed(2)} s`;
// mm:ss.d timecode
export const tc = (ms) => {
  const s = Math.max(0, ms) / 1000;
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${(s % 60).toFixed(1).padStart(4, '0')}`;
};
export const shortEid = (eid) => eid.slice(0, 10);
export const int = (n) => n.toLocaleString('en-US');
// '1 step', '3 steps'
export const count = (n, noun) => `${n} ${noun}${n === 1 ? '' : 's'}`;
