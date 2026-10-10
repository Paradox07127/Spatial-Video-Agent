"""Cached, bounded remote asset inventory validation for application readiness.

Checks every indexed manifest and its direct file/directory/media references. A
nonempty directory prefix is existence evidence, not exhaustive file integrity;
this does not decode media, inspect every directory child, or verify model and
on-demand sidecar availability.
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
import hashlib
import json
import os
from pathlib import PurePosixPath
from threading import Lock, Thread
import time

CONCURRENCY = 4
CHECK_SECONDS = 60
SUCCESS_TTL = 300
FAILURE_TTL = 15
MANIFEST_LIMIT = 2*1024*1024
SCOPE = 'indexed manifests; direct nonempty files/media; nonempty directory prefixes; fleet map assets'


class ValidationFailure(RuntimeError):
    pass


def readiness_client(settings):
    """Independent short-timeout client; never change the Store's serving client."""
    import boto3
    from botocore.config import Config
    if not settings.s3_endpoint or not settings.asset_bucket:
        raise ValidationFailure('remote_asset_configuration_missing')
    if not settings.access_key or not settings.secret_key:
        raise ValidationFailure('remote_asset_credentials_missing')
    return boto3.client('s3',endpoint_url=settings.s3_endpoint,
                        aws_access_key_id=settings.access_key,aws_secret_access_key=settings.secret_key,
                        verify=settings.s3_verify,
                        config=Config(signature_version='s3v4',connect_timeout=1,read_timeout=2,
                                      retries={'max_attempts':0},max_pool_connections=CONCURRENCY))


def references(scenes,bucket):
    if not scenes or not bucket:
        raise ValidationFailure('remote_asset_configuration_missing')
    tasks = {}
    def add(uri,scene=None,prefix=False,manifest=None):
        if uri.startswith('s3://'):
            parts = uri[5:].split('/',1)
            if len(parts)!=2 or not all(parts):
                raise ValidationFailure('invalid_asset_reference')
            target,key = parts
        else:
            path = PurePosixPath(uri)
            if path.is_absolute() or '..' in path.parts:
                raise ValidationFailure('invalid_asset_reference')
            target,key = bucket,'sva/'+(scene+'/' if scene else '')+uri
        if prefix:
            key = key.rstrip('/')+'/'
        if (target,key) not in tasks or manifest is not None:
            tasks[(target,key)] = ('manifest' if manifest is not None else 'prefix' if prefix else 'file',target,key,manifest)
    for scene in scenes:
        key = scene['scene_key']
        add('manifest.json',key,manifest=scene)
        for name,rel in scene.get('assets',{}).items():
            if not rel or scene.get('workspace')=='street' and name=='dets_dir':
                continue
            add(rel,key,prefix=name.endswith('_dir'))
        for camera in scene.get('cams',[]):
            if camera.get('video'):
                add(camera['video'],key)
            if camera.get('thumbs'):
                add(camera['thumbs'],key,prefix=True)
            for chunk in camera.get('chunks',[]):
                add(chunk['uri'],key)
    if any(s.get('workspace')=='fleet' for s in scenes):
        for name in ('style.json','basemap.pmtiles','buildings.pmtiles'):
            add('_map/'+name)
    return list(tasks.values())


def validate_reference(client,task,end):
    def check_budget():
        if time.monotonic()>=end:
            raise ValidationFailure('remote_asset_validation_timeout')
    check_budget()
    kind,bucket,key,manifest = task
    if kind=='manifest':
        data = bytearray()
        with closing(client.get_object(Bucket=bucket,Key=key)['Body']) as body:
            while chunk := body.read(65536):
                check_budget()
                data.extend(chunk)
                if len(data)>MANIFEST_LIMIT:
                    raise ValidationFailure('remote_manifest_too_large')
        if not data or json.loads(data)!=manifest:
            raise ValidationFailure('remote_manifest_mismatch')
    elif kind=='file':
        if client.head_object(Bucket=bucket,Key=key).get('ContentLength',0)<=0:
            raise ValidationFailure('remote_asset_empty')
    else:
        token = None
        while True:
            check_budget()
            response = client.list_objects_v2(Bucket=bucket,Prefix=key,MaxKeys=20,
                                              **({'ContinuationToken':token} if token else {}))
            if any(o.get('Size',0)>0 for o in response.get('Contents',[])):
                break
            token = response.get('NextContinuationToken') if response.get('IsTruncated') else None
            if not token:
                raise ValidationFailure('remote_asset_prefix_empty')
    check_budget()


class RemoteAssetReadiness:
    def __init__(self):
        self.lock = Lock()
        self.identity = None
        self.state = 'waiting'
        self.finished = 0
        self.checked = self.total = 0
        self.reason = None
        self.worker = None

    def snapshot(self):
        return {'state':self.state,'checked':self.checked,'total':self.total,'scope':SCOPE,
                **({'reason':self.reason} if self.reason else {})}

    def status(self,settings,scenes):
        raw = json.dumps([scenes,settings.s3_endpoint,settings.asset_bucket,os.environ.get('EVIDENCE_REVISION','')],
                         sort_keys=True,separators=(',',':'))
        identity = hashlib.sha256(raw.encode()).hexdigest()
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                # A changed release never inherits a previous successful validation.
                if identity!=self.identity:
                    return {'state':'waiting','checked':0,'total':0,'scope':SCOPE,'reason':'release_changed'}
                return self.snapshot()
            ttl = SUCCESS_TTL if self.state=='ready' else FAILURE_TTL
            if identity==self.identity and time.monotonic()-self.finished<ttl:
                return self.snapshot()
            self.identity,self.state,self.reason = identity,'validating',None
            self.checked = self.total = 0
            initial = self.snapshot()
            self.worker = Thread(target=self._validate,args=(settings,scenes),name='remote-asset-readiness',daemon=True)
            self.worker.start()
            return initial

    def _validate(self,settings,scenes):
        client = None
        pool = ThreadPoolExecutor(max_workers=CONCURRENCY,thread_name_prefix='remote-asset-check')
        pending = set()
        try:
            tasks = references(scenes,settings.asset_bucket)
            with self.lock:
                self.total = len(tasks)
            end = time.monotonic()+CHECK_SECONDS
            client = readiness_client(settings)
            todo = iter(tasks)
            for _ in range(CONCURRENCY):
                task = next(todo,None)
                if task is not None:
                    pending.add(pool.submit(validate_reference,client,task,end))
            while pending:
                done,pending = wait(pending,timeout=max(0,end-time.monotonic()),return_when=FIRST_COMPLETED)
                if not done:
                    raise ValidationFailure('remote_asset_validation_timeout')
                for future in done:
                    future.result()
                    with self.lock:
                        self.checked+=1
                    task = next(todo,None)
                    if task is not None:
                        pending.add(pool.submit(validate_reference,client,task,end))
            with self.lock:
                self.state = 'ready'
        except Exception as exc:
            with self.lock:
                self.state = 'failed'
                # Endpoint names, credentials, keys and SDK errors stay out of health responses.
                self.reason = str(exc) if isinstance(exc,ValidationFailure) else 'remote_asset_unavailable'
        finally:
            for future in pending:
                future.cancel()
            pool.shutdown(wait=True,cancel_futures=True)
            if client is not None and hasattr(client,'close'):
                client.close()
            with self.lock:
                self.finished = time.monotonic()
