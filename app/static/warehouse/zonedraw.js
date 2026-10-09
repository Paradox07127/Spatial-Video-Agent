// Floor polygon drawing: click adds a vertex, double-click closes (≥ 3 vertices). The drawn zone stays as a dashed outline.
import * as THREE from 'three';

export function createZoneDraw(kit) {
  const group = new THREE.Group();
  let pts = [], cursor = null, done = null;
  const mat = () => new THREE.LineDashedMaterial({ color: kit.color('--ink'), dashSize: 0.3, gapSize: 0.15 });

  function redraw(closed) {
    kit.clear(group);
    const path = [...pts, ...(cursor && !closed ? [cursor] : [])];
    if (path.length < 2) return;
    const ln = kit.line(path.map(([x, y]) => [x, y, 0.03]), mat(), closed);
    ln.computeLineDistances();
    group.add(ln);
  }

  function finish(poly) {
    const d = done;
    done = cursor = null;
    redraw(!!poly);
    d?.(poly);
  }

  return {
    group,
    get active() {
      return !!done;
    },
    // Resolves to the world polygon [[x, y]...], or null when cancelled.
    start() {
      done?.(null);
      pts = [];
      redraw(false);
      return new Promise((res) => (done = res));
    },
    add(p) {
      const last = pts.at(-1);
      if (p && !(last && Math.hypot(p[0] - last[0], p[1] - last[1]) < 0.05)) pts.push(p);
      redraw(false);
    },
    hover(p) {
      cursor = p;
      redraw(false);
    },
    close() {
      if (pts.length >= 3) finish(pts.map((p) => [+p[0].toFixed(3), +p[1].toFixed(3)]));
    },
    cancel() {
      pts = [];
      finish(null);
    },
  };
}
