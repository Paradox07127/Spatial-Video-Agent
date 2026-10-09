// Selected entity_ids shared by every view; source is 'user' | 'agent' | 'search'.
import { bus } from './bus.js';

export const selection = {
  ids: new Set(),
  source: null,

  set(ids, { source }) {
    selection.ids = new Set(ids);
    selection.source = source;
    bus.emit('selection:change', { ids: selection.ids, source });
  },
  clear() {
    selection.set([], { source: 'user' });
  },
  onChange(fn) {
    bus.on('selection:change', fn);
    return () => bus.off('selection:change', fn);
  },
};
