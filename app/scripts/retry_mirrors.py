"""Offline maintenance: stop the app, then retry pending VastDB rows in its environment.

DuckDB permits one writer process. Do not run this command alongside Uvicorn.
The command only mirrors durable rows; it does not send notifications.
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.store import Store
from app.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit',type=int,default=100)
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error('limit must be between 1 and 1000')
    settings = Settings()
    store = Store(index_path=settings.index_duckdb,app_path=settings.app_duckdb,asset_dir=settings.asset_dir,
                  backend=settings.data_backend,allow_duckdb_fallback=settings.allow_duckdb_fallback)
    try:
        delivered = store.retry_mirrors(args.limit)
        blobs = store.retry_blobs(args.limit)
        pending = store.rows(store.app,'SELECT count(*) AS n FROM mirror_outbox')[0]['n']
        print(f'Mirror rows delivered: {delivered}; pending: {pending}; blobs copied: {blobs}')
    finally:
        store.close()


if __name__ == '__main__':
    main()
