// People from skeleton.json: COCO-17 bones in world metres; a single-view frame (xyz null) shows only the root, tagged single view.
import * as THREE from 'three';

const EDGES = [[0, 1], [0, 2], [1, 3], [2, 4], [5, 6], [5, 7], [7, 9], [6, 8], [8, 10], [5, 11], [6, 12], [11, 12], [11, 13], [13, 15], [12, 14], [14, 16]];
// Samples farther than this from the clock are not drawn (ms).
const MAX_GAP = 150;

export function createSkeletons(kit) {
  const group = new THREE.Group();
  const people = new Map();
  let selected = new Set(), askable = false;

  function tag(p) {
    const on = selected.has(p.id);
    kit.setTag(p.tag, `${p.id}${p.single ? '<span class="lbl-sub">single view</span>' : ''}${on && askable ? kit.askButton(p.id) : ''}`, on);
  }

  return {
    group,
    pickables: [],
    set(json) {
      kit.clear(group);
      people.clear();
      this.pickables = [];
      for (const [id, s] of Object.entries(json.persons)) {
        const pos = new Float32Array(EDGES.length * 6), jpos = new Float32Array(17 * 3);
        const bones = new THREE.LineSegments(new THREE.BufferGeometry().setAttribute('position', new THREE.BufferAttribute(pos, 3)), new THREE.LineBasicMaterial({ color: kit.color('--ink') }));
        // Joint dots in screen pixels: an ink dot over a slightly larger paper dot reads against any cloud colour.
        const jg = new THREE.BufferGeometry().setAttribute('position', new THREE.BufferAttribute(jpos, 3));
        const joints = new THREE.Group().add(
          new THREE.Points(jg, new THREE.PointsMaterial({ color: kit.color('--paper'), size: 5, sizeAttenuation: false })),
          new THREE.Points(jg, new THREE.PointsMaterial({ color: kit.color('--ink'), size: 3, sizeAttenuation: false })),
        );
        bones.frustumCulled = false;
        joints.children.forEach((o) => (o.frustumCulled = false));
        const node = new THREE.Group();
        const shade = new THREE.Mesh(new THREE.CircleGeometry(0.55, 32), new THREE.MeshBasicMaterial({ color: kit.color('--paper'), transparent: true, opacity: 0.55, depthWrite: false }));
        const base = new THREE.Mesh(new THREE.RingGeometry(0.42, 0.5, 48), new THREE.MeshBasicMaterial({ color: kit.color('--ink') }));
        const root = new THREE.Mesh(new THREE.CircleGeometry(0.14, 24), new THREE.MeshBasicMaterial({ color: kit.color('--ink-2') }));
        const ring = new THREE.Mesh(new THREE.RingGeometry(0.66, 0.76, 48), new THREE.MeshBasicMaterial({ color: kit.color('--ink') }));
        const pick = new THREE.Mesh(new THREE.CylinderGeometry(0.4, 0.4, 1.8, 12).rotateX(Math.PI / 2).translate(0, 0, 0.9), kit.pickMaterial);
        pick.userData.entity_id = id;
        const label = kit.label('', 'lbl-ent');
        label.position.z = 2.05;
        shade.position.z = 0.008;
        root.position.z = base.position.z = ring.position.z = 0.012;
        ring.visible = false;
        node.add(shade, base, root, ring, pick, label);
        group.add(node, bones, joints);
        this.pickables.push(pick);
        people.set(id, { id, s, node, bones, pos, joints, jpos, ring, tag: label, single: false, dim: false });
      }
    },
    update(t) {
      for (const p of people.values()) {
        const i = kit.nearest(p.s.t_ms, t);
        const live = Math.abs(p.s.t_ms[i] - t) <= MAX_GAP;
        p.node.visible = live;
        p.bones.visible = p.joints.visible = live && !!p.s.xyz[i]?.some(Boolean);
        if (!live) continue;
        p.node.position.set(...p.s.root_world[i], 0);
        p.single = !p.s.xyz[i]?.some(Boolean);
        if (!p.single) {
          const J = p.s.xyz[i], any = J.find(Boolean);
          // a null joint was not triangulated: its bones collapse onto the other end (or any known joint)
          EDGES.forEach(([a, b], k) => p.pos.set([...(J[a] ?? J[b] ?? any), ...(J[b] ?? J[a] ?? any)], k * 6));
          J.forEach((j, k) => p.jpos.set(j ?? any, k * 3));
          p.bones.geometry.attributes.position.needsUpdate = true;
          p.joints.children[0].geometry.attributes.position.needsUpdate = true;
        }
        tag(p);
      }
    },
    // ids: highlighted entity_ids; ask: show the Ask about this button on them.
    highlight(ids, ask) {
      selected = new Set(ids);
      askable = ask;
      for (const p of people.values()) {
        p.ring.visible = selected.has(p.id);
        p.dim = selected.size > 0 && !selected.has(p.id);
        kit.dim([p.node, p.bones, p.joints], p.dim);
        tag(p);
      }
    },
    // Labels in order selected first, then nearest the eye; one overlapping an earlier shown label is hidden.
    declutter(eye) {
      const shown = [];
      const list = [...people.values()].filter((p) => p.node.visible)
        .sort((a, b) => selected.has(b.id) - selected.has(a.id) || a.node.position.distanceTo(eye) - b.node.position.distanceTo(eye));
      for (const p of list) {
        const el = p.tag.element, r = el.getBoundingClientRect();
        const hide = !selected.has(p.id) && shown.some((s) => r.left < s.right && s.left < r.right && r.top < s.bottom && s.top < r.bottom);
        el.classList.toggle('wh-hid', hide);
        if (!hide) shown.push(r);
      }
    },
    dimmed: () => [...people.values()].filter((p) => p.dim).map((p) => p.id),
    single: () => [...people.values()].filter((p) => p.node.visible && p.single).map((p) => p.id),
    drawn: () => Object.fromEntries([...people.values()].filter((e) => e.node.visible).map((e) => [e.id, [e.node.position.x, e.node.position.y]])),
  };
}
