"""Model and forecast contracts (M3).

A ForecastObject says what it SUPPLIES. Asking it for something it does not
supply refuses (`UnsupportedOutput`): a point forecast never becomes a
density by decoration, quantiles never become a path model.

A Model has fit / forecast / serialize / load / describe. Every fit counts
against the model's declared budget (`fit_count`), and every artifact has a
deterministic digest over its serialized parameters."""
from __future__ import annotations

import hashlib
import json
import math

SUPPLIES = ("mean", "quantiles", "density", "paths", "variance")


class UnsupportedOutput(RuntimeError):
    pass


class ModelRefused(RuntimeError):
    pass


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=float, allow_nan=False).encode()).hexdigest()[:16]


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


class ForecastObject:
    """An explicitly limited forecast. `supplies` is the set of outputs it can answer."""

    def __init__(self, *, model_id: str, artifact_digest: str, horizon_minutes: int, input_cutoff_epoch: float,
                 created_epoch: float, supplies: tuple, mean=None, variance=None, quantiles: dict | None = None,
                 density: dict | None = None, paths=None, meta: dict | None = None):
        bad = [s for s in supplies if s not in SUPPLIES]
        if bad:
            raise ModelRefused("UNKNOWN_SUPPLY: %s" % bad)
        if created_epoch < input_cutoff_epoch:
            raise ModelRefused("CREATED_BEFORE_CUTOFF")
        self.model_id, self.artifact_digest = model_id, artifact_digest
        self.horizon_minutes, self.input_cutoff_epoch, self.created_epoch = horizon_minutes, input_cutoff_epoch, created_epoch
        self.supplies = tuple(supplies)
        self._mean, self._variance, self._quantiles, self._density, self._paths = mean, variance, quantiles, density, paths
        self.meta = meta or {}
        if "mean" in supplies and not _finite(mean):
            raise ModelRefused("MEAN_NOT_FINITE")
        if "variance" in supplies and not (_finite(variance) and variance > 0):
            raise ModelRefused("VARIANCE_INVALID")
        if "quantiles" in supplies:
            if not quantiles or any(not _finite(v) for v in quantiles.values()):
                raise ModelRefused("QUANTILES_INVALID")
        if "density" in supplies and not (isinstance(density, dict) and density.get("family")):
            raise ModelRefused("DENSITY_UNSPECIFIED")

    def _need(self, what):
        if what not in self.supplies:
            raise UnsupportedOutput("%s does not supply %s (supplies %s); no synthesis is performed" % (self.model_id, what, self.supplies))

    def mean(self) -> float:
        self._need("mean"); return self._mean

    def variance(self) -> float:
        self._need("variance"); return self._variance

    def quantiles(self) -> dict:
        self._need("quantiles"); return dict(self._quantiles)

    def density(self) -> dict:
        self._need("density"); return dict(self._density)

    def paths(self):
        self._need("paths"); return self._paths

    def as_record(self) -> dict:
        return {"model_id": self.model_id, "artifact_digest": self.artifact_digest, "horizon_minutes": self.horizon_minutes,
                "input_cutoff_epoch": self.input_cutoff_epoch, "created_epoch": self.created_epoch, "supplies": list(self.supplies),
                "mean": self._mean if "mean" in self.supplies else None, "variance": self._variance if "variance" in self.supplies else None,
                "quantiles": self._quantiles if "quantiles" in self.supplies else None,
                "density": self._density if "density" in self.supplies else None,
                "paths": ("%d paths" % len(self._paths)) if "paths" in self.supplies and self._paths is not None else None,
                "meta": self.meta}


class Model:
    """Base contract. Subclasses implement _fit / _forecast / _params / _from_params."""
    model_id = "ABSTRACT"
    supplies: tuple = ()
    fit_budget: int = 1

    def __init__(self):
        self.fit_count = 0
        self.fitted = False
        self.fit_cutoff_epoch = None

    # ---- accounting
    def _charge_fit(self):
        if self.fit_count >= self.fit_budget:
            raise ModelRefused("FIT_BUDGET_EXHAUSTED: %s allows %d fit(s)" % (self.model_id, self.fit_budget))
        self.fit_count += 1

    def fit(self, rows: list, *, cutoff_epoch: float) -> dict:
        """rows: list of dicts with at least 'event_time' and the model's inputs; ONLY rows whose
        availability <= cutoff_epoch are used; any later row is a firewall violation."""
        late = [r for r in rows if r.get("available", r.get("event_time")) > cutoff_epoch]
        if late:
            raise ModelRefused("FIREWALL: %d row(s) available after cutoff %.0f were offered to fit" % (len(late), cutoff_epoch))
        self._validate(rows)                 # input refusals do not consume a fit; an optimizer run does
        self._charge_fit()
        out = self._fit(rows)
        self.fitted, self.fit_cutoff_epoch = True, cutoff_epoch
        return out

    def forecast(self, *args, **kw) -> ForecastObject:
        if not self.fitted:
            raise ModelRefused("NOT_FITTED: %s" % self.model_id)
        return self._forecast(*args, **kw)

    def serialize(self) -> dict:
        p = self._params()
        return {"model_id": self.model_id, "params": p, "fit_cutoff_epoch": self.fit_cutoff_epoch, "fit_count": self.fit_count,
                "artifact_digest": digest({"model_id": self.model_id, "params": p})}

    @classmethod
    def load(cls, doc: dict):
        m = cls._from_params(doc["params"])
        if digest({"model_id": cls.model_id, "params": doc["params"]}) != doc.get("artifact_digest"):
            raise ModelRefused("ARTIFACT_DIGEST_DISAGREES")
        m.fitted, m.fit_cutoff_epoch, m.fit_count = True, doc.get("fit_cutoff_epoch"), doc.get("fit_count", 1)
        return m

    def describe(self) -> dict:
        return {"model_id": self.model_id, "supplies": list(self.supplies), "fit_budget": self.fit_budget, "fit_count": self.fit_count,
                "fitted": self.fitted, "fit_cutoff_epoch": self.fit_cutoff_epoch,
                "artifact_digest": self.serialize()["artifact_digest"] if self.fitted else None}

    def _validate(self, rows: list) -> None:
        return None

    # ---- to implement
    def _fit(self, rows: list) -> dict:                                     # pragma: no cover - interface
        raise NotImplementedError

    def _forecast(self, *args, **kw) -> ForecastObject:                    # pragma: no cover - interface
        raise NotImplementedError

    def _params(self) -> dict:                                              # pragma: no cover - interface
        raise NotImplementedError

    @classmethod
    def _from_params(cls, p: dict):                                         # pragma: no cover - interface
        raise NotImplementedError
