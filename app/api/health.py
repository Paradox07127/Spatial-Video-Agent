from pathlib import Path
import socket
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


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
    return {'services': services, 'degraded': any(s['status']=='unavailable' for s in services.values()),
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
    try:
        scenes = store.list_scenes()
        assets_ok = local_assets_ready(store, scenes)
        index_rows = store.index_counts()
    except Exception:
        # A local index failure makes the app unready; keep the established response contract.
        assets_ok, index_rows = False, {}
    ok = libs_ready and assets_ok
    return JSONResponse({'ok': ok, 'mode': settings.mode, 'backend': store.backend, 'libs_ready': libs_ready,
                         'index_rows': index_rows, 'assets_ok': assets_ok}, status_code=200 if ok else 503)
