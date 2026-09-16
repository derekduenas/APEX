"""Real director/child readers plus hostile proposal and artifact controls."""
import copy
from dataclasses import replace
import json
import multiprocessing
from pathlib import Path
import shutil
import time

import pytest

from apex import ai_planner, director
from apex.core import Config, Refused, canonical, digest
from apex.ledger import read_verified
from apex.peer_fixtures import peer_demo_document


SYMBOLS = {'target_symbol': 'AAPL', 'market_symbol': 'SPY', 'sector_symbol': 'XLK'}


def proposal(action='RUN_PEER_DISLOCATION_STUDY'):
    return {'schema': 'APEX_RESEARCH_PROPOSAL_V1', 'action': action, **SYMBOLS,
            'rationale': 'Exercise the predefined peer-lag hypothesis on declared synthetic controls.'}


@pytest.fixture(scope='module')
def source(tmp_path_factory):
    root = tmp_path_factory.mktemp('director-source')
    document, plan = peer_demo_document()
    starts = sorted({row['event_epoch'] for row in document['observations']
                     if row['symbol']=='AAPL' and row['event_epoch'] % 86400 == plan.start % 86400})[:4]
    plan = replace(plan, development_start=starts[1], holdout_start=starts[2],
                   end=starts[3]+390*60, minimum_training_labels=2, training_window=20)
    document['observations'] = [row for row in document['observations'] if row['event_epoch'] < plan.end]
    document['source'] = 'SYNTHETIC_DIRECTOR_FOUR_SESSION_CHAIN_CONTROL'
    document['control_design'] = 'Four-session subset exercises orchestration; not a power or edge test'
    path = root / 'input.json'
    path.write_text(canonical(document))
    return path, plan, Config(symbol='AAPL', paths=100, variance='ewma')


@pytest.fixture(scope='module')
def completed(source, tmp_path_factory):
    path, plan, config = source
    root = tmp_path_factory.mktemp('director-complete') / 'run'
    calls = []
    def synthetic(context, symbols, model):
        calls.append((context, symbols, model))
        assert context['input_class'] == 'SYNTHETIC_RESEARCH_CONTROL'
        assert context['outcomes_sent_to_planner'] is False
        assert (root / 'request.json').exists()
        assert not (root / 'laboratory').exists()
        return {'proposal': proposal(), 'provenance': 'SYNTHETIC_PLANNER_TEST_ONLY', 'model': model,
                'request_digest': None, 'response_sha256': None}
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(director, 'request_plan', synthetic)
        report = director.run_director(path, root, plan=plan, config=config,
            market_symbol='SPY', sector_symbol='XLK', model='synthetic-control')
    assert len(calls) == 1
    return root, report


def test_real_connected_chain_and_reader(completed):
    root, report = completed
    assert report['planner_provenance'] == 'SYNTHETIC_PLANNER_TEST_ONLY'
    assert report['input_class'] == 'SYNTHETIC_RESEARCH_CONTROL'
    assert report['decision'] == 'EXPLORATORY_EVIDENCE_ONLY'
    assert report['research_attempts'] == report['research_attempt_budget'] == 1
    assert report['orders'] == report['fills'] == 0 and report['account_pnl'] is None
    assert report['evidence']['peer_experiment']['counts']['paired_outcomes'] > 0
    assert [row['kind'] for row in read_verified(root/'ledger.jsonl')] == [
        'RUN_OPEN', 'PLAN_REQUEST', 'PLAN_ACCEPTED', 'LAB_VERIFIED', 'PEER_EXPERIMENT_VERIFIED', 'DIRECTOR_CLOSE']
    verdict = director.verify_director(root)
    assert verdict['status'] == 'VALID', verdict
    assert verdict['verified_stages'] == 6


def test_external_defer_has_honest_provenance_and_no_research(source, tmp_path):
    path, plan, config = source
    root = tmp_path/'defer'
    report = director.run_director(path, root, plan=plan, config=config,
        market_symbol='SPY', sector_symbol='XLK', proposal=proposal('DEFER_MISSING_DATA'))
    assert report['decision'] == 'WAIT' and report['research_attempts'] == 0
    assert report['planner_provenance'] == 'EXTERNAL_PROPOSAL_NOT_AUTHENTICATED' and report['model'] is None
    assert not (root/'laboratory').exists()
    assert director.verify_director(root)['status'] == 'VALID'
    before = (root/'COMPLETE').read_bytes()
    with pytest.raises(FileExistsError):
        director.run_director(path, root, plan=plan, config=config,
            market_symbol='SPY', sector_symbol='XLK', proposal=proposal())
    assert (root/'COMPLETE').read_bytes() == before


@pytest.mark.parametrize('change', [
    {'action':'PLACE_ORDER'}, {'target_symbol':'TSLA'}, {'max_notional':'900000'},
    {'source_url':'https://arbitrary.invalid'}, {'minimum_probability':.01}, {'rationale':''},
])
def test_proposal_cannot_change_authority_policy_symbols_or_sources(source, tmp_path, monkeypatch, change):
    path, plan, config = source
    root = tmp_path/'refused'
    def never(*args, **kwargs):
        pytest.fail('Lab must not run on invalid proposals')
    monkeypatch.setattr(director, 'run_lab', never)
    bad = {**proposal(), **change}
    with pytest.raises(Refused):
        director.run_director(path, root, plan=plan, config=config,
            market_symbol='SPY', sector_symbol='XLK', proposal=bad)
    assert (root/'FAILED.json').exists() and not (root/'COMPLETE').exists()
    assert director.verify_director(root)['status'] == 'MISMATCH'


def test_missing_peer_or_bad_compute_budget_refuses_before_planner(source, tmp_path, monkeypatch):
    path, plan, config = source
    def never(*args, **kwargs):
        pytest.fail('Planner must not run after failed preflight')
    monkeypatch.setattr(director, 'request_plan', never)
    with pytest.raises(Refused, match='MISSING_REQUIRED_SYMBOL'):
        director.run_director(path, tmp_path/'missing', plan=plan, config=config,
            market_symbol='QQQ', sector_symbol='XLK', model='synthetic-control')
    with pytest.raises(Refused, match='COMPUTE_BUDGET'):
        director.run_director(path, tmp_path/'budget', plan=plan, config=replace(config, paths=2100),
            market_symbol='SPY', sector_symbol='XLK', model='synthetic-control')


def test_missing_key_never_falls_back_to_synthetic_or_rules(source, tmp_path, monkeypatch):
    path, plan, config = source
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr(ai_planner, '_request_bytes', lambda *args: pytest.fail('No network without key'))
    root = tmp_path/'unavailable'
    with pytest.raises(Refused, match='AI_PLANNER_UNAVAILABLE'):
        director.run_director(path, root, plan=plan, config=config,
            market_symbol='SPY', sector_symbol='XLK', model='operator-explicit-model')
    report = json.loads((root/'report.json').read_text())
    assert report['decision'] == 'REFUSED' and report['orders'] == report['fills'] == 0
    assert not (root/'laboratory').exists()


def _rehash(root, rows):
    previous = 'GENESIS'
    for row in rows:
        row['prev_hash'] = previous
        row.pop('hash', None)
        row['hash'] = digest(row)
        previous = row['hash']
    (root/'ledger.jsonl').write_text(''.join(canonical(row)+'\n' for row in rows))
    (root/'COMPLETE').write_text(previous)


@pytest.mark.parametrize('field,value', [('capital_authority','FULL_AUTO'),
    ('decision','PROMOTE_TO_LIVE'), ('research_attempts',10),
    ('next_research_action','PLACE_LIVE_BUY_ORDER')])
def test_rehashed_report_tampering_cannot_promote_or_change_capital(completed, tmp_path, field, value):
    source_root, report = completed
    root = tmp_path/'tamper'
    shutil.copytree(source_root, root)
    modified = {**report, field:value}
    (root/'report.json').write_text(canonical(modified))
    rows = read_verified(root/'ledger.jsonl')
    rows[-1]['payload']['report_digest'] = digest(modified)
    _rehash(root, rows)
    result = director.verify_director(root)
    assert result['status'] == 'MISMATCH'
    assert 'DIRECTOR_REPORT_RECONSTRUCTION_DISAGREES' in result['problems']


def test_valid_prefix_and_child_failure_are_not_completion(completed, tmp_path):
    source_root, _ = completed
    root = tmp_path/'prefix'
    shutil.copytree(source_root, root)
    rows = read_verified(root/'ledger.jsonl')
    _rehash(root, rows[:-1])
    assert director.verify_director(root)['status'] == 'MISMATCH'
    other = tmp_path/'child-failed'
    shutil.copytree(source_root, other)
    (other/'peer-edge'/'FAILED.json').write_text('{"reason":"INTERRUPTED"}')
    assert director.verify_director(other)['status'] == 'MISMATCH'


def _response(value=None, **changes):
    result = {'status':'completed', 'error':None, 'output':[{'type':'message','role':'assistant',
        'status':'completed','content':[{'type':'output_text','text':canonical(value or proposal())}]}]}
    result.update(changes)
    return canonical(result).encode()


def test_responses_schema_and_real_parser_handle_refusal_incomplete_tools_and_extra_fields():
    request = ai_planner.make_request({'symbols':SYMBOLS}, SYMBOLS, 'operator-explicit-model')
    assert request['store'] is False and request['max_output_tokens'] == 1200
    assert request['text']['format']['strict'] is True
    assert request['text']['format']['schema']['additionalProperties'] is False
    assert 'tools' not in request
    assert ai_planner.parse_response(_response(), SYMBOLS) == proposal()
    for raw in (_response(status='incomplete'), _response(output=[{'type':'function_call'}]),
                _response(output=[{'type':'message','role':'assistant','status':'completed',
                    'content':[{'type':'refusal','refusal':'cannot comply'}]}]),
                _response({**proposal(),'capital_authority':'FULL'})):
        with pytest.raises(Refused):
            ai_planner.parse_response(raw, SYMBOLS)
    with pytest.raises(Refused):
        ai_planner.strict_json('{"action":"RUN_PEER_DISLOCATION_STUDY","action":"PLACE_ORDER"}')
    with pytest.raises(Refused):
        ai_planner.parse_response(b' '*(ai_planner.MAX_RESPONSE_BYTES+1), SYMBOLS)


def test_api_credentials_do_not_enter_request_or_result(monkeypatch):
    credential = 'sk-synthetic-credential-test-only'
    monkeypatch.setenv('OPENAI_API_KEY', credential)
    observed = []
    def transport(request, key):
        assert key == credential
        assert credential not in canonical(request)
        observed.append(request)
        return _response()
    monkeypatch.setattr(ai_planner, '_request_bytes', transport)
    result = ai_planner.request_plan({'symbols':SYMBOLS}, SYMBOLS, 'operator-explicit-model')
    assert credential not in canonical(result)
    assert len(observed) == 1
    # The transport is replaced only in this test; this does not commission live API access.
    assert result['request_digest'] == digest(observed[0])
    with pytest.raises(Refused, match='CREDENTIAL'):
        ai_planner.validate_proposal({**proposal(),'rationale':credential}, SYMBOLS)


def test_http_worker_never_follows_redirect_or_exposes_error_body(monkeypatch, tmp_path):
    class Response:
        status = 302
        def read(self, *args):
            pytest.fail('Error body must not be exposed')
    class Connection:
        def __init__(self, host, timeout):
            assert host == 'api.openai.com' and timeout == 15
        def request(self, method, path, body, headers):
            assert method == 'POST' and path == '/v1/responses'
            assert headers['Authorization'] == 'Bearer private'
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(ai_planner.http.client, 'HTTPSConnection', Connection)
    path = tmp_path/'response.bin'
    ai_planner._http_worker(path, b'{}', 'private')
    assert path.read_bytes() == b'{"error":"AI_HTTP_STATUS_302"}'


def test_hard_deadline_includes_partially_written_response(monkeypatch):
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('Local process-bound regression requires fork')
    context = multiprocessing.get_context('fork')
    def partial_then_stall(output_path, request, key):
        Path(output_path).write_bytes(b'O{"status":')
        time.sleep(30)
    monkeypatch.setattr(ai_planner.multiprocessing, 'get_context', lambda method: context)
    monkeypatch.setattr(ai_planner, '_http_worker', partial_then_stall)
    monkeypatch.setattr(ai_planner, 'WALL_SECONDS', .1)
    started = time.monotonic()
    with pytest.raises(Refused, match='WALL_TIME'):
        ai_planner._request_bytes({}, 'synthetic-secret')
    assert time.monotonic() - started < 3


def test_outcomes_change_next_research_action_without_promoting_any_model():
    assert director._assess_holdout(None)['next_action'] == 'COLLECT_SUFFICIENT_HOLDOUT'
    peer = {'holdout': {'paired_samples':20, 'sessions':['2026-09-14','2026-09-15'],
        'paired_crps_delta_vs_own_stock':-.0001, 'models':{
            'PEER_LAG_RIDGE': {'policy_mean_net':2., 'policy_mean_net_cost_3x':.5},
            'OWN_STOCK_RIDGE': {'policy_mean_net':1., 'policy_mean_net_cost_3x':.1}}}}
    reviewed = director._assess_holdout(peer)
    assert reviewed['next_action'] == 'REPLICATE_WITH_UNSEEN_DATA_AND_QUOTES'
    assert reviewed['paired_samples'] == 20 and reviewed['observed_session_count'] == 2
    assert reviewed['policy_mean_net_cost_3x_delta_vs_own_stock'] == .4
    worse_forecast = copy.deepcopy(peer)
    worse_forecast['holdout']['paired_crps_delta_vs_own_stock'] = .00001
    assert director._assess_holdout(worse_forecast)['next_action'] == 'REJECT_OR_REVISE_PEER_HYPOTHESIS_ON_NEW_DATA'
    lost_economics = copy.deepcopy(peer)
    lost_economics['holdout']['models']['PEER_LAG_RIDGE']['policy_mean_net_cost_3x'] = -.1
    assert director._assess_holdout(lost_economics)['status'] == 'HYPOTHESIS_NOT_SUPPORTED'
