from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import logging
import threading
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import database, fulfillment
from .auth_service import login as login_user, user_from_token
from .assistant_service import get_assistant_provider
from .engine import RISK_ORDER, analyze
from .engine import LOCAL_RADIUS_KM
from .engine import SURGERY_TYPES
from .routing import road_route

app = FastAPI(title="MediSupplyIQ API", version="1.0.0", description="Synthetic medical supply decision-support prototype")
_logger = logging.getLogger(__name__)
_CORS_ORIGINS = [
    f"http://{host}:{port}"
    for host in ("localhost", "127.0.0.1")
    for port in range(5173, 5181)
]


@app.middleware("http")
async def catch_all_error_middleware(request: Any, call_next: Any) -> Any:
    try:
        return await call_next(request)
    except Exception as error:
        _logger.exception("Unhandled API request error")
        return JSONResponse(
            status_code=500,
            content={"detail": {"message": str(error) or "Internal server error"}},
        )

SCENARIOS = {
    "normal": "Normal operations",
    "outbreak": "Outbreak surge",
    "critical": "Critical shortage",
    "expiry": "Expiry crisis",
    "redistribution": "Redistribution opportunity",
    "kmch_to_psg": "KMCH to PSG transfer demo",
}
scenario_key = "outbreak"
action_status: dict[str, str] = {}
_cached_data_id: int | None = None
_cached_analysis: dict[str, Any] | None = None
_analysis_lock = threading.RLock()


class LoginRequest(BaseModel):
    hospital_id: str
    password: str


class AssistantQuery(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


class ScenarioChange(BaseModel):
    scenario: str


class ActionResult(BaseModel):
    status: str
    recommendation_id: str


class ForecastRun(BaseModel):
    hospital_id: str = "H003"
    supply_id: str = "MED001"
    horizon_days: int = Field(default=14, ge=7, le=60)


class SurgeryPayload(BaseModel):
    scheduled_date: date
    surgery_type: str
    number_of_cases: int = Field(ge=0, le=1000)
    expected_duration_minutes: int = Field(default=120, ge=1, le=1440)
    estimated_supply_requirements: dict[str, float] | None = None


class ShareablePoolPayload(BaseModel):
    supply_id: str
    shareable_quantity: int
    enabled: bool = True
    valid_until: date | None = None


class SupplyRequestPayload(BaseModel):
    supply_id: str
    requested_quantity: int = Field(ge=1, le=1_000_000)
    urgency_level: str = Field(default="NORMAL")
    delivery_deadline: date | None = None


class FulfillmentFailurePayload(BaseModel):
    reason: str = Field(default="", max_length=500)


class FulfillmentStatusPayload(BaseModel):
    status: str


class ForecastSimulationPayload(BaseModel):
    surgery: SurgeryPayload
    supply_id: str = "MED001"


class TransferSimulationPayload(BaseModel):
    source_hospital_id: str
    supply_id: str
    sharing_enabled: bool = True
    shareable_quantity: int = Field(default=0, ge=0)
    eta_increase_minutes: int = Field(default=0, ge=0, le=10080)


def envelope(data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": data, "meta": {"source": database.DATA_SOURCE, **meta}}


def get_analysis() -> dict[str, Any]:
    global _cached_data_id, _cached_analysis
    with _analysis_lock:
        version = int(database.ACTIVE_DATA.get("_version", 0))
        if _cached_data_id != id(database.ACTIVE_DATA) or _cached_analysis is None or _cached_analysis.get("_version") != version:
            _cached_analysis = analyze(database.ACTIVE_DATA)
            _cached_data_id = id(database.ACTIVE_DATA)
            _cached_analysis["_version"] = version
        return _cached_analysis


def refresh_analysis() -> dict[str, Any]:
    global _cached_data_id, _cached_analysis
    with _analysis_lock:
        _cached_analysis = analyze(database.ACTIVE_DATA)
        _cached_data_id = id(database.ACTIVE_DATA)
        _cached_analysis["_version"] = int(database.ACTIVE_DATA.get("_version", 0))
        database.persist_analysis(_cached_analysis)
        return _cached_analysis


def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    try:
        return user_from_token(authorization)
    except ValueError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error


def scoped_analysis(user: dict[str, Any]) -> dict[str, Any]:
    analysis = get_analysis()
    hospital_id = user["hospital_id"]
    if user.get("role") == "network_admin":
        return analysis

    local_ids = {
        hospital_key
        for hospital_key, hospital in analysis["hospitals"].items()
        if hospital.get("city") == analysis["hospitals"][hospital_id].get("city")
    }

    scoped = dict(analysis)
    scoped["forecasts"] = [row for row in analysis["forecasts"] if row["hospital_id"] == hospital_id]
    scoped["shortages"] = [row for row in analysis["shortages"] if row["hospital_id"] == hospital_id]
    scoped["expiry_risks"] = [row for row in analysis["expiry_risks"] if row["hospital_id"] == hospital_id]
    scoped["priorities"] = [row for row in analysis["priorities"] if row["hospital_id"] == hospital_id]
    enabled_sources = {row["supply_id"] for row in database.ACTIVE_DATA.get("shareable_pool", []) if row["hospital_id"] == hospital_id and row.get("enabled")}
    scoped["transfers"] = [
        row for row in analysis["transfers"]
        if row["destination_hospital_id"] == hospital_id
        or (row["source_hospital_id"] == hospital_id and row["supply_id"] in enabled_sources)
    ]
    scoped["inventory_totals"] = {key: value for key, value in analysis["inventory_totals"].items() if key[0] == hospital_id}
    scoped["safety_totals"] = {key: value for key, value in analysis["safety_totals"].items() if key[0] == hospital_id}
    scoped["hospitals"] = {key: value for key, value in analysis["hospitals"].items() if key in local_ids}
    scoped["history_points"] = {key: value for key, value in analysis["history_points"].items() if key[0] == hospital_id}
    scoped["batches"] = {key: value for key, value in analysis["batches"].items() if key[0] == hospital_id}
    return scoped


def _risk_for(analysis: dict[str, Any], hospital_id: str, supply_id: str) -> str:
    row = next((item for item in analysis["forecasts"] if item["hospital_id"] == hospital_id and item["supply_id"] == supply_id), None)
    return row["risk_level"] if row else "LOW"


def _hospital_summary(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    critical: Counter[str] = Counter()
    expiry: Counter[str] = Counter()
    for item in analysis["shortages"]:
        counts[item["hospital_id"]] += 1
        if item["risk_level"] == "CRITICAL":
            critical[item["hospital_id"]] += 1
    for item in analysis["expiry_risks"]:
        if item["risk_level"] in ("HIGH", "CRITICAL"):
            expiry[item["hospital_id"]] += 1
    rows = []
    for hospital_id, hospital in analysis["hospitals"].items():
        score = max(0, 100 - counts[hospital_id] * 7 - expiry[hospital_id] * 3 - round(hospital["emergency_load"] * 8))
        rows.append({**hospital, "shortage_count": counts[hospital_id], "critical_shortages": critical[hospital_id], "expiry_risk_count": expiry[hospital_id], "supply_health_score": score})
    return rows


def _inventory_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    forecasts = {(item["hospital_id"], item["supply_id"]): item for item in analysis["forecasts"]}
    expiry = defaultdict(list)
    for item in analysis["expiry_risks"]:
        expiry[(item["hospital_id"], item["supply_id"])].append(item)
    result = []
    batches = [batch for batch_list in analysis["batches"].values() for batch in batch_list]
    for batch in batches:
        hospital = analysis["hospitals"][batch["hospital_id"]]
        supply = analysis["supplies"][batch["supply_id"]]
        forecast = forecasts[(batch["hospital_id"], batch["supply_id"])]
        matching_expiry = next((item for item in expiry[(batch["hospital_id"], batch["supply_id"])] if item["batch_id"] == batch["batch_id"]), None)
        result.append({
            **batch, "hospital": hospital["name"], "city": hospital["city"],
            "supply": supply["name"], "category": supply["category"],
            "daily_demand": forecast["forecast_daily_demand"], "risk_level": forecast["risk_level"],
            "days_until_expiry": max(0, (date.fromisoformat(batch["expiry_date"]) - date.today()).days),
            "expiry_risk": matching_expiry["risk_level"] if matching_expiry else "LOW",
        })
    return result


def get_shortage_risks() -> list[dict[str, Any]]:
    return get_analysis()["shortages"]


def get_expiry_risks() -> list[dict[str, Any]]:
    return get_analysis()["expiry_risks"]


def get_inventory_summary() -> dict[str, Any]:
    analysis = get_analysis()
    return {"total_units": sum(analysis["inventory_totals"].values()), "hospital_count": len(analysis["hospitals"]), "supply_count": len(analysis["supplies"])}


def get_redistribution_recommendations() -> list[dict[str, Any]]:
    return get_analysis()["transfers"]


def get_priority_supplies() -> list[dict[str, Any]]:
    return get_analysis()["priorities"]


def get_hospital_summary() -> list[dict[str, Any]]:
    return _hospital_summary(get_analysis())


def _scenario_response() -> dict[str, Any]:
    return {"key": scenario_key, "label": SCENARIOS[scenario_key], "options": [{"key": key, "label": label} for key, label in SCENARIOS.items()]}


def _surgery_requirements(surgery: dict[str, Any]) -> dict[str, float]:
    if surgery.get("estimated_supply_requirements"):
        return {key: round(float(value) * int(surgery["number_of_cases"]), 1) for key, value in surgery["estimated_supply_requirements"].items()}
    return {
        supply_id: round(float(per_case) * int(surgery["number_of_cases"]), 1)
        for supply_id, per_case in SURGERY_TYPES.get(surgery["surgery_type"], {}).items()
    }


def _forecast_series(forecast_row: dict[str, Any], hospital_id: str, supply_id: str, horizon_days: int) -> list[dict[str, Any]]:
    scheduled_by_day: dict[int, float] = defaultdict(float)
    for surgery in database.ACTIVE_DATA.get("surgery_schedules", []):
        if surgery["hospital_id"] != hospital_id or surgery.get("status") != "scheduled":
            continue
        day = (date.fromisoformat(surgery["scheduled_date"]) - date.today()).days + 1
        if 1 <= day <= horizon_days:
            scheduled_by_day[day] += _surgery_requirements(surgery).get(supply_id, 0.0)
    baseline = max(0.1, forecast_row["forecast_daily_demand"] - forecast_row["surgery_additional_demand"])
    trend = forecast_row["trend_percent"] / 100
    points = []
    for day in range(1, horizon_days + 1):
        daily_base = baseline * (1 + max(-0.01, trend * 0.025) * day)
        daily_surgery = scheduled_by_day.get(day, 0.0)
        points.append({
            "day": day,
            "demand": round(max(0.1, daily_base + daily_surgery), 1),
            "surgery_demand": round(daily_surgery, 1),
        })
    return points


def _route_for_ids(source_hospital_id: str, destination_hospital_id: str) -> dict[str, Any]:
    analysis = get_analysis()
    source = analysis["hospitals"].get(source_hospital_id)
    destination = analysis["hospitals"].get(destination_hospital_id)
    if not source or not destination:
        raise HTTPException(status_code=404, detail="Hospital not found")
    if source.get("city") != destination.get("city"):
        raise HTTPException(status_code=403, detail="Road routing is limited to the local hospital network")
    try:
        return road_route(source, destination)
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _public_transfer(item: dict[str, Any], user_hospital_id: str) -> dict[str, Any]:
    route = None
    route_error = None
    try:
        route = _route_for_ids(item["source_hospital_id"], item["destination_hospital_id"])
    except HTTPException as error:
        route_error = str(error.detail)
    deadline_minutes = int(item["days_until_stockout"] * 24 * 60) if "days_until_stockout" in item else None
    route_in_time = bool(route and (deadline_minutes is None or route["duration_minutes"] < deadline_minutes))
    analysis = get_analysis()
    source_forecast = next((row for row in analysis["forecasts"]
                            if row["hospital_id"] == item["source_hospital_id"] and row["supply_id"] == item["supply_id"]), None)
    pool = next((row for row in database.ACTIVE_DATA.get("shareable_pool", [])
                 if row["hospital_id"] == item["source_hospital_id"] and row["supply_id"] == item["supply_id"]), None)
    committed = int(pool.get("committed_quantity", 0)) if pool else 0
    if source_forecast and pool:
        reserve = source_forecast["safety_stock"] + source_forecast["forecast_daily_demand"] * min(7, max(2, source_forecast["supplier_lead_time"]))
        safe_remaining = max(0, round(source_forecast["current_stock"] - reserve - committed))
        pool_remaining = max(0, int(pool["shareable_quantity"]) - committed)
        shareable_remaining = min(safe_remaining, pool_remaining)
    else:
        shareable_remaining = 0
    quantity_remaining = min(int(item["recommended_quantity"]), shareable_remaining)
    source_hospital = analysis["hospitals"].get(item["source_hospital_id"], {})
    destination_hospital = analysis["hospitals"].get(item["destination_hospital_id"], {})
    return {
        "recommendation_id": item["recommendation_id"],
        "source_hospital_id": item["source_hospital_id"], "source_hospital": source_hospital.get("display_name") or source_hospital.get("name") or "Source unavailable",
        "destination_hospital_id": item["destination_hospital_id"], "destination_hospital": destination_hospital.get("display_name") or destination_hospital.get("name") or "Destination unavailable",
        "supply_id": item["supply_id"], "supply": item["supply"],
        "recommended_quantity": quantity_remaining,
        "shareable_quantity": shareable_remaining,
        "priority_score": item["priority_score"], "priority": item["priority"],
        "road_distance_km": route["distance_km"] if route else None,
        "estimated_transport_minutes": route["duration_minutes"] if route else None,
        "route_available": bool(route), "transfer_feasible": route_in_time and quantity_remaining > 0,
        "feasibility_reason": "Shareable quantity, destination need, safety reserve, FEFO, and road ETA pass current checks." if route_in_time else route_error or "Road ETA does not meet the predicted safety-stock deadline.",
        "reason": "This recommendation uses only the source hospital's enabled shareable pool. Internal stock and safety-reserve values are not disclosed.",
        "destination_expected_coverage_days": item["destination_expected_coverage_days"],
        "status": action_status.get(item["recommendation_id"], item["status"]),
    }


@app.post("/api/auth/login")
def auth_login(payload: LoginRequest) -> dict[str, Any]:
    result = login_user(payload.hospital_id, payload.password)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid hospital ID or demo password")
    return envelope(result)


@app.get("/api/auth/me")
def auth_me(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return envelope(user)


@app.on_event("startup")
def startup() -> None:
    global _cached_data_id, _cached_analysis
    database.initialize_database()
    _cached_analysis = analyze(database.ACTIVE_DATA)
    _cached_data_id = id(database.ACTIVE_DATA)
    _cached_analysis["_version"] = int(database.ACTIVE_DATA.get("_version", 0))
    database.persist_analysis(_cached_analysis)


def _shareable_capacity(analysis: dict[str, Any], hospital_id: str, supply_id: str,
                        source: dict[str, Any] | None) -> dict[str, int]:
    forecast = next((row for row in analysis["forecasts"]
                     if row["hospital_id"] == hospital_id and row["supply_id"] == supply_id), None)
    if not forecast:
        return {"safety_reserve": 0, "committed_quantity": 0, "available_source_surplus": 0,
                "eligible_inventory_quantity": 0, "maximum_allowed_shareable": 0}
    committed = int(source.get("committed_quantity", 0)) if source else 0
    reserve = round(forecast["safety_stock"] + forecast["forecast_daily_demand"]
                    * min(7, max(2, forecast["supplier_lead_time"])))
    available_surplus = max(0, round(forecast["current_stock"] - reserve - committed))
    eligible_batches = [batch for batch in analysis["batches"].get((hospital_id, supply_id), [])
                        if batch.get("batch_status") != "expired"
                        and date.fromisoformat(batch["expiry_date"]) >= date.today()]
    eligible_quantity = sum(max(0, int(batch["quantity"]) - int(batch.get("committed_quantity", 0)))
                            for batch in eligible_batches)
    return {
        "safety_reserve": reserve,
        "committed_quantity": committed,
        "available_source_surplus": available_surplus,
        "eligible_inventory_quantity": eligible_quantity,
        "maximum_allowed_shareable": min(available_surplus, eligible_quantity),
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    return envelope({"status": "operational", "database": "connected" if database.MONGO_DB is not None else "demo mode", "demo_mode": database.DEMO_MODE, "disclaimer": "Decision support prototype; not medical advice."})


@app.get("/api/demo/scenario")
def current_scenario() -> dict[str, Any]:
    return envelope(_scenario_response())


@app.post("/api/demo/scenario")
def change_scenario(payload: ScenarioChange) -> dict[str, Any]:
    global scenario_key, _cached_data_id, _cached_analysis
    key = payload.scenario.lower()
    if key not in SCENARIOS:
        raise HTTPException(status_code=422, detail={"message": "Unknown scenario", "valid_scenarios": list(SCENARIOS)})
    scenario_key = key
    database.set_scenario(key)
    _cached_analysis = analyze(database.ACTIVE_DATA)
    _cached_data_id = id(database.ACTIVE_DATA)
    _cached_analysis["_version"] = int(database.ACTIVE_DATA.get("_version", 0))
    database.persist_analysis(_cached_analysis)
    action_status.clear()
    return envelope(_scenario_response())


@app.post("/api/demo/multi-source-fulfillment")
def run_multi_source_fulfillment_demo(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    global scenario_key, _cached_data_id, _cached_analysis
    if user.get("role") != "network_admin" and user["hospital_id"] != "H002":
        raise HTTPException(status_code=403, detail="The multi-source demonstration request must be created by PSG Hospitals")
    active_requests = [row for row in database.ACTIVE_DATA.get("supply_requests", [])
                       if row.get("status") not in ("FULFILLED", "CANCELLED")]
    if active_requests:
        raise HTTPException(status_code=409, detail="Complete or cancel active supply requests before resetting the synthetic demonstration scenario")
    scenario_key = "kmch_to_psg"
    database.set_scenario(scenario_key)
    kumaran_pool = next((row for row in database.ACTIVE_DATA["shareable_pool"]
                         if row["hospital_id"] == "H003" and row["supply_id"] == "MED001"), None)
    if kumaran_pool is None:
        raise HTTPException(status_code=500, detail="The synthetic demonstration source pool is unavailable")
    kumaran_pool.update({"shareable_quantity": 250, "enabled": True, "committed_quantity": 0})
    _cached_analysis = analyze(database.ACTIVE_DATA)
    _cached_data_id = id(database.ACTIVE_DATA)
    _cached_analysis["_version"] = int(database.ACTIVE_DATA.get("_version", 0))
    database.persist_analysis(_cached_analysis)
    request = fulfillment.create_request("H002", "MED001", 600)
    first_offer = next((leg for leg in request["legs"] if leg["status"] == "OFFERED"), None)
    return envelope({
        "request": request,
        "scenario": _scenario_response(),
        "prototype_notice": "Synthetic dispatch methodology only. No courier service or live tracking is connected.",
        "demo_ready": bool(first_offer and first_offer["source_hospital_id"] == "H001" and first_offer["allocated_quantity"] == 400),
    })


@app.get("/api/surgery-types")
def surgery_types(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    supplies = get_analysis()["supplies"]
    rows = [{
        "surgery_type": surgery_type,
        "supplies": [{"supply_id": supply_id, "supply": supplies[supply_id]["name"], "units_per_case": units}
                     for supply_id, units in requirements.items() if supply_id in supplies],
    } for surgery_type, requirements in SURGERY_TYPES.items()]
    return envelope(rows)


@app.get("/api/surgeries")
def surgeries(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    hospital_id = user["hospital_id"]
    supplies = get_analysis()["supplies"]
    rows = []
    for surgery in database.ACTIVE_DATA.get("surgery_schedules", []):
        if surgery["hospital_id"] != hospital_id:
            continue
        impact = _surgery_requirements(surgery)
        rows.append({**surgery, "estimated_supply_requirements": impact,
                     "supply_impact": [{"supply_id": key, "supply": supplies[key]["name"], "quantity": value}
                                       for key, value in impact.items() if key in supplies]})
    return envelope(sorted(rows, key=lambda row: (row["scheduled_date"], row["surgery_id"])), count=len(rows))


def _save_surgery(payload: SurgeryPayload, hospital_id: str, surgery_id: str) -> dict[str, Any]:
    if payload.scheduled_date < date.today():
        raise HTTPException(status_code=422, detail="Scheduled date must be today or later")
    if payload.surgery_type not in SURGERY_TYPES:
        raise HTTPException(status_code=422, detail={"message": "Unknown surgery type", "valid_types": list(SURGERY_TYPES)})
    supply_ids = set(get_analysis()["supplies"])
    if payload.estimated_supply_requirements and any(key not in supply_ids or value < 0 for key, value in payload.estimated_supply_requirements.items()):
        raise HTTPException(status_code=422, detail="Custom per-case supply requirements must use known supplies and non-negative values")
    record = {"surgery_id": surgery_id, "hospital_id": hospital_id, **payload.model_dump(mode="json"), "status": "scheduled"}
    database.save_source_record("surgery_schedules", record, "surgery_id")
    refresh_analysis()
    return record


@app.post("/api/surgeries")
def create_surgery(payload: SurgeryPayload, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return envelope(_save_surgery(payload, user["hospital_id"], f"SURG-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{len(database.ACTIVE_DATA.get('surgery_schedules', [])) + 1}"))


@app.put("/api/surgeries/{surgery_id}")
def update_surgery(surgery_id: str, payload: SurgeryPayload, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    existing = next((row for row in database.ACTIVE_DATA.get("surgery_schedules", []) if row["surgery_id"] == surgery_id), None)
    if not existing or existing["hospital_id"] != user["hospital_id"]:
        raise HTTPException(status_code=404, detail="Surgery schedule not found")
    return envelope(_save_surgery(payload, user["hospital_id"], surgery_id))


@app.delete("/api/surgeries/{surgery_id}")
def cancel_surgery(surgery_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    existing = next((row for row in database.ACTIVE_DATA.get("surgery_schedules", []) if row["surgery_id"] == surgery_id), None)
    if not existing or existing["hospital_id"] != user["hospital_id"]:
        raise HTTPException(status_code=404, detail="Surgery schedule not found")
    existing["status"] = "cancelled"
    database.save_source_record("surgery_schedules", existing, "surgery_id")
    refresh_analysis()
    return envelope(existing)


@app.get("/api/shareable-pool")
def get_shareable_pool(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    hospital_id = user["hospital_id"]
    analysis = get_analysis()
    forecasts = {(row["hospital_id"], row["supply_id"]): row for row in analysis["forecasts"]}
    supplies = analysis["supplies"]
    rows = []
    for (owner, supply_id), forecast_row in forecasts.items():
        if owner != hospital_id:
            continue
        source = next((row for row in database.ACTIVE_DATA.get("shareable_pool", []) if row["hospital_id"] == owner and row["supply_id"] == supply_id), None)
        capacity = _shareable_capacity(analysis, owner, supply_id, source)
        committed = capacity["committed_quantity"]
        configured_quantity = int(source["shareable_quantity"]) if source else 0
        available_shareable = min(max(0, configured_quantity - committed), capacity["maximum_allowed_shareable"])
        rows.append({
            "pool_id": source["pool_id"] if source else f"POOL-{owner}-{supply_id}",
            "hospital_id": owner, "supply_id": supply_id, "supply": supplies[supply_id]["name"],
            "total_stock": forecast_row["current_stock"], "safety_reserve": capacity["safety_reserve"],
            "source_surplus": capacity["available_source_surplus"],
            "eligible_inventory_quantity": capacity["eligible_inventory_quantity"],
            "maximum_allowed_shareable": capacity["maximum_allowed_shareable"],
            "shareable_quantity": available_shareable, "configured_shareable_quantity": configured_quantity,
            "committed_quantity": committed, "available_shareable_quantity": available_shareable,
            "enabled": bool(source and source.get("enabled")), "valid_until": source.get("valid_until") if source else None,
        })
    return envelope(rows, count=len(rows), privacy="Only the authenticated hospital receives its own stock and reserve details")


@app.post("/api/shareable-pool")
def save_shareable_pool(payload: ShareablePoolPayload, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    analysis = get_analysis()
    if payload.supply_id not in analysis["supplies"]:
        raise HTTPException(status_code=404, detail="Supply not found")
    forecast_row = next((row for row in analysis["forecasts"] if row["hospital_id"] == user["hospital_id"] and row["supply_id"] == payload.supply_id), None)
    if not forecast_row:
        raise HTTPException(status_code=404, detail="Supply inventory not found at this hospital")
    hospital_id = user["hospital_id"]
    existing = next((row for row in database.ACTIVE_DATA.get("shareable_pool", [])
                     if row["hospital_id"] == hospital_id and row["supply_id"] == payload.supply_id), None)
    capacity = _shareable_capacity(analysis, hospital_id, payload.supply_id, existing)
    if payload.shareable_quantity < 0:
        raise HTTPException(status_code=422, detail="Shareable quantity cannot be negative.")
    if payload.enabled and capacity["maximum_allowed_shareable"] == 0:
        raise HTTPException(status_code=422, detail="No safe surplus is currently available to share.")
    if payload.enabled and payload.shareable_quantity > capacity["maximum_allowed_shareable"]:
        raise HTTPException(status_code=422, detail=f"Maximum shareable quantity is {capacity['maximum_allowed_shareable']:,} units.")
    committed = capacity["committed_quantity"]
    configured_quantity = committed + (payload.shareable_quantity if payload.enabled else 0)
    record = {
        "pool_id": f"POOL-{hospital_id}-{payload.supply_id}", "hospital_id": hospital_id,
        **payload.model_dump(mode="json"), "shareable_quantity": configured_quantity,
        "committed_quantity": committed,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    database.save_source_record("shareable_pool", record, "pool_id")
    refresh_analysis()
    return envelope(record)


@app.get("/api/routes")
def get_road_route(source_hospital_id: str, destination_hospital_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return envelope(_route_for_ids(source_hospital_id, destination_hospital_id))


@app.get("/api/nearby-supplies")
def nearby_supplies(supply_id: str | None = None, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    analysis = get_analysis()
    destination_id = user["hospital_id"]
    destination = analysis["hospitals"][destination_id]
    destination_forecasts = {row["supply_id"]: row for row in analysis["forecasts"] if row["hospital_id"] == destination_id}
    forecasts = {(row["hospital_id"], row["supply_id"]): row for row in analysis["forecasts"]}
    now = date.today()
    options = []
    for pool in database.ACTIVE_DATA.get("shareable_pool", []):
        if not pool.get("enabled") or pool["hospital_id"] == destination_id or (supply_id and pool["supply_id"] != supply_id):
            continue
        source = analysis["hospitals"].get(pool["hospital_id"])
        source_forecast = forecasts.get((pool["hospital_id"], pool["supply_id"]))
        target = destination_forecasts.get(pool["supply_id"])
        if not source or not source_forecast or not target or source.get("city") != destination.get("city"):
            continue
        if pool.get("valid_until") and date.fromisoformat(pool["valid_until"]) < now:
            continue
        reserve = source_forecast["safety_stock"] + source_forecast["forecast_daily_demand"] * min(7, max(2, source_forecast["supplier_lead_time"]))
        surplus = max(0, round(source_forecast["current_stock"] - reserve))
        destination_need = max(0, target["safety_stock"] + target["forecast_daily_demand"] * min(14, max(1, target["supplier_lead_time"])) - target["current_stock"])
        committed = int(pool.get("committed_quantity", 0))
        shareable = min(max(0, int(pool["shareable_quantity"]) - committed), max(0, surplus - committed))
        if shareable <= 0 or destination_need <= 0:
            continue
        try:
            route = road_route(source, destination)
            eta_ok = route["duration_minutes"] < max(1, target["days_until_stockout"]) * 1440
            arrival_date = now + timedelta(days=max(1, (route["duration_minutes"] + 1439) // 1440))
            eligible_batches = [batch for batch in analysis["batches"][(pool["hospital_id"], pool["supply_id"])]
                                if batch.get("batch_status") != "expired" and date.fromisoformat(batch["expiry_date"]) >= arrival_date]
            quantity = min(shareable, destination_need, sum(batch["quantity"] for batch in eligible_batches))
            need_window_ok = target["days_until_stockout"] <= 14
            quantity_ok = quantity >= max(1, round(target["forecast_daily_demand"] * 0.1))
            feasible = eta_ok and need_window_ok and quantity_ok
            route_error = None
        except ValueError as error:
            route, eligible_batches, quantity, feasible, route_error = None, [], 0, False, str(error)
            eta_ok, need_window_ok, quantity_ok = False, False, False
        reasons = []
        if not eta_ok:
            reasons.append(route_error or "Road ETA is after the predicted safety-stock deadline")
        if route and not need_window_ok:
            reasons.append("Predicted safety-stock breach is outside the 14-day transfer window")
        if not eligible_batches:
            reasons.append("No unexpired source batch remains for the transfer")
        if route and eligible_batches and not quantity_ok:
            reasons.append("Available quantity is below the minimum useful transfer amount")
        recommendation = next((row for row in analysis["transfers"]
                               if row["source_hospital_id"] == pool["hospital_id"]
                               and row["destination_hospital_id"] == destination_id
                               and row["supply_id"] == pool["supply_id"]), None)
        if feasible and not recommendation:
            feasible = False
            reasons.append("No transfer recommendation passes all source reserve and destination need checks")
        options.append({
            "hospital_id": source["hospital_id"], "hospital_name": source["display_name"],
            "latitude": source["latitude"], "longitude": source["longitude"],
            "supply_id": pool["supply_id"], "supply": analysis["supplies"][pool["supply_id"]]["name"],
            "shareable_quantity": shareable, "recommended_quantity": quantity,
            "destination_need": destination_need,
            "recommendation_id": recommendation["recommendation_id"] if recommendation and feasible else None,
            "road_distance_km": route["distance_km"] if route else None,
            "estimated_travel_minutes": route["duration_minutes"] if route else None,
            "route_available": bool(route), "feasible": feasible,
            "status": "FEASIBLE" if feasible else "NOT FEASIBLE",
            "reason": "Opt-in supply, local need, reserve, expiry, and road ETA checks pass." if feasible else "; ".join(reasons),
        })
    options.sort(key=lambda row: (not row["feasible"], row["estimated_travel_minutes"] or 99999, row["road_distance_km"] or 99999))
    return envelope(options, count=len(options), privacy="Only explicitly enabled shareable quantities are included")


@app.post("/api/nearby-supplies/simulate")
def simulate_nearby_supply(payload: TransferSimulationPayload, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    analysis = get_analysis()
    destination_id = user["hospital_id"]
    source = analysis["hospitals"].get(payload.source_hospital_id)
    destination = analysis["hospitals"].get(destination_id)
    supply = analysis["supplies"].get(payload.supply_id)
    source_forecast = next((row for row in analysis["forecasts"] if row["hospital_id"] == payload.source_hospital_id and row["supply_id"] == payload.supply_id), None)
    destination_forecast = next((row for row in analysis["forecasts"] if row["hospital_id"] == destination_id and row["supply_id"] == payload.supply_id), None)
    if not source or not destination or not supply or not source_forecast or not destination_forecast:
        raise HTTPException(status_code=404, detail="Local source, destination, or supply was not found")
    if payload.source_hospital_id == destination_id or source.get("city") != destination.get("city"):
        raise HTTPException(status_code=422, detail="Choose a different facility in the local hospital network")

    source_lead = source_forecast["supplier_lead_time"]
    source_reserve = source_forecast["safety_stock"] + source_forecast["forecast_daily_demand"] * min(7, max(2, source_lead))
    source_surplus = max(0, round(source_forecast["current_stock"] - source_reserve))
    result: dict[str, Any] = {
        "hospital_id": source["hospital_id"], "hospital_name": source["display_name"],
        "supply_id": payload.supply_id, "supply": supply["name"],
        "sharing_enabled": payload.sharing_enabled,
        "shareable_quantity": min(payload.shareable_quantity, source_surplus) if payload.sharing_enabled else 0,
        "eta_increase_minutes": payload.eta_increase_minutes,
        "route_available": False, "road_distance_km": None, "estimated_travel_minutes": None,
        "recommended_quantity": 0, "feasible": False, "status": "NOT FEASIBLE",
    }
    reasons = []
    if not payload.sharing_enabled or payload.shareable_quantity <= 0:
        reasons.append("Source sharing is disabled or the simulated pool is zero")
    try:
        route = road_route(source, destination)
        travel_minutes = route["duration_minutes"] + payload.eta_increase_minutes
        result.update({
            "route_available": True, "road_distance_km": route["distance_km"],
            "estimated_travel_minutes": travel_minutes,
        })
        shareable = min(payload.shareable_quantity, source_surplus) if payload.sharing_enabled else 0
        target_need = max(0, round(
            destination_forecast["safety_stock"]
            + destination_forecast["forecast_daily_demand"] * max(1, destination_forecast["supplier_lead_time"] - travel_minutes / 1440)
            - destination_forecast["current_stock"]
        ))
        arrival_date = date.today() + timedelta(days=max(1, (travel_minutes + 1439) // 1440))
        eligible_batches = [batch for batch in analysis["batches"][(payload.source_hospital_id, payload.supply_id)]
                            if batch.get("batch_status") != "expired" and date.fromisoformat(batch["expiry_date"]) >= arrival_date]
        transfer_cap = int(supply.get("max_transfer_quantity", target_need))
        quantity = min(shareable, target_need, sum(batch["quantity"] for batch in eligible_batches), transfer_cap)
        deadline_minutes = destination_forecast["days_until_stockout"] * 1440
        enough_quantity = quantity >= max(1, round(destination_forecast["forecast_daily_demand"] * 0.1))
        need_window = destination_forecast["days_until_stockout"] <= 14
        eta_in_time = travel_minutes < deadline_minutes
        result["recommended_quantity"] = quantity
        result["feasible"] = bool(payload.sharing_enabled and enough_quantity and need_window and eta_in_time and eligible_batches)
        result["status"] = "FEASIBLE" if result["feasible"] else "NOT FEASIBLE"
        if not payload.sharing_enabled or shareable <= 0:
            reasons.append("No enabled shareable quantity remains after the source reserve")
        if not target_need:
            reasons.append("No predicted destination need through supplier lead time")
        if not eligible_batches:
            reasons.append("No unexpired source batch remains valid through arrival")
        if not enough_quantity:
            reasons.append("Transfer quantity is below the minimum useful amount")
        if not need_window:
            reasons.append("Destination safety-stock breach is outside the 14-day transfer window")
        if not eta_in_time:
            reasons.append("Simulated ETA is after the predicted safety-stock breach")
    except ValueError as error:
        reasons.append(str(error))
    result["reason"] = "Simulated pool, demand, FEFO, source reserve, and road ETA pass." if result["feasible"] else "; ".join(dict.fromkeys(reasons))
    return envelope(result, simulation=True, persisted=False, privacy="No source inventory or reserve values are returned")


@app.post("/api/redistribution/{recommendation_id}/request")
def request_transfer(recommendation_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    item = next((row for row in get_analysis()["transfers"]
                 if row["recommendation_id"] == recommendation_id
                 and row["destination_hospital_id"] == user["hospital_id"]), None)
    if not item:
        raise HTTPException(status_code=404, detail="Transfer recommendation not found")
    safe = _public_transfer(item, user["hospital_id"])
    if not safe["transfer_feasible"]:
        raise HTTPException(status_code=422, detail="Transfer request requires an available route and on-time ETA")
    try:
        request = fulfillment.create_request(user["hospital_id"], item["supply_id"], item["destination_need"], auto_match=False)
        threading.Thread(target=fulfillment.match_request_in_background, args=(request["request_id"],), daemon=True).start()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    action_status[recommendation_id] = "requested"
    first_leg = next(iter(request["legs"]), None)
    return envelope({
        **ActionResult(status="requested", recommendation_id=recommendation_id).model_dump(),
        "request_id": request["request_id"],
        "requested_quantity": request["requested_quantity"],
        "initial_offer_quantity": first_leg["allocated_quantity"] if first_leg else 0,
    })


@app.get("/api/supply-requests")
def list_supply_requests(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    rows = fulfillment.list_visible_requests(user["hospital_id"])
    return envelope(rows, count=len(rows), workflow="Dynamic Multi-Source Fulfilment")


@app.post("/api/supply-requests")
def create_supply_request(payload: SupplyRequestPayload, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    try:
        request = fulfillment.create_request(
            user["hospital_id"],
            payload.supply_id,
            payload.requested_quantity,
            urgency_level=payload.urgency_level,
            delivery_deadline=payload.delivery_deadline,
            auto_match=False,
        )
        threading.Thread(
            target=fulfillment.match_request_in_background,
            args=(request["request_id"],),
            daemon=True,
        ).start()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return envelope(request)


@app.delete("/api/supply-requests/{request_id}")
def cancel_supply_request(request_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    try:
        request = fulfillment.cancel_request(request_id, user["hospital_id"])
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return envelope(request)


@app.post("/api/supply-requests/{request_id}/rematch")
def rematch_supply_request(request_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    try:
        request = fulfillment.rematch_request(request_id, user["hospital_id"])
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return envelope(request)


@app.post("/api/supply-requests/{request_id}/legs/{leg_id}/accept")
def accept_fulfillment_leg(request_id: str, leg_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    try:
        request = fulfillment.accept_leg(request_id, leg_id, user["hospital_id"])
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return envelope(request)


@app.post("/api/supply-requests/{request_id}/legs/{leg_id}/reject")
def reject_fulfillment_leg(request_id: str, leg_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    try:
        request = fulfillment.reject_leg(request_id, leg_id, user["hospital_id"])
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return envelope(request)


@app.post("/api/supply-requests/{request_id}/legs/{leg_id}/status")
def update_fulfillment_leg(request_id: str, leg_id: str, payload: FulfillmentStatusPayload, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    try:
        request = fulfillment.update_leg_status(request_id, leg_id, user["hospital_id"], payload.status.upper())
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if payload.status.upper() in ("PICKED_UP", "IN_TRANSIT", "ARRIVED", "DELIVERED"):
        refresh_analysis()
    return envelope(request)


@app.post("/api/supply-requests/{request_id}/legs/{leg_id}/fail")
def fail_fulfillment_leg(request_id: str, leg_id: str, payload: FulfillmentFailurePayload, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    try:
        request = fulfillment.fail_leg(request_id, leg_id, user["hospital_id"], payload.reason)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return envelope(request)


@app.get("/api/management-report/weekly")
def weekly_management_report(start_date: date | None = None, end_date: date | None = None, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    end = end_date or date.today() + timedelta(days=6)
    start = start_date or end - timedelta(days=6)
    if start > end:
        raise HTTPException(status_code=422, detail="Start date must not be after end date")
    hospital_id = user["hospital_id"]
    analysis = get_analysis()
    shortages = [row for row in analysis["shortages"] if row["hospital_id"] == hospital_id]
    expiries = [row for row in analysis["expiry_risks"] if row["hospital_id"] == hospital_id]
    schedules = [row for row in database.ACTIVE_DATA.get("surgery_schedules", [])
                 if row["hospital_id"] == hospital_id and row.get("status") == "scheduled"
                 and start <= date.fromisoformat(row["scheduled_date"]) <= end]
    transfers = [_public_transfer(row, hospital_id) for row in analysis["transfers"]
                 if row["destination_hospital_id"] == hospital_id or row["source_hospital_id"] == hospital_id]
    fulfillment_requests = [
        row for row in fulfillment.list_visible_requests(hospital_id)
        if start <= datetime.fromisoformat(row["created_at"]).astimezone().date() <= end
    ]
    matching_seconds = []
    eta_minutes = []
    for request in fulfillment_requests:
        offered_at = [datetime.fromisoformat(leg["created_at"]) for leg in request["legs"] if leg.get("created_at")]
        if offered_at:
            matching_seconds.append(max(0.0, (min(offered_at) - datetime.fromisoformat(request["created_at"])).total_seconds()))
        eta_minutes.extend(float(leg["estimated_eta_minutes"]) for leg in request["legs"] if leg.get("estimated_eta_minutes") is not None)
    fulfillment_metrics = {
        "requests": len(fulfillment_requests),
        "total_requested_units": sum(int(row["requested_quantity"]) for row in fulfillment_requests),
        "total_fulfilled_units": sum(int(row.get("fulfilled_quantity", 0)) for row in fulfillment_requests),
        "partially_fulfilled_requests": sum(row["status"] in ("PARTIALLY_FULFILLED", "PARTIALLY_DELIVERED") for row in fulfillment_requests),
        "fully_fulfilled_requests": sum(row["status"] == "FULFILLED" for row in fulfillment_requests),
        "average_matching_time_seconds": round(sum(matching_seconds) / len(matching_seconds), 1) if matching_seconds else None,
        "average_transfer_eta_minutes": round(sum(eta_minutes) / len(eta_minutes), 1) if eta_minutes else None,
        "rematches": sum(int(row.get("rematch_count", 0)) for row in fulfillment_requests),
        "rejected_offers": sum(int(row.get("rejected_offers_count", 0)) for row in fulfillment_requests),
        "failed_transfers": sum(int(row.get("failed_transfers_count", 0)) for row in fulfillment_requests),
        "multi_source_fulfillment_count": sum(len({leg["source_hospital_id"] for leg in row["legs"] if leg["status"] not in ("REJECTED", "EXPIRED", "CANCELLED")}) > 1 for row in fulfillment_requests),
    }
    forecast_index = {(row["hospital_id"], row["supply_id"]): row for row in analysis["forecasts"]}
    surgery_rows = []
    for surgery in schedules:
        impacts = _surgery_requirements(surgery)
        surgery_rows.append({
            **surgery,
            "supply_impact": [{
                "supply_id": supply_id, "supply": analysis["supplies"][supply_id]["name"],
                "additional_units": units,
                "baseline_forecast_daily_demand": forecast_index[(hospital_id, supply_id)]["baseline_forecast_daily_demand"],
                "adjusted_forecast_daily_demand": forecast_index[(hospital_id, supply_id)]["forecast_daily_demand"],
                "baseline_risk_level": forecast_index[(hospital_id, supply_id)]["baseline_risk_level"],
                "adjusted_risk_level": forecast_index[(hospital_id, supply_id)]["risk_level"],
            } for supply_id, units in impacts.items() if (hospital_id, supply_id) in forecast_index],
        })
    actions = []
    for row in shortages:
        actions.append({"priority": row["risk_level"], "action": f"Review {row['supply']} shortage at {row['hospital']}", "basis": f"Safety-stock breach projected in {row['days_until_stockout']} days"})
        if row["risk_level"] in ("CRITICAL", "HIGH") and not any(item["transfer_feasible"] and item["supply_id"] == row["supply_id"] for item in transfers):
            actions.append({"priority": row["risk_level"], "action": f"Contact supplier for {row['supply']}", "basis": f"No route-verified local transfer can meet the {row['days_until_stockout']}-day safety-stock deadline"})
        if row["risk_level"] == "CRITICAL":
            actions.append({"priority": "CRITICAL", "action": f"Escalate unresolved {row['supply']} shortage", "basis": f"Projected safety-stock breach in {row['days_until_stockout']} days"})
    for row in expiries:
        if row["expected_waste"]:
            actions.append({"priority": row["risk_level"], "action": f"Prioritize FEFO use for {row['supply']} batch {row['batch_id']}", "basis": f"{row['expected_waste']} units projected to remain at expiry"})
    for row in transfers:
        if row["transfer_feasible"]:
            actions.append({"priority": row["priority"], "action": f"Confirm receipt of {row['recommended_quantity']} {row['supply']} from {row['source_hospital']}", "basis": f"OSRM road ETA {row['estimated_transport_minutes']} minutes"})
    if schedules:
        actions.append({"priority": "REVIEW", "action": "Review surgery-linked supply demand", "basis": f"{sum(row['number_of_cases'] for row in schedules)} scheduled cases affect this report period"})
    return envelope({
        "period": {"start_date": start.isoformat(), "end_date": end.isoformat()},
        "executive_summary": {
            "hospitals_monitored": len(analysis["hospitals"]),
            "critical_shortages": sum(row["risk_level"] == "CRITICAL" for row in shortages),
            "high_risk_supplies": len({row["supply_id"] for row in shortages if row["risk_level"] in ("HIGH", "CRITICAL")}),
            "expiry_risks": len(expiries), "transfers_recommended": len(transfers),
            "transfers_completed": sum(row["status"] == "approved" for row in transfers),
            "upcoming_surgery_cases": sum(row["number_of_cases"] for row in schedules),
            "fulfillment": fulfillment_metrics,
        },
        "shortages": shortages, "surgeries": surgery_rows, "transfers": transfers,
        "expiry_risks": expiries, "fulfillment_requests": fulfillment_requests,
        "management_actions": actions,
        "disclaimer": "Prototype demonstration: hospital locations are based on publicly available information. Inventory, demand, surgery schedules and transfer data are simulated and do not represent live hospital operational data.",
    })


@app.get("/api/dashboard/summary")
def dashboard_summary(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    analysis = scoped_analysis(user)
    hospitals = _hospital_summary(analysis)
    critical = [item for item in analysis["shortages"] if item["risk_level"] == "CRITICAL"]
    risk_counts = Counter(item["risk_level"] for item in analysis["shortages"])
    expiry = sorted(analysis["expiry_risks"], key=lambda item: (item["days_until_expiry"], -item["expected_waste"]))
    target_hospital = user["hospital_id"]
    demand_values = analysis["history_points"].get((target_hospital, "MED001"), [])[-30:]
    demand_forecast = next((item for item in analysis["forecasts"] if item["hospital_id"] == target_hospital and item["supply_id"] == "MED001"), None)
    demand_chart = []
    for index, value in enumerate(demand_values):
        demand_chart.append({"day": (date.today() - timedelta(days=len(demand_values) - index - 1)).strftime("%d %b"), "historical": round(value, 1), "forecast": None})
    if demand_chart and demand_forecast:
        demand_chart[-1]["forecast"] = demand_chart[-1]["historical"]
        for point in _forecast_series(demand_forecast, target_hospital, "MED001", 7):
            demand_chart.append({"day": f"+{point['day']}d", "historical": None, "forecast": point["demand"]})
    hospital_risk = sorted(hospitals, key=lambda item: (item["critical_shortages"], item["shortage_count"]), reverse=True)[:8]
    upcoming = [row for row in database.ACTIVE_DATA.get("surgery_schedules", [])
                if row["hospital_id"] == target_hospital and row.get("status") == "scheduled"
                and date.today() <= date.fromisoformat(row["scheduled_date"]) <= date.today() + timedelta(days=6)]
    surgery_forecasts = [row for row in get_analysis()["forecasts"] if row["hospital_id"] == target_hospital and row["surgery_additional_units_next_7_days"] > 0]
    safe_transfers = [_public_transfer(item, target_hospital) for item in analysis["transfers"][:6]]
    response = {
        "kpis": {
            "hospitals": len(analysis["hospitals"]), "supplies": len(analysis["supplies"]),
            "critical_shortages": len(critical), "expiry_risks": len(analysis["expiry_risks"]),
            "recommended_transfers": len(analysis["transfers"]),
            "stock_at_risk": sum(item["expected_waste"] for item in analysis["expiry_risks"]),
            "current_inventory_units": sum(analysis["inventory_totals"].values()),
            "critical_supplies_count": len({item["supply_id"] for item in critical}),
            "forecast_alerts": len(analysis["shortages"]),
            "nearby_surplus_hospitals": len({item["source_hospital_id"] for item in analysis["transfers"]}),
        },
        "risk_counts": {level: risk_counts.get(level, 0) for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW")},
        "hospital_risk": hospital_risk,
        "critical_supplies": analysis["priorities"][:6],
        "demand_chart": demand_chart,
        "expiry_risks": expiry[:7],
        "transfers": safe_transfers,
        "upcoming_surgery_impact": {
            "scheduled_cases": sum(row["number_of_cases"] for row in upcoming),
            "affected_supplies": len(surgery_forecasts),
            "additional_units": round(sum(row["surgery_additional_units_next_7_days"] for row in surgery_forecasts), 1),
            "risk_changes": sum(row["baseline_risk_level"] != row["risk_level"] for row in surgery_forecasts),
        },
        "shareable_supply_units": sum(min(int(pool["shareable_quantity"]), next((row["shareable_quantity"] for row in get_analysis()["forecasts"] if row["hospital_id"] == pool["hospital_id"] and row["supply_id"] == pool["supply_id"]), 0)) for pool in database.ACTIVE_DATA.get("shareable_pool", []) if pool.get("enabled") and pool["hospital_id"] == target_hospital),
        "prototype_data_disclaimer": "Hospital locations are based on publicly available information. Inventory, demand, surgery schedules and transfer data are simulated.",
        "critical_hospitals": sorted(hospitals, key=lambda item: item["supply_health_score"])[:6],
        "scenario": _scenario_response(), "hospital": next(iter(analysis["hospitals"].values()), None),
        "local_radius_km": LOCAL_RADIUS_KM, "updated_at": date.today().isoformat(),
    }
    return envelope(response)


@app.get("/api/network")
def network_summary(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    analysis = get_analysis()
    hospital_id = user["hospital_id"]
    share_by_hospital: dict[str, int] = defaultdict(int)
    for pool in database.ACTIVE_DATA.get("shareable_pool", []):
        if pool.get("enabled"):
            forecast = next((row for row in analysis["forecasts"] if row["hospital_id"] == pool["hospital_id"] and row["supply_id"] == pool["supply_id"]), None)
            if forecast:
                available = max(0, int(pool["shareable_quantity"]) - int(pool.get("committed_quantity", 0)))
                share_by_hospital[pool["hospital_id"]] += min(available, forecast["shareable_quantity"])

    nodes = []
    for hospital_id, hospital in analysis["hospitals"].items():
        private_view = user.get("role") != "network_admin" and hospital_id != user["hospital_id"]
        own_forecasts = [row for row in analysis["forecasts"] if row["hospital_id"] == hospital_id]
        shortage_units = sum(max(0, round(row["safety_stock"] + row["forecast_daily_demand"] * row["supplier_lead_time"] - row["current_stock"])) for row in own_forecasts) if not private_view else 0
        critical_shortages = sum(row["risk_level"] == "CRITICAL" for row in own_forecasts) if not private_view else 0
        surplus_units = share_by_hospital[hospital_id]
        roles = []
        if surplus_units:
            roles.append("SURPLUS")
        if shortage_units:
            roles.append("SHORTAGE")
        if surplus_units:
            roles.append("SHAREABLE")
        nodes.append({
            "hospital_id": hospital_id, "name": hospital["name"], "display_name": hospital["display_name"],
            "address": hospital["address"], "hospital_type": hospital["hospital_type"],
            "demo_data_flag": hospital["demo_data_flag"], "city": hospital["city"],
            "latitude": hospital["latitude"], "longitude": hospital["longitude"],
            "surplus_units": surplus_units, "shortage_units": shortage_units,
            "expiry_units_at_risk": 0,
            "critical_shortages": critical_shortages, "roles": roles or ["BALANCED"],
        })
    edges = [{
        "recommendation_id": item["recommendation_id"],
        "source_hospital_id": item["source_hospital_id"], "source": analysis["hospitals"][item["source_hospital_id"]].get("display_name") or item["source_hospital"],
        "destination_hospital_id": item["destination_hospital_id"], "destination": analysis["hospitals"][item["destination_hospital_id"]].get("display_name") or item["destination_hospital"],
        "supply": item["supply"], "quantity": item["recommended_quantity"],
        "priority_score": item["priority_score"],
    } for item in analysis["transfers"] if user.get("role") == "network_admin" or item["destination_hospital_id"] == user["hospital_id"] or (item["source_hospital_id"] == user["hospital_id"] and any(pool["hospital_id"] == user["hospital_id"] and pool["supply_id"] == item["supply_id"] and pool.get("enabled") for pool in database.ACTIVE_DATA.get("shareable_pool", [])))][:20]
    for request in fulfillment.list_visible_requests(hospital_id):
        for leg in request["legs"]:
            if leg["status"] not in ("OFFERED", "COMMITTED", "PICKED_UP", "IN_TRANSIT", "ARRIVED", "RECEIVING", "DELIVERED"):
                continue
            edges.append({
                "recommendation_id": f"FUL-{leg['leg_id']}", "fulfillment_leg_id": leg["leg_id"],
                "request_id": request["request_id"], "leg_status": leg["status"],
                "source_hospital_id": leg["source_hospital_id"], "source": leg["source_hospital"],
                "destination_hospital_id": leg["destination_hospital_id"], "destination": leg["destination_hospital"],
                "supply": leg["supply"], "quantity": leg["allocated_quantity"],
                "priority_score": "FULFILMENT", "route": leg["route"],
                "route_distance_km": leg["route_distance_km"], "estimated_eta_minutes": leg["estimated_eta_minutes"],
            })
    edges.sort(key=lambda edge: 0 if edge.get("fulfillment_leg_id") else 1)
    return envelope({"nodes": nodes, "edges": edges}, node_count=len(nodes), edge_count=len(edges))


@app.get("/api/nearby-hospitals/{hospital_id}")
def nearby_hospitals(hospital_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user.get("role") != "network_admin" and hospital_id != user["hospital_id"]:
        raise HTTPException(status_code=403, detail="Nearby hospital access is scoped to the logged-in facility")
    analysis = get_analysis()
    destination = analysis["hospitals"].get(hospital_id)
    if not destination:
        raise HTTPException(status_code=404, detail="Hospital not found")
    rows = []
    for hospital in analysis["hospitals"].values():
        if hospital["hospital_id"] == hospital_id:
            continue
        matches = [row for row in nearby_supplies(user=user)["data"] if row["hospital_id"] == hospital["hospital_id"]]
        if matches:
            rows.append({
                "hospital_id": hospital["hospital_id"], "hospital_name": hospital.get("display_name", hospital["name"]),
                "city": hospital["city"], "latitude": hospital["latitude"], "longitude": hospital["longitude"],
                "distance_km": min(row["road_distance_km"] for row in matches if row["road_distance_km"] is not None) if any(row["road_distance_km"] is not None for row in matches) else None,
                "eligible_supplies": [{"supply": row["supply"], "quantity": row["recommended_quantity"], "road_distance_km": row["road_distance_km"], "estimated_travel_minutes": row["estimated_travel_minutes"], "status": row["status"]} for row in matches],
                "eligible": any(row["feasible"] for row in matches), "local_radius_km": LOCAL_RADIUS_KM,
            })
    return envelope(sorted(rows, key=lambda row: (not row["eligible"], row["distance_km"] or 999)), count=len(rows))


@app.get("/api/hospitals")
def hospitals(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    rows = _hospital_summary(scoped_analysis(user))
    return envelope(rows, count=len(rows))


@app.get("/api/hospitals/{hospital_id}")
def hospital_detail(hospital_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user.get("role") != "network_admin" and hospital_id != user["hospital_id"]:
        raise HTTPException(status_code=403, detail="Hospital access is scoped to the logged-in facility")
    analysis = scoped_analysis(user)
    hospital = analysis["hospitals"].get(hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail="Hospital not found")
    inventory = [item for item in _inventory_rows(analysis) if item["hospital_id"] == hospital_id]
    shortages = [item for item in analysis["shortages"] if item["hospital_id"] == hospital_id]
    expiry = [item for item in analysis["expiry_risks"] if item["hospital_id"] == hospital_id]
    return envelope({"hospital": next(item for item in _hospital_summary(analysis) if item["hospital_id"] == hospital_id), "inventory": inventory, "shortages": shortages, "expiry_risks": expiry})


@app.get("/api/supplies")
def supplies(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    analysis = scoped_analysis(user)
    rows = []
    for supply_id, supply in analysis["supplies"].items():
        related = [item for item in analysis["forecasts"] if item["supply_id"] == supply_id]
        risks = [item for item in related if item["risk_level"] in ("CRITICAL", "HIGH")]
        expiries = [item for item in analysis["expiry_risks"] if item["supply_id"] == supply_id]
        rows.append({**supply, "total_stock": sum(item["current_stock"] for item in related), "daily_demand": round(sum(item["forecast_daily_demand"] for item in related), 1), "forecast_demand": round(sum(item["forecast_daily_demand"] for item in related) * 14), "shortage_hospitals": len(risks), "expiry_batches": len(expiries), "hospitals_holding": len(related), "risk_level": max((item["risk_level"] for item in related), key=lambda value: RISK_ORDER[value])})
    return envelope(rows, count=len(rows))


@app.get("/api/supply-catalogue")
def supply_catalogue(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    rows = [
        {"supply_id": row["supply_id"], "name": row["name"]}
        for row in database.ACTIVE_DATA.get("supplies", [])
    ]
    return envelope(rows, count=len(rows))


@app.get("/api/inventory")
def inventory(hospital_id: str | None = None, supply_id: str | None = None, risk: str | None = None, search: str | None = None, page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=1000), user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    analysis = scoped_analysis(user)
    if user.get("role") != "network_admin":
        hospital_id = user["hospital_id"]
    rows = _inventory_rows(analysis)
    if hospital_id:
        rows = [item for item in rows if item["hospital_id"] == hospital_id]
    if supply_id:
        rows = [item for item in rows if item["supply_id"] == supply_id]
    if risk:
        rows = [item for item in rows if item["risk_level"] == risk.upper() or item["expiry_risk"] == risk.upper()]
    if search:
        term = search.lower()
        rows = [item for item in rows if term in item["hospital"].lower() or term in item["supply"].lower() or term in item["batch_id"].lower()]
    total = len(rows)
    start = (page - 1) * page_size
    return envelope(rows[start:start + page_size], count=total, page=page, page_size=page_size)


@app.get("/api/forecast")
def forecast(hospital_id: str | None = None, supply_id: str = "MED001", horizon_days: int = Query(default=14, ge=7, le=60), user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    hospital_id = hospital_id or user["hospital_id"]
    if user.get("role") != "network_admin" and hospital_id != user["hospital_id"]:
        raise HTTPException(status_code=403, detail="Forecast access is scoped to the logged-in facility")
    analysis = scoped_analysis(user)
    row = next((item for item in analysis["forecasts"] if item["hospital_id"] == hospital_id and item["supply_id"] == supply_id), None)
    if not row:
        raise HTTPException(status_code=404, detail="No forecast exists for this hospital and supply")
    series = _forecast_series(row, hospital_id, supply_id, horizon_days)
    history = analysis["history_points"][(hospital_id, supply_id)][-30:]
    return envelope({**row, "forecast_horizon_days": horizon_days, "historical_series": history, "forecast_series": series})


@app.post("/api/forecast/run")
def run_forecast(payload: ForecastRun, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    hospital_id = user["hospital_id"] if user.get("role") != "network_admin" else payload.hospital_id
    return forecast(hospital_id, payload.supply_id, payload.horizon_days, user)


@app.post("/api/forecast/simulate")
def simulate_surgery_forecast(payload: ForecastSimulationPayload, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    surgery = payload.surgery
    if surgery.scheduled_date < date.today():
        raise HTTPException(status_code=422, detail="Scheduled date must be today or later")
    if surgery.surgery_type not in SURGERY_TYPES:
        raise HTTPException(status_code=422, detail={"message": "Unknown surgery type", "valid_types": list(SURGERY_TYPES)})
    baseline = get_analysis()
    if payload.supply_id not in baseline["supplies"]:
        raise HTTPException(status_code=404, detail="Supply not found")
    if surgery.estimated_supply_requirements and any(key not in baseline["supplies"] or value < 0 for key, value in surgery.estimated_supply_requirements.items()):
        raise HTTPException(status_code=422, detail="Custom per-case supply requirements must use known supplies and non-negative values")
    hospital_id = user["hospital_id"]
    baseline_row = next((row for row in baseline["forecasts"] if row["hospital_id"] == hospital_id and row["supply_id"] == payload.supply_id), None)
    if not baseline_row:
        raise HTTPException(status_code=404, detail="Forecast not found")
    simulated_surgery = {"surgery_id": "WHAT-IF", "hospital_id": hospital_id, **surgery.model_dump(mode="json"), "status": "scheduled"}
    schedules = [row for row in database.ACTIVE_DATA.get("surgery_schedules", []) if row.get("status") == "scheduled"] + [simulated_surgery]
    result = analyze(database.ACTIVE_DATA, surgery_schedules=schedules)
    adjusted = next(row for row in result["forecasts"] if row["hospital_id"] == hospital_id and row["supply_id"] == payload.supply_id)
    return envelope({
        "supply": adjusted["supply"], "hospital": adjusted["hospital"],
        "baseline_forecast_daily_demand": baseline_row["forecast_daily_demand"],
        "adjusted_forecast_daily_demand": adjusted["forecast_daily_demand"],
        "surgery_additional_units": adjusted["surgery_additional_units_next_7_days"],
        "baseline_risk_level": baseline_row["risk_level"], "adjusted_risk_level": adjusted["risk_level"],
        "baseline_safety_breach_days": baseline_row["days_until_stockout"],
        "adjusted_safety_breach_days": adjusted["days_until_stockout"],
        "scheduled_cases": surgery.number_of_cases, "surgery_type": surgery.surgery_type,
        "scheduled_date": surgery.scheduled_date.isoformat(),
    })


@app.get("/api/shortages")
def shortages(risk: str | None = None, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    rows = scoped_analysis(user)["shortages"]
    if risk:
        rows = [item for item in rows if item["risk_level"] == risk.upper()]
    return envelope(rows, count=len(rows))


@app.get("/api/expiry-risks")
def expiry_risks(risk: str | None = None, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    rows = scoped_analysis(user)["expiry_risks"]
    if risk:
        rows = [item for item in rows if item["risk_level"] == risk.upper()]
    return envelope(rows, count=len(rows))


@app.get("/api/redistribution")
def redistribution(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    rows = [_public_transfer(item, user["hospital_id"]) for item in scoped_analysis(user)["transfers"]]
    return envelope(rows, count=len(rows))


@app.get("/api/redistribution/{recommendation_id}")
def redistribution_detail(recommendation_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    item = next((row for row in scoped_analysis(user)["transfers"] if row["recommendation_id"] == recommendation_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return envelope(_public_transfer(item, user["hospital_id"]))


@app.post("/api/redistribution/{recommendation_id}/approve")
def approve_transfer(recommendation_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    item = next((row for row in scoped_analysis(user)["transfers"] if row["recommendation_id"] == recommendation_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    public = _public_transfer(item, user["hospital_id"])
    if not public["transfer_feasible"]:
        raise HTTPException(status_code=422, detail="Transfer cannot be approved until a road route and on-time ETA are verified")
    action_status[recommendation_id] = "approved"
    return envelope(ActionResult(status="approved", recommendation_id=recommendation_id).model_dump())


@app.post("/api/redistribution/{recommendation_id}/reject")
def reject_transfer(recommendation_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if not any(row["recommendation_id"] == recommendation_id for row in scoped_analysis(user)["transfers"]):
        raise HTTPException(status_code=404, detail="Recommendation not found")
    action_status[recommendation_id] = "rejected"
    return envelope(ActionResult(status="rejected", recommendation_id=recommendation_id).model_dump())


@app.get("/api/prioritisation")
def prioritisation(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    rows = scoped_analysis(user)["priorities"]
    return envelope(rows, count=len(rows))


@app.get("/api/alerts")
def alerts(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    analysis = scoped_analysis(user)
    rows = [{"id": f"short-{index}", "type": "shortage", "risk_level": item["risk_level"], "title": f"{item['supply']} at {item['hospital']}", "detail": f"Projected stock-out in {item['days_until_stockout']} days", "created_at": date.today().isoformat()} for index, item in enumerate(analysis["shortages"][:10])]
    rows.extend({"id": f"expiry-{index}", "type": "expiry", "risk_level": item["risk_level"], "title": f"{item['supply']} batch {item['batch_id']}", "detail": f"{item['expected_waste']} units at risk; expires in {item['days_until_expiry']} days", "created_at": date.today().isoformat()} for index, item in enumerate(analysis["expiry_risks"][:10]))
    return envelope(rows, count=len(rows))


@app.post("/api/analysis/run")
def run_analysis() -> dict[str, Any]:
    analysis = refresh_analysis()
    return envelope({"forecasts": len(analysis["forecasts"]), "shortages": len(analysis["shortages"]), "expiry_risks": len(analysis["expiry_risks"]), "recommendations": len(analysis["transfers"])})


@app.post("/api/assistant/query")
def assistant_query(payload: AssistantQuery, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    try:
        provider = get_assistant_provider()
        analysis = dict(scoped_analysis(user))
        analysis["transfers"] = [_public_transfer(item, user["hospital_id"]) for item in analysis["transfers"]]
        analysis["visible_supply_requests"] = fulfillment.list_visible_requests(user["hospital_id"])
        return envelope(provider.answer(payload.question, analysis))
    except Exception:
        _logger.exception("MediSupply Copilot failed for %s", user.get("hospital_id", "unknown hospital"))
        return envelope({
            "answer": "I could not answer that",
            "sources": [],
            "mode": "RULE-BASED",
            "disclaimer": "Synthetic demo data; verify operational decisions with local teams.",
        })


class _OutermostCORSMiddleware(CORSMiddleware):
    def __getattr__(self, name: str) -> Any:
        inner_app = object.__getattribute__(self, "app")
        return getattr(inner_app, name)


_fastapi_app = app
app = _OutermostCORSMiddleware(
    _fastapi_app,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
