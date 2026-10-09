"""Cosmos Embed1 text queries (256 dimensions)."""
import math
import os
from urllib.parse import urlsplit

import httpx


def validate_vector(vector):
    if len(vector) != 256 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector):
        raise ValueError('Embed1 must return 256 finite numbers')
    return [float(x) for x in vector]


def embed_text(q: str, *, client=None) -> list[float]:
    url = os.environ['EMBED_URL'].rstrip('/')
    ws = urlsplit(url).port == 8103
    body = {'texts': [q]} if ws else {'input': q,
                                    'model': os.environ.get('EMBED_MODEL', 'nvidia/cosmos-embed1-224p'),
                                    'request_type': 'query', 'encoding_format': 'float'}
    if ws:
        url += '/embed/text'
    else:
        if not url.endswith('/v1'):
            url += '/v1'
        url += '/embeddings'
    headers = {'Authorization': 'Bearer '+os.environ['GPU_BEARER_TOKEN']} if os.environ.get('GPU_BEARER_TOKEN') else {}
    def request(c):
        response = c.post(url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        return validate_vector(data['embeddings'][0] if ws else data['data'][0]['embedding'])
    if client is not None:
        return request(client)
    with httpx.Client(timeout=5) as c:
        return request(c)
