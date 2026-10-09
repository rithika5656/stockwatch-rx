from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from math import atan2, cos, radians, sin, sqrt
import os
from typing import Any

from .demo_data import SURGERY_TYPES

RISK_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
RISK_THRESHOLDS = {
    "critical_days": int(os.getenv("RISK_CRITICAL_DAYS", "2")),
    "high_days": int(os.getenv("RISK_HIGH_DAYS", "5")),
    "medium_days": int(os.getenv("RISK_MEDIUM_DAYS", "10")),
}
if not 0 <= RISK_THRESHOLDS["critical_days"] < RISK_THRESHOLDS["high_days"] < RISK_THRESHOLDS["medium_days"]:
    raise ValueError("Risk thresholds must satisfy 0 <= critical < high < medium")
SIMULATION_DAYS = 365
PRIORITY_WEIGHTS = {"emergency_demand": 0.35, "patient_load": 0.25, "stockout_urgency": 0.20, "alternative_availability": 0.10, "supply_criticality": 0.10}
REDISTRIBUTION_WEIGHTS = {"shortage_urgency": 0.375, "demand_pressure": 0.125, "emergency_load": 0.125, "supply_criticality": 0.1875, "expiry_urgency": 0.125, "transport_feasibility": 0.0625}
LOCAL_RADIUS_KM = float(os.getenv("LOCAL_REDISTRIBUTION_RADIUS_KM", "15"))


def _risk(days: float) -> str:
    if days <= RISK_THRESHOLDS["critical_days"]:
        return "CRITICAL"
    if days <= RISK_THRESHOLDS["high_days"]:
        return "HIGH"
    if days <= RISK_THRESHOLDS["medium_days"]:
        return "MEDIUM"
    return "LOW"


def _simulate_depletion(stock: int, safety_stock: int, daily_demand: float, trend: float, scheduled_demand: dict[int, float] | None = None) -> dict[str, Any]:
    remaining = float(stock)
    safety_breach_day = 0 if remaining <= safety_stock else None
    zero_stock_day = 0 if remaining <= 0 else None
    safety_breach_stock = remaining if safety_breach_day == 0 else None
    simulation = []

    for day in range(1, SIMULATION_DAYS + 1):
        baseline = max(0.1, daily_demand * (1 + max(-0.01, min(0.003, trend * 0.001)) * day))
        surgery_demand = (scheduled_demand or {}).get(day, 0.0)
        demand = baseline + surgery_demand
        remaining = max(0.0, remaining - demand)
        simulation.append({"day": day, "forecast_demand": round(demand, 1), "baseline_demand": round(baseline, 1), "surgery_demand": round(surgery_demand, 1), "projected_stock": round(remaining, 1)})
        if safety_breach_day is None and remaining <= safety_stock:
            safety_breach_day = day
            safety_breach_stock = remaining
        if zero_stock_day is None and remaining <= 0:
            zero_stock_day = day
            break

    return {
        "days_to_safety_stock": safety_breach_day if safety_breach_day is not None else SIMULATION_DAYS,
        "days_to_zero_stock": zero_stock_day if zero_stock_day is not None else SIMULATION_DAYS,
        "stock_at_safety_breach": round(safety_breach_stock or 0, 1),
        "projected_remaining_stock": round(remaining, 1),
        "simulation": simulation,
    }


def _distance_km(first: dict[str, Any], second: dict[str, Any]) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (first["latitude"], first["longitude"], second["latitude"], second["longitude"]))
    value = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * atan2(sqrt(value), sqrt(1 - value))


def _simulate_fefo_batches(batches: list[dict[str, Any]], daily_demand: float, trend: float, today: date) -> dict[str, dict[str, int]]:
    ordered = sorted(batches, key=lambda batch: (batch["expiry_date"], batch["batch_id"]))
    expiry_days = {batch["batch_id"]: (date.fromisoformat(batch["expiry_date"]) - today).days for batch in ordered}
    remaining = {batch["batch_id"]: float(max(0, int(batch["quantity"]))) for batch in ordered}
    consumed = {batch["batch_id"]: 0.0 for batch in ordered}

    latest_expiry = max((days for days in expiry_days.values() if days > 0), default=0)
    for day in range(1, latest_expiry + 1):
        demand = max(0.1, daily_demand * (1 + max(-0.01, min(0.003, trend * 0.001)) * day))
        for batch in ordered:
            batch_id = batch["batch_id"]
            if expiry_days[batch_id] < day or batch.get("batch_status") == "expired" or demand <= 0:
                continue
            used = min(remaining[batch_id], demand)
            remaining[batch_id] -= used
            consumed[batch_id] += used
            demand -= used

    return {batch_id: {"expected_usage": round(consumed[batch_id]), "expected_waste": max(0, round(remaining[batch_id]))} for batch_id in remaining}


def _surgery_demand_by_key(dataset: dict[str, Any]) -> tuple[dict[tuple[str, str], dict[int, float]], dict[tuple[str, str], dict[str, float]], dict[str, int]]:
    daily: dict[tuple[str, str], dict[int, float]] = defaultdict(lambda: defaultdict(float))
    totals: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    surgery_counts: dict[str, int] = defaultdict(int)
    today = date.today()
    for surgery in dataset.get("surgery_schedules", []):
        if surgery.get("status", "scheduled") != "scheduled":
            continue
        hospital_id = surgery["hospital_id"]
        surgery_date = date.fromisoformat(surgery["scheduled_date"])
        offset = (surgery_date - today).days
        if offset < 0:
            continue
        surgery_counts[hospital_id] += int(surgery["number_of_cases"])
        requirements = surgery.get("estimated_supply_requirements") or {
            supply_id: int(surgery["number_of_cases"]) * per_case
            for supply_id, per_case in SURGERY_TYPES.get(surgery["surgery_type"], {}).items()
        }
        day = offset + 1
        for supply_id, quantity in requirements.items():
            value = max(0.0, float(quantity))
            daily[(hospital_id, supply_id)][day] += value
            totals[(hospital_id, supply_id)]["total_units"] += value
            if 1 <= day <= 7:
                totals[(hospital_id, supply_id)]["next_7_days"] += value
    return daily, totals, surgery_counts


def analyze(dataset: dict[str, Any], surgery_schedules: list[dict[str, Any]] | None = None, shareable_pool: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if surgery_schedules is not None or shareable_pool is not None:
        dataset = {**dataset}
        if surgery_schedules is not None:
            dataset["surgery_schedules"] = surgery_schedules
        if shareable_pool is not None:
            dataset["shareable_pool"] = shareable_pool
    hospitals = {item["hospital_id"]: item for item in dataset["hospitals"]}
    supplies = {item["supply_id"]: item for item in dataset["supplies"]}
    scheduled_daily, scheduled_totals, surgery_counts = _surgery_demand_by_key(dataset)
    share_by_key = {
        (row["hospital_id"], row["supply_id"]): row
        for row in dataset.get("shareable_pool", []) if row.get("enabled")
    }
    stock_by_key: dict[tuple[str, str], int] = defaultdict(int)
    safety_by_key: dict[tuple[str, str], int] = {}
    batches_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for batch in dataset["inventory"]:
        key = (batch["hospital_id"], batch["supply_id"])
        expiry = date.fromisoformat(batch["expiry_date"])
        if batch.get("batch_status") != "expired" and expiry >= date.today():
            stock_by_key[key] += batch["quantity"]
        safety_by_key[key] = batch["safety_stock"]
        batches_by_key[key].append(batch)

    history_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in dataset["demand_history"]:
        history_by_key[(row["hospital_id"], row["supply_id"])].append(row)

    forecasts = []
    history_points: dict[tuple[str, str], list[float]] = {}
    for key, rows in history_by_key.items():
        rows.sort(key=lambda row: row["date"])
        values = [float(row["quantity_used"]) for row in rows]
        history_points[key] = values
        last_seven = values[-7:]
        last_fourteen = values[-14:]
        prior_fourteen = values[-28:-14]
        average_7 = sum(last_seven) / max(1, len(last_seven))
        average_14 = sum(last_fourteen) / max(1, len(last_fourteen))
        prior_average = sum(prior_fourteen) / max(1, len(prior_fourteen))
        trend = (average_14 - prior_average) / max(1, prior_average)
        outbreak_signal = max((float(row["outbreak_signal"]) for row in rows[-7:]), default=0.0)
        forecast_daily = max(0.1, (average_7 * 0.7 + average_14 * 0.3) * (1 + trend))
        recent_rows = rows[-7:]
        previous_rows = rows[-14:-7]
        patient_recent = sum(float(row.get("patient_load", 0)) for row in recent_rows) / max(1, len(recent_rows))
        patient_previous = sum(float(row.get("patient_load", patient_recent)) for row in previous_rows) / max(1, len(previous_rows))
        patient_change = (patient_recent - patient_previous) / max(0.1, patient_previous)
        emergency_recent = sum(float(row.get("emergency_cases", 0)) for row in recent_rows) / max(1, len(recent_rows))
        emergency_previous = sum(float(row.get("emergency_cases", emergency_recent)) for row in previous_rows) / max(1, len(previous_rows))
        emergency_change = (emergency_recent - emergency_previous) / max(1, emergency_previous)
        seasonality = sum(float(row.get("seasonality_factor", 1)) for row in recent_rows) / max(1, len(recent_rows))
        hospital_id, supply_id = key
        stock = stock_by_key[key]
        safety = safety_by_key[key]
        surgery_impact = scheduled_totals.get(key, {})
        surgery_additional_daily = surgery_impact.get("next_7_days", 0.0) / 7
        adjusted_daily = forecast_daily + surgery_additional_daily
        supplier_lead_time = max((int(batch.get("supplier_lead_time", batch.get("supplier_lead_days", 7))) for batch in batches_by_key[key]), default=7)
        baseline_depletion = _simulate_depletion(stock, safety, forecast_daily, trend)
        baseline_days = baseline_depletion["days_to_safety_stock"]
        baseline_level = _risk(baseline_days)
        depletion = _simulate_depletion(stock, safety, forecast_daily, trend, scheduled_daily.get(key))
        days_left = depletion["days_to_safety_stock"]
        protected_reserve = safety + adjusted_daily * min(7, max(2, supplier_lead_time))
        available_surplus = max(0, round(stock - protected_reserve))
        shareable_quantity = min(int(share_by_key.get(key, {}).get("shareable_quantity", 0)), available_surplus)
        level = _risk(days_left)
        forecasts.append({
            "hospital_id": hospital_id, "hospital": hospitals[hospital_id]["name"],
            "supply_id": supply_id, "supply": supplies[supply_id]["name"],
            "category": supplies[supply_id]["category"], "criticality": supplies[supply_id]["criticality"],
            "current_stock": stock, "safety_stock": safety,
            "reorder_level": max((int(batch.get("reorder_level", safety)) for batch in batches_by_key[key]), default=safety),
            "supplier_lead_time": supplier_lead_time,
            "alternative_available": bool(supplies[supply_id].get("alternative_available", False)),
            "alternative_supply_id": supplies[supply_id].get("alternative_supply_id"),
            "average_daily_demand": round(average_7, 1), "baseline_forecast_daily_demand": round(forecast_daily, 1),
            "surgery_additional_demand": round(surgery_additional_daily, 1), "forecast_daily_demand": round(adjusted_daily, 1),
            "surgery_additional_units_next_7_days": round(surgery_impact.get("next_7_days", 0.0), 1),
            "surgery_additional_units_total": round(surgery_impact.get("total_units", 0.0), 1),
            "scheduled_surgery_cases": surgery_counts.get(hospital_id, 0),
            "shareable_quantity": shareable_quantity,
            "forecast_horizon_days": 14, "days_until_stockout": days_left,
            "days_until_safety_breach": days_left,
            "days_until_zero_stock": depletion["days_to_zero_stock"],
            "days_until_physical_stockout": depletion["days_to_zero_stock"],
            "stockout_date": (date.today() + timedelta(days=days_left)).isoformat(),
            "safety_stock_breach_date": (date.today() + timedelta(days=days_left)).isoformat(),
            "zero_stock_date": (date.today() + timedelta(days=depletion["days_to_zero_stock"])).isoformat(),
            "stock_at_safety_breach": depletion["stock_at_safety_breach"],
            "projected_remaining_stock": depletion["projected_remaining_stock"],
            "simulation": depletion["simulation"],
            "risk_level": level,
            "baseline_risk_level": baseline_level,
            "trend_percent": round(trend * 100, 1), "outbreak_signal": round(outbreak_signal, 2),
            "patient_load_change_percent": round(patient_change * 100, 1), "seasonality_factor": round(seasonality, 3),
            "emergency_cases_change_percent": round(emergency_change * 100, 1),
            "forecast_method": "weighted_moving_average",
            "historical_demand": [round(value, 1) for value in values[-30:]],
            "forecast_series": [round(forecast_daily * (1 + max(-0.01, trend * 0.025) * day) + scheduled_daily.get(key, {}).get(day, 0.0), 1) for day in range(1, 15)],
            "forecast_components": {
                "historical_baseline": round(average_7, 1),
                "forecast_adjustment": round(forecast_daily - average_7, 1),
                "surgery_additional_daily_average": round(surgery_additional_daily, 1),
                "surgery_additional_units_next_7_days": round(surgery_impact.get("next_7_days", 0.0), 1),
                "adjusted_daily_forecast": round(adjusted_daily, 1),
            },
            "explanation": _forecast_explanation(trend, days_left, patient_change, emergency_change, seasonality, average_7, forecast_daily, surgery_additional_daily, baseline_level, level),
        })

    forecast_by_key = {(row["hospital_id"], row["supply_id"]): row for row in forecasts}
    expiry_risks = []
    for key, batches in batches_by_key.items():
        forecast = forecast_by_key[key]
        daily = forecast["forecast_daily_demand"]
        fefo = _simulate_fefo_batches(batches, daily, forecast["trend_percent"] / 100, date.today())
        destinations = sorted((item for item in forecasts if item["supply_id"] == key[1] and item["hospital_id"] != key[0] and item["risk_level"] != "LOW"), key=lambda item: item["days_until_stockout"])
        candidate = destinations[0] if destinations else None
        for fefo_rank, batch in enumerate(sorted(batches, key=lambda item: (item["expiry_date"], item["batch_id"])), start=1):
            days = max(0, (date.fromisoformat(batch["expiry_date"]) - date.today()).days)
            expected_use = fefo[batch["batch_id"]]["expected_usage"]
            expected_waste = fefo[batch["batch_id"]]["expected_waste"]
            waste_ratio = expected_waste / max(1, batch["quantity"])
            if days <= 30 or waste_ratio >= 0.25:
                level = "CRITICAL" if days <= 7 or waste_ratio >= 0.7 else "HIGH" if days <= 14 or waste_ratio >= 0.45 else "MEDIUM" if days <= 30 or waste_ratio >= 0.25 else "LOW"
                action = f"FEFO: consume or redistribute to {candidate['hospital']}" if candidate and expected_waste else "FEFO: consume this lot before later-expiry batches"
                expiry_risks.append({
                    "hospital_id": key[0], "hospital": hospitals[key[0]]["name"],
                    "supply_id": key[1], "supply": supplies[key[1]]["name"],
                    "batch_id": batch["batch_id"], "quantity": batch["quantity"],
                    "expiry_date": batch["expiry_date"], "days_until_expiry": days,
                    "expected_usage": expected_use, "expected_waste": expected_waste,
                    "potential_wastage": expected_waste, "wastage_percentage": round(waste_ratio * 100, 1),
                    "fefo_rank": fefo_rank, "redistribution_candidate": candidate["hospital"] if candidate and expected_waste else None,
                    "risk_level": level, "recommended_action": action,
                })

    shortages = [row for row in forecasts if row["risk_level"] != "LOW"]
    shortages.sort(key=lambda row: (RISK_ORDER[row["risk_level"]], -row["days_until_stockout"]), reverse=True)
    priorities = []
    for row in shortages:
        hospital = hospitals[row["hospital_id"]]
        supply = supplies[row["supply_id"]]
        urgency_score = max(0, min(100, 100 - row["days_until_stockout"] * 10))
        emergency = hospital["emergency_load"] * 100
        patient_load = hospital["occupancy_rate"] * 100
        criticality = {"critical": 100, "high": 76, "medium": 52, "low": 30}.get(supply["criticality"], 50)
        alternative_id = supply.get("alternative_supply_id")
        alternative_row = forecast_by_key.get((row["hospital_id"], alternative_id)) if alternative_id else None
        alternative_in_stock = bool(
            supply.get("alternative_available", False)
            and alternative_row
            and alternative_row["current_stock"] > alternative_row["safety_stock"]
        )
        alternative_score = 0 if alternative_in_stock else 100
        components = {
            factor: round(value * PRIORITY_WEIGHTS[factor])
            for factor, value in {
                "emergency_demand": emergency, "patient_load": patient_load,
                "stockout_urgency": urgency_score, "alternative_availability": alternative_score,
                "supply_criticality": criticality,
            }.items()
        }
        score = min(100, sum(components.values()))
        reasons = [
            f"Emergency load {round(hospital['emergency_load'] * 100)}% (35% weight)",
            f"Patient occupancy {round(hospital['occupancy_rate'] * 100)}% (25% weight)",
            f"Safety-stock breach in {row['days_until_stockout']} days (20% weight)",
            "No alternative is currently available above safety stock (10% weight)" if alternative_score else "An alternative is available above safety stock (10% weight)",
            f"{supply['criticality'].title()} supply criticality (10% weight)",
        ]
        priorities.append({**row, "priority_score": score, "priority": "CRITICAL" if score >= 80 else "HIGH" if score >= 65 else "MEDIUM" if score >= 45 else "LOW", "score_components": components, "reasons": reasons})
    priorities.sort(key=lambda row: row["priority_score"], reverse=True)

    transfers = _recommend_transfers(forecasts, stock_by_key, safety_by_key, batches_by_key, hospitals, supplies, expiry_risks, share_by_key)
    result = {
        "forecasts": forecasts, "shortages": shortages, "expiry_risks": expiry_risks,
        "priorities": priorities, "transfers": transfers,
        "inventory_totals": stock_by_key, "safety_totals": safety_by_key,
        "hospitals": hospitals, "supplies": supplies, "batches": batches_by_key,
        "history_points": history_points,
    }
    return result


def _forecast_explanation(trend: float, days: float, patient_change: float, emergency_change: float, seasonality: float, history_baseline: float, forecast_baseline: float, surgery_additional: float, baseline_risk: str, adjusted_risk: str) -> str:
    drivers = []
    if abs(trend) >= 0.04:
        drivers.append(f"recent demand changed {abs(trend) * 100:.0f}% versus the prior two weeks")
    if abs(patient_change) >= 0.03:
        drivers.append(f"patient load changed {abs(patient_change) * 100:.0f}%")
    if abs(emergency_change) >= 0.05:
        drivers.append(f"emergency cases changed {abs(emergency_change) * 100:.0f}%")
    if abs(seasonality - 1) >= 0.03:
        drivers.append(f"recent seasonality factor is {seasonality:.2f}")
    if surgery_additional > 0:
        drivers.append(f"scheduled surgeries add {surgery_additional:.1f} units/day over the next 7 days")
    if not drivers:
        drivers.append("the forecast uses a 7-day weighted moving average and recent trend")
    drivers.append(f"safety stock is reached in {days:.1f} days")
    explanation = "Forecast uses synthetic consumption history; " + " and ".join(drivers) + "."
    if surgery_additional > 0:
        explanation += (f" Seven-day average: historical baseline {history_baseline:.1f} units/day, forecast trend adjustment "
                f"{forecast_baseline - history_baseline:+.1f}, scheduled surgery demand {surgery_additional:+.1f}, "
                f"adjusted forecast {forecast_baseline + surgery_additional:.1f} units/day. Risk changed from {baseline_risk} to {adjusted_risk}.")
    return explanation


def _recommend_transfers(forecasts: list[dict[str, Any]], stock: dict[tuple[str, str], int], safety: dict[tuple[str, str], int], batches: dict[tuple[str, str], list[dict[str, Any]]], hospitals: dict[str, dict[str, Any]], supplies: dict[str, dict[str, Any]], expiry_risks: list[dict[str, Any]], share_by_key: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    by_supply: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for forecast in forecasts:
        by_supply[forecast["supply_id"]].append(forecast)
    recommendations = []
    planned_outbound: dict[tuple[str, str], int] = defaultdict(int)
    planned_by_batch: dict[tuple[str, str, str], int] = defaultdict(int)
    planned_shareable: dict[tuple[str, str], int] = defaultdict(int)
    for supply_id, locations in by_supply.items():
        typical_demand = sum(item["forecast_daily_demand"] for item in locations) / max(1, len(locations))
        for destination in locations:
            if destination["days_until_stockout"] > 14 and destination["current_stock"] >= destination["reorder_level"]:
                continue
            for source in locations:
                if source["hospital_id"] == destination["hospital_id"]:
                    continue
                source_key = (source["hospital_id"], supply_id)
                source_hospital = hospitals[source["hospital_id"]]
                destination_hospital = hospitals[destination["hospital_id"]]
                distance = _distance_km(source_hospital, destination_hospital)
                if distance > LOCAL_RADIUS_KM:
                    continue
                transport_hours = round(max(2, distance / 55 + 1.5), 1)
                transport_days = transport_hours / 24
                if transport_days >= max(0.25, destination["days_until_zero_stock"]):
                    continue
                destination_lead = destination["supplier_lead_time"]
                need_horizon = max(1, destination_lead - transport_days)
                destination_need = max(0, round(destination["safety_stock"] + destination["forecast_daily_demand"] * need_horizon - destination["current_stock"]))
                if destination_need <= 0:
                    continue
                source_lead = source["supplier_lead_time"]
                source_reserve = safety[source_key] + source["forecast_daily_demand"] * min(7, max(2, source_lead))
                surplus = max(0, round(stock[source_key] - source_reserve - planned_outbound[source_key]))
                if surplus <= 0:
                    continue
                share_record = share_by_key.get(source_key)
                if not share_record:
                    continue
                shareable = max(0, int(share_record.get("shareable_quantity", 0)) - planned_shareable[source_key])
                arrival_day = max(1, int(transport_days + 0.99))
                eligible_batches = sorted(
                    (batch for batch in batches[source_key]
                     if batch.get("batch_status") != "expired"
                     and (date.fromisoformat(batch["expiry_date"]) - date.today()).days >= arrival_day),
                    key=lambda batch: (batch["expiry_date"], batch["batch_id"]),
                )
                batch_available = sum(max(0, int(batch["quantity"]) - planned_by_batch[(*source_key, batch["batch_id"])]) for batch in eligible_batches)
                transfer_limit = int(supplies[supply_id].get("max_transfer_quantity", destination_need))
                qty = min(shareable, surplus, destination_need, batch_available, transfer_limit)
                if qty < max(1, round(destination["forecast_daily_demand"] * 0.1)):
                    continue
                planned_outbound[source_key] += qty
                planned_shareable[source_key] += qty
                shortage_severity = max(0, 100 - destination["days_until_stockout"] * 8)
                criticality = {"critical": 100, "high": 75, "medium": 48, "low": 25}[supplies[supply_id]["criticality"]]
                source_expiry = min((date.fromisoformat(batch["expiry_date"]) for batch in eligible_batches), default=date.today() + timedelta(days=365))
                days_to_source_expiry = max(0, (source_expiry - date.today()).days)
                urgency = max(0, min(100, (30 - days_to_source_expiry) / 30 * 100))
                demand_pressure = max(0, min(100, destination["forecast_daily_demand"] / max(0.1, typical_demand) * 55))
                emergency_load = destination_hospital["emergency_load"] * 100
                transport_feasibility = max(0, 100 - transport_hours / 24 * 100)
                source_after = stock[source_key] - planned_outbound[source_key]
                safety_penalty = max(0, 18 - (source_after - safety[source_key]) / max(1, source["forecast_daily_demand"]) * 2)
                lead_penalty = max(0, source_lead - 7) * 2
                score_components = {
                    "shortage_urgency": round(shortage_severity * REDISTRIBUTION_WEIGHTS["shortage_urgency"]),
                    "demand_pressure": round(demand_pressure * REDISTRIBUTION_WEIGHTS["demand_pressure"]),
                    "emergency_load": round(emergency_load * REDISTRIBUTION_WEIGHTS["emergency_load"]),
                    "supply_criticality": round(criticality * REDISTRIBUTION_WEIGHTS["supply_criticality"]),
                    "expiry_urgency": round(urgency * REDISTRIBUTION_WEIGHTS["expiry_urgency"]),
                    "transport_feasibility": round(transport_feasibility * REDISTRIBUTION_WEIGHTS["transport_feasibility"]),
                }
                penalties = {"source_safety_buffer": round(safety_penalty), "supplier_backup_lead_time": round(lead_penalty)}
                score = max(0, min(100, sum(score_components.values()) - sum(penalties.values())))
                destination_after = destination["current_stock"] + qty
                coverage_before = round(destination["current_stock"] / max(0.1, destination["forecast_daily_demand"]), 1)
                coverage_after = round(destination_after / max(0.1, destination["forecast_daily_demand"]), 1)
                remaining_to_allocate = qty
                selected_batches = []
                for batch in eligible_batches:
                    allocation = min(remaining_to_allocate, max(0, int(batch["quantity"]) - planned_by_batch[(*source_key, batch["batch_id"])]))
                    if allocation > 0:
                        selected_batches.append({"batch_id": batch["batch_id"], "quantity": allocation, "expiry_date": batch["expiry_date"]})
                        planned_by_batch[(*source_key, batch["batch_id"])] += allocation
                        remaining_to_allocate -= allocation
                    if remaining_to_allocate <= 0:
                        break
                reason = (f"{destination_hospital['name']} is projected to breach safety stock in {destination['days_until_stockout']} days "
                          f"({coverage_before} days of on-hand cover) with {round(destination_hospital['emergency_load'] * 100)}% emergency load. "
                          f"Move {qty:,} units to cover demand through the {destination_lead}-day supplier lead time. "
                          f"{source_hospital['name']} retains {source_after:,} units, {source_after - safety[source_key]:,} above safety stock; "
                          f"FEFO selects batch {selected_batches[0]['batch_id']} first and destination cover becomes {coverage_after} days.")
                recommendations.append({
                    "recommendation_id": f"TR-{source['hospital_id']}-{destination['hospital_id']}-{supply_id}",
                    "source_hospital_id": source["hospital_id"], "source_hospital": source_hospital["name"],
                    "destination_hospital_id": destination["hospital_id"], "destination_hospital": destination_hospital["name"],
                    "supply_id": supply_id, "supply": supplies[supply_id]["name"],
                    "recommended_quantity": qty, "destination_need": destination_need,
                    "days_until_stockout": destination["days_until_stockout"],
                    "shareable_quantity": min(int(share_record.get("shareable_quantity", 0)), surplus),
                    "priority_score": score, "score_components": score_components, "score_penalties": penalties,
                    "priority": "CRITICAL" if score >= 80 else "HIGH" if score >= 60 else "MEDIUM",
                    "reason": reason, "estimated_transport_hours": transport_hours,
                    "distance_km": round(distance, 2), "local_radius_km": LOCAL_RADIUS_KM,
                    "source_stock_before": stock[source_key], "source_remaining_stock": source_after,
                    "source_safety_stock": safety[source_key], "source_reserve": round(source_reserve),
                    "destination_stock_before": destination["current_stock"], "destination_stock_after": destination_after,
                    "destination_coverage_before_days": coverage_before, "destination_expected_coverage_days": coverage_after,
                    "source_batch_allocations": selected_batches, "fefo_recommendation": True,
                    "source_expiry_date": source_expiry.isoformat(),
                    "status": "recommended",
                })
    recommendations.sort(key=lambda item: item["priority_score"], reverse=True)
    return recommendations[:60]
