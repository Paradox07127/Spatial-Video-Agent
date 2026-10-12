// Home page (#/, layout H-A): question console on top, scene gallery and presets/recent runs below.
import { icon, level, verdict } from '../kernel/badges.js';
import { count, esc } from '../kernel/format.js';

const PICK_ANY = 'Leave empty and the agent picks the scene';

let el = null, ctx = null, sources = null;
let allSourceRows = [], rawWorkspaces = [];
let activeFilter = 'all'; // all | warehouse | fleet | street
let viewMode = 'scenes'; // for 851-1200px: scenes | examples | recent
let activeTab = 'examples'; // examples | recent

// at: the open @ list {start, end, rows, i}; i === rows.length is the "agent picks" row. src: the picked row or null.
let at = null, src = null, pickerGeneration = 0;

const wsIcon = (ws) => icon({ fleet: 'truck', street: 'camera' }[ws] ?? 'warehouse');
const chip = (q, ws) => `<button type="button" class="preset" data-ws="${ws}">${wsIcon(ws)}${esc(q)}</button>`;
const shortKey = (key) => key.replace(/^fl-pie-(set\d+)_video_(\d+)$/, '$1·$2');

function formatTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return String(iso);
  }
}

// opts: {ws, hash} of a picked source, or {ws, preset: true} for a chip.
function ask(q, opts = {}) {
  el.querySelector('#home-input').value = q;
  el.querySelector('.home-refusal')?.remove();
  if (q.length > 500) return showError('Keep the question to 500 characters or fewer.');
  const owner = el;
  Promise.resolve(ctx.ask(q, opts)).catch(() => {
    if (el === owner) showError('Could not start the investigation. Please retry.');
  });
}

function showError(message) {
  if (!el) return;
  el.querySelector('.home-error')?.remove();
  el.querySelector('.home-out').insertAdjacentHTML('beforeend', `<p class="home-error muted" role="alert">${esc(message)}</p>`);
}

export function mount(root, c) {
  ctx = c;
  at = src = null;
  activeFilter = 'all';
  viewMode = 'scenes';
  activeTab = 'examples';

  const css = Object.assign(document.createElement('link'), {
    rel: 'stylesheet',
    href: new URL('./home.css', import.meta.url).href,
  });
  document.head.append(css);

  const { warehouse = [], fleet = [], street = [] } = ctx.presets ?? {};

  root.innerHTML = `
    <div class="home" data-view-mode="scenes">
      <header class="home-head">
        <div class="home-brand-wrap">
          <div class="home-brand"><span class="home-mark mono">SVA</span>Spatial Video Agent</div>
          <span class="home-summary mono muted" id="home-summary">Loading workspaces…</span>
        </div>
        <div class="home-filters seg" role="group" aria-label="Source filter">
          <button type="button" class="home-filter on" data-filter="all">All</button>
          <button type="button" class="home-filter" data-filter="warehouse">Warehouse</button>
          <button type="button" class="home-filter" data-filter="fleet">Fleet</button>
          <button type="button" class="home-filter" data-filter="street">Street</button>
        </div>
      </header>

      <section class="home-console">
        <h1 class="home-title">What happened, where, and how far apart?</h1>
        <form id="home-form" class="home-ask" autocomplete="off">
          ${icon('search')}<span class="home-src mono" hidden></span><input id="home-input" maxlength="500" role="combobox" aria-autocomplete="list" aria-haspopup="listbox" aria-controls="home-source-list" aria-expanded="false" placeholder="Ask about any moment in the footage">
          <button class="home-go" title="Ask">${icon('corner-down-left')}</button>
          <ul id="home-source-list" class="home-at" role="listbox" hidden></ul>
        </form>
        <div class="home-out"></div>
        <div class="home-chips">
          ${[...warehouse.slice(0, 2).map((q) => chip(q, 'warehouse')), chip(fleet[0], 'fleet')].filter(Boolean).join('')}
        </div>
        <p class="home-note">Numbers come from measurement. Questions about identity, intent or blame are declined.</p>
      </section>

      <main class="home-content">
        <div class="home-view-nav seg" role="tablist" aria-label="Views">
          <button type="button" class="home-view-btn on" data-view="scenes">Scenes</button>
          <button type="button" class="home-view-btn" data-view="examples">Examples</button>
          <button type="button" class="home-view-btn" data-view="recent">Recent</button>
        </div>

        <section class="home-gallery-cell" aria-label="Scene gallery">
          <div class="home-gallery-head">
            <h2 class="p-label">Scene gallery</h2>
          </div>
          <div class="home-gallery" role="feed"></div>
        </section>

        <section class="home-side-cell" aria-label="Presets and investigations">
          <div class="home-side-nav seg" role="tablist">
            <button type="button" class="home-tab on" data-tab="examples" role="tab" aria-selected="true">Example questions</button>
            <button type="button" class="home-tab" data-tab="recent" role="tab" aria-selected="false">Recent investigations</button>
          </div>
          <div class="home-side-content">
            <div class="home-pane home-pane-examples">
              <div class="home-example-group">
                <h3 class="home-group-title">${wsIcon('warehouse')} Warehouse (L3 & L2)</h3>
                <div class="home-group-chips">
                  ${warehouse.map((q) => chip(q, 'warehouse')).join('')}
                </div>
              </div>
              <div class="home-example-group">
                <h3 class="home-group-title">${wsIcon('fleet')} Fleet Dashcam (L2)</h3>
                <div class="home-group-chips">
                  ${fleet.map((q) => chip(q, 'fleet')).join('')}
                </div>
              </div>
              <div class="home-example-group">
                <h3 class="home-group-title">${wsIcon('street')} Street Camera (L1)</h3>
                <div class="home-group-chips">
                  ${street.map((q) => chip(q, 'street')).join('')}
                </div>
              </div>
            </div>
            <div class="home-pane home-pane-recent" hidden>
              <div class="home-recent-list" role="list">
                <p class="muted home-empty">Loading investigations…</p>
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>`;

  el = root.firstElementChild;
  const owner = el;

  sources = ctx.workspaces().then(
    async (wsList) => {
      if (el !== owner) return [];
      rawWorkspaces = wsList;
      allSourceRows = sourceRows(wsList);
      updateCorpusSummary(wsList);
      renderGallery();
      return allSourceRows;
    },
    () => {
      if (el === owner) showError('Sources could not load. Please reload and try again.');
      return [];
    }
  );

  const input = el.querySelector('#home-input');
  el.querySelector('#home-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (q) ask(q, src ? { ws: src.ws, hash: src.hash } : {});
  });

  input.addEventListener('input', onInput);
  input.addEventListener('keydown', onKey);
  input.addEventListener('blur', closeAt);

  // pointerdown keeps the focus in the input, so its blur does not close the list first
  el.querySelector('.home-at').addEventListener('pointerdown', (e) => {
    const row = e.target.closest('.home-at-row');
    e.preventDefault();
    if (row && row.dataset.i != null) pick(+row.dataset.i);
  });

  el.addEventListener('click', (e) => {
    if (e.target.closest('.home-src-x')) return setSource(null);

    // Source filter buttons
    const filterBtn = e.target.closest('.home-filter');
    if (filterBtn) {
      setFilter(filterBtn.dataset.filter);
      return;
    }

    // Narrow layout switcher buttons [Scenes | Examples | Recent]
    const viewBtn = e.target.closest('.home-view-btn');
    if (viewBtn) {
      setViewMode(viewBtn.dataset.view);
      return;
    }

    // Side tabs [Example questions | Recent investigations]
    const sideTab = e.target.closest('.home-tab');
    if (sideTab) {
      setSideTab(sideTab.dataset.tab);
      return;
    }

    // Preset chips
    const b = e.target.closest('.preset');
    if (b) {
      ask(b.textContent.trim(), { ws: b.dataset.ws, preset: true });
      return;
    }

    // Card click or card @ button
    const card = e.target.closest('.home-card');
    if (card) {
      const atBtn = e.target.closest('.home-card-at');
      const key = card.dataset.key;
      const row = allSourceRows.find((r) => r.scene_key === key);
      if (atBtn || e.altKey || e.shiftKey) {
        e.preventDefault();
        e.stopPropagation();
        if (row) setSource(row);
        input.focus();
        return;
      }
      if (card.dataset.hash) {
        location.hash = card.dataset.hash;
      }
      return;
    }

    // Recent run row click
    const runRow = e.target.closest('.home-run-row');
    if (runRow) {
      const href = runRow.getAttribute('href');
      if (href && href.startsWith('#/')) {
        location.hash = href;
      }
    }
  });

  return () => {
    closeAt();
    css.remove();
    el = ctx = sources = at = src = null;
    rawWorkspaces = [];
    allSourceRows = [];
  };
}

function updateCorpusSummary(wsList) {
  const sumEl = el?.querySelector('#home-summary');
  if (!sumEl) return;
  const numWs = wsList.length;
  const totalScenes = wsList.reduce((acc, w) => acc + (w.scenes?.length ?? 0), 0);
  sumEl.textContent = `${numWs} workspaces · ${totalScenes} scenes · L1–L3`;
}

function setFilter(filter) {
  activeFilter = filter;
  for (const btn of el.querySelectorAll('.home-filter')) {
    btn.classList.toggle('on', btn.dataset.filter === filter);
  }
  renderGallery();
  if (at) renderAt();
}

function setViewMode(view) {
  viewMode = view;
  el.setAttribute('data-view-mode', view);
  for (const btn of el.querySelectorAll('.home-view-btn')) {
    btn.classList.toggle('on', btn.dataset.view === view);
  }
  if (view === 'recent') {
    setSideTab('recent');
  } else if (view === 'examples') {
    setSideTab('examples');
  }
}

function setSideTab(tab) {
  activeTab = tab;
  for (const btn of el.querySelectorAll('.home-tab')) {
    const isThis = btn.dataset.tab === tab;
    btn.classList.toggle('on', isThis);
    btn.setAttribute('aria-selected', String(isThis));
  }
  const examplesPane = el.querySelector('.home-pane-examples');
  const recentPane = el.querySelector('.home-pane-recent');
  if (examplesPane) examplesPane.hidden = tab !== 'examples';
  if (recentPane) {
    recentPane.hidden = tab !== 'recent';
    if (tab === 'recent') loadRecentInvestigations();
  }
}

async function loadRecentInvestigations() {
  const listEl = el.querySelector('.home-recent-list');
  if (!listEl) return;
  const owner = el;
  try {
    const res = await ctx.api.get('api/agent/sessions');
    if (el !== owner) return;
    const sessions = res?.sessions ?? [];
    if (!sessions.length) {
      listEl.innerHTML = '<p class="muted home-empty">No recent investigations</p>';
      return;
    }
    const details = await Promise.all(
      sessions.slice(0, 8).map(async (s) => {
        if (s.turns) return s;
        return ctx.api.get(`api/agent/sessions/${encodeURIComponent(s.session_id)}`).catch(() => null);
      })
    );
    if (el !== owner) return;
    const turns = details.filter(Boolean).flatMap((d) =>
      (d.turns || []).map((t) => ({ ...t, session_id: d.session_id }))
    );
    if (!turns.length) {
      listEl.innerHTML = '<p class="muted home-empty">No recent investigations</p>';
      return;
    }
    listEl.innerHTML = turns
      .slice(0, 15)
      .map((t) => {
        const v = t.final || t.status || 'UNRESOLVED';
        const timeStr = t.created ? formatTime(t.created) : '';
        const runId = t.run_id || t.turn_id;
        return `
          <a href="#/run/${esc(runId)}" class="home-run-row" data-run="${esc(runId)}">
            <div class="home-run-main">
              <span class="home-run-q">${esc(t.q)}</span>
              <div class="home-run-meta">
                ${verdict(v)}
                ${timeStr ? `<span class="home-run-time mono muted">${esc(timeStr)}</span>` : ''}
              </div>
            </div>
          </a>
        `;
      })
      .join('');
  } catch {
    if (el === owner) listEl.innerHTML = '<p class="muted home-empty">No recent investigations</p>';
  }
}

function renderGallery() {
  const container = el?.querySelector('.home-gallery');
  if (!container || !rawWorkspaces) return;

  const filteredWorkspaces = rawWorkspaces.filter(
    (w) => activeFilter === 'all' || w.id === activeFilter
  );

  const wsTitles = { warehouse: 'Warehouse', fleet: 'Fleet', street: 'Street' };

  container.innerHTML = filteredWorkspaces
    .map((w) => {
      const cardsHtml = w.scenes
        .map((s) => {
          const thumbUrl = s.thumb ? ctx.api.url(s.thumb) : null;
          const hasDur = Number.isFinite(s.t0_ms) && Number.isFinite(s.t1_ms);
          const dur = hasDur ? `${Math.round((s.t1_ms - s.t0_ms) / 1000)} s` : '';
          const lvl = s.evidence_level || (w.id === 'street' ? 'L1' : (w.id === 'warehouse' && s.metric ? 'L3' : 'L2'));
          const shortId = w.id === 'fleet' ? shortKey(s.scene_key) : s.scene_key;
          const hash = w.id === 'fleet' ? `#/fleet/${s.scene_key}@${s.t0_ms ?? 0}` : `#/${w.id}/${s.scene_key}`;

          return `
            <div class="home-card" data-key="${esc(s.scene_key)}" data-ws="${esc(w.id)}" data-hash="${esc(hash)}" role="button" tabindex="0">
              <div class="home-card-media">
                ${thumbUrl ? `<img class="home-card-thumb" src="${esc(thumbUrl)}" alt="" loading="lazy" onerror="this.style.display='none'; this.nextElementSibling.style.display='grid';">` : ''}
                <div class="home-card-placeholder" style="${thumbUrl ? 'display:none;' : ''}">${wsIcon(w.id)}</div>
                <button type="button" class="home-card-at" title="Set as @ source" aria-label="Set ${esc(s.scene_key)} as source">@</button>
              </div>
              <div class="home-card-info">
                <div class="home-card-row">
                  <span class="home-card-ws">${wsIcon(w.id)}</span>
                  <span class="home-card-id mono">${esc(shortId)}</span>
                  ${level(lvl)}
                </div>
                ${s.title && s.title !== s.scene_key ? `<span class="home-card-title muted" title="${esc(s.title)}">${esc(s.title)}</span>` : ''}
                <div class="home-card-meta">
                  ${dur ? `<span class="home-card-dur mono muted">${esc(dur)}</span>` : ''}
                  ${s.n_cams ? `<span class="home-card-cams muted">${esc(count(s.n_cams, 'cam'))}</span>` : ''}
                </div>
              </div>
            </div>
          `;
        })
        .join('');

      return `
        <div class="home-gallery-group" data-ws="${esc(w.id)}">
          <h3 class="home-group-title">${wsIcon(w.id)}<span>${wsTitles[w.id] ?? w.title}</span></h3>
          <div class="home-card-grid">${cardsHtml}</div>
        </div>
      `;
    })
    .join('');
}

// Scenes and trips of api/workspaces as one list of @ rows.
function sourceRows(workspaces) {
  return workspaces.flatMap((w) =>
    w.scenes.map((s) => {
      const hasDur = Number.isFinite(s.t0_ms) && Number.isFinite(s.t1_ms);
      const dur = hasDur ? `${Math.round((s.t1_ms - s.t0_ms) / 1000)} s` : '';
      const lvl = s.evidence_level || (w.id === 'street' ? 'L1' : (w.id === 'warehouse' && s.metric ? 'L3' : 'L2'));
      if (w.id === 'fleet') {
        return {
          ws: 'fleet',
          id: shortKey(s.scene_key),
          hash: `#/fleet/${s.scene_key}@${s.t0_ms ?? 0}`,
          meta: dur ? `dashcam · ${s.title} · ${dur}` : `dashcam · ${s.title}`,
          level: lvl,
          scene_key: s.scene_key,
          title: s.title,
        };
      }
      if (w.id === 'street') {
        return {
          ws: 'street',
          id: s.scene_key,
          hash: `#/street/${s.scene_key}`,
          meta: dur ? `2D video · ${s.title} · ${dur}` : `2D video · ${s.title}`,
          level: lvl,
          scene_key: s.scene_key,
          title: s.title,
        };
      }
      return {
        ws: 'warehouse',
        id: s.scene_key,
        hash: `#/warehouse/${s.scene_key}`,
        meta: dur,
        level: lvl,
        scene_key: s.scene_key,
        title: s.title,
      };
    })
  );
}

// The @word being typed just before the caret, or null.
function token(input) {
  const end = input.selectionStart,
    m = input.value.slice(0, end).match(/(?:^|\s)@([^\s@]*)$/);
  return m && { start: end - m[1].length - 1, end, q: m[1].toLowerCase() };
}

async function onInput(e) {
  const input = e.target,
    generation = ++pickerGeneration,
    t = token(input),
    rows = await sources;
  if (generation !== pickerGeneration || !el || document.activeElement !== input) return;
  const now = token(input);
  if (t?.start !== now?.start || t?.end !== now?.end || t?.q !== now?.q) return;
  if (!t) return closeAt();

  const filtered = rows.filter(
    (r) =>
      (activeFilter === 'all' || r.ws === activeFilter) &&
      `${r.id} ${r.meta}`.toLowerCase().includes(t.q)
  );

  at = { ...t, rows: filtered, i: 0 };
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
  const input = el.querySelector('#home-input');
  input.setAttribute('aria-expanded', String(!!at));
  if (!at) {
    input.removeAttribute('aria-activedescendant');
    return list.replaceChildren();
  }
  input.setAttribute('aria-activedescendant', `home-source-${at.i}`);

  const row = (i, html, cls = '', ws = '') =>
    `<li id="home-source-${i}" class="home-at-row${cls}${i === at.i ? ' on' : ''}" role="option" aria-selected="${i === at.i}" data-i="${i}"${ws ? ` data-ws="${ws}"` : ''}>${html}</li>`;

  // Group at.rows by workspace
  const groups = new Map();
  at.rows.forEach((r, i) => {
    if (!groups.has(r.ws)) groups.set(r.ws, []);
    groups.get(r.ws).push({ r, i });
  });

  const wsTitles = { warehouse: 'Warehouse', fleet: 'Fleet', street: 'Street' };
  const items = [];
  for (const [ws, rowsInWs] of groups) {
    items.push(`<li class="home-at-header" role="presentation">${wsIcon(ws)}<span>${wsTitles[ws] ?? ws}</span></li>`);
    for (const { r, i } of rowsInWs) {
      items.push(
        row(
          i,
          `${wsIcon(r.ws)}<span class="home-at-id mono">${esc(r.id)}</span>${level(r.level)}<span class="home-at-meta">${esc(r.meta)}</span>`,
          '',
          r.ws
        )
      );
    }
  }

  // Pick any option at the end
  items.push(row(at.rows.length, PICK_ANY, ' home-at-any'));
  list.innerHTML = items.join('');
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
  const input = el.querySelector('#home-input'),
    { start, end, rows } = at;
  input.value = input.value.slice(0, start) + input.value.slice(end);
  input.setSelectionRange(start, start);
  setSource(rows[i] ?? null);
  closeAt();
}

function setSource(r) {
  src = r;
  const tag = el.querySelector('.home-src');
  tag.hidden = !r;
  tag.innerHTML = r
    ? `${wsIcon(r.ws)}${esc(r.id)}<button type="button" class="home-src-x" title="Let the agent pick the scene">${icon('x')}</button>`
    : '';
}

// A refusal card under the box: why, and two questions that can be answered instead ([{q, ws}]).
export function showAbstain(final, reason, suggestions) {
  el.querySelector('.home-refusal')?.remove();
  el.querySelector('.home-out').insertAdjacentHTML(
    'beforeend',
    `
    <div class="home-refusal" data-final="${esc(final)}">
      <p class="hr-title">${icon('ban')}Not answerable from footage</p>
      <p class="hr-body">${esc(reason)}</p>
      <div class="hr-chips">${suggestions.map(({ q, ws }) => `<button type="button" class="preset" data-ws="${ws}">${esc(q)}</button>`).join('')}</div>
    </div>`
  );
}
