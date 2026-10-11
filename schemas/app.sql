-- app.duckdb (application read-write; never rebuilt by the pipeline). VastDB schema `sva` has the same tables on site.
CREATE TABLE IF NOT EXISTS agent_runs(run_id TEXT PRIMARY KEY, kind TEXT, q TEXT, context JSON, steps JSON, claims JSON,
  evidence JSON, hits JSON, status TEXT, final TEXT, source_run_id TEXT, weave_url TEXT, model_revs JSON,
  created TIMESTAMP, elapsed_ms INTEGER);
CREATE TABLE IF NOT EXISTS query_cache(qhash TEXT PRIMARY KEY, q TEXT, context JSON, response JSON, created TIMESTAMP,
  backend TEXT, source_run_id TEXT);
CREATE TABLE IF NOT EXISTS actions(action_id TEXT PRIMARY KEY, scene_key TEXT, event_id TEXT, run_id TEXT,
  claim_id TEXT, auto BOOLEAN, actions TEXT[], review_task_id TEXT, zone_id TEXT, delivered BOOLEAN, created TIMESTAMP,
  UNIQUE(scene_key,event_id));
-- Store's transactional startup migration creates the versioned unique scene/event index.
CREATE TABLE IF NOT EXISTS zones(zone_id TEXT PRIMARY KEY, scene_key TEXT, kind TEXT, polygon_world JSON,
  source_event_id TEXT, action_id TEXT, t_range JSON, created TIMESTAMP);

-- Local startup migrations; never mirrored to the evidence/index backend.
CREATE TABLE IF NOT EXISTS app_migrations(name TEXT PRIMARY KEY, applied TIMESTAMP);

-- Durable investigations. Model observations are assertions with provenance,
-- never promoted to measured world state by persistence alone.
CREATE TABLE IF NOT EXISTS investigations(session_id TEXT PRIMARY KEY, title TEXT, revision INTEGER,
  release_id TEXT, created TIMESTAMP, updated TIMESTAMP);
CREATE TABLE IF NOT EXISTS investigation_turns(turn_id TEXT PRIMARY KEY, session_id TEXT, parent_turn_id TEXT,
  request_id TEXT, q TEXT, context JSON, job_id TEXT, run_id TEXT, status TEXT, final TEXT, created TIMESTAMP,
  UNIQUE(session_id,request_id));
CREATE TABLE IF NOT EXISTS investigation_jobs(job_id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT,
  request_json JSON, status TEXT, attempt INTEGER, last_seq INTEGER, current_attempt_start_seq INTEGER,
  run_id TEXT, spent_ms INTEGER, tools_used INTEGER, lease_started TIMESTAMP, cancel_requested BOOLEAN,
  error TEXT, created TIMESTAMP, updated TIMESTAMP);
CREATE TABLE IF NOT EXISTS investigation_events(event_id TEXT PRIMARY KEY, job_id TEXT, seq INTEGER,
  attempt INTEGER, frame_json JSON, created TIMESTAMP, UNIQUE(job_id,seq));
CREATE TABLE IF NOT EXISTS investigation_observations(observation_id TEXT PRIMARY KEY, session_id TEXT,
  turn_id TEXT, job_id TEXT, run_id TEXT, scene_key TEXT, cam_id TEXT, t0_ms BIGINT, t1_ms BIGINT,
  observation_json JSON, created TIMESTAMP);
