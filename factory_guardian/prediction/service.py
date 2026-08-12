"""TabFM-compatible forecasting over observable factory telemetry.

TabFM is a tabular regression model rather than a native sequence model.  This
module turns a time series into a supervised table (lags, rolling statistics and
calendar/order features), then keeps the actual estimator behind a tiny adapter.
The deterministic ridge fallback makes the API useful in the lightweight demo
environment; it is always reported as a fallback and never presented as TabFM.
"""

from __future__ import annotations

import math
import os
import statistics
import time
from dataclasses import dataclass
from typing import Any, Protocol


SUPPORTED_TARGETS = ("health", "temperature", "vibration", "current", "rpm_pct")
FEATURE_NAMES = (
    "tick", "value", "lag_1", "lag_3", "rolling_mean_3", "rolling_mean_6",
    "rolling_std_6", "slope_3", "factory_health", "production_pct",
)


@dataclass(frozen=True)
class ForecastRequest:
    machine_id: str
    target: str = "health"
    horizon: int = 12
    context_window: int = 60
    model: str = "auto"


class Regressor(Protocol):
    runtime: str

    def fit_predict(self, x_train: list[list[float]], y_train: list[float], x_test: list[list[float]]) -> list[float]: ...


class RidgeRegressor:
    """Small dependency-free ridge model used when TabFM is unavailable."""

    runtime = "ridge-fallback"

    def __init__(self, alpha: float = 1.0) -> None:
        self.alpha = alpha

    def fit_predict(self, x_train: list[list[float]], y_train: list[float], x_test: list[list[float]]) -> list[float]:
        if not x_train:
            return [0.0 for _ in x_test]
        means = [statistics.fmean(col) for col in zip(*x_train)]
        scales = [statistics.pstdev(col) or 1.0 for col in zip(*x_train)]
        x = [[1.0] + [(v - means[i]) / scales[i] for i, v in enumerate(row)] for row in x_train]
        xt = list(zip(*x))
        size = len(x[0])
        gram = [[sum(xt[i][k] * x[k][j] for k in range(len(x))) for j in range(size)] for i in range(size)]
        rhs = [sum(xt[i][k] * y_train[k] for k in range(len(x))) for i in range(size)]
        for i in range(1, size):
            gram[i][i] += self.alpha
        weights = _solve(gram, rhs)
        rows = [[1.0] + [(v - means[i]) / scales[i] for i, v in enumerate(row)] for row in x_test]
        return [sum(w * v for w, v in zip(weights, row)) for row in rows]


class TabFMRegressorAdapter:
    """Lazy adapter for the official google-research/tabfm sklearn API."""

    runtime = "tabfm-v1.0.0"

    def __init__(self) -> None:
        self._estimator: Any | None = None
        self._fitted: int | None = None

    def fit_predict(self, x_train: list[list[float]], y_train: list[float], x_test: list[list[float]]) -> list[float]:
        import numpy as np  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
        from tabfm import TabFMRegressor  # type: ignore[import-not-found]
        backend = os.getenv("FACTORY_GUARDIAN_TABFM_BACKEND", "pytorch")
        if backend == "jax":
            from tabfm import tabfm_v1_0_0_jax as release  # type: ignore[import-not-found]
        else:
            from tabfm import tabfm_v1_0_0_pytorch as release  # type: ignore[import-not-found]
        train = pd.DataFrame(x_train, columns=FEATURE_NAMES)
        test = pd.DataFrame(x_test, columns=FEATURE_NAMES)
        if self._estimator is None:
            # load() defaults to the classification checkpoint; regression
            # weights are required or predict() returns 10 values per row.
            # Cached on the adapter: the ~8s weight load is paid once per process.
            model = release.load(model_type="regression")
            # batch_size=None forwards all ensemble members in one pass instead
            # of looping one at a time — ~1.7x faster for identical output.
            self._estimator = TabFMRegressor(
                model=model, max_num_rows=100, n_estimators=4, batch_size=None
            )
        # The training table is identical on every recursive step, so refit only
        # when it actually changes. The adapter is shared across requests, so the
        # fingerprint must cover the data itself, not just its shape — otherwise
        # a later machine/target would reuse the previous fit.
        fingerprint = hash((tuple(map(tuple, x_train)), tuple(y_train)))
        if fingerprint != self._fitted:
            self._estimator.fit(train, np.asarray(y_train, dtype=float))
            self._fitted = fingerprint
        return [float(value) for value in self._estimator.predict(test)]


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with pivoting; dimensions stay tiny (11x11)."""
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    n = len(a)
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(a[row][col]))
        a[col], a[pivot] = a[pivot], a[col]
        if abs(a[col][col]) < 1e-10:
            a[col][col] = 1e-10
        divisor = a[col][col]
        a[col] = [value / divisor for value in a[col]]
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            a[row] = [value - factor * base for value, base in zip(a[row], a[col])]
    return [a[i][-1] for i in range(n)]


def _value(point: dict[str, Any], machine_id: str, target: str) -> float:
    return float(point.get(f"{machine_id}.{target}", 0.0))


def _features(points: list[dict[str, Any]], values: list[float], index: int) -> list[float]:
    recent3 = values[max(0, index - 2): index + 1]
    recent6 = values[max(0, index - 5): index + 1]
    return [
        float(points[index]["tick"]), values[index], values[max(0, index - 1)], values[max(0, index - 3)],
        statistics.fmean(recent3), statistics.fmean(recent6), statistics.pstdev(recent6) if len(recent6) > 1 else 0.0,
        (recent3[-1] - recent3[0]) / max(1, len(recent3) - 1), float(points[index].get("factory_health", 0.0)),
        float(points[index].get("production_pct", 0.0)),
    ]


class ForecastService:
    """Build a supervised temporal table and forecast one observable signal."""

    def __init__(self) -> None:
        # One adapter for the whole service: TabFM weights cost ~8s to load, so
        # a per-request adapter would pay that on every forecast.
        self._tabfm: TabFMRegressorAdapter | None = None

    def models(self) -> dict[str, Any]:
        try:
            import tabfm  # type: ignore[import-not-found]  # noqa: F401
            available = True
            reason = None
        except ImportError:
            available = False
            reason = "未安裝官方 tabfm runtime；目前使用可重現的 Ridge 時序基線。"
        return {
            "default": "auto",
            "models": [
                {"id": "auto", "label": "Auto / TabFM 優先", "available": True},
                {"id": "tabfm", "label": "Google TabFM v1.0.0", "available": available,
                 "reason": reason, "license": "weights: non-commercial / non-production"},
                {"id": "ridge", "label": "Ridge 時序基線", "available": True},
            ],
            "targets": list(SUPPORTED_TARGETS),
        }

    def forecast(self, history: list[dict[str, Any]], request: ForecastRequest) -> dict[str, Any]:
        if request.target not in SUPPORTED_TARGETS:
            raise ValueError(f"不支援的目標 {request.target}")
        key = f"{request.machine_id}.{request.target}"
        points = [point for point in history[-request.context_window:] if key in point]
        if len(points) < 8:
            raise ValueError("至少需要 8 個時間點；請先推進模擬或執行案例。")
        values = [_value(point, request.machine_id, request.target) for point in points]
        x_train = [_features(points, values, i) for i in range(len(points) - 1)]
        y_train = values[1:]
        model, requested, fallback_reason = self._model(request.model)
        started = time.perf_counter()

        future_points = [dict(point) for point in points]
        future_values = values[:]
        forecasts: list[float] = []
        # Recursive forecast: each prediction becomes the next row's lag/value.
        for _ in range(request.horizon):
            last = dict(future_points[-1])
            last["tick"] = int(last["tick"]) + 1
            index = len(future_values) - 1
            row = _features(future_points, future_values, index)
            try:
                predicted = model.fit_predict(x_train, y_train, [row])[0]
            except Exception as exc:
                if requested != "auto" or model.runtime == "ridge-fallback":
                    raise ValueError(f"{model.runtime} inference 失敗：{type(exc).__name__}: {exc}") from exc
                fallback_reason = f"TabFM inference 失敗（{type(exc).__name__}），已降級為 Ridge 時序基線。"
                model = RidgeRegressor()
                predicted = model.fit_predict(x_train, y_train, [row])[0]
            predicted = self._clamp(request.target, predicted)
            forecasts.append(predicted)
            last[key] = predicted
            future_points.append(last)
            future_values.append(predicted)

        changes = [values[i] - values[i - 1] for i in range(1, len(values))]
        residual = max(
            0.15,
            abs(changes[-1]) if changes else 0.0,
            statistics.pstdev(changes[-6:]) if len(changes) > 1 else 0.0,
        )
        threshold = 75.0 if request.target == "health" else None
        series = []
        for index, value in enumerate(forecasts, 1):
            spread = residual * math.sqrt(index) * 1.64
            series.append({
                "tick": int(points[-1]["tick"]) + index,
                "value": round(value, 3),
                "lower": round(self._clamp(request.target, value - spread), 3),
                "upper": round(self._clamp(request.target, value + spread), 3),
                "risk": round(1.0 / (1.0 + math.exp((value - threshold) / max(1.0, spread))) if threshold else 0.0, 4),
            })
        crossing = next((row["tick"] for row in series if threshold is not None and row["lower"] < threshold), None)
        return {
            "machine_id": request.machine_id, "target": request.target, "unit": "%" if request.target == "health" else "",
            "horizon": request.horizon, "context_rows": len(points), "feature_count": len(FEATURE_NAMES),
            "features": list(FEATURE_NAMES), "requested_model": requested, "runtime": model.runtime,
            # The exact context the model consumed, so the UI can plot the
            # history the forecast was actually derived from rather than a
            # separately-buffered approximation of it.
            "context": [
                {"tick": int(point["tick"]), "value": round(value, 3)}
                for point, value in zip(points, values)
            ],
            "fallback": fallback_reason is not None, "fallback_reason": fallback_reason,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2), "threshold": threshold,
            "threshold_crossing_tick": crossing, "current": round(values[-1], 3), "forecast": series,
            "disclaimer": "預測只使用 Agent 可見的遙測歷史，不含模擬器 Ground Truth；區間為殘差估計，非安全保證。",
        }

    def _model(self, requested: str) -> tuple[Regressor, str, str | None]:
        if requested not in {"auto", "tabfm", "ridge"}:
            raise ValueError(f"未知模型 {requested}")
        if requested == "ridge":
            return RidgeRegressor(), requested, None
        try:
            import tabfm  # type: ignore[import-not-found]  # noqa: F401
            if self._tabfm is None:
                self._tabfm = TabFMRegressorAdapter()
            return self._tabfm, requested, None
        except ImportError:
            if requested == "tabfm":
                raise ValueError("TabFM runtime 未安裝；請安裝官方套件與 backend，或改用 auto/ridge。")
            return RidgeRegressor(), requested, "TabFM runtime 不可用，已自動降級為 Ridge 時序基線。"

    @staticmethod
    def _clamp(target: str, value: float) -> float:
        if target in {"health", "rpm_pct"}:
            return max(0.0, min(100.0, value))
        return max(0.0, value)
