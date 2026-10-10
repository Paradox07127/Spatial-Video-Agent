// Master clock: clock.t (integer ms, scene time or fleet replay wall time) is the single source of truth.
import { bus } from './bus.js';

let generation = 0;
const clamp = (t) => Math.min(Math.max(Math.round(t), clock.range.t0), clock.range.t1 - 1);

export const clock = {
  t: 0,
  playing: false,
  rate: 1,
  range: { t0: 0, t1: 10000 },
  domain: { kind: 'scene' },

  seek(t) {
    clock.t = clamp(t);
    bus.emit('clock:tick', clock.t);
  },
  play() {
    if (clock.playing) return;
    if (clock.t >= clock.range.t1 - 1) clock.seek(clock.range.t0);
    const current = ++generation;
    clock.playing = true;
    bus.emit('clock:play', true);
    let last = performance.now();
    const step = (now) => {
      if (!clock.playing || current !== generation) return;
      clock.seek(clock.t + (now - last) * clock.rate);
      if (!clock.playing || current !== generation) return;
      last = now;
      if (clock.t >= clock.range.t1 - 1) clock.pause();
      else requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  },
  pause() {
    generation++;
    clock.playing = false;
    bus.emit('clock:play', false);
  },
  setRange(t0, t1) {
    clock.range = { t0, t1 };
    bus.emit('clock:range', clock.range);
    clock.seek(clock.t);
  },
  onTick(fn) {
    bus.on('clock:tick', fn);
    return () => bus.off('clock:tick', fn);
  },
  // domain: {kind:'scene'} or {kind:'fleet', replay: ReplayMapping[]} (CONTRACT §3).
  bind(domain) {
    clock.domain = domain;
  },
  // Wall ms -> {trip_id, t_ms}; scene domain is the identity. null when no trip covers wall_ms.
  map(wall_ms) {
    if (clock.domain.kind === 'scene') return { trip_id: null, t_ms: clamp(wall_ms) };
    for (const m of clock.domain.replay) {
      const t = m.source_start_ms + (wall_ms - m.replay_start_ms) * m.rate;
      if (m.coverage_intervals.some(([a, b]) => t >= a && t < b)) return { trip_id: m.trip_id, t_ms: Math.round(t) };
    }
    return null;
  },
  // Inverse of map: scene time of trip_id -> wall ms (identity in the scene domain or for an unknown trip).
  toWall(trip_id, t_ms) {
    const m = clock.domain.kind === 'fleet' && clock.domain.replay.find((r) => r.trip_id === trip_id);
    return m ? Math.round(m.replay_start_ms + (t_ms - m.source_start_ms) / m.rate) : t_ms;
  },
};
