from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import database
from .auth_service import login as login_user, user_from_token
from .assistant_service import get_assistant_provider
from .engine import RISK_ORDER, analyze
from .engine import LOCAL_RADIUS_KM

app = FastAPI(title="MediSupplyIQ API", version="1.0.0", description="Synthetic medical supply decision-support prototype")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SCENARIOS = {
    "normal": "Normal operations",
    "outbreak": "Outbreak surge",
    "critical": "Critical shortage",
    "expiry": "Expiry crisis",
    "redistribution": "Redistribution opportunity",
}
scenario_key = "outbreak"
action_status: dict[str, str] = {}
_cached_data_id: int | None = None
_cached_analysis: dict[str, Any] | None = None


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


def envelope(data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": data, "meta": {"source": database.DATA_SOURCE, **meta}}


def get_analysis() -> dict[str, Any]:
    global _cached_data_id, _cached_analysis
    if _cached_data_id != id(database.ACTIVE_DATA) or _cached_analysis is None:
        _cached_analysis = analyze(database.ACTIVE_DATA)
        _cached_data_id = id(database.ACTIVE_DATA)
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
    scoped["forecasts"] = [row for row in analysis["forecasts"] if row["hospital_id"] in local_ids]
    scoped["shortages"] = [row for row in analysis["shortages"] if row["hospital_id"] in local_ids]
    scoped["expiry_risks"] = [row for row in analysis["expiry_risks"] if row["hospital_id"] in local_ids]
    scoped["priorities"] = [row for row in analysis["priorities"] if row["hospital_id"] in local_ids]
    scoped["transfers"] = [
        row
        for row in analysis["transfers"]
        if row["source_hospital_id"] in local_ids and row["destination_hospital_id"] in local_ids
    ]
    scoped["inventory_totals"] = {key: value for key, value in analysis["inventory_totals"].items() if key[0] in local_ids}
    scoped["safety_totals"] = {key: value for key, value in analysis["safety_totals"].items() if key[0] in local_ids}
    scoped["hospitals"] = {key: value for key, value in analysis["hospitals"].items() if key in local_ids}
    scoped["history_points"] = {key: value for key, value in analysis["history_points"].items() if key[0] in local_ids}
    scoped["batches"] = {key: value for key, value in analysis["batches"].items() if key[0] in local_ids}
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
    database.persist_analysis(_cached_analysis)


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
    database.persist_analysis(_cached_analysis)
    action_status.clear()
    return envelope(_scenario_response())


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
        for day in range(1, 8):
            demand_chart.append({"day": f"+{day}d", "historical": None, "forecast": round(demand_forecast["forecast_daily_demand"] * (1 + day * 0.008), 1)})
    hospital_risk = sorted(hospitals, key=lambda item: (item["critical_shortages"], item["shortage_count"]), reverse=True)[:8]
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
        "transfers": analysis["transfers"][:6],
        "critical_hospitals": sorted(hospitals, key=lambda item: item["supply_health_score"])[:6],
        "scenario": _scenario_response(), "hospital": next(iter(analysis["hospitals"].values()), None),
        "local_radius_km": LOCAL_RADIUS_KM, "updated_at": date.today().isoformat(),
    }
    return envelope(response)


@app.get("/api/network")
def network_summary(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    analysis = get_analysis()
    expiry_by_hospital: dict[str, int] = defaultdict(int)
    for item in analysis["expiry_risks"]:
        expiry_by_hospital[item["hospital_id"]] += item["expected_waste"]

    nodes = []
    for hospital_id, hospital in analysis["hospitals"].items():
        surplus_units = 0
        shortage_units = 0
        critical_shortages = 0
        for forecast in analysis["forecasts"]:
            if forecast["hospital_id"] != hospital_id:
                continue
            lead_days = forecast["supplier_lead_time"]
            reserve = forecast["safety_stock"] + forecast["forecast_daily_demand"] * min(7, max(2, lead_days))
            surplus_units += max(0, round(forecast["current_stock"] - reserve))
            shortage_units += max(0, round(forecast["safety_stock"] + forecast["forecast_daily_demand"] * lead_days - forecast["current_stock"]))
            if forecast["risk_level"] == "CRITICAL":
                critical_shortages += 1
        roles = []
        if surplus_units:
            roles.append("SURPLUS")
        if shortage_units:
            roles.append("SHORTAGE")
        if expiry_by_hospital[hospital_id]:
            roles.append("EXPIRY RISK")
        nodes.append({
            "hospital_id": hospital_id, "name": hospital["name"], "city": hospital["city"],
            "latitude": hospital["latitude"], "longitude": hospital["longitude"],
            "surplus_units": surplus_units, "shortage_units": shortage_units,
            "expiry_units_at_risk": expiry_by_hospital[hospital_id],
            "critical_shortages": critical_shortages, "roles": roles or ["BALANCED"],
        })
    edges = [{
        "recommendation_id": item["recommendation_id"],
        "source_hospital_id": item["source_hospital_id"], "source": item["source_hospital"],
        "destination_hospital_id": item["destination_hospital_id"], "destination": item["destination_hospital"],
        "supply": item["supply"], "quantity": item["recommended_quantity"],
        "transport_hours": item["estimated_transport_hours"], "distance_km": item["distance_km"], "priority_score": item["priority_score"],
    } for item in analysis["transfers"] if user.get("role") == "network_admin" or item["destination_hospital_id"] == user["hospital_id"]][:20]
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
        matches = [item for item in analysis["transfers"] if item["destination_hospital_id"] == hospital_id and item["source_hospital_id"] == hospital["hospital_id"]]
        rows.append({
            "hospital_id": hospital["hospital_id"], "hospital_name": hospital["name"],
            "city": hospital["city"], "latitude": hospital["latitude"], "longitude": hospital["longitude"],
            "distance_km": min((item["distance_km"] for item in matches), default=None),
            "eligible_supplies": [{"supply": item["supply"], "quantity": item["recommended_quantity"], "priority_score": item["priority_score"]} for item in matches],
            "eligible": bool(matches), "local_radius_km": LOCAL_RADIUS_KM,
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
    series = [{"day": i + 1, "demand": round(row["forecast_daily_demand"] * (1 + row["trend_percent"] / 100 * i * 0.025), 1)} for i in range(horizon_days)]
    history = analysis["history_points"][(hospital_id, supply_id)][-30:]
    return envelope({**row, "forecast_horizon_days": horizon_days, "historical_series": history, "forecast_series": series})


@app.post("/api/forecast/run")
def run_forecast(payload: ForecastRun, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    hospital_id = user["hospital_id"] if user.get("role") != "network_admin" else payload.hospital_id
    return forecast(hospital_id, payload.supply_id, payload.horizon_days, user)


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
    rows = [{**item, "status": action_status.get(item["recommendation_id"], item["status"])} for item in scoped_analysis(user)["transfers"]]
    return envelope(rows, count=len(rows))


@app.get("/api/redistribution/{recommendation_id}")
def redistribution_detail(recommendation_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    item = next((row for row in scoped_analysis(user)["transfers"] if row["recommendation_id"] == recommendation_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return envelope({**item, "status": action_status.get(recommendation_id, item["status"])})


@app.post("/api/redistribution/{recommendation_id}/approve")
def approve_transfer(recommendation_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if not any(row["recommendation_id"] == recommendation_id for row in scoped_analysis(user)["transfers"]):
        raise HTTPException(status_code=404, detail="Recommendation not found")
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
    global _cached_data_id, _cached_analysis
    _cached_data_id, _cached_analysis = id(database.ACTIVE_DATA), analyze(database.ACTIVE_DATA)
    database.persist_analysis(_cached_analysis)
    return envelope({"forecasts": len(_cached_analysis["forecasts"]), "shortages": len(_cached_analysis["shortages"]), "expiry_risks": len(_cached_analysis["expiry_risks"]), "recommendations": len(_cached_analysis["transfers"])})


@app.post("/api/assistant/query")
def assistant_query(payload: AssistantQuery, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    provider = get_assistant_provider()
    return envelope(provider.answer(payload.question, scoped_analysis(user)))


@app.exception_handler(Exception)
async def unexpected_error_handler(_request: Any, error: Exception) -> Any:
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"data": None, "error": {"message": str(error)}, "meta": {"source": database.DATA_SOURCE}})
