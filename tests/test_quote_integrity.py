"""Consumer-level regressions for the five published b3ec2d7 review failures."""
import base64
import json
from pathlib import Path

import pytest

from apex import decision_quotes as dq
from apex.admissibility import admissibility, require_mode
from apex.core import Config, Refused, canonical
from apex.data import from_alpaca_quotes, normalize, timestamp_ns, merged_document
from apex.decision import quote_at
from apex.engine import run
from apex.fixtures import demo_document

NOW = timestamp_ns('2026-08-24T13:30:05Z') / 1e9
CONFIG = Config(variance='ewma')


def raw(stamp, ask=641):
    return {'t': stamp, 'bp': 640, 'ap': ask, 'bs': 40, 'as': 240}


def collect(tmp_path, monkeypatch, records):
    monkeypatch.setattr(dq, 'decision_instants', lambda _: [NOW])
    def transport(*args):
        return {'status': 200, 'body_base64': base64.b64encode(json.dumps({'quotes': {'SPY': records}}).encode()).decode()}
    dq.fetch_decision_quotes(tmp_path / 'quotes', plan=None, config=CONFIG, transport=transport)
    return json.loads((tmp_path / 'quotes/quotes.json').read_text())


def test_actual_fetch_and_reader_refuse_one_ns_future(tmp_path, monkeypatch):
    records = [raw('2026-08-24T13:30:04.000000001Z')]
    with pytest.raises(Refused, match='NOT_YET_AVAILABLE'):
        collect(tmp_path, monkeypatch, records)
    rows, rejected = normalize({'schema': 'APEX_DATA_V1', 'observations': from_alpaca_quotes(records, 'SPY', 'r')})
    assert not rejected and rows[0]['available_epoch'] == NOW
    assert quote_at(rows, NOW, CONFIG)[0] is None
    assert quote_at(rows, NOW + .000001, CONFIG)[0] is not None


def test_exact_cutoff_accepted(tmp_path, monkeypatch):
    doc = collect(tmp_path, monkeypatch, [raw('2026-08-24T13:30:04Z')])
    assert quote_at(normalize(doc)[0], NOW, CONFIG)[0] is not None


def test_truncated_group_is_quarantined_and_every_consumer_refuses(tmp_path, monkeypatch):
    doc = collect(tmp_path, monkeypatch, [raw('2026-08-24T13:30:04Z')] * 50)
    assert doc['collection_status'] == 'INCOMPLETE' and doc['observations'] == []
    with pytest.raises(Refused, match='INCOMPLETE'):
        normalize(doc)
    with pytest.raises(Refused, match='INCOMPLETE'):
        require_mode(doc, [], 'OFFLINE_RESEARCH')
    with pytest.raises(Refused, match='INCOMPLETE'):
        merged_document([doc], retrieved_utc='r')
    nested = {'schema': 'APEX_DATA_V1', 'components': [{'components': [doc]}], 'observations': []}
    with pytest.raises(Refused, match='INCOMPLETE'):
        normalize(nested)


def test_ns_grouping_preserves_sparse_full_parity(tmp_path, monkeypatch):
    records = [raw('2026-08-24T13:30:03.999999901Z', 642), raw('2026-08-24T13:30:03.999999900Z', 641)]
    doc = collect(tmp_path, monkeypatch, records)
    full = normalize({'schema': 'APEX_DATA_V1', 'observations': from_alpaca_quotes(records, 'SPY', 'r')})[0]
    assert full[0]['event_epoch'] == full[1]['event_epoch']
    assert quote_at(full, NOW, CONFIG)[0]['ask'] == 642
    assert quote_at(normalize(doc)[0], NOW, CONFIG)[0]['ask'] == 642


def test_engine_refuses_mixed_before_forecasts_or_entries(tmp_path):
    doc, start, end = demo_document()
    doc['components'] = [{'source': 'SYNTHETIC_CONTROL'}, {'source': 'REAL_MARKET'}]
    with pytest.raises(Refused, match='INPUT_NOT_ADMISSIBLE'):
        run(canonical(doc).encode(), tmp_path / 'run', start=start, end=end, config=CONFIG)
    assert not (tmp_path / 'run/ledger.jsonl').exists()
    assert (tmp_path / 'run/FAILED.json').exists()


def test_recorded_and_mixed_do_not_become_synthetic():
    for bases in [('MEASURED_RECEIPT',), ('MEASURED_RECEIPT', 'SYNTHETIC_CLOCK')]:
        rows = [{'availability_basis': b} for b in bases]
        verdict = admissibility({'source': 'REAL_MARKET'}, rows)
        assert not verdict['modes']['SYNTHETIC_CONTROL']
        if len(bases) == 2:
            assert verdict['highest_admissible_mode'] == 'REFUSED'


def test_decision_snapshots_cannot_enter_execution_engine(tmp_path):
    from apex.paper_runtime import _source_problem
    doc, start, end = demo_document()
    doc['evidence_scope'] = 'DECISION_SNAPSHOTS_ONLY'
    rows, rejected = normalize(doc)
    assert _source_problem(doc, rows, rejected, 'SYNTHETIC_PAPER') == 'BLOCKED_DECISION_SNAPSHOTS_NOT_EXECUTION_EVIDENCE'
    with pytest.raises(Refused, match='NOT_EXECUTION_EVIDENCE'):
        run(canonical(doc).encode(), tmp_path / 'run', start=start, end=end, config=CONFIG)
    assert not (tmp_path / 'run/ledger.jsonl').exists()


def test_parent_synthetic_claim_survives_recorded_child_labels():
    rows = [{'availability_basis': 'MEASURED_RECEIPT'}]
    doc = {'source': 'SYNTHETIC_CONTROL', 'components': [{'source': 'REAL_MARKET'}]}
    assert admissibility(doc, rows)['highest_admissible_mode'] == 'REFUSED'
