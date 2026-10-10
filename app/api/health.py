from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


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
