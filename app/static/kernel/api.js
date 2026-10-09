// fetch wrapper. Paths resolve against the page's directory, so the same code works at / and behind PUBLIC_BASE_PATH (/app/).
export const api = {
  url: (path) => new URL(path.replace(/^\/+/, ''), document.baseURI).href,
  asset: (rel) => api.url(`api/assets/${rel}`),

  async get(path) {
    const r = await fetch(api.url(path));
    if (!r.ok) throw new Error(`${r.status} ${path}`);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(api.url(path), { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`${r.status} ${path}`);
    return r.json();
  },
};
