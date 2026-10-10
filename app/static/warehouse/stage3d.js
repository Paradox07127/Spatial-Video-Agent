// Warehouse 3D stage (spec §9.2). World: metres, Z up, floor z = 0. Views: 'free' (orbit), 'top', 'cam:<id>' (the real camera).
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DObject, CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';
import { esc } from '../kernel/format.js';
import { createBlindCells } from './blindcells.js';
import { createCosmos } from './cosmos.js';
import { createForklifts } from './forklift.js';
import { createHazards } from './hazard.js';
import { blocksObject, parsePoints, pointsObject } from './points.js';
import { createSkeletons } from './skeleton.js';
import { createZoneDraw } from './zonedraw.js';

// Distance (m) of the drawn image plane (frustum base) from the camera centre: at least FRUSTUM_D, FRUSTUM_K per metre of
// floor diagonal on larger floors, and short enough that the base stays FRUSTUM_CLEAR (m) above the floor.
const FRUSTUM_D = 3, FRUSTUM_K = 0.07, FRUSTUM_CLEAR = 0.5;
const FOCUS_DIST = 12; // camera distance (m) after focus()
// fit(): camera pitch above the floor, least distance (m), share of the canvas (NDC) the actors fill, actor height (m) kept in view
const FIT_PITCH = Math.PI / 3, FIT_DIST = 18, FIT_NDC = 0.6, ACTOR_H = 1.8;
const PLANE_FADE = 0.25; // opacity of the frustum image planes while actors are framed
const ACT_PAD = 3, ACT_MIN = 12; // home framing: margin (m) around the activity area and its least side (m)
const RING_R = 0.76; // outer radius (m) of the person selection ring in skeleton.js
const DIM = 0.25; // opacity factor of actors outside a non-empty selection

const css = getComputedStyle(document.documentElement);
const color = (name) => new THREE.Color(css.getPropertyValue(name).trim());

const kit = {
  color,
  label(html, cls) {
    const el = document.createElement('div');
    el.className = `wh-lbl ${cls}`;
    el.innerHTML = html;
    return new CSS2DObject(el);
  },
  // Rewrites a label only when its content changes, so a button inside it survives clock ticks.
  setTag(obj, html, on) {
    if (obj.userData.html !== html) obj.element.innerHTML = obj.userData.html = html;
    obj.element.classList.toggle('on', on);
  },
  askButton: (id) => `<button class="wh-ask-this" data-id="${esc(id)}">Ask about this</button>`,
  line(pts, mat, loop) {
    const g = new THREE.BufferGeometry().setFromPoints(pts.map((p) => new THREE.Vector3(...p)));
    return new (loop ? THREE.LineLoop : THREE.Line)(g, mat);
  },
  // CSS2D elements of nested labels are not removed by Object3D.clear().
  clear(group) {
    group.traverse((o) => o.isCSS2DObject && o.element.remove());
    group.clear();
  },
  // Index of the sample in ascending ts closest to t.
  nearest(ts, t) {
    let lo = 0, hi = ts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] < t) lo = mid + 1;
      else hi = mid;
    }
    return lo > 0 && t - ts[lo - 1] < ts[lo] - t ? lo - 1 : lo;
  },
  pickMaterial: new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }),
  // Fades every material and label under objs to DIM of its own opacity, or restores it.
  dim(objs, on) {
    for (const obj of objs) {
      obj.traverse((o) => {
        if (o.isCSS2DObject) o.element.classList.toggle('dim', on);
        if (!o.material || o.material === kit.pickMaterial) return;
        for (const m of [o.material].flat()) {
          const b = (m.userData.base ??= { opacity: m.opacity, transparent: m.transparent });
          m.opacity = on ? b.opacity * DIM : b.opacity;
          m.transparent = on || b.transparent;
          m.needsUpdate = true;
        }
      });
    }
  },
};

export function createStage(canvas) {
  const host = canvas.parentElement;
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(devicePixelRatio);
  renderer.setClearColor(color('--paper'));
  const labels = new CSS2DRenderer();
  labels.domElement.className = 'wh-labels';
  host.append(labels.domElement);

  const scene = new THREE.Scene();
  const free = new THREE.PerspectiveCamera(40, 1, 0.1, 500);
  free.up.set(0, 0, 1);
  const controls = new OrbitControls(free, canvas);
  controls.maxPolarAngle = Math.PI / 2 - 0.05;
  const real = new THREE.PerspectiveCamera(40, 1, 0.05, 500);
  real.matrixAutoUpdate = false;
  real.up.set(0, 0, 1);

  const ground = new THREE.Group(), frustaGroup = new THREE.Group(), pinGroup = new THREE.Group(), trackGroup = new THREE.Group(), cloud = new THREE.Group(), blocks = new THREE.Group();
  const measureGroup = new THREE.Group();
  const people = createSkeletons(kit), lifts = createForklifts(kit), cells = createBlindCells(kit);
  const hazards = createHazards(kit), cosmos = createCosmos(kit), zone = createZoneDraw(kit);
  // measureGroup and cells.group switch their own visibility, so their layers wrap them
  const measureLayer = new THREE.Group().add(measureGroup), blindLayer = new THREE.Group().add(cells.group);
  let answerLayers = null;
  const layerGroups = { people: people.group, forklift: lifts.group, measures: measureLayer, blind: blindLayer, cloud, frusta: frustaGroup };
  cloud.visible = false;
  scene.add(ground, blocks, cloud, blindLayer, zone.group, hazards.group, cosmos.group, trackGroup, measureLayer, people.group, lifts.group, frustaGroup, pinGroup);

  let mf = null, view = 'free', tracks = new Map(), skeleton = null, forklift = null, tween = null, home = null, selectedIds = [], askable = false;
  // focusIds: the actors an evidence cites, shown instead of the selection; back: the view before the first fit()
  let focusIds = null, back = null;
  // measure: {ids: [a, b], t0, t1}, drawn while t0 <= clock < t1; measureEnds: the [x, y] pair it was last drawn at
  let measure = null, measureEnds = null;
  let activeCam = null; // the camera the stage video dock shows; its frustum is drawn in --ink
  const frusta = new Map(), pickFns = new Set();
  const loader = new THREE.TextureLoader(), textures = new Map();

  const camOf = (v) => mf?.cams.find((c) => `cam:${c.id}` === v);
  const active = () => (camOf(view) ? real : free);

  function layout() {
    const W = host.clientWidth, H = host.clientHeight;
    if (!W || !H) return;
    const cam = camOf(view);
    // A camera view is letterboxed to the source image so pixel (u, v) maps linearly onto the canvas.
    const w = cam ? Math.min(W, (H * cam.w) / cam.h) : W, h = cam ? (w * cam.h) / cam.w : H;
    const box = { left: `${(W - w) / 2}px`, top: `${(H - h) / 2}px` };
    Object.assign(canvas.style, box);
    Object.assign(labels.domElement.style, box);
    renderer.setSize(w, h);
    labels.setSize(w, h);
    free.aspect = w / h;
    free.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(layout);
  ro.observe(host);

  function buildGround(g) {
    kit.clear(ground);
    const W = g.nx * g.cell_m, H = g.ny * g.cell_m, [x0, y0] = g.origin;
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(W, H), new THREE.MeshBasicMaterial({ color: color('--card') }));
    floor.position.set(x0 + W / 2, y0 + H / 2, -0.01);
    const minor = [], major = [];
    for (let x = Math.ceil(x0); x <= x0 + W; x++) (x % 5 ? minor : major).push(x, y0, 0, x, y0 + H, 0);
    for (let y = Math.ceil(y0); y <= y0 + H; y++) (y % 5 ? minor : major).push(x0, y, 0, x0 + W, y, 0);
    const seg = (a, c) => new THREE.LineSegments(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(a, 3)), new THREE.LineBasicMaterial({ color: color(c) }));
    ground.add(floor, seg(minor, '--rule'), seg(major, '--thread'));
  }

  // Home framing: the activity area (every person and forklift position of the scene, padded by ACT_PAD, at least ACT_MIN
  // a side, clamped to the floor), else the floor corners plus every camera centre; the free view looks across the floor's long side.
  function frame(m) {
    const { origin: [x0, y0], nx, ny, cell_m } = m.ground, x1 = x0 + nx * cell_m, y1 = y0 + ny * cell_m;
    const xy = [
      ...[...tracks.values()].flatMap((tr) => tr.pts.map((p) => [p[1], p[2]])),
      ...Object.values(skeleton?.persons ?? {}).flatMap((p) => p.root_world.filter(Boolean)),
      ...(forklift?.forklifts ?? []).flatMap((f) => f.track.map((r) => [r.x, r.y])),
    ];
    let box = [[x0, x1], [y0, y1]];
    if (xy.length) {
      box = box.map(([g0, g1], k) => {
        let lo = Infinity, hi = -Infinity;
        for (const p of xy) {
          lo = Math.min(lo, p[k]);
          hi = Math.max(hi, p[k]);
        }
        const grow = Math.max(0, ACT_MIN - (hi - lo) - 2 * ACT_PAD) / 2;
        return [Math.max(g0, lo - ACT_PAD - grow), Math.min(g1, hi + ACT_PAD + grow)];
      });
    }
    const [[bx0, bx1], [by0, by1]] = box;
    const pts = [[bx0, by0, 0], [bx1, by0, 0], [bx1, by1, 0], [bx0, by1, 0], ...(xy.length ? [] : m.cams.map((c) => c.cam2world.slice(12, 15)))].map((p) => new THREE.Vector3(...p));
    const dir = ny > nx ? new THREE.Vector3(-0.85, -0.15, 1.1) : new THREE.Vector3(-0.15, -0.85, 1.1);
    home = { pts, dir: dir.normalize(), target: new THREE.Vector3((bx0 + bx1) / 2, (by0 + by1) / 2, 0) };
  }

  function buildFrustum(c) {
    const [fx, fy, cx, cy] = c.K;
    const rot = new THREE.Matrix3().setFromMatrix4(new THREE.Matrix4().fromArray(c.cam2world)), camZ = c.cam2world[14];
    // Image corners at depth 1 in the OpenGL camera frame (+Y up, looking down −Z).
    const rays = [[0, 0], [c.w, 0], [c.w, c.h], [0, c.h]].map(([u, v]) => new THREE.Vector3((u - cx) / fx, -(v - cy) / fy, -1));
    const { nx, ny, cell_m } = mf.ground, reach = Math.max(FRUSTUM_D, FRUSTUM_K * Math.hypot(nx, ny) * cell_m);
    const drop = Math.max(...rays.map((r) => -r.clone().applyMatrix3(rot).z));
    const d = drop > 0 ? Math.max(0.3, Math.min(reach, (camZ - FRUSTUM_CLEAR) / drop)) : reach;
    const K = rays.map((r) => r.multiplyScalar(d));
    const O = new THREE.Vector3();
    const edges = new THREE.LineSegments(
      new THREE.BufferGeometry().setFromPoints(K.flatMap((k, i) => [O, k, k, K[(i + 1) % 4]])),
      new THREE.LineBasicMaterial({ color: color('--ink-2'), transparent: true, opacity: 0.75 }),
    );
    const quad = new THREE.BufferGeometry().setFromPoints(K);
    quad.setAttribute('uv', new THREE.Float32BufferAttribute([0, 1, 1, 1, 1, 0, 0, 0], 2));
    quad.setIndex([0, 3, 1, 1, 3, 2]);
    const mat = new THREE.MeshBasicMaterial({ color: color('--thread'), side: THREE.DoubleSide });
    const tag = kit.label(esc(c.id), 'lbl-cam');
    // Above the camera centre in world z, clear of the floor cloud below.
    tag.position.set(c.cam2world[12], c.cam2world[13], camZ + 0.6).applyMatrix4(new THREE.Matrix4().fromArray(c.cam2world).invert());
    const node = new THREE.Group();
    node.matrixAutoUpdate = false;
    node.matrix.fromArray(c.cam2world);
    node.matrixWorldNeedsUpdate = true;
    node.add(edges, new THREE.Mesh(quad, mat), tag);
    frustaGroup.add(node);
    return { c, node, mat, edge: edges.material, url: null, tex: null };
  }

  // Frustum base and (in that camera's view) background: the camera's current thumbnail, grey when there is none.
  function paint(f) {
    f.mat.map = f.tex;
    f.mat.color = f.tex ? new THREE.Color(0xffffff) : color('--thread');
    f.mat.needsUpdate = true;
    if (view === `cam:${f.c.id}`) scene.background = f.tex ?? color('--sunk');
  }
  function texture(url) {
    if (!textures.has(url)) {
      textures.set(url, new Promise((res) => loader.load(url, (tx) => {
        tx.colorSpace = THREE.SRGBColorSpace;
        res(tx);
      })));
    }
    return textures.get(url);
  }

  const still = matchMedia('(prefers-reduced-motion: reduce)');
  function flyTo(pos, target, ms) {
    tween = { t0: performance.now(), ms: still.matches ? 1 : Math.max(ms, 1), p0: free.position.clone(), q0: controls.target.clone(), p1: pos, q1: target };
  }
  // Backs the camera off along a fixed direction from d0, re-centring the target on the floor so the projected points sit
  // in the middle of the canvas, until they all project inside FIT of it (NDC).
  function backOff(pts, target0, dir, FIT, d0, ms) {
    const probeCam = free.clone(), fitRay = new THREE.Raycaster();
    const place = (target, d) => {
      probeCam.position.copy(target).addScaledVector(dir, d);
      probeCam.lookAt(target);
      probeCam.updateMatrixWorld();
      return pts.map((p) => p.clone().project(probeCam));
    };
    let target;
    for (let d = d0; d < 400; d *= 1.025) {
      target = target0.clone();
      for (let i = 0; i < 3; i++) {
        const q = place(target, d), mid = (k) => (Math.min(...q.map((p) => p[k])) + Math.max(...q.map((p) => p[k]))) / 2;
        fitRay.setFromCamera(new THREE.Vector2(mid('x'), mid('y')), probeCam);
        target = fitRay.ray.intersectPlane(floorPlane, new THREE.Vector3()) ?? target;
      }
      if (place(target, d).every((q) => q.z < 1 && Math.abs(q.x) <= FIT && Math.abs(q.y) <= FIT)) break;
    }
    flyTo(probeCam.position.clone(), target, ms);
  }
  const pose = (v, ms) => backOff(home.pts, home.target, v === 'top' ? new THREE.Vector3(0, -0.001, 1).normalize() : home.dir, 0.88, 4, ms);

  // Image planes of the frusta, faded so they do not cover framed actors.
  function fade(on) {
    for (const f of frusta.values()) {
      f.mat.transparent = on;
      f.mat.opacity = on ? PLANE_FADE : 1;
      f.mat.depthWrite = !on;
      f.mat.needsUpdate = true;
    }
  }

  const ray = new THREE.Raycaster(), floorPlane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
  function aim(e) {
    const r = canvas.getBoundingClientRect();
    ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), active());
  }
  function floorAt(e) {
    aim(e);
    const p = ray.ray.intersectPlane(floorPlane, new THREE.Vector3());
    return p && [p.x, p.y];
  }
  let down = null;
  const shown = (o) => !o || (o.visible && shown(o.parent));
  canvas.addEventListener('pointerdown', (e) => (down = [e.clientX, e.clientY]));
  canvas.addEventListener('pointerup', (e) => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
    if (zone.active) return zone.add(floorAt(e));
    aim(e);
    const hit = ray.intersectObjects([...people.pickables, ...lifts.pickables], false).find((h) => shown(h.object.parent));
    for (const fn of pickFns) fn(hit ? hit.object.userData.entity_id : null);
  });
  canvas.addEventListener('pointermove', (e) => zone.active && zone.hover(floorAt(e)));
  // a drag takes the camera over from a running fly
  canvas.addEventListener('pointermove', (e) => down && e.buttons && Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4 && (tween = null));
  canvas.addEventListener('dblclick', () => zone.active && zone.close());

  renderer.setAnimationLoop((now) => {
    if (tween) {
      const k = Math.min(1, (now - tween.t0) / tween.ms), e = k * k * (3 - 2 * k);
      free.position.lerpVectors(tween.p0, tween.p1, e);
      controls.target.lerpVectors(tween.q0, tween.q1, e);
      if (k >= 1) tween = null;
    }
    controls.update();
    hazards.animate(now);
    const cam = active();
    if (measure && shown(measureGroup)) stretchMeasure(cam);
    renderer.render(scene, cam);
    labels.render(scene, cam);
    hideOffView(cam);
    people.declutter(cam.position);
    placeTags();
  });

  // CSS2DRenderer hides only labels behind the camera; this also hides those whose anchor projects off the canvas.
  const anchor = new THREE.Vector3();
  function hideOffView(cam) {
    scene.traverseVisible((o) => {
      if (!o.isCSS2DObject || o.element.style.display === 'none') return;
      anchor.setFromMatrixPosition(o.matrixWorld).project(cam);
      if (Math.abs(anchor.x) > 1 || Math.abs(anchor.y) > 1) o.element.style.display = 'none';
    });
  }

  // Shifts each Model tag (CSS translate, on top of CSS2DRenderer's transform) to the first spot around its anchor that
  // overlaps no other label, person ring, forklift box or earlier tag; nearer spots and "above" come first.
  function placeTags() {
    const tags = cosmos.tags.filter((o) => o.element.style.display !== 'none');
    if (!tags.length) return;
    const taken = [...labels.domElement.querySelectorAll('.wh-lbl:not(.lbl-model):not(.lbl-mline):not(.wh-hid), .wh-pin')]
      .filter((e) => e.getClientRects().length).map((e) => e.getBoundingClientRect());
    for (const [x, y] of Object.values(people.drawn())) {
      const q = stage.project(Array.from({ length: 8 }, (_, i) => [x + RING_R * Math.cos((i * Math.PI) / 4), y + RING_R * Math.sin((i * Math.PI) / 4), 0]));
      const xs = q.map((p) => p[0]), ys = q.map((p) => p[1]);
      taken.push({ left: Math.min(...xs), right: Math.max(...xs), top: Math.min(...ys), bottom: Math.max(...ys) });
    }
    for (const c of lifts.corners()) {
      const q = stage.project(c), xs = q.map((p) => p[0]), ys = q.map((p) => p[1]);
      taken.push({ left: Math.min(...xs), right: Math.max(...xs), top: Math.min(...ys), bottom: Math.max(...ys) });
    }
    const overlaps = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
    for (const o of tags) {
      const el = o.element, r = el.getBoundingClientRect(), [sx, sy] = o.userData.shift ?? [0, 0];
      const w = r.width, h = r.height, cx = r.left + w / 2 - sx, cy = r.top + h / 2 - sy;
      const at = ([dx, dy]) => ({ left: cx + dx - w / 2, right: cx + dx + w / 2, top: cy + dy - h / 2, bottom: cy + dy + h / 2 });
      const spots = [];
      for (let d = 6; d <= 300; d += 12) {
        const X = w / 2 + d, Y = h / 2 + d;
        spots.push([0, -Y], [X, 0], [-X, 0], [0, Y], [X, -Y], [-X, -Y], [X, Y], [-X, Y]);
      }
      const shift = spots.find((s) => !taken.some((b) => overlaps(at(s), b))) ?? spots[0];
      o.userData.shift = shift;
      el.style.translate = `${shift[0]}px ${shift[1]}px`;
      taken.push(at(shift));
    }
  }

  // Moves the distance line and its label onto the actors as drawn at t; hidden outside the moment or when one is not drawn.
  function placeMeasure(t) {
    const at = { ...people.drawn(), ...lifts.drawn() }, [a, b] = measure.ids.map((id) => at[id]);
    measureGroup.visible = t >= measure.t0 && t < measure.t1 && !!a && !!b;
    if (!measureGroup.visible) return;
    measureEnds = [a, b];
    const [line, tag] = measureGroup.children;
    line.position.set(a[0], a[1], 0.02);
    tag.position.set((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, 0.02);
  }

  // The line is a CSS element pivoted at end a, so it keeps a pixel width (3 px, its σ band, the hover glow): it is
  // stretched and turned to end b's projection each frame.
  function stretchMeasure(cam) {
    const { width: W, height: H } = labels.getSize();
    const [p, q] = measureEnds.map(([x, y]) => new THREE.Vector3(x, y, 0.02).project(cam)).map((v) => [((v.x + 1) / 2) * W, ((1 - v.y) / 2) * H]);
    const line = measureGroup.children[0];
    line.element.style.width = `${Math.hypot(q[0] - p[0], q[1] - p[1])}px`;
    line.rotation2D = -Math.atan2(q[1] - p[1], q[0] - p[0]);
  }

  function mark() {
    const ids = focusIds ?? selectedIds, ask = askable && !focusIds;
    people.highlight(ids, ask);
    lifts.highlight(ids, ask);
  }

  const stage = {
    // The home view frames the skeleton, forklift and tracks set before it (the floor and cameras when there are none).
    loadManifest(m) {
      mf = m;
      buildGround(m.ground);
      frame(m);
      ground.visible = blocks.visible = true;
      kit.clear(frustaGroup);
      frusta.clear();
      for (const c of m.cams) frusta.set(c.id, buildFrustum(c));
      view = 'free';
      focusIds = back = null;
      layout();
      pose('free', 0);
    },
    setPoints(buf) {
      cloud.clear();
      blocks.clear();
      if (!buf) return;
      const pts = parsePoints(buf);
      cloud.add(pointsObject(pts, mf.ground));
      blocks.add(blocksObject(pts, mf.ground, color('--sunk'), color('--rule')));
    },
    // id: people | forklift | measures | blind | cloud | frusta
    setLayer(id, on) {
      layerGroups[id].visible = on;
    },
    setSkeleton: (json) => people.set((skeleton = json)),
    setForklift: (json) => lifts.set((forklift = json)),
    setVisibility: (json) => cells.set(json),
    setTracks(json) {
      tracks = new Map(json.tracks.map((tr) => [tr.entity_id, tr]));
    },
    setTime(t) {
      people.update(t);
      lifts.update(t);
      cells.update(t);
      if (measure) placeMeasure(t);
    },
    // m: {ids: [a, b], t0, t1, text, sub} of a measured ground distance, or null to clear; t: the clock now.
    setMeasure(m, t) {
      kit.clear(measureGroup);
      measure = m;
      if (!m) return;
      const line = kit.label('<i class="wh-mband"></i><i class="wh-mline"></i>', 'lbl-mline');
      line.center.set(0, 0.5);
      measureGroup.add(line, kit.label(`<span class="wh-mval">${esc(m.text)}</span><span class="wh-msub">${esc(m.sub)}</span>`, 'lbl-measure'));
      placeMeasure(t);
    },
    // Lights the measure line (its evidence is hovered).
    setMeasureHot(on) {
      measureGroup.children[0]?.element.classList.toggle('hot', on);
    },
    // frames: moment.cams [{id, frame_url|null}] of the current second.
    setFrustumThumbs(frames) {
      for (const { id, frame_url } of frames) {
        const f = frusta.get(id);
        f.url = frame_url;
        if (!frame_url) {
          f.tex = null;
          paint(f);
        } else {
          texture(frame_url).then((tx) => {
            if (f.url !== frame_url) return;
            f.tex = tx;
            paint(f);
          });
        }
      }
    },
    // Draws the frustum edges of camera id in --ink at full opacity, the others as built.
    setActiveCam(id) {
      activeCam = id;
      for (const f of frusta.values()) {
        f.edge.color = color(f.c.id === id ? '--ink' : '--ink-2');
        f.edge.opacity = f.c.id === id ? 1 : 0.75;
      }
    },
    setHazards: (zones, fresh) => hazards.set(zones, fresh),
    setModelResults: (results) => cosmos.set(results),
    setAnswerMode(on) {
      const groups = [cosmos.group, pinGroup, frustaGroup, trackGroup, hazards.group];
      if (on && !answerLayers) answerLayers = new Map(groups.map((g) => [g, g.visible]));
      for (const g of groups) g.visible = on ? false : answerLayers?.get(g) ?? g.visible;
      if (!on) answerLayers = null;
    },
    // pins: [{id, xy, text, t_ms, on, kind:'event'|'label'}]
    setPins(pins) {
      kit.clear(pinGroup);
      for (const p of pins) {
        const stem = kit.line([[...p.xy, 0], [...p.xy, 2.6]], new THREE.LineBasicMaterial({ color: color(p.kind === 'event' ? '--danger' : '--ink-2') }));
        const tag = kit.label(`<button class="wh-pin${p.on ? ' on' : ''}" data-pin="${esc(p.id)}" data-t="${p.t_ms}">${p.kind === 'event' ? '<b>Worth a look</b>' : ''}${esc(p.text)}</button>`, 'lbl-pin');
        tag.position.set(...p.xy, 2.6);
        pinGroup.add(stem, tag);
      }
    },
    // Track polyline of one entity in [t0, t1); null clears.
    showTrack(id, t0, t1) {
      kit.clear(trackGroup);
      const pts = tracks.get(id)?.pts.filter(([t]) => t >= t0 && t < t1) ?? [];
      if (pts.length >= 2) trackGroup.add(kit.line(pts.map(([, x, y]) => [x, y, 0.02]), new THREE.LineBasicMaterial({ color: color('--ink'), transparent: true, opacity: 0.6 })));
      return pts.length;
    },
    // World [x, y] of an entity at t from tracks.json; null when it has no track.
    position(id, t) {
      const tr = tracks.get(id);
      if (!tr) return null;
      const s = tr.pts[kit.nearest(tr.pts.map((q) => q[0]), t)];
      return [s[1], s[2]];
    },
    highlight(ids, ask) {
      selectedIds = ids;
      askable = ask;
      mark();
    },
    // ids: entity_ids an evidence cites, at full strength with the others dimmed; null shows the selection again.
    setFocus(ids) {
      focusIds = ids;
      mark();
    },
    // Flies the free camera to frame actors at world points [[x, y], ...] from FIT_PITCH above its current heading, at least
    // FIT_DIST away and far enough that they fill no more than FIT_NDC of the canvas, and fades the frustum image planes;
    // unfit() returns to the view before the first fit.
    fit(pts, ms = 800) {
      back ??= { view, pos: free.position.clone(), target: controls.target.clone() };
      // heading of the pose being flown to; before the first pose lands the camera has none
      const flat = (tween ? tween.p1.clone().sub(tween.q1) : free.position.clone().sub(controls.target)).setZ(0);
      if (flat.length() < 1e-6) flat.copy(home.dir).setZ(0);
      const dir = flat.normalize().multiplyScalar(Math.cos(FIT_PITCH)).setZ(Math.sin(FIT_PITCH));
      if (view !== 'free') stage.setView('free');
      const c = [0, 1].map((k) => pts.reduce((s, p) => s + p[k], 0) / pts.length);
      backOff(pts.flatMap(([x, y]) => [new THREE.Vector3(x, y, 0), new THREE.Vector3(x, y, ACTOR_H)]), new THREE.Vector3(c[0], c[1], 0), dir, FIT_NDC, FIT_DIST, ms);
      fade(true);
    },
    unfit() {
      if (!back) return;
      const b = back;
      back = null;
      fade(false);
      if (b.view !== 'free') stage.setView(b.view);
      else flyTo(b.pos, b.target, 600);
    },
    // Returns the view actually shown; unknown views leave it unchanged.
    setView(v) {
      const cam = camOf(v);
      if (!cam && v !== 'free' && v !== 'top') return view;
      view = v;
      controls.enabled = !cam;
      for (const f of frusta.values()) f.node.visible = f.c !== cam;
      // in a camera view its frame is the floor and the structure
      ground.visible = blocks.visible = !cam;
      lifts.solid(!cam);
      if (cam) {
        const [fx, fy, cx, cy] = cam.K;
        // CONTRACT §3 three.js rule; no flip matrix.
        real.matrixWorld.fromArray(cam.cam2world);
        real.matrix.copy(real.matrixWorld);
        real.matrixWorldInverse.copy(real.matrixWorld).invert();
        real.fov = cam.fov_deg;
        real.aspect = (cam.w * fy) / (cam.h * fx);
        real.setViewOffset(cam.w, cam.h, cam.w / 2 - cx, cam.h / 2 - cy, cam.w, cam.h);
        scene.background = frusta.get(cam.id).tex ?? color('--sunk');
      } else {
        scene.background = null;
      }
      layout();
      if (!cam) pose(v, 600);
      return view;
    },
    get view() {
      return view;
    },
    // target: [x, y] world point; flies the free camera there keeping its direction.
    focus(target, ms = 800) {
      if (view !== 'free') stage.setView('free');
      const to = new THREE.Vector3(target[0], target[1], 0);
      const dir = free.position.clone().sub(controls.target).normalize();
      flyTo(to.clone().addScaledVector(dir, FOCUS_DIST), to, ms);
      return target;
    },
    onPick(fn) {
      pickFns.add(fn);
      return () => pickFns.delete(fn);
    },
    // on: start drawing, resolves to the polygon (null if cancelled); off: cancel and clear the drawn zone.
    drawZone(on) {
      if (on) return zone.start();
      zone.cancel();
      return null;
    },
    // Client pixel positions of world points under the active camera (e2e alignment checks).
    project(points) {
      const cam = active();
      cam.updateMatrixWorld();
      const r = canvas.getBoundingClientRect();
      return points.map((p) => {
        const v = new THREE.Vector3(...p).project(cam);
        return [r.left + ((v.x + 1) / 2) * r.width, r.top + ((1 - v.y) / 2) * r.height];
      });
    },
    probe: () => ({
      view,
      thumbs: Object.fromEntries([...frusta].map(([id, f]) => [id, f.tex?.image?.src ?? null])),
      single: people.single(),
      entities: { ...people.drawn(), ...lifts.drawn() },
      highlighted: [...selectedIds],
      dimmed: [...people.dimmed(), ...lifts.dimmed()],
      measure: measure && measureGroup.visible ? measureEnds : null,
      blind_cells: cells.blindCount(),
      blocks: blocks.children[0]?.count ?? 0,
      cloud: cloud.visible && cloud.children.length > 0,
      floor: ground.visible,
      camera: (() => {
        const d = free.position.clone().sub(controls.target), deg = THREE.MathUtils.radToDeg;
        return { dist: d.length(), pitch: deg(Math.asin(d.z / d.length())), heading: deg(Math.atan2(d.y, d.x)), target: [controls.target.x, controls.target.y] };
      })(),
      planes: [...frusta.values()].map((f) => f.mat.opacity),
      active_cam: activeCam,
    }),
    // Slow turntable of the free camera (home background); controls.update() already runs every frame.
    orbit(on) {
      controls.autoRotate = on && !still.matches;
      controls.autoRotateSpeed = 0.3;
    },
    dispose() {
      renderer.setAnimationLoop(null);
      ro.disconnect();
      controls.dispose();
      renderer.dispose();
      labels.domElement.remove();
    },
  };
  return stage;
}
