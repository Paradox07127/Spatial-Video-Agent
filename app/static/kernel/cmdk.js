// ⌘K palette (#cmdk): deterministic jumps filtered by the typed text; the last entry asks the agent.
import { icon } from './badges.js';
import { esc } from './format.js';

const el = () => document.getElementById('cmdk');
let items = [], shown = [], active = 0, ask = null, invoking = null;

function render() {
  const q = input.value.trim();
  const match = q.toLowerCase();
  shown = items.filter((it) => !match || it.label.toLowerCase().includes(match) || it.hint.toLowerCase().includes(match));
  if (q) shown.push({ icon: 'search', label: `Ask the agent: ${q}`, hint: '↵', run: () => ask(q) });
  active = Math.min(active, Math.max(shown.length - 1, 0));
  el().querySelector('ul').innerHTML = shown
    .map((it, i) => `<li id="cmdk-option-${i}" role="option" aria-selected="${i === active}" class="${i === active ? 'on' : ''}" data-i="${i}">${icon(it.icon)}<span class="cmdk-label">${esc(it.label)}</span><span class="mono hint">${esc(it.hint)}</span></li>`)
    .join('') || '<li class="empty">Type to search scenes, or ask the agent</li>';
  if (shown.length) input.setAttribute('aria-activedescendant', `cmdk-option-${active}`);
  else input.removeAttribute('aria-activedescendant');
  el().querySelector('li.on')?.scrollIntoView({ block: 'nearest' });
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
    if (!cmdk.isOpen()) invoking = document.activeElement;
    items = list;
    ask = onAsk;
    active = 0;
    input.value = '';
    el().hidden = false;
    input.setAttribute('aria-expanded', 'true');
    render();
    input.focus();
  },
  close() {
    el().hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    if (invoking?.isConnected) invoking.focus();
    invoking = null;
  },
};

// Module scripts run after the document is parsed, so #cmdk exists here.
const input = el().querySelector('input');
const listbox = el().querySelector('ul');
el().setAttribute('role', 'dialog');
el().setAttribute('aria-modal', 'true');
el().setAttribute('aria-label', 'Go to a scene or ask the agent');
listbox.id = 'cmdk-options';
listbox.setAttribute('role', 'listbox');
listbox.setAttribute('aria-label', 'Destinations and agent question');
input.setAttribute('role', 'combobox');
input.setAttribute('aria-label', 'Scene, trip, or question');
input.setAttribute('aria-autocomplete', 'list');
input.setAttribute('aria-controls', listbox.id);
input.setAttribute('aria-expanded', 'false');
input.setAttribute('autocomplete', 'off');
document.head.append(Object.assign(document.createElement('link'), { rel: 'stylesheet', href: new URL('./cmdk.css', import.meta.url).href }));
// Focus stays in the combobox while aria-activedescendant names the option.
// Tab loops through any dialog controls without reaching the background page.
el().addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cmdk.close(); return; }
  if (e.key !== 'Tab') return;
  const controls = [...el().querySelectorAll('input, button, a[href], [tabindex]:not([tabindex="-1"])')]
    .filter((node) => !node.disabled && node.getClientRects().length);
  const first = controls[0] ?? input, last = controls.at(-1) ?? input;
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});
document.addEventListener('focusin', (e) => {
  if (cmdk.isOpen() && !el().contains(e.target)) input.focus();
});
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
