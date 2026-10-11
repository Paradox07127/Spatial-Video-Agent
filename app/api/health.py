from pathlib import Path
import socket
import time
import os
import re
from threading import Lock
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
_remote_validator_lock = Lock()


def dependency_status(settings):
    """Cheap reachability checks, separate from asset readiness; never run GPU inference."""
    services = {}
    for name, field in [('vlm','vlm_url'), ('llm','llm_url'), ('embed','embed_url')]:
        url = getattr(settings, field, '')
        state = 'not_configured'
        if url:
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                    raise ValueError('Invalid model endpoint')
                with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme=='https' else 80)), timeout=.4):
                    state = 'reachable'
            except (OSError, ValueError):
                state = 'unavailable'
        services[name] = {'configured': bool(url), 'status': state}
    revision = os.environ.get('SVA_COMMIT','')
    return {'services': services, 'degraded': any(s['status']=='unavailable' for s in services.values()),
            'source_commit': revision if re.fullmatch(r'[0-9a-f]{40}',revision) else 'unknown',
            'check': 'tcp_reachability', 'inference_verified': False}


@router.get('/api/dependencies')
def dependencies(request: Request):
    state = request.app.state
    cached = getattr(state, 'dependency_status', None)
    if cached is None or time.monotonic()-cached[0] >= 5:
        cached = (time.monotonic(), dependency_status(state.settings))
        state.dependency_status = cached
    return cached[1]


def local_assets_ready(store, scenes):
    # Readiness must not make a HEAD call per file or recursively scan the mirror on each probe.
    # Only check indexed manifests and their direct required references in the local release.
    if not scenes or store.asset_dir is None:
        return False
    root = Path(store.asset_dir).resolve()

    def exists(path, directory=False):
        path = path.resolve()
        if not path.is_relative_to(root):
            return False
        return path.is_dir() if directory else path.is_file() and path.stat().st_size > 0

    try:
        for scene in scenes:
            base = root / scene['scene_key']
            if not exists(base / 'manifest.json'):
                return False
            for name, rel in scene.get('assets', {}).items():
                # L1 dets and thumbnails are generated from existing sidecars/chunks on demand.
                if scene.get('workspace') == 'street' and name == 'dets_dir':
                    continue
                if rel and not exists(base / rel, directory=name.endswith('_dir')):
                    return False
            for cam in scene.get('cams', []):
                if cam.get('video') and not exists(base / cam['video']):
                    return False
                for chunk in cam.get('chunks', []):
                    if not chunk['uri'].startswith('s3://') and not exists(base / chunk['uri']):
                        return False
        if any(scene.get('workspace') == 'fleet' for scene in scenes):
            return all(exists(root / '_map' / name) for name in ('style.json', 'basemap.pmtiles', 'buildings.pmtiles'))
        return True
    except (OSError, KeyError, TypeError, ValueError):
        return False


@router.get('/health')
def health(request: Request):
    store, settings = request.app.state.store, request.app.state.settings
    manifest = request.app.state.vendor_manifest
    try:
        libs_ready = all((settings.lib_dir / f['name']).is_file() and (settings.lib_dir / f['name']).stat().st_size > 0
                         for f in manifest['files'])
    except OSError:
        libs_ready = False
    asset_validation = None
    try:
        scenes = store.list_scenes()
        if store.asset_dir is None:
            from app.core.asset_readiness import RemoteAssetReadiness
            state = request.app.state
            # FastAPI sync health handlers may run concurrently in worker threads.
            if not hasattr(state,'remote_asset_readiness'):
                with _remote_validator_lock:
                    if not hasattr(state,'remote_asset_readiness'):
                        state.remote_asset_readiness = RemoteAssetReadiness()
            asset_validation = state.remote_asset_readiness.status(settings,scenes)
            assets_ok = asset_validation['state']=='ready'
        else:
            assets_ok = local_assets_ready(store, scenes)
        index_rows = store.index_counts()
    except Exception:
        # A local index failure makes the app unready; keep the established response contract.
        assets_ok, index_rows = False, {}
    try:
        # App state and the read-only index use separate DuckDB connections.
        # A fatal app-state error must make readiness fail even if assets/index are healthy.
        with store.lock:
            store.app.execute('SELECT action_id FROM actions LIMIT 1').fetchone()
        app_ready = True
    except Exception:
        app_ready = False
    ok = libs_ready and assets_ok and app_ready
    return JSONResponse({'ok': ok, 'mode': settings.mode, 'backend': store.backend, 'libs_ready': libs_ready,
                         'index_rows': index_rows, 'assets_ok': assets_ok, 'agent_protocols': [1, 2],
                         **({'asset_validation':asset_validation} if asset_validation is not None else {})}, status_code=200 if ok else 503)
