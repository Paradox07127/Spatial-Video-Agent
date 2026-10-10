// Forklifts from forklift.json: a solid oriented box over the footprint, its front face light, and a heading arrow
// (heading: rad from world +x, CCW).
import * as THREE from 'three';
import { esc } from '../kernel/format.js';

const HEIGHT = 1.9; // drawn box height (m); the footprint has no height
const MAX_GAP = 150;

export function createForklifts(kit) {
  const group = new THREE.Group();
  const lifts = new Map();
  let selected = new Set(), askable = false;

  function tag(f) {
    const on = selected.has(f.id);
    kit.setTag(f.tag, `${esc(f.id)}${on && askable ? kit.askButton(f.id) : ''}`, on);
  }

  return {
    group,
    pickables: [],
    set(json) {
      kit.clear(group);
      lifts.clear();
      this.pickables = [];
      for (const fk of json?.forklifts ?? []) {
        // Footprint in the forklift frame (+x = heading), from the first sample.
        const r0 = fk.track[0], c = Math.cos(-r0.heading), s = Math.sin(-r0.heading);
        const local = r0.footprint.map(([x, y]) => [(x - r0.x) * c - (y - r0.y) * s, (x - r0.x) * s + (y - r0.y) * c]);
        const xs = local.map((p) => p[0]), ys = local.map((p) => p[1]);
        const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
        const box = new THREE.BoxGeometry(x1 - x0, y1 - y0, HEIGHT).translate((x0 + x1) / 2, (y0 + y1) / 2, HEIGHT / 2);
        const side = new THREE.MeshBasicMaterial({ color: kit.color('--thread') }), front = new THREE.MeshBasicMaterial({ color: kit.color('--ink-2') });
        // BoxGeometry face order: +x (front), -x, +y, -y, +z, -z
        const body = new THREE.Mesh(box, [front, side, side, side, side, side]);
        body.userData.entity_id = fk.entity_id;
        const edges = new THREE.LineSegments(new THREE.EdgesGeometry(box), new THREE.LineBasicMaterial({ color: kit.color('--ink-2') }));
        const foot = new THREE.Mesh(
          new THREE.PlaneGeometry(x1 - x0, y1 - y0).translate((x0 + x1) / 2, (y0 + y1) / 2, 0.015),
          new THREE.MeshBasicMaterial({ color: kit.color('--ink-2'), transparent: true, opacity: 0.3, depthWrite: false }),
        );
        const outline = kit.line([[x0, y0, 0.02], [x1, y0, 0.02], [x1, y1, 0.02], [x0, y1, 0.02]], new THREE.LineBasicMaterial({ color: kit.color('--ink') }), true);
        const arrow = new THREE.Mesh(
          new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(x1 + 0.2, 0.5, 0.02), new THREE.Vector3(x1 + 1.1, 0, 0.02), new THREE.Vector3(x1 + 0.2, -0.5, 0.02)]),
          new THREE.MeshBasicMaterial({ color: kit.color('--ink'), side: THREE.DoubleSide }),
        );
        const pad = 0.3;
        const halo = kit.line([[x0 - pad, y0 - pad, 0.02], [x1 + pad, y0 - pad, 0.02], [x1 + pad, y1 + pad, 0.02], [x0 - pad, y1 + pad, 0.02]], new THREE.LineBasicMaterial({ color: kit.color('--ink') }), true);
        halo.visible = false;
        const label = kit.label('', 'lbl-ent');
        label.position.z = HEIGHT + 0.3;
        const node = new THREE.Group();
        node.add(body, edges, foot, outline, arrow, halo, label);
        group.add(node);
        this.pickables.push(body);
        lifts.set(fk.entity_id, { id: fk.entity_id, track: fk.track, ts: fk.track.map((r) => r.t_ms), node, body, edges, halo, tag: label, dim: false });
      }
    },
    update(t) {
      for (const f of lifts.values()) {
        const r = f.track[kit.nearest(f.ts, t)];
        f.node.visible = Math.abs(r.t_ms - t) <= MAX_GAP;
        f.node.position.set(r.x, r.y, 0);
        f.node.rotation.z = r.heading;
        tag(f);
      }
    },
    highlight(ids, ask) {
      selected = new Set(ids);
      askable = ask;
      for (const f of lifts.values()) {
        f.halo.visible = selected.has(f.id);
        f.edges.material.color = kit.color(selected.has(f.id) ? '--ink' : '--ink-2');
        f.dim = selected.size > 0 && !selected.has(f.id);
        kit.dim([f.node], f.dim);
        tag(f);
      }
    },
    // on: draw the box faces; off leaves the outline over a camera frame.
    solid(on) {
      for (const f of lifts.values()) f.body.visible = on;
    },
    dimmed: () => [...lifts.values()].filter((e) => e.dim).map((e) => e.id),
    drawn: () => Object.fromEntries([...lifts.values()].filter((e) => e.node.visible).map((e) => [e.id, [e.node.position.x, e.node.position.y]])),
    // world corners of each visible box, for keeping labels off it
    corners: () => [...lifts.values()].filter((e) => e.node.visible).map((e) => {
      const b = new THREE.Box3().setFromObject(e.node);
      return [0, 1, 2, 3, 4, 5, 6, 7].map((i) => [i & 1 ? b.max.x : b.min.x, i & 2 ? b.max.y : b.min.y, i & 4 ? b.max.z : b.min.z]);
    }),
  };
}
