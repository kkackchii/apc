"""26Q4 N-code demand forecast — Layer 3 statistical fill-in + confidence grading.

Layers 1/2/4 (booked orders, customer forecast, consignment stock) are computed
in app/routers/dashboard.py from real data the user enters/uploads. This module
only covers the pieces with no direct data source:

  Layer 3: for N-codes/months with no booked order and no customer forecast,
  estimate demand from the last 6 months of actuals (see TASK.md 11.2).
  Model is chosen per N-code from its own demand pattern — Croston's method for
  intermittent demand, EWMA for regular demand, flat zero for fully inactive codes.

  Confidence: a HIGH/MID/LOW label summarizing how much of an N-code's Q4
  forecast rests on hard data (booked/forecast) vs. the Layer 3 estimate.
"""
from typing import Optional


def select_stat_model(history: list[float]) -> str:
    """Pick a Layer 3 model from a 6-month demand history, per TASK.md 11.2."""
    active_months = sum(1 for v in history if v > 0)
    if active_months == 0:
        return "ZERO"
    if active_months <= 2:
        return "CROSTON"
    return "EWMA"


def ewma_forecast(history: list[float], alpha: float = 0.35) -> float:
    """Exponentially weighted moving average — final smoothed level is used
    as a flat per-month forecast (no seasonality signal in this data)."""
    if not history:
        return 0.0
    level = history[0]
    for v in history[1:]:
        level = alpha * v + (1 - alpha) * level
    return level


def croston_forecast(history: list[float], alpha: float = 0.35) -> float:
    """Croston's method for intermittent demand: smooths demand size and
    inter-demand interval separately, forecast = smoothed size / smoothed interval."""
    demands: list[float] = []
    intervals: list[int] = []
    last_demand_idx: Optional[int] = None
    for i, v in enumerate(history):
        if v > 0:
            interval = (i + 1) if last_demand_idx is None else (i - last_demand_idx)
            demands.append(v)
            intervals.append(interval)
            last_demand_idx = i

    if not demands:
        return 0.0

    z = demands[0]
    p = intervals[0]
    for d, q in zip(demands[1:], intervals[1:]):
        z = alpha * d + (1 - alpha) * z
        p = alpha * q + (1 - alpha) * p

    return z / p if p > 0 else 0.0


def stat_forecast(history: list[float]) -> tuple[str, float]:
    """Model selection + calculation in one call. Returns (model_name, monthly_mt)."""
    model = select_stat_model(history)
    if model == "ZERO":
        return model, 0.0
    if model == "CROSTON":
        return model, croston_forecast(history)
    return model, ewma_forecast(history)


def assign_confidence(booked_mt: float, fcst_mt: float, stat_mt: float, final_mt: float) -> Optional[str]:
    """Q4-total confidence grade, per TASK.md 11.3. None when there's no forecast at all."""
    if final_mt <= 0:
        return None
    booked_ratio = booked_mt / final_mt
    combined_ratio = (booked_mt + fcst_mt) / final_mt
    stat_ratio = stat_mt / final_mt
    if booked_ratio >= 0.7:
        return "HIGH"
    if combined_ratio >= 0.7 and stat_ratio <= 0.3:
        return "MID"
    return "LOW"
