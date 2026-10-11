"""Cosmos Embed1 text queries (256 dimensions)."""
import math
import os
from urllib.parse import urlsplit

import httpx

from app.core.deadline import remaining
from app.settings import Settings


def validate_vector(vector):
    if len(vector) != 256 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector):
        raise ValueError('Embed1 must return 256 finite numbers')
    return [float(x) for x in vector]


def embed_text(q: str, *, client=None, deadline=None) -> list[float]:
    s = Settings()
    if not s.embed_url:
        raise KeyError('EMBED_URL')
    url = s.embed_url.rstrip('/')
    # EMBED_PROTOCOL: 'vh' (/embed/text) or 'openai' (/v1/embeddings); unset keeps port 8103 as the VH service
    protocol = os.environ.get('EMBED_PROTOCOL', '')
    if protocol not in ('', 'vh', 'openai'):
        raise ValueError('EMBED_PROTOCOL must be vh or openai')
    ws = protocol == 'vh' if protocol else urlsplit(url).port == 8103
    body = {'texts': [q]} if ws else {'input': q,
                                    'model': s.embed_model or 'nvidia/cosmos-embed1-224p',
                                    'request_type': 'query', 'encoding_format': 'float'}
    if ws:
        url += '/embed/text'
    else:
        if not url.endswith('/v1'):
            url += '/v1'
        url += '/embeddings'
    headers = {'Authorization': 'Bearer '+os.environ['GPU_BEARER_TOKEN']} if os.environ.get('GPU_BEARER_TOKEN') else {}
    def request(c):
        response = c.post(url, json=body, headers=headers, timeout=remaining(5,deadline))
        remaining(5,deadline)
        response.raise_for_status()
        data = response.json()
        return validate_vector(data['embeddings'][0] if ws else data['data'][0]['embedding'])
    if client is not None:
        return request(client)
    with httpx.Client(timeout=remaining(5,deadline)) as c:
        return request(c)
