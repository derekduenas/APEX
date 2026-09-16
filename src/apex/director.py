"""Bounded AI-directed research orchestration with a rechecking completion reader.

The planner selects one reviewed experiment or waits. Deterministic code owns
policy, input paths, verification and reporting. There is no broker adapter,
order tool, threshold mutation, autonomous code execution, or capital authority.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import hashlib
from pathlib import Path
import re

from .ai_planner import (ACTIONS, SYSTEM_PROMPT, make_request, proposal_schema,
                         request_plan, strict_json, symbol_valid, validate_proposal)
from .core import Config, Refused, canonical, code_manifest, digest, finite
from .data import normalize, regular, session
from .ledger import Ledger, read_complete
from .research import ResearchPlan, input_class_of
from .strategy_lab import LabPolicy, run_lab
from .strategy_lab_verification import verify_lab
from .strategy_simulation import SimulationCosts


AUTHORITY = 'NONE_RESEARCH_ONLY'
MAX_INPUT_BYTES = 128 * 1024 * 1024
POLICY = {
    'schema': 'APEX_DIRECTOR_POLICY_V1',
    'allowed_actions': list(ACTIONS), 'research_attempt_budget': 1,
    'maximum_input_bytes': MAX_INPUT_BYTES, 'maximum_observations': 200000,
    'maximum_paths': 2000, 'maximum_planned_decisions': 5000,
    'maximum_horizon_minutes': 60, 'maximum_training_returns': 5000,
    'maximum_training_window': 2000, 'capital_authority': AUTHORITY,
    'broker_tools': [], 'policy_mutation_allowed': False,
    'repeated_experiment_control': 'NOT_STATISTICALLY_CONTROLLED_ACROSS_RUNS',
    'laboratory_policy': asdict(LabPolicy()),
    'simulation_costs': asdict(SimulationCosts()),
    'holdout_review': 'Descriptive only: negative CRPS delta versus OWN_STOCK_RIDGE and positive '
        'absolute and incremental mean policy net at base and triple costs are required to request replication; '
        'any missing metric, paired sample or observed session requires more holdout data.',
}
SCOPE = (
    'Verifies retained input, proposal, policy, stage ordering, child completion and '
    'recomputes the report using independently invoked child readers. Shared calculations '
    'are not a second mathematical implementation. Hashes do not authenticate provider '
    'history, LLM authorship, executed source or prospective registration. No edge, '
    'calibration, live execution readiness or trading authority is established.'
)


def _require(condition, reason):
    if not condition:
        raise Refused(reason)


def _same(left, right):
    return canonical(left) == canonical(right)


def _write(path, value):
    with path.open('x') as handle:
        handle.write(canonical(value))


def _read(path):
    _require(path.is_file() and not path.is_symlink(), 'DIRECTOR_ARTIFACT_UNAVAILABLE')
    return strict_json(_bounded_bytes(path, 8 * 1024 * 1024))


def _bounded_bytes(path, limit):
    _require(path.is_file() and not path.is_symlink(), 'DIRECTOR_ARTIFACT_UNAVAILABLE')
    _require(path.stat().st_size <= limit, 'DIRECTOR_ARTIFACT_BYTES_EXCEEDED')
    with path.open('rb') as handle:
        raw = handle.read(limit + 1)
    _require(len(raw) <= limit, 'DIRECTOR_ARTIFACT_BYTES_EXCEEDED')
    return raw


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _preflight(raw, plan, config, symbols):
    _require(isinstance(plan, ResearchPlan) and isinstance(config, Config), 'DIRECTOR_TYPED_PLAN_CONFIG_REQUIRED')
    _require(_same(asdict(ResearchPlan(**asdict(plan))), asdict(plan))
             and _same(Config(**config.record()).record(), config.record()), 'DIRECTOR_INVALID_DECLARED_CONTRACT')
    _require(all(symbol_valid(s) for s in symbols.values()) and len(set(symbols.values())) == 3,
             'DIRECTOR_REQUIRES_THREE_DISTINCT_VALID_SYMBOLS')
    _require(plan.scan_minutes >= config.horizon_minutes, 'DIRECTOR_OVERLAPPING_OPPORTUNITIES')
    _require(config.paths <= POLICY['maximum_paths'] and config.horizon_minutes <= POLICY['maximum_horizon_minutes']
             and config.training_returns <= POLICY['maximum_training_returns']
             and plan.training_window <= POLICY['maximum_training_window']
             and (plan.end - plan.start) / (plan.scan_minutes * 60) <= POLICY['maximum_planned_decisions'],
             'DIRECTOR_COMPUTE_BUDGET_EXCEEDED')
    _require(len(raw) <= MAX_INPUT_BYTES, 'DIRECTOR_INPUT_BYTES_EXCEEDED')
    document = strict_json(raw)
    _require(isinstance(document, dict) and isinstance(document.get('observations'), list), 'DIRECTOR_INPUT_INVALID')
    _require(len(document['observations']) <= POLICY['maximum_observations'], 'DIRECTOR_OBSERVATION_BUDGET_EXCEEDED')
    observations, rejected = normalize(document)
    from .admissibility import require_mode
    require_mode(document, observations, "SYNTHETIC_CONTROL" if input_class_of(document, observations) == "SYNTHETIC_RESEARCH_CONTROL" else "OFFLINE_RESEARCH")
    _require(not rejected and observations, 'DIRECTOR_INVALID_OR_EMPTY_OBSERVATIONS')
    bars = [b for b in observations if b['kind'] == 'bar' and regular(b['event_epoch'])
            and plan.start <= b['event_epoch'] < plan.end and b['available_epoch'] <= plan.end]
    counts = Counter(b['symbol'] for b in bars)
    _require(all(counts[s] > 0 for s in symbols.values()), 'DIRECTOR_MISSING_REQUIRED_SYMBOL_BARS')
    return {'input_class': input_class_of(document, observations),
            'observation_count': len(observations), 'rejected_observations': len(rejected),
            'symbols': symbols,
            'bar_counts': {s: counts[s] for s in symbols.values()},
            'observed_sessions': {s: len({session(b['event_epoch']) for b in bars if b['symbol'] == s})
                                  for s in symbols.values()},
            'coverage_scope': 'Observed regular-session bars; metadata does not establish completeness or original availability',
            'source_text_sent_to_planner': False, 'outcomes_sent_to_planner': False}


def _request_record(context, symbols, model):
    return {'schema': 'APEX_DIRECTOR_REQUEST_V1', 'system_prompt': SYSTEM_PROMPT,
            'context': context, 'response_schema': proposal_schema(symbols), 'model': model}


def _planner_record(planned, context, symbols, model):
    _require(isinstance(planned, dict), 'DIRECTOR_PLANNER_RECORD_INVALID')
    required = {'proposal', 'provenance', 'model', 'request_digest', 'response_sha256'}
    _require(set(planned) == required, 'DIRECTOR_PLANNER_RECORD_INVALID')
    validate_proposal(planned['proposal'], symbols)
    provenance = planned['provenance']
    _require(planned['model'] == model, 'DIRECTOR_PLANNER_MODEL_DISAGREES')
    if provenance == 'OPENAI_RESPONSES_NOT_INDEPENDENTLY_AUTHENTICATED':
        _require(planned['request_digest'] == digest(make_request(context, symbols, model)),
                 'DIRECTOR_AI_REQUEST_NOT_BOUND')
        _require(isinstance(planned['response_sha256'], str)
                 and re.fullmatch('[0-9a-f]{64}', planned['response_sha256']), 'DIRECTOR_AI_RESPONSE_DIGEST_INVALID')
    elif provenance == 'CODEX_CHATGPT_CLI_NOT_INDEPENDENTLY_AUTHENTICATED':
        from .codex_planner import make_codex_request
        _require(planned['request_digest'] == digest(make_codex_request(context, symbols, model)),
                 'DIRECTOR_CODEX_REQUEST_NOT_BOUND')
        _require(isinstance(planned['response_sha256'], str)
                 and re.fullmatch('[0-9a-f]{64}', planned['response_sha256']), 'DIRECTOR_AI_RESPONSE_DIGEST_INVALID')
    elif provenance == 'SYNTHETIC_PLANNER_TEST_ONLY':
        _require(context['input_class'] == 'SYNTHETIC_RESEARCH_CONTROL'
                 and planned['request_digest'] is None and planned['response_sha256'] is None,
                 'DIRECTOR_SYNTHETIC_PLANNER_REQUIRES_SYNTHETIC_INPUT')
    elif provenance == 'EXTERNAL_PROPOSAL_NOT_AUTHENTICATED':
        _require(model is None and planned['request_digest'] is None and planned['response_sha256'] is None,
                 'DIRECTOR_EXTERNAL_PROVENANCE_INVALID')
    else:
        raise Refused('DIRECTOR_PLANNER_PROVENANCE_INVALID')
    return planned


def _assess_holdout(peer):
    holdout = peer.get('holdout', {}) if peer else {}
    models = holdout.get('models', {})
    own, augmented = models.get('OWN_STOCK_RIDGE', {}), models.get('PEER_LAG_RIDGE', {})
    values = {
        'crps_delta_vs_own_stock': holdout.get('paired_crps_delta_vs_own_stock'),
        'peer_policy_mean_net': augmented.get('policy_mean_net'),
        'own_stock_policy_mean_net': own.get('policy_mean_net'),
        'peer_policy_mean_net_cost_3x': augmented.get('policy_mean_net_cost_3x'),
        'own_stock_policy_mean_net_cost_3x': own.get('policy_mean_net_cost_3x'),
    }
    count, sessions = holdout.get('paired_samples', 0), holdout.get('sessions', [])
    enough = type(count) is int and count > 0 and len(sessions) > 0 and all(finite(v) for v in values.values())
    base_delta = values['peer_policy_mean_net'] - values['own_stock_policy_mean_net'] if enough else None
    stress_delta = values['peer_policy_mean_net_cost_3x'] - values['own_stock_policy_mean_net_cost_3x'] if enough else None
    improves = enough and values['crps_delta_vs_own_stock'] < 0 and base_delta > 0 and stress_delta > 0 \
        and values['peer_policy_mean_net'] > 0 and values['peer_policy_mean_net_cost_3x'] > 0
    return {'status': 'INSUFFICIENT_HOLDOUT' if not enough else
                     'DESCRIPTIVE_IMPROVEMENT_REQUIRES_REPLICATION' if improves else 'HYPOTHESIS_NOT_SUPPORTED',
            'paired_samples': count, 'observed_session_count': len(sessions), 'observed_sessions': sessions,
            'metrics': values, 'policy_mean_net_delta_vs_own_stock': base_delta,
            'policy_mean_net_cost_3x_delta_vs_own_stock': stress_delta,
            'next_action': 'COLLECT_SUFFICIENT_HOLDOUT' if not enough else
                           'REPLICATE_WITH_UNSEEN_DATA_AND_QUOTES' if improves else
                           'REJECT_OR_REVISE_PEER_HYPOTHESIS_ON_NEW_DATA',
            'scope': 'Descriptive paired-sample review only; no significance, independence, calibration or market-edge claim'}


def _summary(manifest, proposal, planner, lab=None, peer=None):
    ran = lab is not None and peer is not None
    evidence = None
    if ran:
        _require(lab['orders'] == lab['fills'] == peer['orders'] == peer['fills'] == 0
                 and lab['account_pnl'] is None and peer['account_pnl'] is None,
                 'DIRECTOR_CHILD_CAPITAL_AUTHORITY_VIOLATION')
        evidence = {'laboratory': {key: lab[key] for key in (
            'tournaments', 'strategy_scenario_evaluations', 'research_opportunities',
            'completed_counterfactual_episodes', 'selected_strategy_counts', 'regimes')},
            'peer_experiment': peer}
    has_evidence = ran and peer['counts']['forecasts'] > 0 and peer['counts']['paired_outcomes'] > 0
    assessment = _assess_holdout(peer)
    return {'schema': 'APEX_DIRECTOR_REPORT_V1', 'status': 'RESEARCH_COMPLETE' if ran else 'WAIT',
            'decision': 'EXPLORATORY_EVIDENCE_ONLY' if has_evidence else 'WAIT',
            'reason': 'REVIEW_HELDOUT_EXPLORATORY_RESULTS' if has_evidence else
                      'NO_MATURED_PEER_FORECAST_EVIDENCE' if ran else 'PLANNER_DEFERRED_MISSING_DATA',
            'input_class': manifest['context']['input_class'],
            'planner_provenance': planner['provenance'], 'model': planner['model'],
            'runtime_llm_call_completed': planner['provenance'] in (
                'OPENAI_RESPONSES_NOT_INDEPENDENTLY_AUTHENTICATED',
                'CODEX_CHATGPT_CLI_NOT_INDEPENDENTLY_AUTHENTICATED'),
            'action': proposal['action'], 'symbols': manifest['symbols'],
            'proposal_digest': digest(proposal), 'research_attempts': int(ran),
            'research_attempt_budget': 1,
            'repeated_experiment_control': POLICY['repeated_experiment_control'],
            'evidence': evidence,
            'hypothesis_assessment': assessment,
            'next_research_action': assessment['next_action'],
            'capital_authority': AUTHORITY, 'orders': 0, 'fills': 0, 'account_pnl': None,
            'readiness': 'RESEARCH_ONLY_NO_STRATEGY_PROMOTION_OR_BROKER_AUTHORITY',
            'limitations': ['The AI chooses one reviewed study or WAIT; it cannot redesign policy or deploy models.',
                'A fresh run directory does not make reused data an untouched holdout or control repeated experimentation.',
                'Synthetic results are controls, not observed market performance.',
                'Minute-bar counterfactual economics are not executable fills or account returns.']}


def _safe_reason(exc):
    reason = str(exc) if isinstance(exc, Refused) else 'DIRECTOR_STAGE_FAILED'
    return reason if re.fullmatch(r'[A-Z0-9_:.\-]{1,160}', reason) else 'DIRECTOR_STAGE_FAILED'


def run_director(input_path: Path, out: Path, *, plan: ResearchPlan, config: Config,
                 market_symbol: str, sector_symbol: str, proposal: dict | None = None,
                 model: str | None = None, codex_model: str | None = None) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    ledger = None
    try:
        _require(sum(x is not None for x in (proposal, model, codex_model)) <= 1,
                 'DIRECTOR_CHOOSE_EXTERNAL_PROPOSAL_OR_AI_MODEL')
        use_codex = codex_model is not None
        if use_codex:
            model = codex_model
        input_path = Path(input_path)
        _require(input_path.is_file() and not input_path.is_symlink(), 'DIRECTOR_INPUT_UNAVAILABLE')
        _require(input_path.stat().st_size <= MAX_INPUT_BYTES, 'DIRECTOR_INPUT_BYTES_EXCEEDED')
        raw = _bounded_bytes(input_path, MAX_INPUT_BYTES)
        symbols = {'target_symbol': config.symbol, 'market_symbol': market_symbol, 'sector_symbol': sector_symbol}
        context = _preflight(raw, plan, config, symbols)
        request = _request_record(context, symbols, model)
        manifest = {'schema': 'APEX_DIRECTOR_MANIFEST_V1', 'input_sha256': _sha(raw),
                    'plan': asdict(plan), 'config': config.record(), 'symbols': symbols,
                    'policy': POLICY, 'context': context, 'request_digest': digest(request),
                    'code': code_manifest(), 'authority': AUTHORITY}
        with (out / 'input.json').open('xb') as handle:
            handle.write(raw)
        _write(out / 'manifest.json', manifest)
        _write(out / 'request.json', request)
        ledger = Ledger(out / 'ledger.jsonl')
        ledger.append('RUN_OPEN', plan.start, {'manifest_digest': digest(manifest), 'authority': AUTHORITY})
        ledger.append('PLAN_REQUEST', plan.start, {'request_digest': digest(request), 'research_attempt_budget': 1})
        if proposal is not None:
            planned = {'proposal': proposal, 'provenance': 'EXTERNAL_PROPOSAL_NOT_AUTHENTICATED',
                       'model': None, 'request_digest': None, 'response_sha256': None}
        elif use_codex:
            from .codex_planner import request_codex_plan
            planned = request_codex_plan(context, symbols, model)
        else:
            planned = request_plan(context, symbols, model)
        planned = _planner_record(planned, context, symbols, model)
        proposal = planned['proposal']
        _write(out / 'proposal.json', proposal)
        _write(out / 'planner.json', planned)
        ledger.append('PLAN_ACCEPTED', plan.start, {'proposal_digest': digest(proposal),
            'planner_digest': digest(planned), 'action': proposal['action'], 'authority': AUTHORITY})
        lab, peer = None, None
        if proposal['action'] == 'RUN_PEER_DISLOCATION_STUDY':
            from .peer_experiment import run_peer_experiment, verify_peer_experiment
            run_lab(out / 'input.json', out / 'laboratory', plan=plan, config=config)
            lab_verification = verify_lab(out / 'laboratory')
            _require(lab_verification['status'] == 'VALID', 'DIRECTOR_LAB_VERIFICATION_FAILED')
            lab = _read(out / 'laboratory' / 'summary.json')
            _write(out / 'laboratory-verification.json', lab_verification)
            ledger.append('LAB_VERIFIED', plan.end, {'summary_digest': digest(lab),
                          'verification_digest': digest(lab_verification)})
            run_peer_experiment(out / 'laboratory', out / 'peer-edge',
                                market_symbol=market_symbol, sector_symbol=sector_symbol)
            peer_verification = verify_peer_experiment(out / 'peer-edge')
            _require(peer_verification['status'] == 'VALID', 'DIRECTOR_PEER_VERIFICATION_FAILED')
            peer = _read(out / 'peer-edge' / 'summary.json')
            _write(out / 'peer-verification.json', peer_verification)
            ledger.append('PEER_EXPERIMENT_VERIFIED', plan.end, {'summary_digest': digest(peer),
                          'verification_digest': digest(peer_verification)})
        report = _summary(manifest, proposal, planned, lab, peer)
        _write(out / 'report.json', report)
        ledger.append('DIRECTOR_CLOSE', plan.end, {'report_digest': digest(report),
                      'research_attempts': report['research_attempts'], 'authority': AUTHORITY})
        rows = ledger.verified_close(expected_last_kind='DIRECTOR_CLOSE', expected_epoch=plan.end)
        ledger = None
        with (out / 'COMPLETE').open('x') as handle:
            handle.write(rows[-1]['hash'])
        return report
    except Exception as exc:
        reason = _safe_reason(exc)
        if ledger is not None:
            try:
                ledger.append('PLAN_REFUSED', plan.end,
                              {'reason': reason, 'authority': AUTHORITY})
            except Exception:
                pass
            try:
                ledger.close()
            except Exception:
                pass
        failure = {'schema': 'APEX_DIRECTOR_FAILURE_V1', 'status': 'REFUSED', 'decision': 'REFUSED',
                   'reason': reason, 'error_type': type(exc).__name__, 'capital_authority': AUTHORITY,
                   'orders': 0, 'fills': 0, 'account_pnl': None}
        _write(out / 'FAILED.json', failure)
        if not (out / 'report.json').exists():
            _write(out / 'report.json', failure)
        raise Refused(reason) from None


def _verify_director(root):
    _require(not root.is_symlink() and not (root / 'FAILED.json').exists(), 'DIRECTOR_FAILED_RUN_NOT_COMPLETE')
    manifest, request = _read(root / 'manifest.json'), _read(root / 'request.json')
    plan, config = ResearchPlan(**manifest['plan']), Config(**manifest['config'])
    raw = _bounded_bytes(root / 'input.json', MAX_INPUT_BYTES)
    symbols = {'target_symbol': config.symbol, 'market_symbol': manifest['symbols']['market_symbol'],
               'sector_symbol': manifest['symbols']['sector_symbol']}
    context = _preflight(raw, plan, config, symbols)
    expected_manifest = {'schema': 'APEX_DIRECTOR_MANIFEST_V1', 'input_sha256': _sha(raw),
        'plan': asdict(plan), 'config': config.record(), 'symbols': symbols, 'policy': POLICY,
        'context': context, 'request_digest': digest(request), 'code': manifest['code'], 'authority': AUTHORITY}
    _require(_same(manifest, expected_manifest), 'DIRECTOR_MANIFEST_CONTRACT_DISAGREES')
    _require(_same(request, _request_record(context, symbols, request['model'])), 'DIRECTOR_REQUEST_CONTRACT_DISAGREES')
    planned = _planner_record(_read(root / 'planner.json'), context, symbols, request['model'])
    proposal = validate_proposal(_read(root / 'proposal.json'), symbols)
    _require(_same(planned['proposal'], proposal), 'DIRECTOR_PROPOSAL_NOT_PLANNER_BOUND')
    _bounded_bytes(root / 'ledger.jsonl', 65536)
    _bounded_bytes(root / 'COMPLETE', 64)
    rows = read_complete(root / 'ledger.jsonl', expected_last_kind='DIRECTOR_CLOSE', expected_epoch=plan.end,
                         completion_path=root / 'COMPLETE')
    expected = [('RUN_OPEN', plan.start, {'manifest_digest': digest(manifest), 'authority': AUTHORITY}),
        ('PLAN_REQUEST', plan.start, {'request_digest': digest(request), 'research_attempt_budget': 1}),
        ('PLAN_ACCEPTED', plan.start, {'proposal_digest': digest(proposal), 'planner_digest': digest(planned),
                                      'action': proposal['action'], 'authority': AUTHORITY})]
    lab, peer = None, None
    if proposal['action'] == 'RUN_PEER_DISLOCATION_STUDY':
        from .peer_experiment import verify_peer_experiment
        for child in ('laboratory', 'peer-edge'):
            _require(not (root / child).is_symlink(), 'DIRECTOR_CHILD_PATH_INVALID')
        lab_verification = verify_lab(root / 'laboratory')
        peer_verification = verify_peer_experiment(root / 'peer-edge')
        _require(lab_verification['status'] == peer_verification['status'] == 'VALID', 'DIRECTOR_CHILD_VERIFICATION_FAILED')
        lab_manifest = _read(root / 'laboratory' / 'manifest.json')
        _require(lab_manifest['input_sha256'] == manifest['input_sha256']
                 and _same(lab_manifest['plan'], manifest['plan'])
                 and _same(lab_manifest['config'], manifest['config'])
                 and _same(lab_manifest['policy'], POLICY['laboratory_policy'])
                 and _same(lab_manifest['costs'], POLICY['simulation_costs'])
                 and _same(lab_manifest['code'], manifest['code']), 'DIRECTOR_CHILD_BINDING_DISAGREES')
        peer_manifest = _read(root / 'peer-edge' / 'manifest.json')
        _require(peer_manifest['source_path_basis'] == 'RELATIVE_TO_EXPERIMENT_ROOT'
                 and (root / 'peer-edge' / peer_manifest['source_run']).resolve() == (root / 'laboratory').resolve()
                 and peer_manifest['market_symbol'] == symbols['market_symbol']
                 and peer_manifest['sector_symbol'] == symbols['sector_symbol']
                 and _same(peer_manifest['code'], manifest['code']), 'DIRECTOR_PEER_SOURCE_OR_SYMBOL_DISAGREES')
        lab, peer = _read(root / 'laboratory' / 'summary.json'), _read(root / 'peer-edge' / 'summary.json')
        stored_lab, stored_peer = _read(root / 'laboratory-verification.json'), _read(root / 'peer-verification.json')
        _require(stored_lab['status'] == stored_peer['status'] == 'VALID', 'DIRECTOR_STORED_CHILD_VERIFICATION_INVALID')
        for stored, current in ((stored_lab, lab_verification), (stored_peer, peer_verification)):
            _require(_same({k: v for k, v in stored.items() if k != 'code_manifest_matches_current'},
                           {k: v for k, v in current.items() if k != 'code_manifest_matches_current'}),
                     'DIRECTOR_STORED_VERIFICATION_RECONSTRUCTION_DISAGREES')
        # Current source identity may legitimately differ from the recorded run;
        # retained verified heads and semantic child readers remain mandatory.
        expected.extend([
            ('LAB_VERIFIED', plan.end, {'summary_digest': digest(lab), 'verification_digest': digest(stored_lab)}),
            ('PEER_EXPERIMENT_VERIFIED', plan.end, {'summary_digest': digest(peer), 'verification_digest': digest(stored_peer)})])
    else:
        _require(not (root / 'laboratory').exists() and not (root / 'peer-edge').exists(),
                 'DIRECTOR_DEFER_HAS_UNDECLARED_RESEARCH')
    report = _summary(manifest, proposal, planned, lab, peer)
    _require(_same(_read(root / 'report.json'), report), 'DIRECTOR_REPORT_RECONSTRUCTION_DISAGREES')
    expected.append(('DIRECTOR_CLOSE', plan.end, {'report_digest': digest(report),
                     'research_attempts': report['research_attempts'], 'authority': AUTHORITY}))
    _require(len(rows) == len(expected), 'DIRECTOR_STAGE_COUNT_DISAGREES')
    for row, (kind, epoch, payload) in zip(rows, expected):
        _require(row['kind'] == kind and row['epoch'] == epoch and _same(row['payload'], payload),
                 'DIRECTOR_STAGE_RECONSTRUCTION_DISAGREES')
    return {'status': 'VALID', 'problems': [], 'ledger_head': rows[-1]['hash'], 'verified_stages': len(rows),
            'research_attempts': report['research_attempts'], 'decision': report['decision'],
            'code_matches_current': _same(manifest['code'], code_manifest()), 'scope': SCOPE}


def verify_director(root: Path) -> dict:
    try:
        return _verify_director(Path(root))
    except (Refused, ValueError, TypeError, KeyError, OSError, IndexError, AttributeError) as exc:
        return {'status': 'MISMATCH', 'problems': [_safe_reason(exc)], 'scope': SCOPE}
