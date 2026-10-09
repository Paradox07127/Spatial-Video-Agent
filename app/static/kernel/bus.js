// Publish/subscribe shared by the shell and the page modules.
const subs = new Map();

export const bus = {
  on(type, fn) {
    if (!subs.has(type)) subs.set(type, new Set());
    subs.get(type).add(fn);
  },
  off(type, fn) {
    subs.get(type)?.delete(fn);
  },
  emit(type, payload) {
    for (const fn of [...(subs.get(type) || [])]) fn(payload);
  },
};
