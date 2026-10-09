from pathlib import PurePosixPath

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter()


@router.get('/api/assets/{path:path}')
def assets(path: str, request: Request):
    store = request.app.state.store
    if PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or '/' not in path:
        raise FileNotFoundError('Asset not found')
    if store.asset_dir is None:
        scene_key, relpath = path.split('/', 1)
        return RedirectResponse(store.asset_url(scene_key, relpath), status_code=302)
    return FileResponse(store.local_asset(path))
