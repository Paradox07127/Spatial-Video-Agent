"""Application runs after the reverse proxy removes PUBLIC_BASE_PATH."""
from contextlib import asynccontextmanager
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import actions, agent, assets, fleet, health, scenes, search
from app.core.store import Store
from app.core.vss import VSS
from app.settings import Settings

STATIC_DIR = Path(__file__).resolve().parent / 'static'


class RevalidatedFiles(StaticFiles):
    # ES modules keep their URLs across deploys; a browser that does not revalidate runs a stale mix of modules
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers['Cache-Control'] = 'no-cache'
        return response


def create_app():
    @asynccontextmanager
    async def lifespan(app):
        settings = Settings()
        app.state.settings = settings
        app.state.vendor_manifest = json.loads((Path(__file__).parent / 'scripts/vendor_manifest.json').read_text())
        app.state.vss = maps = None
        if settings.mode == 'site':
            from pipeline.common import site_map
            maps = site_map.load(settings.site_map_path)
            def map_hit(hit):
                row = site_map.map_hit(maps, hit)
                return (row['scene_key'], row['cam_id'], row['t_ms']) if row else None
            app.state.vss = VSS(settings, map_hit=map_hit)
        app.state.store = Store(index_path=settings.index_duckdb, app_path=settings.app_duckdb, asset_dir=settings.asset_dir,
                                backend=settings.data_backend, allow_duckdb_fallback=settings.allow_duckdb_fallback)
        if maps is not None and app.state.store.backend == 'vastdb':
            app.state.store.load_vss_collection(maps)
        try:
            yield
        finally:
            app.state.store.close()

    app = FastAPI(lifespan=lifespan)
    # fleet JSON is ~25 MB raw and ~2.3 MB gzipped; GZipMiddleware leaves 206 ranges (PMTiles), SSE and video uncompressed
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    for module in (health, scenes, fleet, assets, actions, search, agent):
        app.include_router(module.router)

    @app.exception_handler(FileNotFoundError)
    async def missing(request: Request, exc: FileNotFoundError):
        return JSONResponse({'detail': str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError):
        return JSONResponse({'detail': str(exc)}, status_code=422)

    app.mount('/lib', RevalidatedFiles(directory=Settings().lib_dir, check_dir=False), name='lib')
    app.mount('/', RevalidatedFiles(directory=STATIC_DIR, html=True), name='static')
    return app


app = create_app()
