"""Persisted action upgrades retain data, unique keys and retry semantics."""
from types import SimpleNamespace
import sys

import duckdb
import pytest

from app.api.actions import trigger
from app.core.models import TriggerRequest
from app.core.store import SCHEMAS_DIR, Store
from app.tests.unit.test_actions import alerts, index_db, store
from app.tests.unit.test_store import SVA, WH


def test_raw_app_ddl_preserves_event_uniqueness_without_store(tmp_path):
    con = duckdb.connect(str(tmp_path / 'raw-schema.duckdb'))
    try:
        con.execute((SCHEMAS_DIR / 'app.sql').read_text())
        con.execute("INSERT INTO actions(action_id,scene_key,event_id) VALUES ('a','s','e')")
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO actions(action_id,scene_key,event_id) VALUES ('b','s','e')")
        con.execute("INSERT INTO actions(action_id,scene_key,event_id) VALUES ('b','s','different-event')")
        assert con.execute('SELECT count(*) FROM actions').fetchone()[0] == 2
    finally:
        con.close()


def create_legacy_schema(con):
    # Prior releases used only the explicit index, without the new inline UNIQUE.
    ddl = (SCHEMAS_DIR / 'app.sql').read_text().replace(',\n  UNIQUE(scene_key,event_id)', '')
    con.execute(ddl)
    con.execute('CREATE UNIQUE INDEX actions_event ON actions(scene_key,event_id)')


def test_duplicate_trigger_retries_without_rewriting_action_list(store, alerts):
    req = TriggerRequest(scene_key=WH, event_id='E1', auto=False, actions=['wandb_alert', 'create_review_task'])
    first = trigger(store, req)
    assert first.delivered and len(alerts) == 1

    class RejectListRewrite:
        def __init__(self, con):
            self.con = con

        def execute(self, sql, *args):
            if sql.startswith('UPDATE actions SET') and 'actions=' in sql:
                raise AssertionError('Duplicate trigger attempted a LIST rewrite')
            return self.con.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.con, name)

    store.app = RejectListRewrite(store.app)
    second = trigger(store, req)
    assert second.existing and second.delivered and second.action_id == first.action_id
    assert second.review_task_id == first.review_task_id and len(alerts) == 1
    assert len(store.list_actions(WH)) == 1


def test_persisted_legacy_index_rebuild_survives_reopen_and_list_updates(index_db, tmp_path, monkeypatch):
    monkeypatch.setenv('RUNS_DIR', str(tmp_path / 'runs'))
    path = tmp_path / 'legacy.duckdb'
    con = duckdb.connect(str(path))
    create_legacy_schema(con)
    # A pre-upgrade database has the explicit ART index and no migration marker.
    con.execute("INSERT INTO actions VALUES ('a_legacy',? ,? ,NULL,NULL,false,['wandb_alert'],'RT-7',NULL,true,TIMESTAMP '2026-10-10 00:00:00')", [WH, WH + ':E1'])
    before = con.execute('SELECT * FROM actions').fetchall()
    con.execute('CHECKPOINT')
    con.close()
    legacy_requests = ['wandb_alert']
    notifications = []
    monkeypatch.setitem(sys.modules, 'wandb', SimpleNamespace(init=lambda **kw: None, alert=lambda **kw: notifications.append(kw)))
    for cycle in range(4):
        s = Store(index_path=index_db, app_path=path, asset_dir=SVA / 'fixtures/sva-out')
        try:
            s.settings.mode, s.settings.wandb_api_key, s.settings.wandb_project = 'site', 'fake-key', 'fake-project'
            assert s.app.execute('SELECT count(*) FROM app_migrations').fetchone()[0] == 1
            if cycle == 0:
                assert s.app.execute('SELECT * FROM actions').fetchall() == before
            # Retrying an old delivered action must not notify again after restart.
            req = TriggerRequest(scene_key=WH, event_id='E1', auto=False, actions=legacy_requests)
            result = trigger(s, req)
            assert result.action_id == 'a_legacy' and result.delivered
            if cycle == 0:
                legacy_requests += ['create_review_task', 'hazard_zone']
                assert trigger(s, req.model_copy(update={'actions': legacy_requests})).zone_id
            # Exercise the exact persisted LIST rewrite, including a no-op, each reopen.
            s.app.execute('UPDATE actions SET actions=? WHERE action_id=?', [legacy_requests, 'a_legacy'])
            assert s.app.execute('SELECT actions FROM actions').fetchone()[0] == legacy_requests
            with pytest.raises(duckdb.ConstraintException):
                s.app.execute("INSERT INTO actions(action_id,scene_key,event_id) VALUES ('different-id',?,?)", [WH, WH + ':E1'])
            with pytest.raises(duckdb.ConstraintException):
                s.app.execute("INSERT INTO actions(action_id,scene_key,event_id) VALUES ('a_legacy','different-scene','different-event')")
            assert s.app.execute('SELECT count(*) FROM actions').fetchone()[0] == 1
            assert notifications == []
            assert s.app.execute("SELECT index_name,is_unique FROM duckdb_indexes() WHERE table_name='actions'").fetchall() == [('actions_event_v2', True)]
            s.app.execute('CHECKPOINT')
        finally:
            s.close()


def test_index_rebuild_rolls_back_ddl_and_marker_together(tmp_path):
    con = duckdb.connect(str(tmp_path / 'rollback.duckdb'))
    create_legacy_schema(con)
    con.execute("INSERT INTO actions(action_id,scene_key,event_id,actions) VALUES ('a','s','e',['create_review_task'])")

    class FailRebuild:
        def execute(self, sql, *args):
            if sql.startswith('DROP INDEX IF EXISTS actions_event'):
                raise RuntimeError('Injected rebuild failure')
            return con.execute(sql, *args)

    try:
        with pytest.raises(RuntimeError, match='Injected rebuild failure'):
            Store._repair_actions_event_index(SimpleNamespace(app=FailRebuild()))
        assert con.execute('SELECT count(*) FROM app_migrations').fetchone()[0] == 0
        assert con.execute('SELECT action_id,actions FROM actions').fetchall() == [('a', ['create_review_task'])]
        assert con.execute("SELECT is_unique FROM duckdb_indexes() WHERE index_name='actions_event'").fetchone() == (True,)
        assert con.execute("SELECT count(*) FROM duckdb_indexes() WHERE index_name='actions_event_v2'").fetchone() == (0,)
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO actions(action_id,scene_key,event_id) VALUES ('b','s','e')")
        Store._repair_actions_event_index(SimpleNamespace(app=con))
        assert con.execute('SELECT count(*) FROM app_migrations').fetchone()[0] == 1
    finally:
        con.close()
