"""Causal regime-conditioned strategy experiments; no broker or capital authority.

The laboratory reads the actual chronological forecast artifacts, then walks
those decision instants in order. A future path may enter analogue memory or
feedback only after every constituent bar is available. A strategy opportunity
is a counterfactual research object; it is never a fill or a portfolio return.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .analog_worlds import sample_analog_worlds
from .core import Config, Refused, canonical, code_manifest, digest, finite
from .data import normalize, regular, session, visible
from .decision import quote_at
from .ledger import Ledger, read_verified
from .regimes import classify_regime
from .research import ResearchPlan, _selection, run_research
from .strategy_evidence import session_bootstrap
from .strategy_simulation import SimulationCosts, evaluate_strategies, registry_record


@dataclass(frozen=True)
class LabPolicy:
    minimum_analogs: int = 20
    maximum_analogs: int = 40
    minimum_analog_sessions: int = 3
    volatility_stress: float = 1.5
    cost_stress: float = 3.0
    jump_sigma: float = 3.0
    max_stressed_loss_fraction: float = .02
    minimum_feedback_samples: int = 20
    feedback_window: int = 30
    minimum_recent_coverage: float = .60
    minimum_evidence_sessions: int = 10
    bootstrap_samples: int = 2000

    def __post_init__(self):
        for name in ('minimum_analogs', 'maximum_analogs', 'minimum_analog_sessions',
                     'minimum_feedback_samples', 'feedback_window', 'minimum_evidence_sessions', 'bootstrap_samples'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 2:
                raise Refused('INVALID_LAB_POLICY:' + name)
        if self.maximum_analogs < self.minimum_analogs or self.feedback_window < self.minimum_feedback_samples:
            raise Refused('INVALID_LAB_WINDOW')
        if self.minimum_evidence_sessions < 10 or self.bootstrap_samples < 100:
            raise Refused('INVALID_LAB_EVIDENCE_BUDGET')
        for name in ('volatility_stress', 'cost_stress', 'jump_sigma'):
            if not finite(getattr(self, name)) or not 1 <= getattr(self, name) <= 10:
                raise Refused('INVALID_LAB_STRESS:' + name)
        if not finite(self.max_stressed_loss_fraction) or not finite(self.minimum_recent_coverage) or not 0 < self.max_stressed_loss_fraction < 1 or not 0 < self.minimum_recent_coverage < .9:
            raise Refused('INVALID_LAB_RISK_OR_COVERAGE')


def _compact_evaluation(result):
    """Keep full-output commitments and sufficient input paths to reconstruct.

    The evaluator returns per-scenario executions too. Persist their digest;
    storing six duplicate JSON copies of each 1000-path grid adds no evidence.
    """
    return {k: ({sid: {key: value for key, value in item.items() if key != 'paths'}
                        for sid, item in v.items()} if k == 'strategies' else v)
            for k, v in result.items()}


def _cost_stress(costs, multiplier):
    values = asdict(costs)
    return SimulationCosts(**{k: v * multiplier if isinstance(v, (int, float)) else v for k, v in values.items()})


def _feedback(previous, policy):
    items = [x for x in previous if x.get('selected_interval_90') is not None][-policy.feedback_window:]
    count = len(items)
    coverage = float(np.mean([x['selected_interval_90'][0] <= x['path_log_returns'][-1] <= x['selected_interval_90'][1] for x in items])) if items else None
    alarm = count >= policy.minimum_feedback_samples and coverage < policy.minimum_recent_coverage
    return {'status': 'MISSPECIFICATION_ALERT' if alarm else 'MONITORING' if count >= policy.minimum_feedback_samples else 'INSUFFICIENT_MATURED_FEEDBACK',
            'sample_ids': [x['sample_id'] for x in items], 'samples': count,
            'observed_90_interval_coverage': coverage, 'blocks_candidate': alarm,
            'scope': 'Predeclared rolling diagnostic, not conditional calibration or a statistical change-point guarantee'}


def _candidate(worlds, *, analog, analog_problem, feedback, capital, policy):
    required = ('SELECTED_MODEL', 'HISTORICAL_ANALOGS', 'VOLATILITY_1_5X', 'COST_3X', 'ANALOG_COST_3X',
                'FRONT_LOADED_DRIFT', 'BACK_LOADED_DRIFT')
    rankings = []
    if analog_problem:
        reason = 'ANALOG_MODEL_UNAVAILABLE:' + analog_problem
    elif not analog.get('in_support', False):
        reason = 'CURRENT_STATE_OUTSIDE_ANALOG_SUPPORT'
    elif feedback['blocks_candidate']:
        reason = 'RECENT_FORECAST_MISSPECIFICATION'
    else:
        reason = None
    for sid, item in worlds['SELECTED_MODEL']['strategies'].items():
        if sid == 'WAIT':
            continue
        entry = {'strategy_id': sid, 'eligible': item['eligible'], 'robust_mean_log_growth': None,
                 'adverse_cvar_05_net': None, 'refusal': item.get('reason') if not item['eligible'] else reason}
        if entry['refusal'] is None and all(name in worlds for name in required):
            values = [worlds[name]['strategies'][sid]['summary']['mean_log_growth'] for name in required]
            if any(v is None or not finite(v) for v in values):
                entry['refusal'] = 'CAPITAL_EXHAUSTION_OR_UNDEFINED_LOG_GROWTH'
                rankings.append(entry)
                continue
            entry['robust_mean_log_growth'] = min(values)
            entry['adverse_cvar_05_net'] = worlds['ADVERSE_JUMP']['strategies'][sid]['summary']['cvar_05_net']
            if entry['adverse_cvar_05_net'] < -capital * policy.max_stressed_loss_fraction:
                entry['refusal'] = 'ADVERSE_STRESS_LOSS_ABOVE_RESEARCH_LIMIT'
            elif entry['robust_mean_log_growth'] <= 0:
                entry['refusal'] = 'NO_POSITIVE_GROWTH_ACROSS_REQUIRED_WORLDS'
        rankings.append(entry)
    eligible = [x for x in rankings if x['refusal'] is None and x['robust_mean_log_growth'] is not None]
    chosen = min(eligible, key=lambda x: (-x['robust_mean_log_growth'], x['strategy_id'])) if eligible else None
    return {'strategy_id': chosen['strategy_id'] if chosen else 'WAIT',
            'status': 'RESEARCH_OPPORTUNITY' if chosen else 'WAIT', 'rankings': rankings,
            'selection': 'Maximize the minimum modeled mean log growth across seven named worlds; WAIT has zero growth',
            'uncertainty': 'Model-conditioned ranking, not an estimated real success probability. Resampled analog count does not increase historical sample size.',
            'capital_authority': 'NONE_RESEARCH_ONLY'}


def _path_episode(sample, observations, now, symbol, horizon):
    bars, _ = visible(observations, now=now, symbol=symbol, kind='bar')
    by_close = {b['event_epoch'] + 60: b for b in bars if regular(b['event_epoch'])}
    origin = sample['price_origin_epoch']
    selected = [by_close.get(origin + k * 60) for k in range(horizon + 1)]
    if any(b is None for b in selected) or any(session(b['event_epoch']) != session(origin) for b in selected):
        return None
    # Revisions/conflicts may invalidate the origin after prediction: never
    # silently score a different price from the one actually forecast.
    if selected[0]['observation_id'] != sample['origin_bar_id']:
        return None
    path = [math.log(b['close'] / sample['spot']) for b in selected]
    return {'sample_id': sample['sample_id'], 'decision_epoch': sample['decision_epoch'],
            'price_origin_epoch': origin, 'target_epoch': origin + horizon * 60,
            'label_available_epoch': max(b['available_epoch'] for b in selected),
            'features': sample['features'], 'path_log_returns': path,
            'regime': sample['regime_name'], 'bar_ids': [b['observation_id'] for b in selected]}


def lab_events(source_rows, observations, *, plan, config, policy, costs, load_paths):
    """Deterministic downstream reader. Emits expected artifacts for a verifier too."""
    grouped = defaultdict(list)
    for row in source_rows:
        grouped[row['epoch']].append(row)
    pending, samples, episodes, outcomes, scores = {}, {}, [], [], []
    frozen_selection = None
    for now, source_group in sorted(grouped.items()):
        # Outcomes enter feedback before decisions at this same actual instant,
        # but never before every minute in the path has become available.
        for sample_id, item in list(pending.items()):
            if item['target_epoch'] > now:
                continue
            episode = _path_episode(item, observations, now, config.symbol, config.horizon_minutes)
            if episode is None:
                continue
            episodes.append(episode)
            result = evaluate_strategies(np.asarray([episode['path_log_returns']]), spot=item['spot'],
                capital=float(config.starting_cash), max_notional=float(config.max_notional),
                eligible_setups=item['eligible_setups'], costs=costs, vwap=item['vwap'], fixed_quantity=item.get('fixed_quantity'),
                earliest_entry_step=item['earliest_entry_step'])
            strategy_outcomes = {sid: result['strategies'][sid]['summary']['expected_net'] for sid in result['strategies']}
            realized = {**episode, 'session': session(item['decision_epoch']), 'phase': item['phase'],
                        'strategy_outcomes': strategy_outcomes, 'net_returns': {k: v / float(config.starting_cash) for k, v in strategy_outcomes.items()},
                        'chosen_strategy': item.get('chosen_strategy', 'WAIT'),
                        'selected_interval_90': item.get('selected_interval_90'),
                        'execution_reconstruction': _compact_evaluation(result),
                        'basis': 'COUNTERFACTUAL_MINUTE_CLOSE_PATH_WITH_ASSUMED_COSTS_NOT_FILLS_OR_ACCOUNT_PNL'}
            outcomes.append(realized)
            yield 'STRATEGY_OUTCOME', now, realized, None
            del pending[sample_id]
        for row in source_group:
            p, kind = row['payload'], row['kind']
            if kind == 'RESEARCH_SCORE':
                scores.append(p)
            elif kind == 'SELECTION_FROZEN':
                frozen_selection = p
                yield 'FORECAST_SELECTION_FROZEN', now, p, None
            elif kind == 'RESEARCH_SAMPLE':
                state = p['snapshot']
                intelligence = classify_regime(observations, now=now, symbol=config.symbol)
                today, _ = visible(observations, now=now, symbol=config.symbol, kind='bar')
                origin = next(b for b in today if b['event_epoch'] + 60 == p['price_origin_epoch'])
                item = {k: p[k] for k in ('sample_id', 'decision_epoch', 'price_origin_epoch', 'target_epoch', 'spot', 'features', 'phase')}
                item.update(origin_bar_id=origin['observation_id'], regime_name=intelligence['regime'],
                            eligible_setups=intelligence['eligible_setups'], vwap=state['fields']['close_weighted_vwap_proxy'],
                            earliest_entry_step=math.ceil((now - p['price_origin_epoch']) / 60))
                samples[p['sample_id']] = item
                pending[p['sample_id']] = item
                yield 'INTELLIGENCE', now, {'sample_id': p['sample_id'], 'source_sample_hash': row['hash'], 'intelligence': intelligence,
                                           'consumption': {'regime': 'analog distance and setup eligibility', 'eligible_setups': 'strategy evaluator'}}, None
            elif kind == 'RESEARCH_FORECAST':
                item = samples[p['sample_id']]
                source_selection = frozen_selection or _selection(scores, plan)
                name = source_selection['selected_model']
                if name not in p['variants']:
                    name = 'ZERO_DRIFT'
                base = p['base_forecast']
                original = load_paths(p['base_path_file'])
                h = config.horizon_minutes
                grid = np.arange(h + 1) / h
                noise = original - grid * base['direction']['per_minute_log_drift'] * h
                drift = p['variants'][name]['total_log_drift']
                selected_paths = noise + grid * drift
                cutoff = min(now, plan.holdout_start) - plan.embargo_minutes * 60
                train = [e for e in episodes if e['target_epoch'] <= cutoff and e['label_available_epoch'] <= cutoff][-plan.training_window:]
                analog, analog_paths, problem = None, None, None
                try:
                    analog, analog_paths = sample_analog_worlds(train, item['features'], cutoff=cutoff, now=now,
                        horizon_minutes=h, paths=config.paths, seed=int(digest({'sample': item['sample_id'], 'seed': config.seed, 'purpose': 'analog'})[:8], 16),
                        minimum_neighbors=policy.minimum_analogs, maximum_neighbors=policy.maximum_analogs,
                        minimum_training_sessions=policy.minimum_analog_sessions, regime=item['regime_name'])
                except Refused as exc:
                    problem = str(exc)
                stressed_costs = _cost_stress(costs, policy.cost_stress)
                jump = selected_paths.copy()
                jump_step = min(h, item['earliest_entry_step'] + 1)
                jump[:, jump_step:] -= policy.jump_sigma * float(np.std(noise[:, -1], ddof=1))
                path_worlds = {'SELECTED_MODEL': (selected_paths, costs), 'VOLATILITY_1_5X': (noise * policy.volatility_stress + grid * drift, costs),
                               'COST_3X': (selected_paths, stressed_costs), 'ADVERSE_JUMP': (jump, costs),
                               'ZERO_DRIFT_REFERENCE': (noise, costs),
                               'FRONT_LOADED_DRIFT': (noise + np.sqrt(grid) * drift, costs),
                               'BACK_LOADED_DRIFT': (noise + grid**2 * drift, costs)}
                if analog_paths is not None:
                    path_worlds.update(HISTORICAL_ANALOGS=(analog_paths, costs), ANALOG_COST_3X=(analog_paths, stressed_costs))
                worlds = {}
                for world, (paths, world_costs) in path_worlds.items():
                    result = evaluate_strategies(paths, spot=item['spot'], capital=float(config.starting_cash),
                        max_notional=float(config.max_notional), eligible_setups=item['eligible_setups'], costs=world_costs, vwap=item['vwap'],
                        fixed_quantity=item.get('fixed_quantity'), earliest_entry_step=item['earliest_entry_step'])
                    if world == 'SELECTED_MODEL':
                        item['fixed_quantity'] = result['strategies']['HOLD_LONG']['fixed_quantity']
                    worlds[world] = _compact_evaluation(result)
                feedback = _feedback(outcomes, policy)
                candidate = _candidate(worlds, analog=analog, analog_problem=problem, feedback=feedback,
                                       capital=float(config.starting_cash), policy=policy)
                q, q_problem = quote_at(observations, now, config)
                candidate['quote_id'] = q['observation_id'] if q else None
                candidate['observed_quote_status'] = q_problem or 'RETRIEVED_UNUSED_IN_BAR_ORIGIN_ECONOMICS'
                candidate['execution_readiness'] = 'BAR_ORIGIN_RESEARCH_REQUIRES_DECISION_QUOTE_REVALUATION'
                candidate['economics_price_origin_epoch'] = item['price_origin_epoch']
                candidate['actual_decision_epoch'] = now
                candidate['economics_price_basis'] = 'PRECEDING_COMPLETED_BAR_CLOSE_NOT_EXECUTABLE_DECISION_QUOTE'
                candidate['earliest_entry_grid_step'] = item['earliest_entry_step']
                candidate['holding_period_basis'] = 'Absolute forecast target; delayed trigger shortens holding time, never extends the target'
                item['chosen_strategy'] = candidate['strategy_id']
                item['selected_interval_90'] = np.quantile(selected_paths[:, -1], [.05, .95]).tolist()
                record = {'sample_id': item['sample_id'], 'phase': item['phase'], 'source_forecast_hash': row['hash'],
                          'forecast_id': base['forecast_id'], 'selected_forecast_model': name,
                          'selection_basis': source_selection, 'training_cutoff_epoch': cutoff,
                          'regime': item['regime_name'], 'eligible_setups': item['eligible_setups'],
                          'analog': analog, 'analog_problem': problem, 'feedback': feedback,
                          'worlds': worlds, 'candidate': candidate,
                          'scenario_path_count_per_world': config.paths,
                          'common_world_scope': 'All strategies see identical paths within each world; different hypotheses are not claimed to share innovations',
                          'path_timing_assumption': 'Ridge predicts terminal return, not drift timing. Linear, square-root and squared drift schedules share the terminal shift but challenge barrier outcomes.',
                          'stress_parameters': {'volatility_multiplier': policy.volatility_stress, 'cost_multiplier': policy.cost_stress,
                                                'jump_terminal_sigma_multiplier': policy.jump_sigma, 'jump_step': jump_step},
                          'world_name_basis': 'Names identify default-policy roles; stress_parameters carries actual frozen values',
                          'adverse_jump_basis': 'Declared terminal-return standard-deviation multiple applied after the first permitted entry grid; stress magnitude only, no assigned probability',
                          'authority': 'NONE_RESEARCH_ONLY'}
                yield 'STRATEGY_TOURNAMENT', now, record, None
    yield 'LAB_CLOSE', plan.end, {'pending_samples': sorted(pending),
        'pending_by_phase': {phase: sorted(sid for sid, item in pending.items() if item['phase'] == phase)
                             for phase in ('WARMUP', 'DEVELOPMENT', 'HOLDOUT')},
        'completed_episodes': len(episodes)}, None


def summarize_lab(rows, *, policy, config):
    tournaments = [r['payload'] for r in rows if r['kind'] == 'STRATEGY_TOURNAMENT']
    outcomes = [r['payload'] for r in rows if r['kind'] == 'STRATEGY_OUTCOME']
    facts = [r['payload']['intelligence'] for r in rows if r['kind'] == 'INTELLIGENCE']
    ids = [s['strategy_id'] for s in registry_record()['strategies']]
    empirical = {}
    for phase in ('DEVELOPMENT', 'HOLDOUT'):
        paired = [o for o in outcomes if o['phase'] == phase]
        complete = not rows[-1]['payload'].get('pending_by_phase', {}).get(phase)
        empirical[phase] = session_bootstrap(paired, strategy_ids=ids, minimum_sessions=policy.minimum_evidence_sessions,
            bootstrap_samples=policy.bootstrap_samples, seed=config.seed, opportunity_coverage_complete=complete)
    chosen = Counter(t['candidate']['strategy_id'] for t in tournaments)
    hold = [o for o in outcomes if o['phase'] == 'HOLDOUT']
    selected_mean = float(np.mean([o['net_returns'][o['chosen_strategy']] for o in hold])) if hold else None
    return {'schema': 'APEX_STRATEGY_LAB_RESULT_V1', 'status': 'RESEARCH_COMPLETE',
            'intelligence_snapshots': len(facts), 'tournaments': len(tournaments),
            'strategy_scenario_evaluations': sum(sum(len(w['strategies']) * t['scenario_path_count_per_world'] for w in t['worlds'].values()) for t in tournaments),
            'regimes': dict(Counter(t['regime'] for t in tournaments)),
            'selected_strategy_counts': dict(chosen), 'research_opportunities': sum(v for k, v in chosen.items() if k != 'WAIT'),
            'analog_refusals': dict(Counter(t['analog_problem'] for t in tournaments if t['analog_problem'])),
            'feedback_alerts': sum(t['feedback']['blocks_candidate'] for t in tournaments),
            'completed_counterfactual_episodes': len(outcomes),
            'heldout_selected_policy_mean_return_on_fixed_research_capital': selected_mean,
            'selected_policy_evidence_scope': 'Descriptive opportunity mean, not covered by the five fixed-strategy family test and not a compounded portfolio return',
            'empirical_strategy_evidence': empirical,
            'hypothesis_registry': registry_record(), 'hypothesis_count': len(ids),
            'pending_samples': rows[-1]['payload']['pending_samples'],
            'orders': 0, 'fills': 0, 'account_pnl': None,
            'readiness': 'RESEARCH_ONLY_NO_STRATEGY_PROMOTION_OR_BROKER_AUTHORITY',
            'limitations': ['Selection metrics are model-conditioned, not calibrated success probabilities',
                'Historical paths and bar-close executions omit intraminute order, queue, spreads and executable fills',
                'All reported strategy returns use fixed illustrative capital and costs, not an account equity curve',
                'Repeated dataset use is not an untouched holdout; finite-family diagnostics do not cover earlier experiments',
                'No options, shorting, LLM news generation or cross-symbol allocation in this release',
                'No exchange holiday/early-close calendar; observed weekday-session coverage only']}


def run_lab(input_path, out: Path, *, plan: ResearchPlan, config: Config, policy: LabPolicy | None = None,
            costs: SimulationCosts | None = None):
    policy, costs = policy or LabPolicy(), costs or SimulationCosts()
    out.mkdir(parents=True, exist_ok=False)
    raw = input_path.read_bytes() if isinstance(input_path, Path) else input_path
    manifest = {'schema': 'APEX_STRATEGY_LAB_MANIFEST_V1', 'input_sha256': hashlib.sha256(raw).hexdigest(),
                'plan': asdict(plan), 'config': config.record(), 'policy': asdict(policy), 'costs': asdict(costs),
                'registry': registry_record(), 'code': code_manifest(), 'authority': 'NONE_RESEARCH_ONLY'}
    (out / 'manifest.json').write_text(canonical(manifest))
    ledger = None
    try:
        run_research(raw, out / 'forecast', plan=plan, config=config)
        source_rows = read_verified(out / 'forecast' / 'ledger.jsonl')
        observations, _ = normalize(json.loads(raw))
        ledger = Ledger(out / 'ledger.jsonl')
        ledger.append('RUN_OPEN', plan.start, {'manifest_digest': digest(manifest), 'forecast_head': source_rows[-1]['hash'],
                                            'config': config.record(), 'input_class': source_rows[0]['payload']['input_class']})
        for kind, now, record, _ in lab_events(source_rows, observations, plan=plan, config=config, policy=policy, costs=costs,
                load_paths=lambda filename: np.load(out / 'forecast' / filename, allow_pickle=False)):
            ledger.append(kind, now, record)
        ledger.close()
        ledger = None
        rows = read_verified(out / 'ledger.jsonl')
        summary = summarize_lab(rows, policy=policy, config=config)
        (out / 'summary.json').write_text(canonical(summary))
        (out / 'COMPLETE').write_text(rows[-1]['hash'])
        return summary
    except Exception as exc:
        if ledger:
            ledger.close()
        (out / 'FAILED.json').write_text(canonical({'error': type(exc).__name__, 'reason': str(exc)}))
        raise
