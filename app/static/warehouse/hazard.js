// Dynamic hazard zones: red translucent polygons labelled Hazard zone #id; a newly triggered one spreads out from the incident point.
import * as THREE from 'three';
import { esc } from '../kernel/format.js';

const SPREAD_MS = 1100;

// Backend zone ids are z_ + 32 hex; labels show the first 6 hex, the full id stays in data-zone and title.
export const shortZone = (id) => (/^z_[0-9a-f]{32}$/.test(id) ? id.slice(0, 8) : id);

export function createHazards(kit) {
  const group = new THREE.Group();
  let anim = [];

  return {
    group,
    // zones: [{zone_id, center:[x,y], polygon:[[x,y]...]}]; fresh: zone_id that was just triggered.
    set(zones, fresh) {
      kit.clear(group);
      anim = [];
      for (const z of zones) {
        const [cx, cy] = z.center;
        const shape = new THREE.Shape(z.polygon.map(([x, y]) => new THREE.Vector2(x - cx, y - cy)));
        const fill = new THREE.Mesh(new THREE.ShapeGeometry(shape), new THREE.MeshBasicMaterial({ color: kit.color('--danger'), transparent: true, opacity: 0.22, depthWrite: false }));
        const edge = kit.line(z.polygon.map(([x, y]) => [x - cx, y - cy, 0.01]), new THREE.LineBasicMaterial({ color: kit.color('--danger') }), true);
        const node = new THREE.Group();
        node.position.set(cx, cy, 0.008);
        node.add(fill, edge);
        const tag = kit.label(`Hazard zone <span class="mono" title="${esc(z.zone_id)}">#${esc(shortZone(z.zone_id))}</span>`, 'lbl-hazard');
        tag.position.set(cx, Math.max(...z.polygon.map((p) => p[1])), 0.1);
        tag.element.dataset.zone = z.zone_id;
        group.add(node, tag);
        if (z.zone_id === fresh) {
          const ring = new THREE.Mesh(new THREE.RingGeometry(0.94, 1, 64), new THREE.MeshBasicMaterial({ color: kit.color('--danger'), transparent: true, depthWrite: false }));
          ring.position.set(cx, cy, 0.012);
          group.add(ring);
          anim.push({ node, ring, t0: performance.now(), r: Math.max(...z.polygon.map(([x, y]) => Math.hypot(x - cx, y - cy))) });
        }
      }
    },
    animate(now) {
      anim = anim.filter((a) => {
        const k = Math.min(1, (now - a.t0) / SPREAD_MS), e = 1 - (1 - k) ** 3;
        a.node.scale.setScalar(Math.max(e, 0.01));
        a.ring.scale.setScalar(Math.max(a.r * (0.2 + 1.2 * k), 0.01));
        a.ring.material.opacity = 1 - k;
        if (k >= 1) group.remove(a.ring);
        return k < 1;
      });
    },
  };
}
