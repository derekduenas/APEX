"""Connected strategy-lab tests exercise actual readers, payoff paths and feedback."""
import copy
from dataclasses import replace
import json

import numpy as np
import pytest

from apex.core import Config, Refused, canonical
from apex.fixtures import strategy_demo_document
from apex.ledger import read_verified
from apex.strategy_lab import LabPolicy, _candidate, _feedback, _path_episode, run_lab
from apex.strategy_simulation import SimulationCosts, evaluate_strategies


@pytest.fixture(scope='module')
def lab_flight(tmp_path_factory):
    out = tmp_path_factory.mktemp('strategy') / 'flight'
    doc, plan = strategy_demo_document()
    config = Config(paths=100, variance='ewma')
    summary = run_lab(canonical(doc).encode(), out, plan=plan, config=config)
    rows = read_verified(out/'ledger.jsonl')
    return out, doc, plan, config, summary, rows


def test_connected_intelligence_simulation_strategy_outcome_and_feedback(lab_flight):
    out, doc, plan, config, result, rows = lab_flight
    assert result['tournaments'] == 88
    assert result['strategy_scenario_evaluations'] > 200000
    assert result['completed_counterfactual_episodes'] > result['tournaments']
    assert result['orders'] == result['fills'] == 0 and result['account_pnl'] is None
    seen = {}
    used_feedback = False
    for row in rows:
        p = row['payload']
        if row['kind'] == 'STRATEGY_OUTCOME':
            assert p['label_available_epoch'] <= row['epoch']
            seen[p['sample_id']] = p
        if row['kind'] == 'STRATEGY_TOURNAMENT':
            assert set(p['eligible_setups']) <= set(p['worlds']['SELECTED_MODEL']['strategies'])
            assert all(i in seen for i in p['feedback']['sample_ids'])
            used_feedback |= p['feedback']['samples'] > 0
            assert p['candidate']['economics_price_origin_epoch'] < p['candidate']['actual_decision_epoch']
            assert p['candidate']['execution_readiness'] == 'BAR_ORIGIN_RESEARCH_REQUIRES_DECISION_QUOTE_REVALUATION'
            if p['analog']:
                for sample in p['analog']['training_records']:
                    assert sample['target_epoch'] <= p['training_cutoff_epoch']
                    assert sample['label_available_epoch'] <= p['training_cutoff_epoch']
                    assert sample['sample_id'] in seen
    assert used_feedback


def test_same_proposed_quantity_across_all_stress_worlds(lab_flight):
    *_, rows = lab_flight
    for row in rows:
        if row['kind'] != 'STRATEGY_TOURNAMENT':
            continue
        worlds = row['payload']['worlds']
        quantity = worlds['SELECTED_MODEL']['strategies']['HOLD_LONG']['fixed_quantity']
        assert all(w['strategies']['HOLD_LONG']['fixed_quantity'] == quantity for w in worlds.values())
        hashes = [w['inputs']['paths_sha256'] for w in (worlds['SELECTED_MODEL'], worlds['COST_3X'])]
        assert hashes[0] == hashes[1]


def test_holdout_analogue_membership_and_supervised_choice_frozen(lab_flight):
    out, _, plan, _, _, rows = lab_flight
    records = [r['payload'] for r in rows if r['kind'] == 'STRATEGY_TOURNAMENT' and r['payload']['phase'] == 'HOLDOUT']
    assert len({r['selected_forecast_model'] for r in records}) == 1
    member_sets = {tuple(r['analog']['training_sample_ids']) for r in records if r['analog']}
    assert len(member_sets) == 1
    assert all(r['training_cutoff_epoch'] == plan.holdout_start - 60 for r in records)


def test_predefined_null_and_stress_ranking_does_not_promote_win_frequency():
    paths = np.tile(np.linspace(0, .02, 16), (100, 1))
    common = evaluate_strategies(paths, spot=100, capital=10000, max_notional=1000,
                                eligible_setups={'HOLD_LONG': True})
    worlds = {k: copy.deepcopy(common) for k in ('SELECTED_MODEL', 'HISTORICAL_ANALOGS', 'VOLATILITY_1_5X', 'COST_3X', 'ANALOG_COST_3X', 'FRONT_LOADED_DRIFT', 'BACK_LOADED_DRIFT', 'ADVERSE_JUMP')}
    analog = {'in_support': True}
    feedback = {'blocks_candidate': False}
    assert _candidate(worlds, analog=analog, analog_problem=None, feedback=feedback, capital=10000, policy=LabPolicy())['strategy_id'] == 'HOLD_LONG'
    # Keep an attractive stated win frequency while the actual cost stress loses.
    worlds['COST_3X']['strategies']['HOLD_LONG']['summary']['mean_log_growth'] = -.001
    assert _candidate(worlds, analog=analog, analog_problem=None, feedback=feedback, capital=10000, policy=LabPolicy())['strategy_id'] == 'WAIT'
    worlds['COST_3X']['strategies']['HOLD_LONG']['summary']['mean_log_growth'] = None
    assert _candidate(worlds, analog=analog, analog_problem=None, feedback=feedback, capital=10000, policy=LabPolicy())['rankings'][0]['refusal'] == 'CAPITAL_EXHAUSTION_OR_UNDEFINED_LOG_GROWTH'


def test_support_and_matured_coverage_can_stop_otherwise_attractive_candidate():
    paths = np.tile(np.linspace(0, .02, 16), (100, 1))
    result = evaluate_strategies(paths, spot=100, capital=10000, max_notional=1000, eligible_setups={'HOLD_LONG': True})
    worlds = {k: result for k in ('SELECTED_MODEL', 'HISTORICAL_ANALOGS', 'VOLATILITY_1_5X', 'COST_3X', 'ANALOG_COST_3X', 'FRONT_LOADED_DRIFT', 'BACK_LOADED_DRIFT', 'ADVERSE_JUMP')}
    bad = _candidate(worlds, analog={'in_support': False}, analog_problem=None, feedback={'blocks_candidate': False}, capital=10000, policy=LabPolicy())
    assert bad['strategy_id'] == 'WAIT'
    prior = [{'sample_id': str(i), 'selected_interval_90': [-.001,.001], 'path_log_returns': [0,.01]} for i in range(20)]
    feedback = _feedback(prior, LabPolicy())
    assert feedback['blocks_candidate'] and feedback['observed_90_interval_coverage'] == 0
    blocked = _candidate(worlds, analog={'in_support': True}, analog_problem=None, feedback=feedback, capital=10000, policy=LabPolicy())
    assert blocked['strategy_id'] == 'WAIT'
    assert not _feedback(prior[:19], LabPolicy())['blocks_candidate']


def test_full_path_waits_for_interior_receipt_not_only_terminal():
    from apex.data import normalize
    origin = 1789394460.0  # 2026-09-14 regular session
    raw = [{'kind':'bar','symbol':'SPY','event_epoch':origin-60+k*60,
            'available_epoch':origin+k*60+(300 if k==1 else 0), 'availability_basis':'MEASURED_RECEIPT',
            'open':100.,'high':101.,'low':99.,'close':100.+k/100,'volume':100} for k in range(4)]
    obs,_ = normalize({'schema':'APEX_DATA_V1','observations':raw})
    origin_row = next(b for b in obs if b['event_epoch']==origin-60)
    sample = {'sample_id':'a','price_origin_epoch':origin,'decision_epoch':origin+5,'spot':100.,
              'features':{},'regime_name':'RANGE','origin_bar_id':origin_row['observation_id']}
    assert _path_episode(sample, obs, origin+180,'SPY',3) is None
    completed = _path_episode(sample, obs, origin+360,'SPY',3)
    assert completed and completed['label_available_epoch'] == origin+360


def test_bad_evidence_budget_refuses_before_a_run_directory_is_created():
    for key,value in [('minimum_evidence_sessions',3),('bootstrap_samples',50),('minimum_recent_coverage',float('nan'))]:
        with pytest.raises(Refused):
            LabPolicy(**{key:value})


def test_existing_run_directory_is_never_overwritten(lab_flight):
    out, doc, plan, config, *_ = lab_flight
    before = (out/'COMPLETE').read_bytes()
    with pytest.raises(FileExistsError):
        run_lab(canonical(doc).encode(),out,plan=plan,config=config)
    assert (out/'COMPLETE').read_bytes()==before


def test_equal_terminal_forecasts_can_produce_different_barrier_outcomes():
    grid = np.arange(16)/15
    front, back = np.sqrt(grid)*.01, grid**2*.01
    costs = SimulationCosts(half_spread_bps=0, slippage_bps=0, commission_per_share=0, minimum_commission=0)
    results = [evaluate_strategies(np.asarray([p]), spot=100, capital=10000, max_notional=2000,
                eligible_setups={'MOMENTUM_BREAKOUT_LONG': True}, costs=costs, fixed_quantity=10,
                earliest_entry_step=1)['strategies']['MOMENTUM_BREAKOUT_LONG'] for p in (front,back)]
    assert front[-1] == back[-1] == .01
    assert results[0]['paths']['entry_step'] == [1]
    assert results[1]['paths']['entry_step'] == [7]
    assert results[0]['summary']['expected_net'] == pytest.approx(1000*(np.exp(.01)-np.exp(front[1])))
    assert results[1]['summary']['expected_net'] == pytest.approx(1000*(np.exp(.01)-np.exp(back[7])))
    assert results[0]['summary']['expected_net'] != results[1]['summary']['expected_net']


def test_selected_policy_denominator_keeps_forecast_refused_wait():
    from apex.strategy_lab import summarize_lab
    from apex.strategy_simulation import registry_record
    ids = [s['strategy_id'] for s in registry_record()['strategies']]
    def outcome(sample_id, selected, interval, gain):
        return {'kind':'STRATEGY_OUTCOME', 'payload':{'sample_id':sample_id,'session':'2026-09-14',
            'phase':'HOLDOUT','selected_interval_90':interval, 'chosen_strategy':selected,
            'net_returns':{name:(gain if name=='HOLD_LONG' else 0.) for name in ids}}}
    rows = [outcome('one','HOLD_LONG',[-.01,.01],.01), outcome('two','WAIT',None,0.),
            {'kind':'LAB_CLOSE','payload':{'pending_samples':[]}}]
    result = summarize_lab(rows,policy=LabPolicy(),config=Config())
    assert result['heldout_selected_policy_mean_return_on_fixed_research_capital'] == .005
    assert result['empirical_strategy_evidence']['HOLDOUT']['opportunity_count'] == 2


def test_entire_lab_can_select_an_obvious_positive_control(tmp_path):
    doc, plan = strategy_demo_document(world='positive')
    out = tmp_path/'positive'
    result = run_lab(canonical(doc).encode(),out,plan=plan,config=Config(paths=100,variance='ewma'))
    assert result['research_opportunities'] > 0
    chosen = [r['payload'] for r in read_verified(out/'ledger.jsonl') if r['kind']=='STRATEGY_TOURNAMENT' and r['payload']['candidate']['strategy_id']!='WAIT']
    assert chosen
    for p in chosen:
        winner = next(c for c in p['candidate']['rankings'] if c['strategy_id']==p['candidate']['strategy_id'])
        assert winner['robust_mean_log_growth'] > 0 and winner['refusal'] is None
        assert p['candidate']['capital_authority']=='NONE_RESEARCH_ONLY'
        assert p['candidate']['earliest_entry_grid_step']==1
    assert result['account_pnl'] is None and result['orders']==result['fills']==0
