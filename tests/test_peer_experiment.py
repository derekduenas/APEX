"""Connected peer experiments, mature labels, paired ablation and refusals."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import shutil

import numpy as np
import pytest

from apex.core import Config, Refused, canonical
from apex.data import normalize
from apex.ledger import Ledger, read_verified
from apex.peer_experiment import MODELS, peer_events, run_peer_experiment, verify_peer_experiment
from apex.peer_fixtures import peer_demo_document
from apex.strategy_lab import run_lab


@pytest.fixture(scope='module')
def flight(tmp_path_factory):
    root = tmp_path_factory.mktemp('peer-flight')
    doc, plan = peer_demo_document()
    config = Config(symbol='AAPL', paths=100, variance='ewma')
    run_lab(canonical(doc).encode(), root/'lab', plan=plan, config=config)
    result = run_peer_experiment(root/'lab', root/'peer', market_symbol='SPY', sector_symbol='XLK')
    return root, doc, plan, config, result, read_verified(root/'peer'/'ledger.jsonl')


def test_entire_peer_hypothesis_and_fair_ablation_are_reconstructed(flight):
    root, _, _, _, result, rows = flight
    verdict = verify_peer_experiment(root/'peer')
    assert verdict['status'] == 'VALID', verdict
    assert result['counts']['paired_outcomes'] > 20
    assert result['counts']['eligible_setups'] > 0
    assert result['counts']['research_opportunities'] > 0  # unmistakably planted control
    assert result['holdout']['paired_samples'] > 0
    assert result['orders'] == result['fills'] == 0 and result['account_pnl'] is None
    assert all(r['payload']['candidate']['quote_id'] is None for r in rows if r['kind'] == 'PEER_FORECAST')
    assert all(r['payload']['candidate']['execution_readiness'].startswith('NO_EXECUTABLE_CLAIM') for r in rows if r['kind'] == 'PEER_FORECAST')
    assert all(r['payload']['candidate']['authority'] == 'NONE_RESEARCH_ONLY' for r in rows if r['kind'] == 'PEER_FORECAST')


def test_baselines_share_full_paths_and_freeze_the_same_quantity_under_cost_stress(flight):
    root, *_, rows = flight
    original = {r['payload']['sample_id']: r['payload'] for r in read_verified(root/'lab'/'forecast'/'ledger.jsonl') if r['kind'] == 'RESEARCH_FORECAST'}
    for row in rows:
        if row['kind'] != 'PEER_FORECAST':
            continue
        p = row['payload']
        quantities = set()
        for name, world in p['worlds'].items():
            one, stress = world['evaluation'], world['cost_3x_evaluation']
            assert one['inputs']['paths_sha256'] == stress['inputs']['paths_sha256'] == world['full_paths_sha256']
            assert one['inputs']['earliest_entry_step'] == 1
            quantities.update((one['strategies']['HOLD_LONG']['fixed_quantity'], stress['strategies']['HOLD_LONG']['fixed_quantity']))
            if name in ('ZERO_DRIFT', 'SHRUNK_MEAN'):
                assert world['terminal_digest'] == original[p['sample_id']]['variants'][name]['terminal_digest']
        assert len(quantities) == 1
        assert p['path_shape'][1] == 16


def test_matured_exact_paths_train_both_models_and_holdout_never_retrains(flight):
    _, _, plan, _, _, rows = flight
    seen, frozen = {}, []
    for row in rows:
        p = row['payload']
        if row['kind'] == 'PEER_OUTCOME':
            assert p['label_available_epoch'] <= row['epoch']
            assert len(p['bar_ids']) == 16
            seen[p['sample_id']] = p
        if row['kind'] == 'PEER_FORECAST':
            fit = p['fit']
            for record in fit['training_records']:
                assert record['sample_id'] in seen
                assert record['target_epoch'] <= p['training_cutoff_epoch']
                assert record['label_available_epoch'] <= p['training_cutoff_epoch']
                assert record['peer_feature'] is not None
            if fit['own_stock_model']:
                assert fit['training_sample_ids'] == fit['own_stock_model']['training_sample_ids']
                assert fit['alpha'] == fit['own_stock_model']['alpha']
                assert fit['feature_names'] == fit['own_stock_model']['feature_names'] + ['lag_gap']
            if p['phase'] == 'HOLDOUT':
                frozen.append(canonical(fit))
                assert p['training_cutoff_epoch'] == plan.holdout_start - 60
    assert len(set(frozen)) == 1


def test_changed_future_observations_cannot_change_prior_peer_decisions(flight):
    root, doc, plan, config, _, rows = flight
    altered = copy.deepcopy(doc)
    for bar in altered['observations']:
        if bar['event_epoch'] >= plan.holdout_start:
            for key in ('open', 'high', 'low', 'close'):
                bar[key] *= 1.2
    observations, rejected = normalize(altered)
    assert not rejected
    source_rows = read_verified(root/'lab'/'forecast'/'ledger.jsonl')
    source_rows = [r for r in source_rows if r['epoch'] < plan.holdout_start]
    from apex.strategy_simulation import SimulationCosts
    actual = [(kind, epoch, payload) for kind, epoch, payload in peer_events(source_rows, observations,
        plan=plan, config=config, costs=SimulationCosts(), market_symbol='SPY', sector_symbol='XLK',
        load_paths=lambda name: np.load(root/'lab'/'forecast'/name, allow_pickle=False)) if epoch < plan.holdout_start]
    expected = [(r['kind'], r['epoch'], r['payload']) for r in rows[1:] if r['epoch'] < plan.holdout_start]
    assert canonical(actual) == canonical(expected)


def test_missing_peer_is_refused_by_real_consumer_without_zero_imputation(tmp_path):
    doc, plan = peer_demo_document()
    doc['observations'] = [r for r in doc['observations'] if r['symbol'] != 'XLK']
    plan = replace(plan, scan_minutes=60)
    run_lab(canonical(doc).encode(), tmp_path/'lab', plan=plan, config=Config(symbol='AAPL', paths=100, variance='ewma'))
    result = run_peer_experiment(tmp_path/'lab', tmp_path/'peer', market_symbol='SPY', sector_symbol='XLK')
    assert result['counts']['peer_available'] == result['counts']['research_opportunities'] == 0
    assert result['counts']['paired_outcomes'] == 0
    assert result['refusals']['PEER_DATA_UNAVAILABLE'] == result['counts']['source_samples']
    rows = read_verified(tmp_path/'peer'/'ledger.jsonl')
    assert all(r['payload']['peer_feature'] is None for r in rows if r['kind'] == 'PEER_SAMPLE')
    assert all(set(r['payload']['worlds']) == {'ZERO_DRIFT', 'SHRUNK_MEAN'} for r in rows if r['kind'] == 'PEER_FORECAST')


def test_planted_relationship_removed_control_does_not_claim_an_edge(tmp_path):
    doc, plan = peer_demo_document(world='null')
    plan = replace(plan, scan_minutes=60)
    run_lab(canonical(doc).encode(), tmp_path/'lab', plan=plan, config=Config(symbol='AAPL', paths=100, variance='ewma'))
    result = run_peer_experiment(tmp_path/'lab', tmp_path/'peer', market_symbol='SPY', sector_symbol='XLK')
    assert result['input_class'] == 'SYNTHETIC_RESEARCH_CONTROL'
    assert result['holdout']['paired_samples'] > 0
    assert result['holdout']['models']['PEER_LAG_RIDGE']['policy_mean_net_cost_3x'] <= 0
    assert result['readiness'] == 'RESEARCH_ONLY_NO_PROMOTION_OR_TRADING_AUTHORITY'
    assert result['orders'] == result['fills'] == 0


def test_source_tamper_refuses_before_downstream_consumption_and_keeps_failure(flight, tmp_path):
    root, *_ = flight
    source = tmp_path/'lab'
    shutil.copytree(root/'lab', source)
    summary = json.loads((source/'summary.json').read_text())
    summary['orders'] = 1
    (source/'summary.json').write_text(canonical(summary))
    with pytest.raises(Refused, match='PEER_SOURCE_LAB_INVALID'):
        run_peer_experiment(source, tmp_path/'bad-peer', market_symbol='SPY', sector_symbol='XLK')
    assert (tmp_path/'bad-peer'/'FAILED.json').exists()
    assert not (tmp_path/'bad-peer'/'COMPLETE').exists()
    assert not (tmp_path/'bad-peer'/'ledger.jsonl').exists()


def test_valid_rehash_cannot_hide_changed_event_or_summary(flight, tmp_path):
    root, *_ = flight
    copied = root/'tampered-peer'
    shutil.copytree(root/'peer', copied)
    rows = read_verified(copied/'ledger.jsonl')
    (copied/'ledger.jsonl').unlink()
    writer = Ledger(copied/'ledger.jsonl')
    changed = False
    for row in rows:
        payload = copy.deepcopy(row['payload'])
        if row['kind'] == 'PEER_FORECAST' and not changed:
            payload['candidate']['strategy_id'] = 'HOLD_LONG' if payload['candidate']['strategy_id'] == 'WAIT' else 'WAIT'
            changed = True
        writer.append(row['kind'], row['epoch'], payload)
    writer.close()
    (copied/'COMPLETE').write_text(writer.head)
    verdict = verify_peer_experiment(copied)
    assert verdict['status'] == 'MISMATCH'
    assert any('PEER_EVENT_RECONSTRUCTION_DISAGREES' in p for p in verdict['problems'])


def test_existing_output_and_source_directories_are_never_overwritten(flight):
    root, *_ = flight
    before = (root/'peer'/'COMPLETE').read_bytes()
    with pytest.raises(FileExistsError):
        run_peer_experiment(root/'lab', root/'peer', market_symbol='SPY', sector_symbol='XLK')
    assert before == (root/'peer'/'COMPLETE').read_bytes()
    with pytest.raises(Refused, match='SEPARATE_FROM_SOURCE'):
        run_peer_experiment(root/'lab', root/'lab'/'nested', market_symbol='SPY', sector_symbol='XLK')
    assert not (root/'lab'/'nested').exists()
