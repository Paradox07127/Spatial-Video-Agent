"""Statistics preserve both request provenance and the observed full-window frame grid."""
from app.core import street
from app.tests.unit.a5_fakes import L1_CAM, L1_KEY, fixture_l1_store
from app.tests.unit.test_street import s3_with, sidecar


def test_statistics_s3_fetch_then_local_replay_keep_values_and_coverage(tmp_path):
    store = fixture_l1_store(tmp_path,site=True)
    gets = []
    store.s3 = s3_with(lambda key: sidecar(50),gets)
    first = street.people_statistics(store,L1_KEY,L1_CAM,0,5000,'peak_count')
    second = street.people_statistics(store,L1_KEY,L1_CAM,0,5000,'peak_count')
    assert (first.envelope.exec_state,second.envelope.exec_state)==('live','precomputed')
    assert len(gets)==1
    assert first.envelope.output==second.envelope.output
    assert first.envelope.output['frames_read']==50
    assert first.envelope.output['coverage']==1
    assert first.envelope.output['per_second']
    assert first.evidence==second.evidence
