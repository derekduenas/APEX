"""Paired, fixed-family strategy diagnostics with sessions as resampling units.

This is a White-style centered maximum-mean bootstrap, specialized to an IID
session approximation. It is not White's stationary bootstrap, Hansen's SPA,
an anytime-valid test, a probability of profit, or a capital authorization.
Within-session observations are kept together, and every registered strategy
must have an outcome at every opportunity. A deliberate no-entry contributes
its actual zero return; a missing outcome is refused, never imputed to zero.

Reference: White (2000), "A Reality Check for Data Snooping", Econometrica
68(5), 1097-1126: https://users.ssc.wisc.edu/~bhansen/718/White2000.pdf
The adaptation and its additional independent/stationary-session assumption
are explicit in every returned record.
"""
from __future__ import annotations

import math

import numpy as np

from .core import Refused, digest, finite


def session_bootstrap(outcomes: list[dict], *, strategy_ids: list[str] | tuple[str, ...],
                      benchmark: str = "WAIT", minimum_sessions: int = 10,
                      bootstrap_samples: int = 2000, seed: int = 7919,
                      confidence: float = 0.95, opportunity_coverage_complete: bool = True) -> dict:
    """Compare the complete declared family on identical realized opportunities.

    Input rows contain ``sample_id``, ``session``, and the exact registered
    ``net_returns`` mapping. The caller establishes realized outcome maturity,
    nonoverlap, and that registration preceded the evaluated observations.
    This routine cannot infer those facts from these three fields.

    Each session contributes its arithmetic mean opportunity return. The
    estimand is the equally weighted mean of these session means, not compound
    account growth or a trade-weighted average. Bootstrap draws jointly sample
    whole session vectors with replacement, after centering each excess-return
    column at its observed mean. The maximum includes the benchmark's exact
    zero. A finite-resampling correction prevents a reported p-value of zero.

    The same centered maximum distribution supplies a one-sided simultaneous
    lower-mean-excess bound for the registered family, conditional on the IID
    session bootstrap assumptions. No repeated-look or earlier-search burden
    is covered. Numerical, incomplete, and ambiguous inputs fail closed.
    """
    if (not isinstance(strategy_ids, (list, tuple)) or len(strategy_ids) < 2
            or any(not isinstance(s, str) or not s for s in strategy_ids)
            or len(set(strategy_ids)) != len(strategy_ids)):
        raise Refused("STRATEGY_EVIDENCE_INVALID_FAMILY")
    if not isinstance(benchmark, str) or benchmark not in strategy_ids:
        raise Refused("STRATEGY_EVIDENCE_BENCHMARK_NOT_REGISTERED")
    if type(minimum_sessions) is not int or minimum_sessions < 10:
        raise Refused("STRATEGY_EVIDENCE_MINIMUM_SESSIONS_BELOW_TEN")
    if type(bootstrap_samples) is not int or bootstrap_samples < 100:
        raise Refused("STRATEGY_EVIDENCE_INSUFFICIENT_RESAMPLING_BUDGET")
    if type(seed) is not int or not 0 <= seed < 2 ** 64:
        raise Refused("STRATEGY_EVIDENCE_INVALID_SEED")
    if not finite(confidence) or not 0.5 < confidence < 1:
        raise Refused("STRATEGY_EVIDENCE_INVALID_CONFIDENCE")
    if not isinstance(outcomes, list):
        raise Refused("STRATEGY_EVIDENCE_INVALID_OUTCOMES")
    if type(opportunity_coverage_complete) is not bool:
        raise Refused("STRATEGY_EVIDENCE_INVALID_COVERAGE_DECLARATION")

    names, seen, copied = sorted(strategy_ids), set(), []
    for row in outcomes:
        if not isinstance(row, dict):
            raise Refused("STRATEGY_EVIDENCE_INVALID_OUTCOME")
        sample_id, session = row.get("sample_id"), row.get("session")
        if not isinstance(sample_id, str) or not sample_id or sample_id in seen:
            raise Refused("STRATEGY_EVIDENCE_INVALID_OR_DUPLICATE_SAMPLE")
        if not isinstance(session, str) or not session:
            raise Refused("STRATEGY_EVIDENCE_INVALID_SESSION")
        values = row.get("net_returns")
        if not isinstance(values, dict) or set(values) != set(names):
            raise Refused("STRATEGY_EVIDENCE_INCOMPLETE_OR_UNREGISTERED_OUTCOME_FAMILY")
        if any(not finite(values[s]) for s in names):
            raise Refused("STRATEGY_EVIDENCE_NONFINITE_RETURN")
        if "WAIT" in values and values["WAIT"] != 0:
            raise Refused("STRATEGY_EVIDENCE_WAIT_MUST_BE_ZERO")
        seen.add(sample_id)
        copied.append({"sample_id": sample_id, "session": session,
                       "net_returns": {s: float(values[s]) for s in names}})
    copied.sort(key=lambda row: (row["session"], row["sample_id"]))
    grouped: dict[str, list[dict]] = {}
    for row in copied:
        grouped.setdefault(row["session"], []).append(row)

    result = {
        "schema": "APEX_STRATEGY_EVIDENCE_V1",
        "status": "INSUFFICIENT_SESSIONS" if opportunity_coverage_complete else "INCOMPLETE_OPPORTUNITY_COVERAGE",
        "opportunity_coverage_complete": opportunity_coverage_complete,
        "method": "SESSION_IID_CENTERED_MAX_MEAN_EXCESS_V1",
        "strategy_ids": names, "benchmark": benchmark,
        "fixed_family_count_including_benchmark": len(names),
        "opportunity_count": len(copied), "session_count": len(grouped),
        "effective_sampling_unit": "SESSION_NOT_SIMULATED_PATH_OR_TRADE",
        "minimum_sessions": minimum_sessions,
        "consumed_outcomes_digest": digest(copied),
        "bootstrap_samples_requested": bootstrap_samples, "bootstrap_samples_used": 0, "seed": seed,
        "rng": "NUMPY_PCG64", "confidence": float(confidence),
        "estimand": "EQUAL_SESSION_MEAN_OF_OPPORTUNITY_NET_RETURNS",
        "null_hypothesis": "NO_REGISTERED_STRATEGY_HAS_POSITIVE_EXPECTED_SESSION_MEAN_EXCESS",
        "statistic_definition": "SQRT_SESSION_COUNT_TIMES_MAX_MEAN_EXCESS_INCLUDING_BENCHMARK_ZERO",
        "lower_bound_definition": "OBSERVED_MEAN_EXCESS_MINUS_COMMON_MAX_CENTERED_ERROR_QUANTILE",
        "session_means": [], "by_strategy": {},
        "best_observed_strategy": None, "observed_max_mean_excess": None,
        "observed_max_statistic": None, "family_p_value": None,
        "simultaneous_error_quantile": None, "centered_max_statistics_digest": None,
        "promotion": "NOT_AUTHORIZED",
        "limitations": [
            "INDEPENDENT_STATIONARY_SESSIONS_ASSUMED_NOT_ESTABLISHED",
            "WITHIN_SESSION_DEPENDENCE_PRESERVED_BETWEEN_SESSION_DEPENDENCE_NOT_MODELLED",
            "FIXED_SUPPLIED_FAMILY_ONLY_REGISTRATION_TIMING_NOT_VERIFIED_HERE",
            "REPEATED_LOOKS_AND_PRIOR_EXPERIMENTS_NOT_CONTROLLED",
            "MATURITY_NONOVERLAP_AND_OPPORTUNITY_COVERAGE_REQUIRE_CALLER_EVIDENCE",
            "RETURNS_AND_COST_ASSUMPTIONS_SUPPLIED_NOT_INDEPENDENTLY_VERIFIED_HERE",
            "NOT_CALIBRATED_PROFIT_PROBABILITY_OR_EXECUTION_AUTHORIZATION",
            "FINITE_SAMPLE_BOOTSTRAP_APPROXIMATION_NOT_A_COVERAGE_GUARANTEE",
        ],
    }
    if not grouped:
        result["evidence_hash"] = digest(result)
        return result

    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            matrix = np.asarray([
                np.asarray([[r["net_returns"][s] for s in names] for r in rows], dtype=float).mean(axis=0)
                for rows in grouped.values()
            ])
            means = matrix.mean(axis=0)
            excess = matrix - matrix[:, names.index(benchmark), None]
            mean_excess = excess.mean(axis=0)
            centered = excess - mean_excess
    except FloatingPointError as exc:
        raise Refused("STRATEGY_EVIDENCE_NUMERIC_FAILURE") from exc
    if not all(np.all(np.isfinite(v)) for v in (matrix, means, excess, mean_excess, centered)):
        raise Refused("STRATEGY_EVIDENCE_NUMERIC_FAILURE")
    result["session_means"] = [
        {"session": session, "opportunities": len(grouped[session]),
         "net_returns": dict(zip(names, values.tolist()))}
        for session, values in zip(grouped, matrix)
    ]
    result["by_strategy"] = {
        s: {"mean_net_return": float(means[i]), "mean_excess_return": float(mean_excess[i]),
            "simultaneous_lower_mean_excess": None}
        for i, s in enumerate(names)
    }
    # The benchmark wins exact ties, avoiding a fabricated best positive rule.
    best = max(range(len(names)), key=lambda i: (mean_excess[i], names[i] == benchmark, -i))
    result["best_observed_strategy"] = names[best]
    result["observed_max_mean_excess"] = float(mean_excess[best])
    if len(grouped) < minimum_sessions or not opportunity_coverage_complete:
        result["evidence_hash"] = digest(result)
        return result

    n_sessions = len(grouped)
    rng = np.random.Generator(np.random.PCG64(seed))
    maxima = []
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            for offset in range(0, bootstrap_samples, 128):
                count = min(128, bootstrap_samples - offset)
                indices = rng.integers(0, n_sessions, size=(count, n_sessions))
                # Every strategy receives exactly the same sampled sessions.
                maxima.extend(centered[indices].mean(axis=1).max(axis=1).tolist())
            maxima = np.asarray(maxima)
            observed = float(math.sqrt(n_sessions) * mean_excess[best])
            null_statistics = math.sqrt(n_sessions) * maxima
    except FloatingPointError as exc:
        raise Refused("STRATEGY_EVIDENCE_NUMERIC_FAILURE") from exc
    if not math.isfinite(observed) or not np.all(np.isfinite(null_statistics)):
        raise Refused("STRATEGY_EVIDENCE_NUMERIC_FAILURE")
    # Higher order statistic avoids interpolation toward a smaller critical value.
    critical = float(np.quantile(maxima, confidence, method="higher"))
    for s, row in result["by_strategy"].items():
        row["simultaneous_lower_mean_excess"] = (
            0.0 if s == benchmark else row["mean_excess_return"] - critical)
    result.update(
        status="DESCRIPTIVE_BOOTSTRAP_ONLY", observed_max_statistic=observed,
        bootstrap_samples_used=bootstrap_samples,
        family_p_value=float((1 + np.count_nonzero(null_statistics >= observed)) / (bootstrap_samples + 1)),
        simultaneous_error_quantile=critical,
        centered_max_statistics_digest=digest(null_statistics.tolist()),
    )
    result["evidence_hash"] = digest(result)
    return result
