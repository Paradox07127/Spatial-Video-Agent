// Overview map (spec §9.3): offline dark basemap with Toronto 3D Massing extruded from zoom 15, every trip's route (GPS gaps
// dashed), cars placed by the replay clock with a 1 fps dashcam chip (<img>, never <video>), the selected trip's trail, event
// pins, buildings.json extruded from zoom 15 on top. In the result layout (board v2-4a) the same map shows one trip's route,
// the car in its GPS uncertainty ring and the place footprint.
import * as maplibregl from 'maplibre-gl';
import { Protocol } from 'pmtiles';
import { count, esc } from '../kernel/format.js';
import { posAt, rings, tripT } from './geo.js';

// style.json, basemap.pmtiles and buildings.pmtiles, built by pipeline/map/; nothing is requested from another host.
const MAP = 'api/assets/_map';
// Used when the map assets are absent.
const BLANK = { version: 8, sources: {}, layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#0E1218' } }] };
const TRAIL_MS = 30000;
// Route line widths (px); the selected trip's are doubled and the other trips drop to FADED opacity.
const ROUTE_W = { 'fl-route': 2.5, 'fl-route-gap': 1.5 };
const FADED = 0.35;
// Result layout: the trip's route at 3 px in its colour, the other routes at 2 px in this grey.
const OTHER = 'rgba(154,165,180,0.35)';
// Metres per pixel at zoom 0 on the equator (MapLibre's world is 512 · 2^zoom px).
const M_PER_PX0 = 40075016.686 / 512;
const ARROW = '<svg class="fl-arrow" viewBox="-12 -12 24 24" aria-hidden="true"><path d="M0,-9 L7,8 L0,4 L-7,8Z"/></svg>';
const empty = { type: 'FeatureCollection', features: [] };
const line = (coordinates, properties = {}) => ({ type: 'Feature', properties, geometry: { type: 'LineString', coordinates } });

maplibregl.addProtocol('pmtiles', new Protocol().tile);

async function baseStyle(api) {
  try {
    const style = await api.get(`${MAP}/style.json`);
    style.sources.openmaptiles.url = `pmtiles://${api.url(`${MAP}/basemap.pmtiles`)}`;
    style.sources.massing = { type: 'vector', url: `pmtiles://${api.url(`${MAP}/buildings.pmtiles`)}` };
    return style;
  } catch {
    return BLANK;
  }
}

// Route pieces split where the GPS state switches between a fix and last_known, so gaps can be drawn dashed.
function routePieces(trip) {
  const out = [];
  let cur = null;
  for (const p of trip.pts) {
    const gap = p.state === 'last_known';
    if (!cur || cur.properties.gap !== gap) {
      if (cur) cur.geometry.coordinates.push([p.lon, p.lat]);
      out.push((cur = line([], { gap, trip: trip.id, color: trip.color })));
    }
    cur.geometry.coordinates.push([p.lon, p.lat]);
  }
  return out.filter((f) => f.geometry.coordinates.length > 1);
}

// hasVideo(trip, s): the second s of the trip has dashcam video (timeline lanes), else the chip is a grey block.
const hasVideo = (trip, s) => (trip.timeline?.lanes[trip.cam.id] ?? []).some(([a, b, st]) => st !== 'no_video' && a < (s + 1) * 1000 && b > s * 1000);

// Moves the pitched camera so the projected box of lngLats fills the map inside PAD (px); fitBounds sizes the box as if the
// map were flat, which leaves pitched trips small.
const PAD = { top: 56, right: 200, bottom: 64, left: 72 };
function fitPitched(map, el, lngLats) {
  const W = el.clientWidth - PAD.left - PAD.right, H = el.clientHeight - PAD.top - PAD.bottom;
  for (let i = 0; i < 4; i++) {
    const px = lngLats.map((ll) => map.project(ll)), xs = px.map((p) => p.x), ys = px.map((p) => p.y);
    const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const c = map.project(map.getCenter());
    const center = map.unproject([c.x + (x0 + x1) / 2 - (PAD.left + W / 2), c.y + (y0 + y1) / 2 - (PAD.top + H / 2)]);
    map.jumpTo({ center, zoom: map.getZoom() + Math.log2(Math.min(W / Math.max(x1 - x0, 1), H / Math.max(y1 - y0, 1))) });
  }
}

export async function createOverview(el, { trips, api, onCar, onEvent, onBuilding }) {
  const gps = trips.flatMap((t) => t.pts.map((p) => [p.lon, p.lat]));
  const map = new maplibregl.Map({
    container: el,
    style: await baseStyle(api),
    bounds: [Math.min(...gps.map((c) => c[0])), Math.min(...gps.map((c) => c[1])), Math.max(...gps.map((c) => c[0])), Math.max(...gps.map((c) => c[1]))],
    fitBoundsOptions: { padding: 120 },
    pitch: 50,
    bearing: -16,
    attributionControl: { compact: true },
  });
  await map.once('load');
  fitPitched(map, el, gps);

  // Above every road and area layer (a line drawn after an extrusion paints over it), below the place labels.
  const layers = map.getStyle().layers, beforeLabels = layers[layers.findLastIndex((l) => l.type !== 'symbol') + 1]?.id;
  if (map.getSource('massing')) {
    map.addLayer({
      id: 'fl-massing', type: 'fill-extrusion', source: 'massing', 'source-layer': 'buildings', minzoom: 15,
      paint: { 'fill-extrusion-color': '#1E242E', 'fill-extrusion-height': ['get', 'h'] },
    }, beforeLabels);
  }
  const bld = trips.flatMap((t) => Object.entries(t.buildings).map(([bid, b]) => ({ type: 'Feature', properties: { bid, height_m: b.height_m ?? 10 }, geometry: b.geom })));
  map.addSource('fl-bld', { type: 'geojson', data: { type: 'FeatureCollection', features: bld }, promoteId: 'bid' });
  map.addLayer({
    id: 'fl-bld', type: 'fill-extrusion', source: 'fl-bld', minzoom: 15,
    paint: { 'fill-extrusion-color': ['case', ['boolean', ['feature-state', 'focus'], false], '#009E73', '#4A5462'], 'fill-extrusion-height': ['get', 'height_m'], 'fill-extrusion-opacity': 0.9 },
  });
  map.addSource('fl-route', { type: 'geojson', data: { type: 'FeatureCollection', features: trips.flatMap(routePieces) } });
  map.addLayer({ id: 'fl-route', type: 'line', source: 'fl-route', filter: ['!', ['get', 'gap']], layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': ['get', 'color'], 'line-width': ROUTE_W['fl-route'] } });
  map.addLayer({ id: 'fl-route-gap', type: 'line', source: 'fl-route', filter: ['get', 'gap'], paint: { 'line-color': ['get', 'color'], 'line-width': ROUTE_W['fl-route-gap'], 'line-dasharray': [2, 2] } });
  map.addSource('fl-place', { type: 'geojson', data: empty });
  map.addLayer({ id: 'fl-place', type: 'line', source: 'fl-place', layout: { 'line-cap': 'round' }, paint: { 'line-color': '#009E73', 'line-width': 1.5, 'line-dasharray': [0.1, 2] } });
  map.addSource('fl-trail', { type: 'geojson', data: empty, lineMetrics: true });
  map.addLayer({
    id: 'fl-trail', type: 'line', source: 'fl-trail', layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-width': 4, 'line-gradient': ['interpolate', ['linear'], ['line-progress'], 0, 'rgba(237,240,244,0)', 1, 'rgba(237,240,244,1)'] },
  });
  map.on('click', 'fl-bld', (e) => onBuilding(e.features[0].id));
  map.on('mouseenter', 'fl-bld', () => (map.getCanvas().style.cursor = 'pointer'));
  map.on('mouseleave', 'fl-bld', () => (map.getCanvas().style.cursor = ''));

  const cars = trips.map((trip) => {
    const div = document.createElement('div');
    div.className = 'fl-car';
    div.dataset.trip = trip.id;
    div.style.setProperty('--trip', trip.color);
    div.innerHTML = `${ARROW}<span class="fl-tag"><span class="mono">${esc(trip.id)}</span><span class="fl-age mono"></span></span>${trip.cam ? '<span class="fl-chip"><img alt=""></span>' : ''}`;
    div.addEventListener('click', () => onCar(trip.id));
    return { trip, div, s: null, marker: new maplibregl.Marker({ element: div }).setLngLat(trip.line[0]).addTo(map) };
  });
  // Event pins are clustered by the fl-ev source; the DOM markers of whatever it renders (pins or counted clusters) are
  // swapped in on every render.
  const events = new Map(trips.flatMap((trip) => trip.events.map((e) => {
    const p = posAt(trip.pts, e.t0_ms);
    return [`${trip.id}:${e.id}`, { trip, e, lngLat: [p.lon, p.lat], marker: null }];
  })));
  map.addSource('fl-ev', {
    type: 'geojson', cluster: true, clusterRadius: 40, clusterProperties: { sev: ['max', ['get', 'severity']] },
    data: { type: 'FeatureCollection', features: [...events].map(([key, v]) => ({ type: 'Feature', properties: { key, severity: v.e.severity }, geometry: { type: 'Point', coordinates: v.lngLat } })) },
  });
  // Invisible; it only makes the source's tiles load so querySourceFeatures sees them.
  map.addLayer({ id: 'fl-ev', type: 'circle', source: 'fl-ev', paint: { 'circle-radius': 0, 'circle-opacity': 0 } });
  const pin = (v) => {
    const div = document.createElement('div');
    div.className = `fl-pin sev-${v.e.severity}`;
    div.title = `${v.trip.id} ${v.e.id} ${v.e.type} · severity ${v.e.severity}`;
    div.innerHTML = '<i></i>';
    div.addEventListener('click', () => onEvent(v.trip, v.e));
    return new maplibregl.Marker({ element: div, anchor: 'bottom' }).setLngLat(v.lngLat);
  };
  const cluster = (f) => {
    const { cluster_id: id, point_count: n, sev } = f.properties, center = f.geometry.coordinates;
    const div = document.createElement('div');
    div.className = `fl-cluster sev-${sev} mono`;
    div.title = `${count(n, 'event')} · click to zoom in`;
    div.textContent = n;
    div.addEventListener('click', async () => map.easeTo({ center, zoom: await map.getSource('fl-ev').getClusterExpansionZoom(id) }));
    return new maplibregl.Marker({ element: div }).setLngLat(center);
  };
  let clusters = new Map(), shown = new Set();
  map.on('render', () => {
    if (!map.isSourceLoaded('fl-ev')) return;
    const next = new Set(), nextClusters = new Map();
    for (const f of map.querySourceFeatures('fl-ev')) {
      if (f.properties.cluster) {
        const id = f.properties.cluster_id;
        if (!nextClusters.has(id)) nextClusters.set(id, clusters.get(id) ?? cluster(f).addTo(map));
      } else {
        const v = events.get(f.properties.key);
        v.marker ??= pin(v);
        if (!next.has(v) && !shown.has(v)) v.marker.addTo(map);
        next.add(v);
      }
    }
    for (const [id, m] of clusters) if (!nextClusters.has(id)) m.remove();
    for (const v of shown) if (!next.has(v)) v.marker.remove();
    clusters = nextClusters;
    shown = next;
  });
  const hitDiv = document.createElement('div');
  hitDiv.className = 'fl-hit';
  const hit = new maplibregl.Marker({ element: hitDiv });
  const gpsDiv = document.createElement('div');
  gpsDiv.className = 'fl-gps';
  gpsDiv.innerHTML = '<i></i>';
  const ring = new maplibregl.Marker({ element: gpsDiv });
  let selected = null, wall = 0, gpsAt = null;
  // The ring's radius is sigma_m in pixels at the car's latitude and the current zoom.
  const sizeRing = () => gpsAt && gpsDiv.style.setProperty('--r', `${(gpsAt[1] * 2 ** map.getZoom()) / (M_PER_PX0 * Math.cos((gpsAt[0][1] * Math.PI) / 180))}px`);
  map.on('zoom', sizeRing);

  const ov = {
    map,
    update(w) {
      wall = w;
      for (const c of cars) {
        const t = tripT(c.trip.replay, w);
        const p = t == null ? null : posAt(c.trip.pts, t);
        c.div.hidden = !p;
        if (c.trip.id === selected) {
          const tail = p ? c.trip.pts.filter((q) => q.t_ms > t - TRAIL_MS && q.t_ms <= t).map((q) => [q.lon, q.lat]).concat([[p.lon, p.lat]]) : [];
          map.getSource('fl-trail').setData(tail.length > 1 ? line(tail) : empty);
        }
        if (!p) continue;
        c.marker.setLngLat([p.lon, p.lat]);
        c.div.firstChild.style.transform = `rotate(${p.heading - map.getBearing()}deg)`;
        c.div.classList.toggle('stale', p.state === 'last_known');
        c.div.querySelector('.fl-age').textContent = p.state === 'last_known' ? `last fix ${(p.age_ms / 1000).toFixed(1)} s ago` : '';
        const s = Math.floor(t / 1000);
        if (c.trip.cam && s !== c.s) {
          c.s = s;
          const chip = c.div.querySelector('.fl-chip'), ok = hasVideo(c.trip, s);
          chip.classList.toggle('empty', !ok);
          chip.title = ok ? `${c.trip.cam.id} · ${s} s` : 'No video in this second';
          if (ok) chip.firstChild.src = api.url(`api/assets/${c.trip.id}/${c.trip.cam.thumbs}/${String(s).padStart(5, '0')}.jpg`);
        }
      }
    },
    select(id) {
      selected = id;
      for (const c of cars) c.div.classList.toggle('sel', c.trip.id === id);
      const isSel = ['==', ['get', 'trip'], id];
      for (const [layer, w] of Object.entries(ROUTE_W)) {
        map.setPaintProperty(layer, 'line-color', ['get', 'color']);
        map.setPaintProperty(layer, 'line-width', ['case', isSel, 2 * w, w]);
        map.setPaintProperty(layer, 'line-opacity', ['case', isSel, 1, FADED]);
        map.setLayoutProperty(layer, 'line-sort-key', ['case', isSel, 1, 0]);
      }
      map.getSource('fl-trail').setData(empty);
      ov.update(wall);
    },
    focusBuilding(bid, on) {
      map.setFeatureState({ source: 'fl-bld', id: bid }, { focus: on });
    },
    // Result layout for trip id (null: back to the overview styling of the selected trip).
    result(id) {
      if (!id) return ov.select(selected);
      const isSel = ['==', ['get', 'trip'], id];
      for (const layer of Object.keys(ROUTE_W)) {
        map.setPaintProperty(layer, 'line-color', ['case', isSel, ['get', 'color'], OTHER]);
        map.setPaintProperty(layer, 'line-width', ['case', isSel, 3, 2]);
        map.setPaintProperty(layer, 'line-opacity', 1);
      }
    },
    // The car dot in its GPS uncertainty ring; lngLat null removes it.
    gps(lngLat, sigma_m) {
      gpsAt = lngLat && [lngLat, sigma_m];
      if (!lngLat) return ring.remove();
      ring.setLngLat(lngLat).addTo(map);
      sizeRing();
    },
    // GeoJSON footprint of the place under the car, or nothing.
    place(geom) {
      map.getSource('fl-place').setData(geom ? { type: 'Feature', properties: {}, geometry: geom } : empty);
    },
    // lngLat null removes the agent's hit marker.
    hit(lngLat) {
      lngLat ? hit.setLngLat(lngLat).addTo(map) : hit.remove();
    },
    fly(center, ms) {
      map.flyTo({ center, zoom: 18, pitch: 60, duration: ms, essential: true });
    },
    // Top-down on center at once, for the result layout's map cell (resized to it first).
    frame(center) {
      map.resize();
      map.jumpTo({ center, zoom: 17, pitch: 0, bearing: 0 });
    },
    camera: () => ({ center: map.getCenter().toArray(), zoom: map.getZoom(), bearing: map.getBearing(), pitch: map.getPitch() }),
    restore(cam) {
      map.stop();
      map.resize();
      map.jumpTo(cam);
    },
    dispose() {
      map.remove();
    },
  };
  map.on('rotate', () => ov.update(wall));
  return ov;
}
