// Fleet geometry (CONTRACT §3): replay mapping per trip, GPS lookup at a trip time, local east/north metres.
const M_PER_DEG = (Math.PI / 180) * 6378137;

// Trip t_ms at wall_ms under one ReplayMapping; null outside its coverage_intervals.
export function tripT(m, wall) {
  const t = m.source_start_ms + (wall - m.replay_start_ms) * m.rate;
  return m.coverage_intervals.some(([a, b]) => t >= a && t < b) ? Math.round(t) : null;
}

export const wallOf = (m, t) => Math.round(m.replay_start_ms + (t - m.source_start_ms) / m.rate);

// GPS state at trip time t from the 2 Hz points (trip.geojson properties). Linear between fixes; inside a gap the
// position stays at the last fix with state 'last_known' and age_ms = time since that fix. null before the first point.
export function posAt(pts, t) {
  const i = pts.findLastIndex((p) => p.t_ms <= t);
  if (i < 0) return null;
  const p = pts[i], q = pts[i + 1];
  if (p.state === 'last_known' || q?.state === 'last_known') return { ...p, state: 'last_known', age_ms: p.gap_ms + t - p.t_ms };
  if (!q) return { ...p, age_ms: 0 };
  const f = (t - p.t_ms) / (q.t_ms - p.t_ms);
  const dh = ((q.heading - p.heading + 540) % 360) - 180;
  return {
    ...p, lon: p.lon + f * (q.lon - p.lon), lat: p.lat + f * (q.lat - p.lat),
    heading: (p.heading + f * dh + 360) % 360, speed: p.speed + f * (q.speed - p.speed), age_ms: 0,
  };
}

// [east, north] metres of [lon, lat] from origin [lon0, lat0]; equirectangular, good to a few km.
export const en = ([lon0, lat0], [lon, lat]) => [(lon - lon0) * M_PER_DEG * Math.cos((lat0 * Math.PI) / 180), (lat - lat0) * M_PER_DEG];

// Polygons of a GeoJSON Polygon / MultiPolygon, each [outer ring, ...holes].
export const polygons = (geom) => (geom.type === 'Polygon' ? [geom.coordinates] : geom.coordinates);

// Outer rings of a GeoJSON Polygon / MultiPolygon.
export const rings = (geom) => polygons(geom).map((p) => p[0]);
