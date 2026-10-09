from fastapi import APIRouter, Request

router = APIRouter()


@router.get('/health')
def health(request: Request):
    store, settings = request.app.state.store, request.app.state.settings
    manifest = request.app.state.vendor_manifest
    libs_ready = all((settings.lib_dir / f['name']).is_file() for f in manifest['files'])
    scenes = store.list_scenes()
    assets_ok = bool(scenes) and all(store.asset_exists(s['scene_key'], 'manifest.json') for s in scenes)
    return {'ok': True, 'mode': settings.mode, 'backend': store.backend, 'libs_ready': libs_ready, 'index_rows': store.index_counts(), 'assets_ok': assets_ok}
