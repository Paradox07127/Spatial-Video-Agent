import io
import time
from types import SimpleNamespace

import httpx
import pytest

from app.core import deadline, embed, street


def test_embed_timeout_consumes_remaining_budget(monkeypatch):
    monkeypatch.setenv('EMBED_URL','http://embed:8103')
    seen=[]
    def respond(req):
        seen.append(req.extensions['timeout']['read'])
        return httpx.Response(200,json={'embeddings':[[.1]*256]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with deadline.scope(time.monotonic()+.2):
            assert len(embed.embed_text('person',client=client))==256
        assert 0<seen[0]<=.2
        with pytest.raises(deadline.DeadlineExceeded):
            embed.embed_text('person',client=client,deadline=time.monotonic()-1)
    assert len(seen)==1


def test_relative_street_assets_work_without_local_mirror(tmp_path):
    bodies=[]
    gets=[]
    def get_object(**params):
        gets.append(params)
        body=io.BytesIO(b'asset')
        bodies.append(body)
        return {'Body':body}
    store=SimpleNamespace(asset_dir=None,runs_dir=tmp_path,settings=SimpleNamespace(asset_bucket='assets'),
                          s3=lambda:SimpleNamespace(get_object=get_object),
                          manifest=lambda key:{'workspace':'street','cams':[{'id':'cam','thumbs':'thumbs/cam'}]})
    path=street.local_chunk(store,'st-example',{'uri':'chunks/cam/0000.mp4'})
    assert path.read_bytes()==b'asset'
    thumb=street.thumb(store,'st-example','cam',1200)
    assert thumb.read_bytes()==b'asset'
    assert gets==[{'Bucket':'assets','Key':'sva/st-example/chunks/cam/0000.mp4'},
                  {'Bucket':'assets','Key':'sva/st-example/thumbs/cam/00001.jpg'}]
    assert all(body.closed for body in bodies)
    assert street.local_chunk(store,'st-example',{'uri':'chunks/cam/0000.mp4'})==path
    assert len(gets)==2


def test_missing_remote_street_chunk_is_missing_media_not_decode_failure(tmp_path):
    from botocore.exceptions import ClientError
    def missing(**params):
        raise ClientError({'Error':{'Code':'NoSuchKey','Message':'missing'}},'GetObject')
    store=SimpleNamespace(asset_dir=None,runs_dir=tmp_path,settings=SimpleNamespace(asset_bucket='assets'),
                          s3=lambda:SimpleNamespace(get_object=missing))
    with pytest.raises(FileNotFoundError,match='Media asset not found'):
        street.local_chunk(store,'st-example',{'uri':'chunks/cam/0000.mp4'})
    assert not list(tmp_path.rglob('*.mp4'))
