"""Application runs after the reverse proxy removes PUBLIC_BASE_PATH."""
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
from threading import Event, Thread

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import actions, agent, assets, fleet, health, scenes, search, investigations
from app.core.investigations import Investigations
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


def static_version(root):
    h = hashlib.sha256()
    for p in sorted(p for p in root.rglob('*') if p.suffix in ('.js', '.css', '.html') and p.relative_to(root).parts[0] != 'lib'):
        h.update(str(p.relative_to(root)).encode() + b'\0' + p.read_bytes())
    return h.hexdigest()[:12]


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
        app.state.investigations = Investigations(app.state.store, vss=app.state.vss)
        app.state.investigations.start()
        maintenance_stop = Event()
        def maintain():
            while not maintenance_stop.wait(10):
                try:
                    app.state.store.retry_blobs(limit=16, should_stop=maintenance_stop.is_set)
                    app.state.store.retry_mirrors(limit=64, should_stop=maintenance_stop.is_set)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception('Application outbox maintenance failed')
        maintenance = Thread(target=maintain, name='application-outboxes', daemon=True)
        maintenance.start()
        try:
            yield
        finally:
            maintenance_stop.set()
            app.state.investigations.close()
            maintenance.join(50)
            if maintenance.is_alive():
                raise RuntimeError('Application outbox maintenance did not release the store')
            app.state.store.close()

    app = FastAPI(lifespan=lifespan)
    # fleet JSON is ~25 MB raw and ~2.3 MB gzipped; GZipMiddleware leaves 206 ranges (PMTiles), SSE and video uncompressed
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    for module in (health, scenes, fleet, assets, actions, search, agent, investigations):
        app.include_router(module.router)

    @app.exception_handler(FileNotFoundError)
    async def missing(request: Request, exc: FileNotFoundError):
        return JSONResponse({'detail': str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError):
        return JSONResponse({'detail': str(exc)}, status_code=422)

    # entry URLs carry the static hash so a new index.html never runs a cached old app.js or styles.css
    version = static_version(STATIC_DIR)
    index = (STATIC_DIR / 'index.html').read_text().replace('href="styles.css"', f'href="styles.css?v={version}"') \
        .replace('src="app.js"', f'src="app.js?v={version}"')

    @app.get('/', include_in_schema=False)
    @app.get('/index.html', include_in_schema=False)
    async def index_html():
        return HTMLResponse(index, headers={'Cache-Control': 'no-cache'})

    app.mount('/lib', RevalidatedFiles(directory=Settings().lib_dir, check_dir=False), name='lib')
    app.mount('/', RevalidatedFiles(directory=STATIC_DIR, html=True), name='static')
    return app


app = create_app()
