// Floor cells from visibility.json (encoding u8_cell_cam: byte (iy·nx + ix)·n_cams + cam, 1 = visible).
// A cell no camera sees is grey with hatching; cells seen by fewer cameras get a stronger tint.
import * as THREE from 'three';

const PX = 12; // canvas pixels per cell
const TINT = [0, 0.13, 0.06, 0]; // alpha by number of cameras that see the cell (3+ = none)

export function createBlindCells(kit) {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false }));
  mesh.renderOrder = 1;
  const group = new THREE.Group();
  group.add(mesh);
  let vis = null, secs = [], shown = -1, blind = 0;

  const css = getComputedStyle(document.documentElement);
  const tile = document.createElement('canvas');
  tile.width = tile.height = 8;
  const tc = tile.getContext('2d');
  tc.fillStyle = css.getPropertyValue('--thread');
  tc.globalAlpha = 0.45;
  tc.fillRect(0, 0, 8, 8);
  tc.globalAlpha = 0.9;
  tc.strokeStyle = css.getPropertyValue('--ink-2');
  tc.lineWidth = 1.2;
  tc.beginPath();
  tc.moveTo(0, 8);
  tc.lineTo(8, 0);
  tc.stroke();
  const hatch = ctx.createPattern(tile, 'repeat');
  const ink2 = new THREE.Color(css.getPropertyValue('--ink-2').trim());
  const tint = (a) => `rgb(${ink2.r * 255} ${ink2.g * 255} ${ink2.b * 255} / ${a})`;

  function draw(s) {
    const { nx, ny, cam_bit_order } = vis, nc = cam_bit_order.length, bytes = secs[s];
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    blind = 0;
    for (let iy = 0; iy < ny; iy++) {
      for (let ix = 0; ix < nx; ix++) {
        let seen = 0;
        for (let c = 0; c < nc; c++) seen += bytes[(iy * nx + ix) * nc + c] === 1;
        if (seen >= TINT.length - 1) continue;
        if (!seen) blind++;
        ctx.fillStyle = seen ? tint(TINT[seen]) : hatch;
        // Canvas row 0 is the texture top, i.e. the largest y.
        ctx.fillRect(ix * PX, (ny - 1 - iy) * PX, PX, PX);
      }
    }
    tex.needsUpdate = true;
  }

  return {
    group,
    set(json) {
      vis = json;
      group.visible = !!json;
      shown = -1;
      if (!json) return;
      secs = json.per_sec.map((b64) => Uint8Array.from(atob(b64), (ch) => ch.charCodeAt(0)));
      canvas.width = json.nx * PX;
      canvas.height = json.ny * PX;
      tex.dispose(); // the GPU copy has the old canvas size
      const W = json.nx * json.cell_m, H = json.ny * json.cell_m;
      mesh.scale.set(W, H, 1);
      mesh.position.set(json.origin[0] + W / 2, json.origin[1] + H / 2, 0.004);
    },
    update(t) {
      if (!vis) return;
      const s = Math.min(Math.max(Math.floor(t / 1000) - vis.t0_s, 0), secs.length - 1);
      if (s !== shown) draw((shown = s));
    },
    blindCount: () => blind,
  };
}
