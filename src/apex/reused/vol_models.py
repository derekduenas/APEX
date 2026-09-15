"""Conditional-variance candidates (M3): RollingVariance, EWMA, GARCH(1,1), GJR-GARCH(1,1).

Convention (mandate §2):  r_t = mu_t + e_t,  e_t = sqrt(h_t) z_t,  E[z_t^2] = 1
    GARCH:  h_{t+1} = omega + alpha e_t^2 + beta h_t
    GJR:    h_{t+1} = omega + (alpha + gamma * 1[e_t < 0]) e_t^2 + beta h_t
Innovations are STANDARDIZED Student-t with nu > 2 (variance 1), i.e. z = T / sqrt(nu/(nu-2)) with
T ~ t_nu. A conventional Student-t SCALE s relates to a standard deviation by sd^2 = s^2 nu/(nu-2);
`t_scale_from_variance` / `variance_from_t_scale` make that conversion explicit so a conditional
standard deviation is never handed to a scorer that expects a t scale.

Horizon (15 one-minute bars) — three DIFFERENT objects, named:
    next_bar_variance        h_{t+1}
    integrated_variance      sum_{k=1..H} E[h_{t+k}]    (analytic recursion; assumes zero-mean, uncorrelated
                             innovations; for GJR the expectation of the asymmetry term uses P(e<0)=1/2,
                             valid for symmetric innovations)
    cumulative_return_var    variance of sum_{k} e_{t+k}; equals integrated_variance under the same
                             assumptions, else obtained by SIMULATION (`simulate_horizon`)
Analytic multi-step availability depends on the recursion; the simulation path always exists.

Refusals: non-finite inputs, too few observations, optimizer failure, non-stationarity
(alpha + beta + gamma/2 >= 1), nu at a bound, budget exhaustion."""
from __future__ import annotations

import math

import numpy as np
from scipy import optimize, special

from .contracts import ForecastObject, Model, ModelRefused

MIN_OBS = 200
NU_BOUNDS = (2.05, 60.0)


def t_scale_from_variance(var: float, nu: float) -> float:
    return math.sqrt(var * (nu - 2.0) / nu)


def variance_from_t_scale(s: float, nu: float) -> float:
    return s * s * nu / (nu - 2.0)


def std_t_logpdf(z: np.ndarray, nu: float) -> np.ndarray:
    """log density of the STANDARDIZED Student-t (variance 1)."""
    c = special.gammaln((nu + 1) / 2) - special.gammaln(nu / 2) - 0.5 * math.log(math.pi * (nu - 2))
    return c - (nu + 1) / 2 * np.log1p(z * z / (nu - 2))


def std_t_rvs(rng: np.random.Generator, nu: float, size) -> np.ndarray:
    return rng.standard_t(nu, size=size) / math.sqrt(nu / (nu - 2.0))


def _returns(rows: list, key: str) -> np.ndarray:
    x = np.array([r[key] for r in rows], dtype=float)
    if not np.all(np.isfinite(x)):
        raise ModelRefused("NONFINITE_INPUT")
    if len(x) < MIN_OBS:
        raise ModelRefused("TOO_FEW_OBSERVATIONS: %d < %d" % (len(x), MIN_OBS))
    return x


class _VarianceModel(Model):
    supplies = ("variance", "density")
    key = "ret_1"

    def _validate(self, rows):
        _returns(rows, self.key)

    def _forecast_from_h(self, h1: float, *, cutoff_epoch: float, created_epoch: float, horizon_bars: int, nu: float | None,
                         integrated: float, extra: dict) -> ForecastObject:
        fam = "STUDENT_T" if nu else "GAUSSIAN"
        dens = {"family": fam, "location": 0.0, "variance": integrated}
        if nu:
            dens.update(nu=nu, scale=t_scale_from_variance(integrated, nu), scale_convention="Student-t scale from variance: s^2 = var (nu-2)/nu")
        return ForecastObject(model_id=self.model_id, artifact_digest=self.serialize()["artifact_digest"], horizon_minutes=horizon_bars,
                              input_cutoff_epoch=cutoff_epoch, created_epoch=created_epoch, supplies=self.supplies,
                              variance=integrated, density=dens,
                              meta={"next_bar_variance": h1, "integrated_variance": integrated, "horizon_bars": horizon_bars,
                                    "aggregation": "sum of E[h_{t+k}] (zero-mean, uncorrelated innovations)", **extra})


class RollingVariance(_VarianceModel):
    """Frozen-window realized variance of the last `window` returns; the flat baseline."""
    model_id = "ROLLING_VAR"

    def __init__(self, window: int = 30):
        super().__init__(); self.window = window; self.h = None

    def _fit(self, rows):
        x = _returns(rows, self.key)
        self.h = float(np.mean(x[-self.window:] ** 2))
        return {"window": self.window, "h": self.h}

    def _forecast(self, *, cutoff_epoch, created_epoch, horizon_bars=15, recent=None):
        h = float(np.mean(np.array(recent[-self.window:]) ** 2)) if recent is not None and len(recent) >= self.window else self.h
        return self._forecast_from_h(h, cutoff_epoch=cutoff_epoch, created_epoch=created_epoch, horizon_bars=horizon_bars, nu=None,
                                     integrated=h * horizon_bars, extra={"window": self.window})

    def _params(self): return {"window": self.window, "h": self.h}

    @classmethod
    def _from_params(cls, p):
        m = cls(p["window"]); m.h = p["h"]; return m


class EWMA(_VarianceModel):
    model_id = "EWMA"

    def __init__(self, lam: float = 0.94):
        super().__init__(); self.lam = lam; self.h = None

    def _fit(self, rows):
        x = _returns(rows, self.key)
        h = float(np.mean(x[:30] ** 2))
        for e in x:
            h = self.lam * h + (1 - self.lam) * e * e
        self.h = h
        return {"lambda": self.lam, "h": h}

    def _forecast(self, *, cutoff_epoch, created_epoch, horizon_bars=15, recent=None):
        h = self.h
        if recent is not None:
            for e in recent:
                h = self.lam * h + (1 - self.lam) * e * e
        return self._forecast_from_h(h, cutoff_epoch=cutoff_epoch, created_epoch=created_epoch, horizon_bars=horizon_bars, nu=None,
                                     integrated=h * horizon_bars, extra={"lambda": self.lam, "note": "EWMA has no mean reversion: flat multi-step"})

    def _params(self): return {"lambda": self.lam, "h": self.h}

    @classmethod
    def _from_params(cls, p):
        m = cls(p["lambda"]); m.h = p["h"]; return m


def _filter(x: np.ndarray, omega: float, alpha: float, beta: float, gamma: float, h0: float) -> np.ndarray:
    h = np.empty(len(x)); h[0] = h0
    for t in range(1, len(x)):
        e = x[t - 1]
        h[t] = omega + (alpha + (gamma if e < 0 else 0.0)) * e * e + beta * h[t - 1]
    return h


def _nll(theta, x, gjr: bool):
    omega, alpha, beta = math.exp(theta[0]), _sig(theta[1]), _sig(theta[2])
    gamma = _sig(theta[3]) if gjr else 0.0
    nu = 2.0 + math.exp(theta[4 if gjr else 3])
    if alpha + beta + gamma / 2 >= 0.9999:
        return 1e12
    h = _filter(x, omega, alpha, beta, gamma, float(np.var(x)))
    if np.any(h <= 0) or not np.all(np.isfinite(h)):
        return 1e12
    z = x / np.sqrt(h)
    return float(-np.sum(std_t_logpdf(z, nu) - 0.5 * np.log(h)))


def _sig(u):
    return 1.0 / (1.0 + math.exp(-u))


class GARCH(_VarianceModel):
    """GARCH(1,1) (gjr=False) or GJR-GARCH(1,1) (gjr=True), standardized Student-t innovations, zero mean."""
    model_id = "GARCH11_T"
    fit_budget = 1

    def __init__(self, *, gjr: bool = False):
        super().__init__()
        self.gjr = gjr
        self.model_id = "GJR_GARCH11_T" if gjr else "GARCH11_T"
        self.p: dict = {}

    def _fit(self, rows):
        x = _returns(rows, self.key)
        v = float(np.var(x))
        x0 = [math.log(v * 0.05), -2.2, 1.9] + ([-3.0] if self.gjr else []) + [math.log(6.0)]
        res = optimize.minimize(_nll, x0, args=(x, self.gjr), method="L-BFGS-B")
        method = "L-BFGS-B"
        if not res.success:
            # an unsuccessful termination is NOT accepted as a fit: polish with a derivative-free method from the L-BFGS-B point
            # and accept only if THAT converges (ABNORMAL_TERMINATION_IN_LNSRCH is a line-search failure, not a solution)
            res2 = optimize.minimize(_nll, res.x, args=(x, self.gjr), method="Nelder-Mead", options={"maxiter": 6000, "xatol": 1e-7, "fatol": 1e-9})
            if not res2.success:
                raise ModelRefused("OPTIMIZER_FAILED: L-BFGS-B %r; Nelder-Mead polish %r" % (str(res.message)[:60], str(res2.message)[:60]))
            res = res2; method = "L-BFGS-B (unsuccessful) + Nelder-Mead polish (converged)"
        # independent convergence check: a second start must reach the same optimum (within tolerance); the better point is kept
        rng = np.random.default_rng(int(abs(res.fun)) % (2 ** 31))
        start_b = None
        for _ in range(8):                                             # a second start INSIDE the feasible region (not the penalty plateau)
            cand = np.asarray(res.x, dtype=float) + rng.normal(0.0, 0.15, size=len(res.x))
            if _nll(cand, x, self.gjr) < 1e11:
                start_b = cand; break
        if start_b is None:
            raise ModelRefused("NOT_CONVERGED: no feasible second start near the optimum")
        res_a = res                                                    # BOTH results are preserved; nothing is selected before the rule is applied
        res_b = optimize.minimize(_nll, start_b, args=(x, self.gjr), method="L-BFGS-B")
        fun_a, fun_b = float(res_a.fun), float(res_b.fun)
        gap = abs(fun_a - fun_b)
        scale = max(1.0, abs(min(fun_a, fun_b)))
        if not res_b.success:
            raise ModelRefused("NOT_CONVERGED: second start failed (%s)" % str(res_b.message)[:60])
        if gap > 1e-4 * scale:                                         # declared acceptance rule: |NLL_a - NLL_b| <= 1e-4 * max(1, |NLL|)
            raise ModelRefused("NOT_CONVERGED: multi-start NLL disagreement %.6g vs %.6g (gap %.3g > %.3g)" % (fun_a, fun_b, gap, 1e-4 * scale))
        res = res_b if fun_b < fun_a else res_a                        # only now: the better of two AGREEING optima
        grad = optimize.approx_fprime(np.asarray(res.x, dtype=float), lambda t: _nll(t, x, self.gjr), 1e-6)
        convergence = {"method": method, "success": bool(res.success),
                       "start_a": {"success": bool(res_a.success), "message": str(res_a.message)[:60], "nll": fun_a},
                       "start_b": {"success": bool(res_b.success), "message": str(res_b.message)[:60], "nll": fun_b},
                       "multi_start_nll_gap": gap, "gap_tolerance": 1e-4 * scale, "selected": "b" if res is res_b else "a",
                       "grad_inf_norm": float(np.max(np.abs(grad))),
                       "rule": "unsuccessful termination refused unless a derivative-free polish converges; two starts must agree on the NLL within 1e-4 relative BEFORE the better is selected"}
        th = res.x
        omega, alpha, beta = math.exp(th[0]), _sig(th[1]), _sig(th[2])
        gamma = _sig(th[3]) if self.gjr else 0.0
        nu = 2.0 + math.exp(th[4 if self.gjr else 3])
        if alpha + beta + gamma / 2 >= 0.999:
            raise ModelRefused("NONSTATIONARY: alpha+beta+gamma/2 = %.4f >= 1" % (alpha + beta + gamma / 2))
        if nu <= NU_BOUNDS[0] + 1e-3 or nu >= NU_BOUNDS[1] - 1e-3:
            raise ModelRefused("NU_AT_BOUND: nu=%.3f" % nu)
        h = _filter(x, omega, alpha, beta, gamma, v)
        self.p = {"omega": omega, "alpha": alpha, "beta": beta, "gamma": gamma, "nu": nu, "gjr": self.gjr,
                  "h_last": float(h[-1]), "e_last": float(x[-1]), "n": int(len(x)), "nll": float(res.fun), "convergence": convergence,
                  "unconditional_variance": omega / (1 - alpha - beta - gamma / 2), "persistence": alpha + beta + gamma / 2}
        return dict(self.p)

    def next_h(self, h_t: float, e_t: float) -> float:
        p = self.p
        return p["omega"] + (p["alpha"] + (p["gamma"] if e_t < 0 else 0.0)) * e_t * e_t + p["beta"] * h_t

    def integrated_variance(self, h1: float, horizon_bars: int) -> float:
        """sum_{k=1..H} E[h_{t+k} | F_t]: E[h_{k+1}] = omega + (alpha + gamma/2 + beta) E[h_k] for k >= 1."""
        p = self.p
        phi = p["alpha"] + p["gamma"] / 2 + p["beta"]
        total, eh = 0.0, h1
        for _ in range(horizon_bars):
            total += eh
            eh = p["omega"] + phi * eh
        return total

    def _forecast(self, *, cutoff_epoch, created_epoch, horizon_bars=15, recent=None):
        h, e = self.p["h_last"], self.p["e_last"]
        if recent is not None:                       # roll the filter forward over post-fit returns (no refit)
            for r in recent:
                h = self.next_h(h, e); e = r
        h1 = self.next_h(h, e)
        integ = self.integrated_variance(h1, horizon_bars)
        return self._forecast_from_h(h1, cutoff_epoch=cutoff_epoch, created_epoch=created_epoch, horizon_bars=horizon_bars, nu=self.p["nu"],
                                     integrated=integ, extra={k: self.p[k] for k in ("omega", "alpha", "beta", "gamma", "nu", "persistence")})

    def simulate_horizon(self, *, horizon_bars: int, n_paths: int, seed: int, recent=None) -> dict:
        """Cumulative-return variance by SIMULATION of the declared process; Monte Carlo error reported."""
        rng = np.random.default_rng(seed)
        h, e = self.p["h_last"], self.p["e_last"]
        if recent is not None:
            for r in recent:
                h = self.next_h(h, e); e = r
        H = np.full(n_paths, self.next_h(h, e)); E = np.zeros(n_paths); cum = np.zeros(n_paths)
        for _ in range(horizon_bars):
            Z = std_t_rvs(rng, self.p["nu"], n_paths)
            E = np.sqrt(H) * Z
            cum += E
            H = self.p["omega"] + (self.p["alpha"] + self.p["gamma"] * (E < 0)) * E * E + self.p["beta"] * H
        var = float(np.var(cum))
        return {"cumulative_return_var": var, "mc_se_of_var": float(np.std(cum ** 2) / math.sqrt(n_paths)),
                "n_paths": n_paths, "seed": seed, "cum_returns": cum}

    def _params(self): return dict(self.p)

    @classmethod
    def _from_params(cls, p):
        m = cls(gjr=bool(p.get("gjr"))); m.p = dict(p); return m


def simulate_garch(*, n: int, omega: float, alpha: float, beta: float, gamma: float = 0.0, nu: float = 6.0, seed: int = 0) -> np.ndarray:
    """A synthetic world with the declared convention, for tests only."""
    rng = np.random.default_rng(seed)
    h = omega / (1 - alpha - beta - gamma / 2)
    x = np.empty(n); e = 0.0
    for t in range(n):
        if t:
            h = omega + (alpha + (gamma if e < 0 else 0.0)) * e * e + beta * h
        e = math.sqrt(h) * float(std_t_rvs(rng, nu, 1)[0])
        x[t] = e
    return x
