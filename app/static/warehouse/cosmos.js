// Model primitives (spec §2.5): C2 ghost track as a dashed line, C3 occluder outlines (dashed; solid once depth-checked).
// Every primitive carries the Model source badge.
import * as THREE from 'three';
import { source } from '../kernel/badges.js';
import { esc } from '../kernel/format.js';

export function createCosmos(kit) {
  const group = new THREE.Group();
  const dashed = () => new THREE.LineDashedMaterial({ color: kit.color('--src-model'), dashSize: 0.25, gapSize: 0.18 });
  const tag = (html, [x, y]) => {
    const l = kit.label(`${source('model')}<span>${html}</span>`, 'lbl-model');
    l.position.set(x, y, 0.1);
    group.add(l);
  };

  return {
    group,
    set(results) {
      kit.clear(group);
      const c2 = new Map(); // actor (or record) → its predictions
      for (const r of results) {
        if (r.task === 'C2' && r.payload.ghost) {
          const g = r.payload.ghost;
          const ln = kit.line(g.map(([, x, y]) => [x, y, 0.05]), dashed());
          ln.computeLineDistances();
          group.add(ln);
          const id = r.actor_refs?.[0]?.entity_id ?? r.record_id;
          c2.set(id, [...(c2.get(id) ?? []), r]);
        }
        if (r.task === 'C3') {
          for (const o of r.payload.objects) {
            const solid = o.geometric_support;
            const ln = kit.line(o.ground_poly.map(([x, y]) => [x, y, 0.03]), solid ? new THREE.LineBasicMaterial({ color: kit.color('--src-model') }) : dashed(), true);
            if (!solid) ln.computeLineDistances();
            group.add(ln);
            const cx = o.ground_poly.reduce((s, p) => s + p[0], 0) / o.ground_poly.length;
            tag(`${esc(o.label)} · depth ${solid ? 'checked' : 'unchecked'}`, [cx, Math.max(...o.ground_poly.map((p) => p[1]))]);
          }
        }
      }
      // One tag per actor at the end of its latest arrow, listing its predicted actions in time order.
      for (const rs of c2.values()) {
        rs.sort((a, b) => a.t_ms - b.t_ms);
        const last = rs.at(-1), actor = last.actor_refs?.[0]?.entity_id;
        const acts = rs.map((r) => r.payload.actions.join('/')).filter((a, i, xs) => a !== xs[i - 1]);
        tag(`${actor ? `${esc(actor)} ` : ''}predicts ${esc(acts.join(' → '))} · ${esc(last.payload.horizon_s)} s`, last.payload.ghost.at(-1).slice(1));
      }
    },
    // CSS2D objects of the tags, for the stage to move them clear of other labels.
    get tags() {
      return group.children.filter((o) => o.isCSS2DObject);
    },
  };
}
