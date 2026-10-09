// Minimal page module for the kernel e2e (route #/test): same interface as warehouse/fleet pages; records every call in window.__pageCalls.
const calls = (window.__pageCalls = []);

export async function mount(root, ctx) {
  calls.push(['mount', ctx.route]);
  root.innerHTML = '<p class="stage-msg">kernel test page</p>';
  ctx.clock.setRange(0, 10000);
  const off = ctx.selection.onChange(({ ids, source }) => calls.push(['selection', [...ids], source]));
  return () => {
    off();
    calls.push(['dispose']);
  };
}

export const applyHits = (moments) => calls.push(['applyHits', moments]);
export const applyUI = (cmd) => calls.push(['applyUI', cmd]);
export const applyModelResults = (results) => calls.push(['applyModelResults', results]);
export const loadScene = (key) => calls.push(['loadScene', key]);
