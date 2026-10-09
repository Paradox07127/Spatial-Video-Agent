// points.bin (CONTRACT §4.2), little-endian: 32 B header "SVAP", u32 N, f32 bbox[6]; then f32 xyz×N, u8 rgb×N.
// Drawn two ways: block massing of the static structure, and the raw cloud (a layer that is off by default).
import * as THREE from 'three';

const MARGIN = 1; // m of cloud kept around the floor; depth samples past it are not drawn
// Opacity at the floor centre and at the farthest corner; the 8-bit log depth bands into rings that widen with range.
const ALPHA_NEAR = 0.6, ALPHA_FAR = 0.12;

export function parsePoints(buf) {
  const n = new DataView(buf).getUint32(4, true);
  return { n, bbox: [...new Float32Array(buf, 8, 6)], xyz: new Float32Array(buf, 32, 3 * n), rgb: new Uint8Array(buf, 32 + 12 * n, 3 * n) };
}

// Cells of the manifest ground grid (the same cell_m cells as visibility.json) are the block footprints.
const BLOCK_Z = 0.5; // m: a cell whose highest point is lower stays floor (floor depth noise, pallets)
const BLOCK_PTS = 5; // points above BLOCK_Z a cell needs to become a block; fewer are stray depth samples

// Static structure as massing: every ground cell with enough points above BLOCK_Z becomes a box up to its highest point.
// side, top: THREE.Color of the walls and the roof of the boxes.
export function blocksObject({ n, xyz }, ground, side, top) {
  const { origin: [x0, y0], cell_m, nx, ny } = ground;
  const zmax = new Float32Array(nx * ny), count = new Uint32Array(nx * ny);
  for (let i = 0; i < n; i++) {
    const ix = Math.floor((xyz[3 * i] - x0) / cell_m), iy = Math.floor((xyz[3 * i + 1] - y0) / cell_m), z = xyz[3 * i + 2];
    if (ix < 0 || ix >= nx || iy < 0 || iy >= ny || z <= BLOCK_Z) continue;
    const k = iy * nx + ix;
    count[k]++;
    zmax[k] = Math.max(zmax[k], z);
  }
  const cells = [...count.keys()].filter((k) => count[k] >= BLOCK_PTS);
  const sideMat = new THREE.MeshBasicMaterial({ color: side }), topMat = new THREE.MeshBasicMaterial({ color: top });
  // BoxGeometry face order: +x, -x, +y, -y, +z (roof), -z
  const mesh = new THREE.InstancedMesh(new THREE.BoxGeometry(cell_m, cell_m, 1).translate(0, 0, 0.5), [sideMat, sideMat, sideMat, sideMat, topMat, sideMat], cells.length);
  const m = new THREE.Matrix4();
  cells.forEach((k, i) => {
    m.makeScale(1, 1, zmax[k]).setPosition(x0 + ((k % nx) + 0.5) * cell_m, y0 + (Math.floor(k / nx) + 0.5) * cell_m, 0);
    mesh.setMatrixAt(i, m);
  });
  return mesh;
}

// ground: manifest.ground; draws only the points over the floor (plus MARGIN), fading away from its centre.
export function pointsObject({ n, xyz, rgb }, ground) {
  const { origin: [x0, y0], cell_m, nx, ny } = ground, x1 = x0 + nx * cell_m, y1 = y0 + ny * cell_m;
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, rmax = Math.hypot(x1 - cx, y1 - cy);
  const pos = new Float32Array(3 * n), col = new Uint8Array(4 * n);
  let k = 0;
  for (let i = 0; i < n; i++) {
    const x = xyz[3 * i], y = xyz[3 * i + 1];
    if (x < x0 - MARGIN || x > x1 + MARGIN || y < y0 - MARGIN || y > y1 + MARGIN) continue;
    pos.set(xyz.subarray(3 * i, 3 * i + 3), 3 * k);
    col.set(rgb.subarray(3 * i, 3 * i + 3), 4 * k);
    const f = Math.min(1, Math.hypot(x - cx, y - cy) / rmax);
    col[4 * k + 3] = 255 * (ALPHA_NEAR + (ALPHA_FAR - ALPHA_NEAR) * f * f);
    k++;
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos.subarray(0, 3 * k), 3));
  g.setAttribute('color', new THREE.BufferAttribute(col.subarray(0, 4 * k), 4, true));
  return new THREE.Points(g, new THREE.PointsMaterial({ size: 0.035, vertexColors: true, transparent: true, depthWrite: false }));
}
