// ViewState = home | default | searching | hit | abstained (CONTRACT §7), plus the shell's layer state.
import { bus } from './bus.js';

const LAYERS_KEY = 'sva.layers';
function storedLayers() {
  try {
    const v = JSON.parse(localStorage.getItem(LAYERS_KEY));
    return Array.isArray(v) ? new Set(v) : null;
  } catch {
    return null;
  }
}

export const state = {
  view: 'default',
  panelTab: 'answer', // answer | evidence | steps | videos | trip
  stageMode: 'explore', // explore | evidence
  focusEid: null,
  hoverEid: null,
  agentCamera: true, // false once the user takes over the camera during a run
  layers: storedLayers() ?? new Set(),
  cameraLanesOpen: false,
  sourceTag: null, // {hash, workspace, scene_key, trip_id} the next question runs against
};

export function setState(s) {
  state.view = s;
  bus.emit('view:state', s);
}

export function setLayers(ids) {
  state.layers = new Set(ids);
  try {
    localStorage.setItem(LAYERS_KEY, JSON.stringify([...state.layers]));
  } catch {
    // storage may be unavailable (private mode, quota); the layers still apply for this session
  }
}
