-- app.duckdb (application read-write; never rebuilt by the pipeline). VastDB schema `sva` has the same tables on site.
CREATE TABLE IF NOT EXISTS agent_runs(run_id TEXT PRIMARY KEY, kind TEXT, q TEXT, context JSON, steps JSON, claims JSON,
  evidence JSON, hits JSON, status TEXT, final TEXT, source_run_id TEXT, weave_url TEXT, model_revs JSON,
  created TIMESTAMP, elapsed_ms INTEGER);
CREATE TABLE IF NOT EXISTS query_cache(qhash TEXT PRIMARY KEY, q TEXT, context JSON, response JSON, created TIMESTAMP,
  backend TEXT, source_run_id TEXT);
CREATE TABLE IF NOT EXISTS actions(action_id TEXT PRIMARY KEY, scene_key TEXT, event_id TEXT, run_id TEXT,
  claim_id TEXT, auto BOOLEAN, actions TEXT[], review_task_id TEXT, zone_id TEXT, delivered BOOLEAN, created TIMESTAMP);
CREATE UNIQUE INDEX IF NOT EXISTS actions_event ON actions(scene_key, event_id);
CREATE TABLE IF NOT EXISTS zones(zone_id TEXT PRIMARY KEY, scene_key TEXT, kind TEXT, polygon_world JSON,
  source_event_id TEXT, action_id TEXT, t_range JSON, created TIMESTAMP);
