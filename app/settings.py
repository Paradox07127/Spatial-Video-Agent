"""Environment settings from CONTRACT §6.3."""
from dataclasses import dataclass, field
import os
from pathlib import Path


def env(name, default='', *, secret=False):
    return field(default_factory=lambda: os.environ.get(name, default), repr=not secret)


@dataclass
class Settings:
    mode: str = env('MODE', 'dev')
    port: int = field(default_factory=lambda: int(os.environ.get('PORT', '8300')))
    public_base_path: str = env('PUBLIC_BASE_PATH')
    data_backend: str = field(default_factory=lambda: os.environ.get('DATA_BACKEND', 'vastdb' if os.environ.get('MODE') == 'site' else 'duckdb'))
    allow_duckdb_fallback: bool = field(default_factory=lambda: os.environ.get('ALLOW_DUCKDB_FALLBACK') == '1')
    asset_dir: Path | None = field(default_factory=lambda: (lambda d: Path(d) if d else None)(os.environ.get('ASSET_DIR', 'fixtures/sva-out' if os.environ.get('MODE', 'dev') == 'dev' else '')))
    index_duckdb: Path = field(default_factory=lambda: Path(os.environ.get('INDEX_DUCKDB', 'index.duckdb')))
    app_duckdb: Path = field(default_factory=lambda: Path(os.environ.get('APP_DUCKDB', 'app.duckdb')))
    runs_dir: Path = field(default_factory=lambda: Path(os.environ.get('RUNS_DIR', 'runs')))
    lib_dir: Path = field(default_factory=lambda: Path(os.environ.get('LIB_DIR', 'app/static/lib')))
    vss_url: str = field(default_factory=lambda: os.environ.get('VSS_URL', os.environ.get('INGRESS_URL', '')))
    vss_username: str = field(default_factory=lambda: os.environ.get('VSS_USERNAME', os.environ.get('USERNAME', '')), repr=False)
    vss_password: str = field(default_factory=lambda: os.environ.get('VSS_PASSWORD', os.environ.get('PASSWORD', '')), repr=False)
    site_map_path: Path = field(default_factory=lambda: Path(os.environ.get('SITE_MAP', 'site_map.json')))
    s3_endpoint: str = env('S3_ENDPOINT')
    s3_verify: bool = field(default_factory=lambda: os.environ.get('S3_VERIFY', '1') != '0')
    access_key: str = env('ACCESS_KEY', secret=True)
    secret_key: str = env('SECRET_KEY', secret=True)
    asset_bucket: str = env('ASSET_BUCKET')
    vastdb_bucket: str = env('VASTDB_BUCKET')
    vdb_schema: str = env('VDB_SCHEMA')
    vdb_collection: str = env('VDB_COLLECTION')
    vdb_endpoint: str = env('VDB_ENDPOINT')
    vlm_url: str = field(default_factory=lambda: os.environ.get('VLM_URL', os.environ.get('COSMOS3_REASON_URL', '')))
    vlm_model: str = field(default_factory=lambda: os.environ.get('VLM_MODEL', os.environ.get('COSMOS3_REASON_MODEL', '')))
    embed_url: str = field(default_factory=lambda: os.environ.get('EMBED_URL', os.environ.get('COSMOS_EMBED1_URL', '')))
    embed_model: str = field(default_factory=lambda: os.environ.get('EMBED_MODEL', os.environ.get('COSMOS_EMBED1_MODEL', '')))
    yolo_url: str = env('YOLO_URL')
    gpu_bearer_token: str = env('GPU_BEARER_TOKEN', secret=True)
    llm_url: str = env('LLM_URL')
    llm_model_small: str = env('LLM_MODEL_SMALL')
    llm_model_large: str = env('LLM_MODEL_LARGE')
    llm_api_key: str = env('LLM_API_KEY', secret=True)
    wandb_api_key: str = env('WANDB_API_KEY', secret=True)
    wandb_team: str = env('WANDB_TEAM')
    wandb_project: str = env('WANDB_PROJECT')
    weave_project: str = env('WEAVE_PROJECT')

    def __post_init__(self):
        if self.mode not in ('dev', 'site') or self.data_backend not in ('duckdb', 'vastdb'):
            raise ValueError('Invalid MODE or DATA_BACKEND')
        if self.mode == 'site' and self.data_backend == 'duckdb' and not self.allow_duckdb_fallback:
            raise ValueError('site DuckDB requires ALLOW_DUCKDB_FALLBACK=1')
