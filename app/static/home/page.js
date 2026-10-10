// Home page (#/, board v2-1a): the last warehouse scene behind one question box with an @ source picker and example
// questions. A question goes through ctx.ask, which opens its workspace on the first hits; a refused question stays here
// and showAbstain() explains it under the box.
import { icon } from '../kernel/badges.js';
import { esc } from '../kernel/format.js';

const LAST_ROUTE = 'sva.lastRoute';
const PICK_ANY = 'Leave empty and the agent picks the scene';

let el = null, ctx = null, sources = null;
// at: the open @ list {start, end, rows, i}; i === rows.length is the "agent picks" row. src: the picked row or null.
let at = null, src = null, pickerGeneration = 0;

const wsIcon = (ws) => icon({ fleet: 'truck', street: 'camera' }[ws] ?? 'warehouse');
const chip = (q, ws) => `<button type="button" class="preset" data-ws="${ws}">${wsIcon(ws)}${esc(q)}</button>`;
const shortKey = (key) => key.replace(/^fl-pie-(set\d+)_video_(\d+)$/, '$1·$2');

function lastRoute() {
  try {
    return localStorage.getItem(LAST_ROUTE);
  } catch {
    return null;
  }
}

// opts: {ws, hash} of a picked source, or {ws, preset: true} for a chip.
function ask(q, opts) {
  el.querySelector('#home-input').value = q;
  el.querySelector('.home-refusal')?.remove();
  ctx.ask(q, opts);
}

export function mount(root, c) {
  ctx = c;
  at = src = null;
  const css = Object.assign(document.createElement('link'), { rel: 'stylesheet', href: new URL('./home.css', import.meta.url).href });
  document.head.append(css);
  const { warehouse, fleet } = ctx.presets;
  root.innerHTML = `
    <div class="home">
      <div class="home-bg" aria-hidden="true"></div>
      <div class="home-vignette"></div>
      <div class="home-brand"><span class="home-mark mono">SVA</span>Spatial Video Agent</div>
      <span class="home-scene mono" hidden></span>
      <h1 class="home-title">What happened, where, and how far apart?</h1>
      <form id="home-form" class="home-ask" autocomplete="off">
        ${icon('search')}<span class="home-src mono" hidden></span><input id="home-input" placeholder="Ask about any moment in the footage">
        <button class="home-go" title="Ask">${icon('corner-down-left')}</button>
        <ul class="home-at" role="listbox" hidden></ul>
      </form>
      <div class="home-out"></div>
      <div class="home-chips">${[...warehouse.slice(0, 2).map((q) => chip(q, 'warehouse')), chip(fleet[0], 'fleet')].join('')}</div>
      <p class="home-note">Numbers come from measurement. Questions about identity, intent or blame are declined.</p>
    </div>`;
  el = root.firstElementChild;
  sources = ctx.workspaces().then(sourceRows);
  const input = el.querySelector('#home-input');
  el.querySelector('#home-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (q) ask(q, { ws: src?.ws, hash: src?.hash });
  });
  input.addEventListener('input', onInput);
  input.addEventListener('keydown', onKey);
  input.addEventListener('blur', closeAt);
  // pointerdown keeps the focus in the input, so its blur does not close the list first
  el.querySelector('.home-at').addEventListener('pointerdown', (e) => {
    const row = e.target.closest('.home-at-row');
    e.preventDefault();
    if (row) pick(+row.dataset.i);
  });
  el.addEventListener('click', (e) => {
    if (e.target.closest('.home-src-x')) return setSource(null);
    const b = e.target.closest('.preset');
    if (b) ask(b.textContent, { ws: b.dataset.ws, preset: true });
  });

  let stage = null, gone = false;
  const key = lastRoute()?.match(/^#\/warehouse\/([a-z0-9_-]+)$/)?.[1];
  const host = el.querySelector('.home-bg'), label = el.querySelector('.home-scene');
  if (key) {
    background(host, key).then(
      (s) => {
        if (gone) return s.dispose();
        stage = s;
        label.textContent = `Warehouse · ${key}`;
        label.hidden = false;
      },
      () => host.replaceChildren(),
    );
  }
  return () => {
    closeAt();
    gone = true;
    stage?.dispose();
    css.remove();
    el = ctx = sources = at = src = null;
  };
}

// A read-only stage of the scene: structure and actors at its first frame, loaded like warehouse/page.js loadScene.
async function background(host, key) {
  const { api } = ctx;
  const [{ createStage }, mf] = await Promise.all([import('../warehouse/stage3d.js'), api.get(`api/scene/${key}/manifest`)]);
  const asset = (k) => mf.assets[k] && api.get(`api/assets/${key}/${mf.assets[k]}`);
  const [buf, skeleton, forklift] = await Promise.all([
    mf.assets.points && fetch(api.asset(`${key}/${mf.assets.points}`)).then((r) => r.arrayBuffer()),
    asset('skeleton'), asset('forklift'),
  ]);
  const canvas = document.createElement('canvas');
  host.append(canvas);
  const stage = createStage(canvas);
  stage.loadManifest(mf);
  stage.setPoints(buf);
  stage.setSkeleton(skeleton);
  stage.setForklift(forklift);
  stage.setTime(mf.t0_ms);
  stage.orbit(true);
  return stage;
}

// Scenes and trips of api/workspaces as one list of @ rows.
function sourceRows(workspaces) {
  return workspaces.flatMap((w) => w.scenes.map((s) => (w.id === 'fleet'
    ? { ws: 'fleet', id: shortKey(s.scene_key), hash: `#/fleet/${s.scene_key}@${s.t0_ms ?? 0}`, meta: `dashcam · ${s.title}` }
    : w.id === 'street'
    ? { ws: 'street', id: s.scene_key, hash: `#/street/${s.scene_key}`, meta: `2D video · ${s.title}` }
    : { ws: 'warehouse', id: s.scene_key, hash: `#/warehouse/${s.scene_key}`, meta: `${Math.round((s.t1_ms - s.t0_ms) / 1000)} s` })));
}

// The @word being typed just before the caret, or null.
function token(input) {
  const end = input.selectionStart, m = input.value.slice(0, end).match(/(?:^|\s)@([^\s@]*)$/);
  return m && { start: end - m[1].length - 1, end, q: m[1].toLowerCase() };
}

async function onInput(e) {
  const input = e.target, generation = ++pickerGeneration, t = token(input), rows = await sources;
  if (generation !== pickerGeneration || !el || document.activeElement !== input) return;
  const now = token(input);
  if (t?.start !== now?.start || t?.end !== now?.end || t?.q !== now?.q) return;
  if (!t) return closeAt();
  at = { ...t, rows: rows.filter((r) => `${r.id} ${r.meta}`.toLowerCase().includes(t.q)), i: 0 };
  renderAt();
}

function onKey(e) {
  if (!at) return;
  pickerGeneration++;
  const n = at.rows.length + 1;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    at.i = (at.i + (e.key === 'ArrowDown' ? 1 : n - 1)) % n;
    renderAt();
  } else if (e.key === 'Enter') pick(at.i);
  else if (e.key === 'Escape') closeAt();
  else return;
  // Enter must not submit the form, Escape must not reach the shell's step back
  e.preventDefault();
  e.stopPropagation();
}

function renderAt() {
  const list = el.querySelector('.home-at');
  list.hidden = !at;
  if (!at) return list.replaceChildren();
  const row = (i, html, cls = '', ws = '') =>
    `<li class="home-at-row${cls}${i === at.i ? ' on' : ''}" role="option" aria-selected="${i === at.i}" data-i="${i}"${ws && ` data-ws="${ws}"`}>${html}</li>`;
  list.innerHTML = [
    ...at.rows.map((r, i) => row(i, `${wsIcon(r.ws)}<span class="home-at-id mono">${esc(r.id)}</span><span class="home-at-meta">${esc(r.meta)}</span>`, '', r.ws)),
    row(at.rows.length, PICK_ANY, ' home-at-any'),
  ].join('');
  list.querySelector('.on')?.scrollIntoView({ block: 'nearest' });
}

function closeAt() {
  pickerGeneration++;
  if (!at || !el) return;
  at = null;
  renderAt();
}

// Replaces the typed @word with the picked source's chip; the "agent picks" row clears the chip.
function pick(i) {
  const input = el.querySelector('#home-input'), { start, end, rows } = at;
  input.value = input.value.slice(0, start) + input.value.slice(end);
  input.setSelectionRange(start, start);
  setSource(rows[i] ?? null);
  closeAt();
}

function setSource(r) {
  src = r;
  const tag = el.querySelector('.home-src');
  tag.hidden = !r;
  tag.innerHTML = r ? `${wsIcon(r.ws)}${esc(r.id)}<button type="button" class="home-src-x" title="Let the agent pick the scene">${icon('x')}</button>` : '';
}

// A refusal card under the box: why, and two questions that can be answered instead ([{q, ws}]).
export function showAbstain(final, reason, suggestions) {
  el.querySelector('.home-refusal')?.remove();
  el.querySelector('.home-out').insertAdjacentHTML('beforeend', `
    <div class="home-refusal" data-final="${esc(final)}">
      <p class="hr-title">${icon('ban')}Not answerable from footage</p>
      <p class="hr-body">${esc(reason)}</p>
      <div class="hr-chips">${suggestions.map(({ q, ws }) => `<button type="button" class="preset" data-ws="${ws}">${esc(q)}</button>`).join('')}</div>
    </div>`);
}
