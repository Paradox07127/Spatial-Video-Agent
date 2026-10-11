// Building card (spec §9.3): picked in the map, the 3D view or a video column. Name, address, height, POIs from the map
// snapshot, and the C5 sign reading checked against the map in three states; an unchecked reading is labelled as such.
import { icon, source } from '../kernel/badges.js';
import { count, esc } from '../kernel/format.js';

const SIGN = { verified: 'Sign matches the map', conflict: 'Sign does not match the map', unverified: 'Model output, unchecked' };

// tripOf(bid) -> the trip whose buildings.json has bid; onFocus(bid | null) mirrors the focus into the map and 3D view.
export function createBuildings(el, { api, tripOf, onFocus }) {
  const c5 = new Map();
  let token = 0, current = null;

  function sign(r) {
    if (!r) return '<div class="fl-sign muted">No sign reading for this building (C5).</div>';
    const s = r.verification.status;
    return `<div class="fl-sign">
        <div class="fl-sign-head">${source('model')}<span>Sign check</span><span class="mp-check ck-${esc(s)}" title="${esc(r.verification.method)}">${SIGN[s]}</span></div>
        <p>${r.payload.sign_text ? `"<span class="mono">${esc(r.payload.sign_text)}</span>" ` : ''}${esc(r.payload.text)}</p>
      </div>`;
  }

  const card = {
    get bid() { return current; },
    async focus(bid) {
      const my = ++token;
      if (current) onFocus(current, false);
      onFocus((current = bid), true);
      el.hidden = false;
      el.dataset.bid = bid;
      el.innerHTML = '<p class="muted" role="status">Loading building…</p>';
      try {
        const trip = await Promise.resolve(tripOf(bid));
        let modelPromise = Promise.resolve([]);
        if (trip) {
          if (!c5.has(trip.id)) {
            const q = `scene_key=${trip.id}&t_ms=${trip.mf.t0_ms}&window_ms=${trip.mf.t1_ms - trip.mf.t0_ms}`;
            const request = api.get(`api/model-results?${q}`).then((rs) => rs.filter((r) => r.task === 'C5')).catch((error) => {
              if (c5.get(trip.id) === request) c5.delete(trip.id);
              throw error;
            });
            c5.set(trip.id, request);
          }
          modelPromise = c5.get(trip.id);
        }
        const [building, model] = await Promise.allSettled([api.get(`api/fleet/building/${bid}`), modelPromise]);
        if (my !== token) return;
        if (building.status !== 'fulfilled') throw building.reason;
        const b = building.value;
        el.innerHTML = `
          <header class="fl-bhead"><b>${esc(b.name ?? bid)}</b>${source('map')}<button class="icon-btn fl-close" title="Close">${icon('x')}</button></header>
          <dl class="fl-bdl">
            <dt>Address</dt><dd>${b.addr ? esc(b.addr) : '<span class="muted">No address on the map</span>'}</dd>
            <dt>Height</dt><dd class="mono">${b.height_m != null ? `${esc(b.height_m)} m` : '—'}${b.floors ? ` · ${esc(count(b.floors, 'floor'))}` : ''}</dd>
            <dt>POI</dt><dd>${b.pois.map((p) => `${esc(p.name)} <span class="muted">${esc(p.kind)}</span>`).join(', ') || '<span class="muted">none</span>'}</dd>
            <dt>Source</dt><dd class="mono muted">${esc(b.source)} ${esc(b.osm_id ?? '')} · snapshot ${esc(b.map_snapshot_date)}</dd>
          </dl>
          ${model.status === 'fulfilled' ? sign(model.value.find((r) => r.payload.region_ref === bid)) : '<div class="fl-sign muted" role="alert">Sign reading could not load. <button class="fl-retry">Retry</button></div>'}`;
      } catch {
        if (my !== token) return;
        el.innerHTML = '<p class="muted" role="alert">Building details could not load. <button class="fl-retry">Retry</button><button class="fl-close">Close</button></p>';
      }
    },
    close() {
      token++;
      if (current) onFocus(current, false);
      current = null;
      el.hidden = true;
    },
  };
  el.addEventListener('click', (e) => {
    if (e.target.closest('.fl-close')) card.close();
    else if (e.target.closest('.fl-retry') && current) card.focus(current);
  });
  return card;
}
