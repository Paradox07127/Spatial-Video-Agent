// Playback availability comes from the media element, independently of timeline coverage.
// Hide stale decoded pixels and annotations while loading, buffering or failed; retry keeps the same source/clock.
export function createMediaStatus(video, { host, onUnavailable = () => {}, onReady = () => {} }) {
  const status = document.createElement('div');
  status.className = 'media-status';
  status.setAttribute('role', 'status');
  host.append(status);
  let state = null, alive = true;
  const labels = { loading: 'Loading video…', waiting: 'Waiting for video…', error: 'Video unavailable' };
  function set(next) {
    if (!alive || state === next) return;
    state = next;
    host.dataset.mediaState = state;
    host.classList.toggle('media-unavailable', state !== 'ready');
    status.hidden = state === 'ready';
    status.replaceChildren();
    if (state === 'ready') return onReady();
    status.append(document.createTextNode(labels[state]));
    if (state === 'error') {
      const retry = document.createElement('button');
      retry.type = 'button'; retry.className = 'media-retry ghost'; retry.textContent = 'Retry video';
      retry.onclick = () => { set('loading'); video.load(); };
      status.append(retry);
    }
    onUnavailable(state);
  }
  function present() {
    if (!alive || video.error || video.readyState < 2 || video.seeking) return false;
    set('ready');
    return true;
  }
  const events = {
    loadstart: () => set('loading'),
    seeking: () => set('loading'),
    waiting: () => set('waiting'),
    stalled: () => set('waiting'),
    error: () => set('error'),
    loadeddata: present,
    canplay: present,
    playing: present,
    seeked: present,
  };
  for (const [event, handler] of Object.entries(events)) video.addEventListener(event, handler);
  set(video.error ? 'error' : video.readyState >= 2 && !video.seeking ? 'ready' : 'loading');
  return {
    get available() { return alive && state === 'ready' && !video.error; },
    present,
    dispose() {
      alive = false;
      for (const [event, handler] of Object.entries(events)) video.removeEventListener(event, handler);
      status.remove();
    },
  };
}
