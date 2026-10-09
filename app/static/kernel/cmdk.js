// ⌘K palette (#cmdk): deterministic jumps filtered by the typed text; the last entry asks the agent.
import { icon } from './badges.js';
import { esc } from './format.js';

const el = () => document.getElementById('cmdk');
let items = [], shown = [], active = 0, ask = null;

function render() {
  const q = input.value.trim();
  shown = items.filter((it) => !q || it.label.includes(q) || it.hint.includes(q));
  if (q) shown.push({ icon: 'search', label: `Ask the agent: ${q}`, hint: '↵', run: () => ask(q) });
  active = Math.min(active, Math.max(shown.length - 1, 0));
  el().querySelector('ul').innerHTML = shown
    .map((it, i) => `<li class="${i === active ? 'on' : ''}" data-i="${i}">${icon(it.icon)}<span class="cmdk-label">${esc(it.label)}</span><span class="mono hint">${esc(it.hint)}</span></li>`)
    .join('') || '<li class="empty">Type to search scenes, or ask the agent</li>';
}

function pick(i) {
  const it = shown[i];
  cmdk.close();
  it?.run();
}

export const cmdk = {
  isOpen: () => !el().hidden,
  // list: [{icon, label, hint, run()}]; onAsk(q) runs the "ask agent" entry.
  open(list, onAsk) {
    items = list;
    ask = onAsk;
    active = 0;
    input.value = '';
    el().hidden = false;
    render();
    input.focus();
  },
  close() {
    el().hidden = true;
  },
};

// Module scripts run after the document is parsed, so #cmdk exists here.
const input = el().querySelector('input');
input.addEventListener('input', () => ((active = 0), render()));
input.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { e.stopPropagation(); cmdk.close(); }
  else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    active = (active + (e.key === 'ArrowDown' ? 1 : -1) + shown.length) % Math.max(shown.length, 1);
    render();
  } else if (e.key === 'Enter') { e.preventDefault(); pick(active); }
});
el().addEventListener('click', (e) => {
  const li = e.target.closest('li[data-i]');
  if (li) pick(+li.dataset.i);
  else if (e.target === el()) cmdk.close();
});
