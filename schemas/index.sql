-- index.duckdb (pipeline output, mounted read-only by the app; VastDB schema `sva` has the same tables).
-- Time filters use interval intersection: t0_ms < :q_end AND t1_ms > :q_start.
CREATE TABLE IF NOT EXISTS clips(clip_id TEXT PRIMARY KEY, scene_id TEXT, scene_key TEXT, workspace TEXT, cam_id TEXT,
  t0_ms BIGINT, t1_ms BIGINT, capture_ms BIGINT, upload_ms BIGINT, analyzed_ms BIGINT, vss_source TEXT,
  caption TEXT, v_txt FLOAT[256], v_vis FLOAT[256], n_person SMALLINT, n_vehicle SMALLINT,
  lat DOUBLE, lon DOUBLE, heading REAL, speed REAL, place_id TEXT, zone_ids TEXT[], video_uri TEXT);
CREATE TABLE IF NOT EXISTS detections(scene_id TEXT, scene_key TEXT, cam_id TEXT, t_ms BIGINT, cls TEXT, conf REAL,
  x1 REAL, y1 REAL, x2 REAL, y2 REAL, track_id INTEGER, entity_id TEXT, wx REAL, wy REAL, sigma_m REAL);
CREATE TABLE IF NOT EXISTS tracks(gtrack_id TEXT PRIMARY KEY, scene_id TEXT, scene_key TEXT, entity_id TEXT, cls TEXT,
  t0_ms BIGINT, t1_ms BIGINT, cam_ids TEXT[], source TEXT);
CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, scene_id TEXT, scene_key TEXT, type TEXT, t0_ms BIGINT,
  t1_ms BIGINT, actor_ids TEXT[], place_id TEXT, severity SMALLINT, source TEXT, params JSON, clip_ids TEXT[]);
CREATE TABLE IF NOT EXISTS gps(trip_id TEXT, t_ms BIGINT, lat DOUBLE, lon DOUBLE, heading REAL, speed REAL,
  way_id BIGINT, node_id BIGINT, place_id TEXT, state TEXT, gap_ms INTEGER);
CREATE TABLE IF NOT EXISTS places(place_id TEXT PRIMARY KEY, kind TEXT, name TEXT, aliases TEXT[], lat DOUBLE,
  lon DOUBLE, geom JSON);
CREATE TABLE IF NOT EXISTS scene_manifest(scene_id TEXT PRIMARY KEY, scene_key TEXT, workspace TEXT, title TEXT,
  t0_ms BIGINT, t1_ms BIGINT, manifest JSON);
