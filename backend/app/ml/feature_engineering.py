from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import pstdev
from typing import Any

from ..demo_data import SURGERY_TYPES

DEMAND_FEATURES = (
    "day_of_week", "day_of_month", "week_of_year", "month", "quarter", "year",
    "lag_1", "lag_7", "lag_14", "lag_30",
    "rolling_mean_7", "rolling_mean_14", "rolling_mean_30", "rolling_std_7", "rolling_std_30",
    "demand_growth_7", "demand_growth_30", "patient_load", "emergency_cases", "hospital_capacity",
    "surgery_count", "emergency_surgery_count", "surgery_type_count", "expected_surgery_demand",
    "surgery_duration", "surgery_day_indicator", "hospital_code", "supply_code", "base_daily_demand",
)
ANOMALY_FEATURES = (
    "daily_demand", "rolling_mean_7", "rolling_mean_30", "demand_growth_7", "demand_growth_30",
    "demand_variability", "emergency_cases", "patient_load", "surgery_count",
)
STOCKOUT_FEATURES = (
    "current_stock", "forecast_demand", "demand_growth_7", "demand_growth_30", "safety_stock",
    "supplier_lead_time", "patient_load", "emergency_cases", "surgery_count", "emergency_surgery_count",
    "expected_surgery_demand", "demand_variability", "historical_stockout_frequency", "expiry_days",
    "incoming_stock", "shareable_supply_available",
)
RISK_CLASSES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _growth(recent: list[float], prior: list[float]) -> float:
    return _mean(recent) / max(0.1, _mean(prior)) - 1.0 if prior else 0.0


def _date_features(value: date) -> dict[str, float]:
    return {
        "day_of_week": float(value.weekday()), "day_of_month": float(value.day),
        "week_of_year": float(value.isocalendar().week), "month": float(value.month),
        "quarter": float((value.month - 1) // 3 + 1), "year": float(value.year),
    }


def _series_index(dataset: dict[str, Any]) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, int], dict[str, int]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in dataset["demand_history"]:
        grouped[(row["hospital_id"], row["supply_id"])].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: row["date"])
    hospital_codes = {key: index for index, key in enumerate(sorted(item["hospital_id"] for item in dataset["hospitals"]))}
    supply_codes = {key: index for index, key in enumerate(sorted(item["supply_id"] for item in dataset["supplies"]))}
    return grouped, hospital_codes, supply_codes


def _base_features(
    rows: list[dict[str, Any]], index: int, hospital: dict[str, Any], supply: dict[str, Any],
    hospital_code: int, supply_code: int, operational: dict[str, float] | None = None,
) -> dict[str, float]:
    target_date = date.fromisoformat(rows[index]["date"])
    prior = [float(row["quantity_used"]) for row in rows[:index]]
    last_7, last_14, last_30 = prior[-7:], prior[-14:], prior[-30:]
    prior_7, prior_30 = prior[-14:-7], prior[-60:-30]
    mean_30 = _mean(last_30)
    op = operational or {}
    source_row = rows[index] if index < len(rows) else {}
    prior_rows = rows[:index][-7:]
    features = _date_features(target_date)
    features.update({
        "lag_1": prior[-1] if len(prior) >= 1 else 0.0,
        "lag_7": prior[-7] if len(prior) >= 7 else 0.0,
        "lag_14": prior[-14] if len(prior) >= 14 else 0.0,
        "lag_30": prior[-30] if len(prior) >= 30 else 0.0,
        "rolling_mean_7": _mean(last_7), "rolling_mean_14": _mean(last_14), "rolling_mean_30": mean_30,
        "rolling_std_7": pstdev(last_7) if len(last_7) > 1 else 0.0,
        "rolling_std_30": pstdev(last_30) if len(last_30) > 1 else 0.0,
        "demand_growth_7": _growth(last_7, prior_7), "demand_growth_30": _growth(last_30, prior_30),
        "patient_load": float(op.get("patient_load", _mean([float(row.get("patient_load", 0.0)) for row in prior_rows]) or hospital.get("occupancy_rate", 0.0))),
        "emergency_cases": float(op.get("emergency_cases", _mean([float(row.get("emergency_cases", 0.0)) for row in prior_rows]))),
        "hospital_capacity": float(hospital.get("capacity", hospital.get("bed_capacity", 0))),
        "surgery_count": float(op.get("surgery_count", source_row.get("surgery_count", 0))),
        "emergency_surgery_count": float(op.get("emergency_surgery_count", source_row.get("emergency_surgery_count", 0))),
        "surgery_type_count": float(op.get("surgery_type_count", source_row.get("surgery_type_count", 0))),
        "expected_surgery_demand": float(op.get("expected_surgery_demand", source_row.get("expected_surgery_demand", 0.0))),
        "surgery_duration": float(op.get("surgery_duration", source_row.get("surgery_duration", 0.0))),
        "surgery_day_indicator": float(op.get("surgery_day_indicator", source_row.get("surgery_day_indicator", 0))),
        "hospital_code": float(hospital_code), "supply_code": float(supply_code),
        "base_daily_demand": float(supply.get("base_daily_demand", 0.0)),
    })
    features["demand_variability"] = features["rolling_std_30"] / max(0.1, mean_30)
    return features


def build_demand_training_rows(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    grouped, hospital_codes, supply_codes = _series_index(dataset)
    hospitals = {row["hospital_id"]: row for row in dataset["hospitals"]}
    supplies = {row["supply_id"]: row for row in dataset["supplies"]}
    samples = []
    for (hospital_id, supply_id), rows in grouped.items():
        for index in range(60, len(rows)):
            target = float(rows[index]["quantity_used"])
            if target < 0:
                continue
            features = _base_features(rows, index, hospitals[hospital_id], supplies[supply_id], hospital_codes[hospital_id], supply_codes[supply_id])
            samples.append({"date": rows[index]["date"], "hospital_id": hospital_id, "supply_id": supply_id, "features": features, "target": target})
    return sorted(samples, key=lambda row: (row["date"], row["hospital_id"], row["supply_id"]))


def _surgery_features(schedules: list[dict[str, Any]], hospital_id: str, supply_id: str, target_date: date) -> dict[str, float]:
    surgery_count = emergency_count = type_count = duration = expected = 0.0
    types: set[str] = set()
    for row in schedules:
        if row.get("hospital_id") != hospital_id or row.get("status", "scheduled") != "scheduled":
            continue
        if date.fromisoformat(row["scheduled_date"]) != target_date:
            continue
        cases = max(0, int(row.get("number_of_cases", 0)))
        surgery_type = row.get("surgery_type", "")
        surgery_count += cases
        emergency_count += cases if surgery_type == "emergency" else 0
        if cases:
            types.add(surgery_type)
        duration += cases * float(row.get("expected_duration_minutes", 120))
        requirements = row.get("estimated_supply_requirements") or SURGERY_TYPES.get(surgery_type, {})
        expected += float(requirements.get(supply_id, 0.0)) * cases
    return {
        "surgery_count": surgery_count, "emergency_surgery_count": emergency_count,
        "surgery_type_count": float(len(types)), "expected_surgery_demand": expected,
        "surgery_duration": duration, "surgery_day_indicator": float(surgery_count > 0),
    }


def build_prediction_features(dataset: dict[str, Any], hospital_id: str, supply_id: str, target_date: date | None = None) -> dict[str, float]:
    grouped, hospital_codes, supply_codes = _series_index(dataset)
    hospital = next(row for row in dataset["hospitals"] if row["hospital_id"] == hospital_id)
    supply = next(row for row in dataset["supplies"] if row["supply_id"] == supply_id)
    rows = grouped[(hospital_id, supply_id)]
    target_date = target_date or date.today() + timedelta(days=1)
    operational = _surgery_features(dataset.get("surgery_schedules", []), hospital_id, supply_id, target_date)
    features = _base_features(rows + [{"date": target_date.isoformat()}], len(rows), hospital, supply, hospital_codes[hospital_id], supply_codes[supply_id], operational)
    return features


def _simulated_stock_path(dataset: dict[str, Any], key: tuple[str, str], rows: list[dict[str, Any]]) -> tuple[list[float], list[float], list[float]]:
    inventory = [row for row in dataset["inventory"] if (row["hospital_id"], row["supply_id"]) == key]
    usable_stock = sum(int(row["quantity"]) for row in inventory if row.get("batch_status") != "expired")
    safety = max((int(row["safety_stock"]) for row in inventory), default=0)
    lead = max((int(row.get("supplier_lead_time", 7)) for row in inventory), default=7)
    values = [float(row["quantity_used"]) for row in rows]
    capacity = max(usable_stock, round(safety + _mean(values) * (lead + 7)))
    refill_period = max(21, lead * 2)
    stock = float(capacity)
    snapshots, incoming, breaches = [], [], []
    for index, row in enumerate(rows):
        refill = float(capacity) if index > 0 and index % refill_period == 0 else 0.0
        if refill:
            stock = refill
        snapshots.append(stock)
        incoming.append(refill)
        breaches.append(float(stock <= safety))
        stock = max(0.0, stock - float(row["quantity_used"]))
    return snapshots, incoming, breaches


def _stockout_class(stock: float, safety: float, future_demands: list[float]) -> str:
    remaining = stock
    safety_day = zero_day = None
    for day, demand in enumerate(future_demands, start=1):
        remaining = max(0.0, remaining - demand)
        if safety_day is None and remaining <= safety:
            safety_day = day
        if zero_day is None and remaining <= 0:
            zero_day = day
    if zero_day is not None and zero_day <= 3:
        return "CRITICAL"
    if safety_day is not None and safety_day <= 7:
        return "HIGH"
    if safety_day is not None and safety_day <= 14:
        return "MEDIUM"
    return "LOW"


def build_stockout_training_rows(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    grouped, hospital_codes, supply_codes = _series_index(dataset)
    hospitals = {row["hospital_id"]: row for row in dataset["hospitals"]}
    supplies = {row["supply_id"]: row for row in dataset["supplies"]}
    pools = {(row["hospital_id"], row["supply_id"]): row for row in dataset.get("shareable_pool", []) if row.get("enabled")}
    samples = []
    for key, rows in grouped.items():
        hospital_id, supply_id = key
        inventory = [row for row in dataset["inventory"] if (row["hospital_id"], row["supply_id"]) == key]
        safety = max((int(row["safety_stock"]) for row in inventory), default=0)
        lead = max((int(row.get("supplier_lead_time", 7)) for row in inventory), default=7)
        expiry_dates = [date.fromisoformat(row["expiry_date"]) for row in inventory if row.get("batch_status") != "expired"]
        snapshots, incoming, breach_history = _simulated_stock_path(dataset, key, rows)
        for index in range(60, len(rows) - 14):
            features = _base_features(rows, index, hospitals[hospital_id], supplies[supply_id], hospital_codes[hospital_id], supply_codes[supply_id])
            event_date = date.fromisoformat(rows[index]["date"])
            future = [float(row["quantity_used"]) for row in rows[index + 1:index + 15]]
            recent_breaches = breach_history[max(0, index - 30):index]
            share = pools.get(key)
            features.update({
                "current_stock": snapshots[index],
                "forecast_demand": max(0.1, features["rolling_mean_7"] * (1 + max(-0.25, min(0.45, features["demand_growth_7"] * 0.25)))),
                "safety_stock": float(safety), "supplier_lead_time": float(lead),
                "historical_stockout_frequency": _mean(recent_breaches),
                "expiry_days": float(max(0, (min(expiry_dates) - event_date).days)) if expiry_dates else 365.0,
                "incoming_stock": incoming[index],
                "shareable_supply_available": float(share["shareable_quantity"]) if share else 0.0,
            })
            samples.append({
                "date": rows[index]["date"], "hospital_id": hospital_id, "supply_id": supply_id,
                "features": {name: features[name] for name in STOCKOUT_FEATURES},
                "target": _stockout_class(snapshots[index], safety, future),
            })
    return sorted(samples, key=lambda row: (row["date"], row["hospital_id"], row["supply_id"]))


def build_anomaly_training_rows(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    grouped, hospital_codes, supply_codes = _series_index(dataset)
    hospitals = {row["hospital_id"]: row for row in dataset["hospitals"]}
    supplies = {row["supply_id"]: row for row in dataset["supplies"]}
    samples = []
    for (hospital_id, supply_id), rows in grouped.items():
        for index in range(30, len(rows)):
            base = _base_features(rows, index, hospitals[hospital_id], supplies[supply_id], hospital_codes[hospital_id], supply_codes[supply_id])
            features = {
                "daily_demand": float(rows[index]["quantity_used"]),
                "rolling_mean_7": base["rolling_mean_7"], "rolling_mean_30": base["rolling_mean_30"],
                "demand_growth_7": base["demand_growth_7"], "demand_growth_30": base["demand_growth_30"],
                "demand_variability": base["demand_variability"], "emergency_cases": base["emergency_cases"],
                "patient_load": base["patient_load"], "surgery_count": base["surgery_count"],
            }
            samples.append({"date": rows[index]["date"], "hospital_id": hospital_id, "supply_id": supply_id, "features": features})
    return sorted(samples, key=lambda row: (row["date"], row["hospital_id"], row["supply_id"]))