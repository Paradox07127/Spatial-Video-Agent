"""Site VSS /search as a native comparison; upload-time filters remain distinct from scene time."""
from __future__ import annotations

import time

import httpx

from app.core.deadline import remaining, scope


class VSS:
    def __init__(self, settings, *, map_hit, client=None):
        self.settings = settings
        self.map_hit = map_hit
        self.client = client
        self.token = None

    def _send(self, method, path, *, authenticated=True, timeout=30, **kwargs):
        url = self.settings.vss_url.rstrip('/')
        if not url.endswith('/api/v1'):
            url += '/api/v1'
        headers = {'Authorization':'Bearer '+self.token} if authenticated else {}
        def request(client):
            response = client.request(method, url+path, headers=headers, timeout=remaining(timeout), **kwargs)
            response.raise_for_status()
            return response.json()
        if self.client is not None:
            return request(self.client)
        # /search always runs its LLM summary, which takes seconds
        with httpx.Client(timeout=timeout) as client:
            return request(client)

    def _post(self, path, body, *, authenticated=True, timeout=30):
        return self._send('POST', path, json=body, authenticated=authenticated, timeout=timeout)

    def login(self, *, timeout=30):
        result = self._post('/auth/login', {'username':self.settings.vss_username,
                                           'password':self.settings.vss_password}, authenticated=False, timeout=timeout)
        self.token = result['access_token']

    def _authorized(self, method, path, *, timeout=30, **kwargs):
        if self.token is None:
            self.login(timeout=timeout)
        try:
            return self._send(method, path, timeout=timeout, **kwargs)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 401:
                raise
            self.login(timeout=timeout)
            return self._send(method, path, timeout=timeout, **kwargs)

    def search(self, q, top_k, filters, *, timeout=30):
        # llm_top_n 0 is rejected with 422
        body = {'query':q,'top_k':top_k,'llm_top_n':1,**filters.get('time_filter', {'time_filter':'all'})}
        if filters.get('metadata_filters'):
            body['metadata_filters'] = filters['metadata_filters']
        # Login and a 401 retry share the search budget instead of restarting it.
        with scope(time.monotonic()+timeout):
            return self.parse_response(self._authorized('POST', '/search', json=body, timeout=timeout))

    def parse_response(self, response):
        """Every hit in VSS order; scene_key, cam_id and t_ms stay null when the site map does not place it."""
        hits = []
        for rank, hit in enumerate(response['results'], 1):
            key, cam, t_ms = self.map_hit(hit) or (None, None, None)
            hits.append({'rank':rank,'source':hit['source'],'scene_key':key,'cam_id':cam,'t_ms':t_ms,
                         'similarity':float(hit['similarity_score']),'caption':(hit.get('reasoning_content') or '')[:200]})
        return {'hits':hits,'sql_query':response.get('sql_query')}
