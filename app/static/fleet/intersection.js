// Intersection view (spec §9.3): three.js scene in the ego frame (x forward, y left, z up, metres). Ego box, schematic
// pedestrian stick figures (opacity = keypoint confidence), vehicle boxes, buildings.json extruded and moved into the ego
// frame (facades dotted green), the dashed GNSS ±σ ring, the cited actor's measure line and dashed path; agents outside the
// dashcam's field of view are grey. Views: chase (follow) / top / dashcam, and free for an agent's setView.
import * as THREE from 'three';
import { state } from '../kernel/state.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DObject, CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';
import { esc } from '../kernel/format.js';
import { en, polygons } from './geo.js';
import { fisheye, undistortPx } from './videooverlay.js';

const C = { ink: 0xedf0f4, ink2: 0x9aa5b4, rule: 0x2a313c, thread: 0x4a5462, paper: 0x0e1218, sunk: 0x1e242e, measure: 0x56b4e9, map: 0x009e73 };
const VIEWS = { follow: 'Chase', top: 'Top', dashcam: 'Dashcam' };
// [camera position, look-at] of the fixed views.
const POSES = { follow: [[-22, 0, 11], [18, 0, 0]], top: [[12, 0, 80], [12, 0, 0]] };
const Z = new THREE.Vector3(0, 0, 1);
// Building labels farther than this from the ego (m) are hidden; beyond it they pile up on the horizon of the follow view.
const LABEL_RANGE = 70;

// A generic standing figure of height 1 (segment pairs); the pose itself is not measured, only ground point and height.
const FIGURE = [
  [0, 0, 0.86, 0, 0, 0.52], [0, 0.13, 0.82, 0, -0.13, 0.82], [0, 0.13, 0.82, 0, 0.19, 0.5], [0, -0.13, 0.82, 0, -0.19, 0.5],
  [0, 0.08, 0.52, 0, -0.08, 0.52], [0, 0.08, 0.52, 0, 0.1, 0], [0, -0.08, 0.52, 0, -0.1, 0],
].flat();
for (let i = 0; i < 12; i++) {
  const a = (i / 12) * 2 * Math.PI, b = ((i + 1) / 12) * 2 * Math.PI;
  FIGURE.push(0, 0.07 * Math.cos(a), 0.93 + 0.07 * Math.sin(a), 0, 0.07 * Math.cos(b), 0.93 + 0.07 * Math.sin(b));
}

function label(html, cls) {
  const div = document.createElement('div');
  div.className = `fl-3dl ${cls}`;
  div.innerHTML = html;
  return new CSS2DObject(div);
}

const ring = (opacity) => new THREE.Mesh(new THREE.RingGeometry(0.96, 1, 96), new THREE.MeshBasicMaterial({ color: C.ink2, transparent: true, opacity, side: THREE.DoubleSide, depthWrite: false }));
const dashed = (line) => {
  line.computeLineDistances();
  return line;
};

// cam: manifest camera (cam2world: OpenGL camera -> ego); origin: [lon, lat] the buildings' east/north metres are taken from;
// cite: entity_id of the actor the answer cites, or null.
export function createIntersection(el, { cam, origin, buildings, onPick, cite = null }) {
  el.innerHTML = `<span class="fl-3dcap mono">Ego-frame 3D · car at origin</span><span class="fl-badge">Monocular range: indicative only</span>
    <nav class="seg fl-views" aria-label="View">${Object.entries(VIEWS).map(([k, v]) => `<button data-v="${k}">${v}</button>`).join('')}</nav>`;
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(devicePixelRatio);
  el.prepend(renderer.domElement);
  const labels = new CSS2DRenderer();
  labels.domElement.className = 'fl-labels';
  el.append(labels.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(C.paper);
  scene.fog = new THREE.Fog(C.paper, 70, 160);
  scene.add(new THREE.HemisphereLight(0xdde6f0, 0x0e1218, 2));
  const sun = new THREE.DirectionalLight(0xffffff, 1.2);
  sun.position.set(-30, 20, 60);
  scene.add(sun);
  const grid = new THREE.GridHelper(240, 48, C.thread, C.rule);
  grid.rotation.x = Math.PI / 2;
  scene.add(grid);

  const egoBox = new THREE.BoxGeometry(4.5, 1.8, 1.5);
  const ego = new THREE.Group().add(
    new THREE.Mesh(egoBox, new THREE.MeshStandardMaterial({ color: C.sunk })),
    new THREE.LineSegments(new THREE.EdgesGeometry(egoBox), new THREE.LineBasicMaterial({ color: C.ink })),
  );
  ego.position.z = 0.75;
  scene.add(ego);

  // Dashcam field of view on the ground: the image's left / right edge rays at the principal row, undistorted.
  const c2w = new THREE.Matrix4().fromArray(cam.cam2world), toCam = c2w.clone().invert();
  const camPos = new THREE.Vector3().setFromMatrixPosition(c2w).setZ(0.02);
  const edge = (u) => {
    const [x] = undistortPx(cam, [u, cam.K[3]]);
    const d = new THREE.Vector3((x - cam.K[2]) / cam.K[0], 0, -1).transformDirection(c2w).setZ(0).normalize();
    return camPos.clone().addScaledVector(d, 45);
  };
  const fov = new THREE.BufferGeometry().setFromPoints([camPos, edge(0), edge(cam.w)]);
  scene.add(
    new THREE.Mesh(fov, new THREE.MeshBasicMaterial({ color: C.measure, transparent: true, opacity: 0.06, side: THREE.DoubleSide, depthWrite: false })),
    new THREE.LineLoop(fov, new THREE.LineBasicMaterial({ color: C.measure, transparent: true, opacity: 0.45 })),
  );
  const inFov = (p) => {
    const q = p.clone().applyMatrix4(toCam), uv = fisheye(cam, [q.x, -q.y, -q.z]);
    return !!uv && uv[0] >= 0 && uv[0] < cam.w && uv[1] >= 0 && uv[1] < cam.h;
  };

  const circle = new THREE.BufferGeometry().setFromPoints(new THREE.Path().absarc(0, 0, 1, 0, 2 * Math.PI).getPoints(96));
  const gnss = new THREE.Group().add(dashed(new THREE.LineLoop(circle, new THREE.LineDashedMaterial({ color: C.measure, dashSize: 0.12, gapSize: 0.08 }))));
  gnss.add(new THREE.Mesh(new THREE.CircleGeometry(1, 96), new THREE.MeshBasicMaterial({ color: C.measure, transparent: true, opacity: 0.05, depthWrite: false })));
  const gnssLabel = label('', 'fl-gnss');
  gnssLabel.position.set(0, -1, 0);
  gnss.add(gnssLabel);
  gnss.position.z = 0.03;
  scene.add(gnss);

  // Buildings live in a world group (east/north metres from origin) that is moved into the ego frame every frame.
  const world = new THREE.Group();
  scene.add(world);
  const meshes = new Map(), blabels = [];
  for (const [bid, b] of Object.entries(buildings)) {
    const height = b.height_m ?? 10;
    const xy = (r) => r.map((ll) => new THREE.Vector2(...en(origin, ll)));
    const shapes = polygons(b.geom).map(([outer, ...holes]) => Object.assign(new THREE.Shape(xy(outer)), { holes: holes.map((h) => new THREE.Path(xy(h))) }));
    const geo = new THREE.ExtrudeGeometry(shapes, { depth: height, bevelEnabled: false });
    // The polygon offset pushes the faces back so the edge lines lying on them do not z-fight.
    const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: C.thread, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }));
    mesh.userData.bid = bid;
    // Edges only where faces meet at over 20°, so the roof triangulation is not drawn.
    world.add(mesh, dashed(new THREE.LineSegments(new THREE.EdgesGeometry(geo, 20), new THREE.LineDashedMaterial({ color: C.map, dashSize: 0.25, gapSize: 0.5 }))));
    meshes.set(mesh, bid);
    geo.computeBoundingBox();
    const c = geo.boundingBox.getCenter(new THREE.Vector3());
    const l = label(esc(b.name && b.name !== bid ? b.name : 'Map facade'), 'fl-bl');
    // Street-level label: the follow view does not see roof tops.
    l.position.set(c.x, c.y, Math.min(height, 6));
    l.userData.bid = bid;
    l.element.title = bid;
    l.element.addEventListener('click', () => onPick(bid));
    world.add(l);
    blabels.push(l);
  }

  // Cited actor: measure line from the ego origin (ego frame) and its path over the loaded frames (world group).
  const measure = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0.05), new THREE.Vector3()]), new THREE.LineBasicMaterial({ color: C.measure }));
  const measureLabel = label('', 'fl-ml');
  measure.add(measureLabel);
  measure.visible = false;
  const path = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineDashedMaterial({ color: C.ink, dashSize: 0.5, gapSize: 0.4 }));
  path.visible = false;
  scene.add(measure);
  world.add(path);

  const agents = new Map();
  let selected = new Set();
  function agent(id, make) {
    if (!agents.has(id)) {
      const o = make();
      o.userData.label = label('', 'fl-al');
      o.add(o.userData.label);
      scene.add(o);
      agents.set(id, o);
    }
    return agents.get(id);
  }
  const ped = () => {
    const g = new THREE.Group();
    const fig = new THREE.LineSegments(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(FIGURE, 3)), new THREE.LineBasicMaterial({ transparent: true }));
    fig.name = 'fig';
    g.add(fig, Object.assign(ring(0.5), { name: 'sigma' }));
    return g;
  };
  const veh = () => {
    const box = new THREE.BoxGeometry(1, 1, 1).translate(0, 0, 0.5);
    const g = new THREE.Group();
    g.add(Object.assign(new THREE.Mesh(box, new THREE.MeshStandardMaterial({ transparent: true, opacity: 0.35 })), { name: 'body' }));
    g.add(Object.assign(new THREE.LineSegments(new THREE.EdgesGeometry(box), new THREE.LineBasicMaterial()), { name: 'edge' }));
    return g;
  };

  const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 1000);
  camera.up.copy(Z);
  const controls = new OrbitControls(camera, renderer.domElement);
  // The dashcam view follows the CONTRACT §3 three.js rule (pinhole approximation of the fisheye).
  const dash = new THREE.PerspectiveCamera(cam.fov_deg, (cam.w * cam.K[1]) / (cam.h * cam.K[0]), 0.1, 1000);
  c2w.decompose(dash.position, dash.quaternion, dash.scale);
  dash.setViewOffset(cam.w, cam.h, cam.w / 2 - cam.K[2], cam.h / 2 - cam.K[3], cam.w, cam.h);
  let view = null;

  function setView(v) {
    view = v;
    controls.enabled = v === 'free';
    camera.up.set(...(v === 'top' ? [1, 0, 0] : [0, 0, 1]));
    if (POSES[v]) {
      camera.position.set(...POSES[v][0]);
      controls.target.set(...POSES[v][1]);
      camera.lookAt(controls.target);
    }
    labels.domElement.hidden = v === 'dashcam';
    el.querySelectorAll('.fl-views button').forEach((b) => b.classList.toggle('on', b.dataset.v === v));
  }
  el.querySelector('.fl-views').addEventListener('click', (e) => e.target.closest('button') && setView(e.target.closest('button').dataset.v));
  setView('follow');

  const ro = new ResizeObserver(() => {
    renderer.setSize(el.clientWidth, el.clientHeight);
    labels.setSize(el.clientWidth, el.clientHeight);
  });
  ro.observe(el);
  renderer.setAnimationLoop(() => {
    const w = el.clientWidth, h = el.clientHeight;
    if (view === 'dashcam') {
      const s = Math.min(w / cam.w, h / cam.h);
      renderer.setViewport((w - cam.w * s) / 2, (h - cam.h * s) / 2, cam.w * s, cam.h * s);
      renderer.render(scene, dash);
      return;
    }
    renderer.setViewport(0, 0, w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.render(scene, camera);
    labels.render(scene, camera);
    declutter();
  });

  // Agent labels in order selected first, then nearest the ego; one overlapping an earlier shown label is hidden.
  // Selected agents are always labelled.
  function declutter() {
    const shown = [];
    const list = [...agents].filter(([, o]) => o.visible && o.userData.label.element.style.display !== 'none')
      .sort(([a, oa], [b, ob]) => selected.has(b) - selected.has(a) || oa.position.length() - ob.position.length());
    for (const [id, o] of list) {
      const el = o.userData.label.element, r = el.getBoundingClientRect();
      const hide = !selected.has(id) && shown.some((s) => r.left < s.right && s.left < r.right && r.top < s.bottom && s.top < r.bottom);
      el.classList.toggle('fl-hid', hide);
      if (!hide) shown.push(r);
    }
  }

  const ray = new THREE.Raycaster();
  let down = null;
  renderer.domElement.addEventListener('pointerdown', (e) => (down = [e.clientX, e.clientY]));
  renderer.domElement.addEventListener('pointerup', (e) => {
    if (view === 'dashcam' || !down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
    const r = renderer.domElement.getBoundingClientRect();
    ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, 1 - ((e.clientY - r.top) / r.height) * 2), camera);
    const hit = ray.intersectObjects([...meshes.keys()])[0];
    if (hit) onPick(meshes.get(hit.object));
  });

  return {
    setView,
    // frame: one ego/<chunk>.json frame; sel: selected entity_ids.
    setFrame(f, sel) {
      selected = sel;
      const [x, y] = en(origin, [f.ego.lon, f.ego.lat]), th = ((f.ego.heading - 90) * Math.PI) / 180;
      world.rotation.z = th;
      world.position.set(-x, -y, 0).applyAxisAngle(Z, th);
      world.updateMatrixWorld();
      for (const l of blabels) {
        l.visible = (state.stageMode !== 'evidence' || l.userData.bid === cite || sel.has(l.userData.bid)) && l.getWorldPosition(new THREE.Vector3()).setZ(0).length() < LABEL_RANGE;
        l.element.title = state.stageMode === 'explore' ? l.userData.bid : '';
      }
      const stale = f.ego.state === 'last_known';
      gnss.scale.setScalar(f.ego.sigma_m);
      gnssLabel.element.textContent = `GNSS ±${f.ego.sigma_m.toFixed(1)} m${stale ? ' · last fix' : ''}`;
      gnssLabel.element.classList.toggle('stale', stale);

      const seen = new Set();
      for (const p of f.peds) {
        const o = agent(p.entity_id, ped);
        const conf = p.kpts2d.reduce((a, k) => a + k[2], 0) / p.kpts2d.length;
        const color = sel.has(p.entity_id) ? C.ink : inFov(new THREE.Vector3(p.x, p.y, p.h / 2)) ? C.measure : C.thread;
        o.position.set(p.x, p.y, 0);
        const fig = o.getObjectByName('fig');
        fig.scale.setScalar(p.h);
        fig.material.color.setHex(color);
        fig.material.opacity = state.stageMode === 'evidence' && p.entity_id !== cite && !sel.has(p.entity_id) ? 0.15 : conf;
        o.getObjectByName('sigma').scale.setScalar(p.sigma_m);
        o.userData.label.position.set(0, 0, p.h + 0.5);
        o.userData.label.element.innerHTML = `<b>${esc(p.entity_id)}</b> approx <span class="mono">±${p.sigma_m.toFixed(1)} m</span>`;
        seen.add(p.entity_id);
      }
      for (const v of f.vehs) {
        const o = agent(v.entity_id, veh);
        const color = sel.has(v.entity_id) ? C.ink : inFov(new THREE.Vector3(v.x, v.y, v.dims[2] / 2)) ? C.measure : C.thread;
        o.position.set(v.x, v.y, 0);
        o.rotation.z = v.yaw;
        for (const n of ['body', 'edge']) {
          const m = o.getObjectByName(n);
          m.scale.set(...v.dims);
          m.material.color.setHex(color);
        }
        o.userData.label.position.set(0, 0, v.dims[2] + 0.6);
        o.userData.label.element.innerHTML = `<b>${esc(v.entity_id)}</b> ${esc(v.cls)} <span class="mono">±${v.sigma_m.toFixed(1)} m</span>`;
        seen.add(v.entity_id);
      }
      for (const [id, o] of agents) o.visible = seen.has(id);
      const a = [...f.peds, ...f.vehs].find((o) => o.entity_id === cite);
      measure.visible = !!a;
      if (a) {
        measure.geometry.attributes.position.setXYZ(1, a.x, a.y, 0.05);
        measure.geometry.attributes.position.needsUpdate = true;
        measure.geometry.computeBoundingSphere();
        measureLabel.position.set(a.x / 2, a.y / 2, 0.05);
        measureLabel.element.textContent = `${Math.hypot(a.x, a.y).toFixed(1)} m ±${a.sigma_m.toFixed(1)}`;
      }
    },
    // frames: one ego chunk; the cited actor's ground positions in it, moved from each frame's ego into world metres.
    setPath(frames) {
      const pts = frames.flatMap((g) => {
        const a = [...g.peds, ...g.vehs].find((o) => o.entity_id === cite);
        if (!a) return [];
        const [x, y] = en(origin, [g.ego.lon, g.ego.lat]), th = ((g.ego.heading - 90) * Math.PI) / 180;
        return [new THREE.Vector3(a.x, a.y, 0.05).applyAxisAngle(Z, -th).add(new THREE.Vector3(x, y, 0))];
      });
      path.geometry.dispose();
      path.geometry = new THREE.BufferGeometry().setFromPoints(pts);
      path.computeLineDistances();
      path.visible = pts.length > 1;
    },
    focus(bid) {
      for (const [m, b] of meshes) m.material.color.setHex(b === bid ? C.map : C.thread);
    },
    dispose() {
      renderer.setAnimationLoop(null);
      ro.disconnect();
      controls.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      el.replaceChildren();
    },
  };
}
