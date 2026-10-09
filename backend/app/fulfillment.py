from __future__ import annotations

import secrets
import threading
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import database
from .engine import analyze
from .routing import road_route


_lock = threading.RLock()
OFFER_TIMEOUT_SECONDS = max(1, int(os.getenv("FULFILLMENT_OFFER_TIMEOUT_SECONDS", "30")))
_ALLOCATED_STATUSES = {
    "COMMITTED", "DISPATCHING", "PICKUP_PENDING", "PICKED_UP",
    "IN_TRANSIT", "ARRIVED", "RECEIVING", "DELIVERED",
}
_IN_TRANSIT_STATUSES = {"IN_TRANSIT", "ARRIVED", "RECEIVING"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_event(request: dict[str, Any], event: str, message: str,
                  leg: dict[str, Any] | None = None) -> None:
    occurred_at = _now()
    entry = {"event_id": f"EVT-{secrets.token_hex(4).upper()}", "event": event,
             "message": message, "occurred_at": occurred_at}
    if leg:
        entry.update({
            "leg_id": leg["leg_id"],
            "source_hospital_id": leg["source_hospital_id"],
            "destination_hospital_id": leg["destination_hospital_id"],
            "quantity": int(leg["allocated_quantity"]),
        })
    request.setdefault("timeline", []).append(entry)
    request["updated_at"] = occurred_at
    database.save_workflow_record("supply_requests", request)


def _requests() -> list[dict[str, Any]]:
    return database.ACTIVE_DATA.setdefault("supply_requests", [])


def _legs() -> list[dict[str, Any]]:
    return database.ACTIVE_DATA.setdefault("fulfillment_legs", [])


def _request(request_id: str) -> dict[str, Any] | None:
    return next((item for item in _requests() if item["request_id"] == request_id), None)


def _leg(leg_id: str) -> dict[str, Any] | None:
    return next((item for item in _legs() if item["leg_id"] == leg_id), None)


def _request_legs(request_id: str) -> list[dict[str, Any]]:
    return [item for item in _legs() if item["request_id"] == request_id]


def _expire_offers(request: dict[str, Any]) -> bool:
    now = datetime.now(timezone.utc)
    expired = False
    for leg in _request_legs(request["request_id"]):
        if leg["status"] != "OFFERED":
            continue
        created_at = datetime.fromisoformat(leg["created_at"])
        if (now - created_at).total_seconds() < OFFER_TIMEOUT_SECONDS:
            continue
        leg["status"] = "EXPIRED"
        leg["lifecycle_status"] = "EXPIRED"
        leg["failure_reason"] = "Source did not respond. Searching for another supplier."
        leg["expired_at"] = _now()
        request["expired_offers_count"] = int(request.get("expired_offers_count", 0)) + 1
        database.save_workflow_record("fulfillment_legs", leg)
        _record_event(request, "OFFER_EXPIRED", leg["failure_reason"], leg)
        expired = True
    if expired:
        database.save_workflow_record("supply_requests", request)
    return expired


def _fulfilled_quantity(request_id: str) -> int:
    return sum(int(item["allocated_quantity"]) for item in _request_legs(request_id)
               if item["status"] == "DELIVERED")


def _allocated_quantity(request_id: str) -> int:
    return sum(int(item["allocated_quantity"]) for item in _request_legs(request_id)
               if item["status"] in _ALLOCATED_STATUSES)


def _in_transit_quantity(request_id: str) -> int:
    return sum(int(item["allocated_quantity"]) for item in _request_legs(request_id)
               if item["status"] in _IN_TRANSIT_STATUSES)


def _refresh_request(request: dict[str, Any], matching_status: str | None = None) -> None:
    requested = int(request["requested_quantity"])
    fulfilled = min(requested, _fulfilled_quantity(request["request_id"]))
    allocated = min(requested, _allocated_quantity(request["request_id"]))
    in_transit = min(allocated - fulfilled, _in_transit_quantity(request["request_id"]))
    request["fulfilled_quantity"] = fulfilled
    request["allocated_quantity"] = allocated
    request["in_transit_quantity"] = in_transit
    request["remaining_quantity"] = max(0, requested - allocated)
    if fulfilled >= requested:
        request["status"] = "FULFILLED"
        request["matching_status"] = "COMPLETE"
    elif request["remaining_quantity"] == 0 and fulfilled > 0:
        request["status"] = "PARTIALLY_DELIVERED"
        request["matching_status"] = "COMPLETE"
    elif request["remaining_quantity"] == 0 and in_transit:
        request["status"] = "IN_TRANSIT"
        request["matching_status"] = "COMPLETE"
    elif request["remaining_quantity"] == 0:
        request["status"] = "FULLY_ALLOCATED"
        request["matching_status"] = "COMPLETE"
    elif matching_status == "NO_SOURCE_AVAILABLE":
        request["status"] = "PARTIALLY_FULFILLED" if allocated else "NO_SOURCE_AVAILABLE"
        request["matching_status"] = matching_status
    elif allocated or fulfilled:
        request["status"] = "PARTIALLY_FULFILLED"
        request["matching_status"] = matching_status or "SEARCHING"
    else:
        request["status"] = "SEARCHING"
        request["matching_status"] = matching_status or "SEARCHING"
    request["updated_at"] = _now()
    database.save_workflow_record("supply_requests", request)


def _committed_allocations(source_id: str, supply_id: str) -> tuple[int, dict[str, int]]:
    pool = next((row for row in database.ACTIVE_DATA.get("shareable_pool", [])
                 if row["hospital_id"] == source_id and row["supply_id"] == supply_id), None)
    quantity = int(pool.get("committed_quantity", 0)) if pool else 0
    batches = {
        row["batch_id"]: int(row.get("committed_quantity", 0))
        for row in database.ACTIVE_DATA.get("inventory", [])
        if row["hospital_id"] == source_id and row["supply_id"] == supply_id and int(row.get("committed_quantity", 0)) > 0
    }
    return quantity, batches


def _candidate_capacity(request: dict[str, Any], source_id: str, analysis: dict[str, Any],
                        excluded_sources: set[str]) -> dict[str, Any] | None:
    destination_id = request["destination_hospital_id"]
    supply_id = request["supply_id"]
    if source_id == destination_id or source_id in excluded_sources:
        return None
    hospitals = analysis["hospitals"]
    source = hospitals.get(source_id)
    destination = hospitals.get(destination_id)
    source_forecast = next((row for row in analysis["forecasts"]
                            if row["hospital_id"] == source_id and row["supply_id"] == supply_id), None)
    destination_forecast = next((row for row in analysis["forecasts"]
                                 if row["hospital_id"] == destination_id and row["supply_id"] == supply_id), None)
    recommendation = next((row for row in analysis["transfers"]
                           if row["source_hospital_id"] == source_id
                           and row["destination_hospital_id"] == destination_id
                           and row["supply_id"] == supply_id), None)
    if (not source or not destination or not source.get("active", True) or not source_forecast
            or not destination_forecast or not recommendation or source.get("city") != destination.get("city")):
        return None
    pool = next((row for row in database.ACTIVE_DATA.get("shareable_pool", [])
                 if row["hospital_id"] == source_id and row["supply_id"] == supply_id and row.get("enabled")), None)
    if not pool or (pool.get("valid_until") and date.fromisoformat(pool["valid_until"]) < date.today()):
        return None
    try:
        route = road_route(source, destination)
    except ValueError:
        return None
    deadline_minutes = max(1, int(destination_forecast["days_until_stockout"])) * 1440
    if route["duration_minutes"] >= deadline_minutes:
        return None

    reserved_quantity, reserved_batches = _committed_allocations(source_id, supply_id)
    source_reserve = (source_forecast["safety_stock"] + source_forecast["forecast_daily_demand"]
                      * min(7, max(2, source_forecast["supplier_lead_time"])))
    safe_surplus = max(0, round(source_forecast["current_stock"] - source_reserve - reserved_quantity))
    pool_remaining = max(0, int(pool["shareable_quantity"]) - int(pool.get("committed_quantity", 0)))
    arrival_date = date.today() + timedelta(days=max(1, (route["duration_minutes"] + 1439) // 1440))
    source_batches = analysis["batches"].get((source_id, supply_id), [])
    eligible_batches = sorted(
        (batch for batch in source_batches
         if batch.get("batch_status") != "expired"
         and date.fromisoformat(batch["expiry_date"]) >= arrival_date
         and int(batch["quantity"]) - reserved_batches.get(batch["batch_id"], 0) > 0),
        key=lambda batch: (batch["expiry_date"], batch["batch_id"]),
    )
    batch_quantity = sum(max(0, int(batch["quantity"]) - reserved_batches.get(batch["batch_id"], 0))
                         for batch in eligible_batches)
    transfer_limit = int(analysis["supplies"][supply_id].get("max_transfer_quantity", request["remaining_quantity"]))
    available = min(pool_remaining, safe_surplus, batch_quantity, transfer_limit)
    quantity = min(int(request["remaining_quantity"]), available)
    if quantity <= 0:
        return None

    allocations = []
    left = quantity
    for batch in eligible_batches:
        available_batch = max(0, int(batch["quantity"]) - reserved_batches.get(batch["batch_id"], 0))
        take = min(left, available_batch)
        if take:
            allocations.append({"batch_id": batch["batch_id"], "quantity": take, "expiry_date": batch["expiry_date"]})
            left -= take
        if not left:
            break
    earliest_expiry_days = max(0, (date.fromisoformat(eligible_batches[0]["expiry_date"]) - date.today()).days)
    score = (float(recommendation["priority_score"])
             + 18 * quantity / max(1, int(request["remaining_quantity"]))
             + max(0, 30 - earliest_expiry_days) / 30 * 4
             - route["duration_minutes"] / max(1, deadline_minutes) * 10)
    return {
        "source": source, "recommendation": recommendation, "route": route,
        "quantity": quantity, "allocations": allocations, "score": score,
    }


def _match_next(request: dict[str, Any]) -> dict[str, Any] | None:
    _refresh_request(request)
    if request["status"] == "CANCELLED":
        return None
    if request["remaining_quantity"] <= 0:
        for offer in _request_legs(request["request_id"]):
            if offer["status"] == "OFFERED":
                offer["status"] = "CANCELLED"
                offer["lifecycle_status"] = "WITHDRAWN"
                offer["failure_reason"] = "Request need is fully allocated; unused offer withdrawn."
                offer["cancelled_at"] = _now()
                database.save_workflow_record("fulfillment_legs", offer)
                _record_event(request, "OFFER_WITHDRAWN", offer["failure_reason"], offer)
        return None
    _expire_offers(request)
    attempts = int(request.get("matching_attempts", 0))
    if attempts:
        request["rematch_count"] = int(request.get("rematch_count", 0)) + 1
        _record_event(request, "REMATCHING", f"Searching sources for the remaining {request['remaining_quantity']} units.")
    else:
        _record_event(request, "MATCHING", "Searching nearby eligible sources.")
    request["matching_attempts"] = attempts + 1
    request["matching_status"] = "MATCHING"
    request["last_matching_at"] = _now()
    database.save_workflow_record("supply_requests", request)
    analysis = analyze(database.ACTIVE_DATA)
    open_offers = [leg for leg in _request_legs(request["request_id"]) if leg["status"] == "OFFERED"]
    for leg in open_offers:
        candidate = _candidate_capacity(request, leg["source_hospital_id"], analysis, set())
        if candidate:
            previous_quantity = int(leg["allocated_quantity"])
            leg["allocated_quantity"] = candidate["quantity"]
            leg["source_batch_allocations"] = candidate["allocations"]
            leg["route_distance_km"] = candidate["route"]["distance_km"]
            leg["estimated_eta_minutes"] = candidate["route"]["duration_minutes"]
            leg["route"] = candidate["route"]["coordinates"]
            database.save_workflow_record("fulfillment_legs", leg)
            if previous_quantity != candidate["quantity"]:
                _record_event(request, "OFFER_UPDATED", f"{leg['source_hospital']} offer adjusted to {candidate['quantity']} units for the remaining need.", leg)
            continue
        leg["status"] = "REJECTED"
        leg["lifecycle_status"] = "WITHDRAWN"
        leg["failure_reason"] = "Current stock, route, expiry, or opt-in eligibility changed while this offer was open."
        database.save_workflow_record("fulfillment_legs", leg)
    used_sources = {row["source_hospital_id"] for row in _request_legs(request["request_id"])}
    candidates = []
    for pool in database.ACTIVE_DATA.get("shareable_pool", []):
        candidate = _candidate_capacity(request, pool["hospital_id"], analysis, used_sources)
        if candidate:
            candidates.append(candidate)
    candidates.sort(key=lambda item: (item["score"], item["quantity"], -item["route"]["duration_minutes"]), reverse=True)
    if not candidates:
        if any(row["status"] == "OFFERED" for row in _request_legs(request["request_id"])):
            request["matching_status"] = "OFFERED"
            _refresh_request(request, "OFFERED")
            return next(row for row in _request_legs(request["request_id"]) if row["status"] == "OFFERED")
        _refresh_request(request, "NO_SOURCE_AVAILABLE")
        return None

    created_offers = []
    for selected in candidates:
        source = selected["source"]
        route = selected["route"]
        leg = {
            "leg_id": f"LEG-{secrets.token_hex(6).upper()}",
            "request_id": request["request_id"],
            "source_hospital_id": source["hospital_id"],
            "source_hospital": source["display_name"],
            "destination_hospital_id": request["destination_hospital_id"],
            "destination_hospital": request["destination_hospital"],
            "supply_id": request["supply_id"], "supply": request["supply"],
            "allocated_quantity": selected["quantity"], "status": "OFFERED",
            "route_distance_km": route["distance_km"], "estimated_eta_minutes": route["duration_minutes"],
            "route": route["coordinates"], "route_provider": route["provider"],
            "source_batch_allocations": selected["allocations"],
            "created_at": _now(), "accepted_at": None, "picked_up_at": None,
            "in_transit_at": None, "delivered_at": None,
        }
        _legs().append(leg)
        database.save_workflow_record("fulfillment_legs", leg)
        _record_event(request, "OFFERED", f"{source['display_name']} can offer up to {selected['quantity']} units.", leg)
        created_offers.append(leg)
    request["matching_status"] = "OFFERED"
    _refresh_request(request, "OFFERED")
    return created_offers[0] if created_offers else None


def _view(request: dict[str, Any]) -> dict[str, Any]:
    hospitals = {row["hospital_id"]: row for row in database.ACTIVE_DATA.get("hospitals", [])}
    destination = hospitals.get(request.get("destination_hospital_id"))
    legs = []
    for leg in sorted(_request_legs(request["request_id"]), key=lambda item: item["created_at"]):
        source = hospitals.get(leg.get("source_hospital_id"))
        leg_destination = hospitals.get(leg.get("destination_hospital_id"))
        legs.append({
            **leg,
            "source_hospital": (source.get("display_name") or source.get("name") or "Source unavailable") if source else "Source unavailable",
            "destination_hospital": (leg_destination.get("display_name") or leg_destination.get("name") or "Destination unavailable") if leg_destination else "Destination unavailable",
        })
    return {
        **request,
        "destination_hospital": (destination.get("display_name") or destination.get("name")) if destination else None,
        "created_by_hospital_id": request.get("created_by_hospital_id", request["destination_hospital_id"]),
        "legs": legs,
    }


def _fail_leg_locked(request: dict[str, Any], leg: dict[str, Any], reason: str,
                     consumed_allocations: list[dict[str, Any]] | None = None) -> None:
    previous_status = leg["status"]
    if previous_status in ("COMMITTED", "PICKED_UP", "DISPATCHING"):
        consumed_by_batch = {row["batch_id"]: int(row["quantity"]) for row in consumed_allocations or []}
        consumed_quantity = sum(consumed_by_batch.values())
        unconsumed_quantity = max(0, int(leg["allocated_quantity"]) - consumed_quantity)
        if consumed_quantity:
            database.release_shareable_quantity(leg["source_hospital_id"], leg["supply_id"], consumed_quantity, delivered=True)
        if unconsumed_quantity:
            database.release_shareable_quantity(leg["source_hospital_id"], leg["supply_id"], unconsumed_quantity)
        for allocation in leg.get("source_batch_allocations", []):
            unconsumed = int(allocation["quantity"]) - consumed_by_batch.get(allocation["batch_id"], 0)
            if unconsumed > 0:
                database.release_batch_quantity(allocation["batch_id"], unconsumed)
    leg["status"] = "CANCELLED"
    leg["lifecycle_status"] = "FAILED"
    leg["failure_reason"] = reason
    leg["cancelled_at"] = _now()
    leg["failed_at"] = leg["cancelled_at"]
    leg["failure_inventory_disposition"] = "released_to_source" if previous_status in ("COMMITTED", "PICKED_UP", "DISPATCHING") else "lost_or_in_transit"
    request["failed_transfers_count"] = int(request.get("failed_transfers_count", 0)) + 1
    database.save_workflow_record("fulfillment_legs", leg)
    _record_event(request, "FAILED", f"{reason} Remaining need is {request['remaining_quantity']} units.", leg)
    _refresh_request(request)
    _match_next(request)


def create_request(destination_id: str, supply_id: str, requested_quantity: int,
                  urgency_level: str = "NORMAL", delivery_deadline: date | str | None = None,
                  auto_match: bool = True) -> dict[str, Any]:
    with _lock:
        destination = next((row for row in database.ACTIVE_DATA.get("hospitals", []) if row["hospital_id"] == destination_id), None)
        supply = next((row for row in database.ACTIVE_DATA.get("supplies", []) if row["supply_id"] == supply_id), None)
        if not destination or not supply:
            raise ValueError("Destination hospital or supply was not found")
        if requested_quantity < 1:
            raise ValueError("Requested quantity must be at least one unit")
        normalized_urgency = (urgency_level or "NORMAL").upper()
        allowed_urgency = {"NORMAL", "LOW", "MEDIUM", "URGENT", "CRITICAL"}
        if normalized_urgency not in allowed_urgency:
            raise ValueError("Urgency level must be NORMAL, LOW, MEDIUM, URGENT, or CRITICAL")
        normalized_deadline = None
        if delivery_deadline is not None:
            if isinstance(delivery_deadline, date) and not isinstance(delivery_deadline, datetime):
                normalized_deadline = delivery_deadline.isoformat()
            else:
                try:
                    normalized_deadline = date.fromisoformat(str(delivery_deadline)).isoformat()
                except ValueError as error:
                    raise ValueError("Delivery deadline must be a valid ISO date") from error
        request = {
            "request_id": f"SWRX-{secrets.token_hex(4).upper()}",
            "created_by_hospital_id": destination_id,
            "destination_hospital_id": destination_id,
            "destination_hospital": destination["display_name"],
            "supply_id": supply_id, "supply": supply["name"],
            "requested_quantity": int(requested_quantity), "fulfilled_quantity": 0,
            "allocated_quantity": 0, "in_transit_quantity": 0,
            "remaining_quantity": int(requested_quantity), "status": "SEARCHING",
            "matching_status": "SEARCHING", "matching_attempts": 0,
            "rematch_count": 0, "rejected_offers_count": 0,
            "expired_offers_count": 0, "failed_transfers_count": 0,
            "urgency_level": normalized_urgency,
            "delivery_deadline": normalized_deadline,
            "timeline": [],
            "created_at": _now(), "updated_at": _now(),
        }
        _requests().append(request)
        database.save_workflow_record("supply_requests", request)
        _record_event(request, "REQUESTED", f"{requested_quantity} units of {supply['name']} requested for {normalized_urgency} urgency.")
        if auto_match:
            _match_next(request)
        return _view(request)


def match_request_in_background(request_id: str) -> None:
    with _lock:
        request = _request(request_id)
        if request is not None:
            _match_next(request)


def list_visible_requests(hospital_id: str) -> list[dict[str, Any]]:
    with _lock:
        rows = []
        for request in _requests():
            if _expire_offers(request):
                _refresh_request(request)
                _match_next(request)
            legs = _request_legs(request["request_id"])
            if request["destination_hospital_id"] == hospital_id or any(leg["source_hospital_id"] == hospital_id for leg in legs):
                rows.append(_view(request))
        return sorted(rows, key=lambda item: item["created_at"], reverse=True)


def _find_leg_for_user(request_id: str, leg_id: str, hospital_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    request = _request(request_id)
    leg = _leg(leg_id)
    if not request or not leg or leg["request_id"] != request_id:
        raise ValueError("Supply request leg was not found")
    if hospital_id not in (request["destination_hospital_id"], leg["source_hospital_id"]):
        raise PermissionError("This hospital is not a participant in the request")
    return request, leg


def accept_leg(request_id: str, leg_id: str, hospital_id: str) -> dict[str, Any]:
    with _lock:
        request, leg = _find_leg_for_user(request_id, leg_id, hospital_id)
        if _expire_offers(request):
            _refresh_request(request)
            _match_next(request)
        if leg["source_hospital_id"] != hospital_id:
            raise PermissionError("Only the source hospital can accept this offer")
        if leg["status"] != "OFFERED":
            raise ValueError("Only an offered fulfillment leg can be accepted")
        if not database.claim_leg_acceptance(leg_id):
            raise ValueError("This fulfillment offer is already being accepted or is no longer available")
        analysis = analyze(database.ACTIVE_DATA)
        candidate = _candidate_capacity(request, hospital_id, analysis, set())
        if not candidate:
            leg["status"] = "REJECTED"
            leg["failure_reason"] = "Current stock, route, expiry, or opt-in eligibility changed before acceptance."
            database.save_workflow_record("fulfillment_legs", leg)
            _match_next(request)
            return _view(request)
        quantity = min(int(leg["allocated_quantity"]), int(candidate["quantity"]))
        if quantity <= 0:
            leg["status"] = "REJECTED"
            database.save_workflow_record("fulfillment_legs", leg)
            _match_next(request)
            return _view(request)
        allocations = []
        left = quantity
        for allocation in candidate["allocations"]:
            take = min(left, allocation["quantity"])
            if take:
                allocations.append({**allocation, "quantity": take})
                left -= take
        if left:
            raise ValueError("Eligible batch quantity changed before stock could be committed")
        source_forecast = next(row for row in analysis["forecasts"]
                               if row["hospital_id"] == hospital_id and row["supply_id"] == request["supply_id"])
        reserve = source_forecast["safety_stock"] + source_forecast["forecast_daily_demand"] * min(7, max(2, source_forecast["supplier_lead_time"]))
        safe_surplus_limit = max(0, round(source_forecast["current_stock"] - reserve))
        if not database.reserve_shareable_quantity(hospital_id, request["supply_id"], quantity, safe_surplus_limit):
            leg["status"] = "REJECTED"
            leg["failure_reason"] = "Shareable stock was committed to another request before acceptance."
            database.save_workflow_record("fulfillment_legs", leg)
            _match_next(request)
            return _view(request)
        reserved_batches = []
        batch_race = False
        for allocation in allocations:
            if not database.reserve_batch_quantity(allocation["batch_id"], allocation["quantity"]):
                batch_race = True
                break
            reserved_batches.append(allocation)
        if batch_race:
            for allocation in reserved_batches:
                database.release_batch_quantity(allocation["batch_id"], allocation["quantity"])
            database.release_shareable_quantity(hospital_id, request["supply_id"], quantity)
            leg["status"] = "REJECTED"
            leg["failure_reason"] = "Eligible batch stock was committed to another request before acceptance."
            database.save_workflow_record("fulfillment_legs", leg)
            _match_next(request)
            return _view(request)
        try:
            leg["allocated_quantity"] = quantity
            leg["source_batch_allocations"] = allocations
            leg["status"] = "COMMITTED"
            leg["lifecycle_status"] = "PICKUP_PENDING"
            leg["accepted_at"] = _now()
            leg["pickup_pending_at"] = _now()
            database.save_workflow_record("fulfillment_legs", leg)
            _record_event(request, "ACCEPTED", f"{leg['source_hospital']} accepted {quantity} units; pickup is pending.", leg)
        except Exception:
            for allocation in reserved_batches:
                database.release_batch_quantity(allocation["batch_id"], allocation["quantity"])
            database.release_shareable_quantity(hospital_id, request["supply_id"], quantity)
            leg["status"] = "OFFERED"
            database.save_workflow_record("fulfillment_legs", leg)
            raise
        _refresh_request(request)
        if request["remaining_quantity"]:
            _match_next(request)
        return _view(request)


def reject_leg(request_id: str, leg_id: str, hospital_id: str) -> dict[str, Any]:
    with _lock:
        request, leg = _find_leg_for_user(request_id, leg_id, hospital_id)
        if _expire_offers(request):
            _refresh_request(request)
            _match_next(request)
        if leg["source_hospital_id"] != hospital_id:
            raise PermissionError("Only the source hospital can reject this offer")
        if leg["status"] != "OFFERED":
            raise ValueError("Only an offered fulfillment leg can be rejected")
        leg["status"] = "REJECTED"
        leg["rejected_at"] = _now()
        request["rejected_offers_count"] = int(request.get("rejected_offers_count", 0)) + 1
        database.save_workflow_record("fulfillment_legs", leg)
        _record_event(request, "REJECTED", f"{leg['source_hospital']} declined the {leg['allocated_quantity']} unit offer.", leg)
        _refresh_request(request)
        _match_next(request)
        return _view(request)


def rematch_request(request_id: str, hospital_id: str) -> dict[str, Any]:
    with _lock:
        request = _request(request_id)
        if not request or request["destination_hospital_id"] != hospital_id:
            raise ValueError("Supply request was not found")
        if request["status"] == "CANCELLED":
            raise ValueError("A cancelled supply request cannot be matched")
        _refresh_request(request)
        if request["remaining_quantity"]:
            _match_next(request)
        return _view(request)


def cancel_request(request_id: str, hospital_id: str) -> dict[str, Any]:
    with _lock:
        request = _request(request_id)
        if not request or request["destination_hospital_id"] != hospital_id:
            raise ValueError("Supply request was not found")
        if request["status"] == "CANCELLED":
            return _view(request)
        request_legs = _request_legs(request_id)
        if any(leg["status"] in ("DISPATCHING", "IN_TRANSIT", "DELIVERED") for leg in request_legs):
            raise ValueError("A request with a dispatching, in-transit, or delivered leg cannot be cancelled")
        for leg in request_legs:
            if leg["status"] == "COMMITTED":
                database.release_shareable_quantity(leg["source_hospital_id"], leg["supply_id"], leg["allocated_quantity"])
                for allocation in leg.get("source_batch_allocations", []):
                    database.release_batch_quantity(allocation["batch_id"], allocation["quantity"])
            if leg["status"] in ("OFFERED", "COMMITTED"):
                leg["status"] = "CANCELLED"
                leg["cancelled_at"] = _now()
                database.save_workflow_record("fulfillment_legs", leg)
        request["status"] = "CANCELLED"
        request["matching_status"] = "CANCELLED"
        request["fulfilled_quantity"] = 0
        request["remaining_quantity"] = int(request["requested_quantity"])
        request["updated_at"] = _now()
        database.save_workflow_record("supply_requests", request)
        return _view(request)


def _receive_leg(request: dict[str, Any], leg: dict[str, Any]) -> None:
    if not database.claim_leg_receipt(leg["leg_id"]):
        raise ValueError("This delivery is already being reconciled or is no longer in transit")
    source_batches = {
        batch["batch_id"]: batch
        for batch in database.ACTIVE_DATA.get("inventory", [])
        if batch["hospital_id"] == leg["source_hospital_id"] and batch["supply_id"] == leg["supply_id"]
    }
    destination_batches = [
        batch for batch in database.ACTIVE_DATA.get("inventory", [])
        if batch["hospital_id"] == leg["destination_hospital_id"] and batch["supply_id"] == leg["supply_id"]
    ]
    destination_safety = max((int(batch["safety_stock"]) for batch in destination_batches), default=0)
    destination_reorder = max((int(batch.get("reorder_level", destination_safety)) for batch in destination_batches), default=destination_safety)
    destination_lead = max((int(batch.get("supplier_lead_time", 7)) for batch in destination_batches), default=7)
    allocations = leg.get("source_batch_allocations", [])
    for index, allocation in enumerate(allocations, start=1):
        source_batch = source_batches.get(allocation["batch_id"])
        expiry_date = allocation.get("expiry_date") or (source_batch or {}).get("expiry_date")
        if not source_batch or not expiry_date or date.fromisoformat(expiry_date) < date.today():
            _fail_leg_locked(request, leg, "Allocated FEFO batch is unavailable or expired at receipt.")
            return

    try:
        received_ids = []
        for index, allocation in enumerate(allocations, start=1):
            source_batch = source_batches[allocation["batch_id"]]
            received_batch = {
                "inventory_id": f"RECV-{leg['leg_id']}-{index}",
                "batch_id": f"{leg['leg_id']}-{allocation['batch_id']}",
                "source_batch_id": allocation["batch_id"], "transfer_leg_id": leg["leg_id"],
                "hospital_id": leg["destination_hospital_id"], "supply_id": leg["supply_id"],
                "quantity": int(allocation["quantity"]), "safety_stock": destination_safety,
                "expiry_date": allocation.get("expiry_date") or source_batch["expiry_date"],
                "reorder_level": destination_reorder,
                "supplier_id": source_batch.get("supplier_id", "TRANSFER"),
                "supplier_lead_time": destination_lead, "supplier_lead_days": destination_lead,
                "last_restock_date": date.today().isoformat(),
                "storage_condition": source_batch.get("storage_condition", "ambient"),
                "batch_status": "active", "committed_quantity": 0,
            }
            database.save_source_record("inventory", received_batch, "inventory_id")
            received_ids.append(received_batch["inventory_id"])
    except Exception:
        leg["status"] = "IN_TRANSIT"
        database.save_workflow_record("fulfillment_legs", leg)
        raise
    leg["received_inventory_ids"] = received_ids
    leg["status"] = "DELIVERED"
    leg["delivered_at"] = _now()


def update_leg_status(request_id: str, leg_id: str, hospital_id: str, status: str) -> dict[str, Any]:
    with _lock:
        request, leg = _find_leg_for_user(request_id, leg_id, hospital_id)
        if status == "PICKED_UP":
            if leg["source_hospital_id"] != hospital_id or leg["status"] != "COMMITTED":
                raise PermissionError("Only the source hospital can confirm pickup of an accepted leg")
            leg["status"] = "PICKED_UP"
            leg["lifecycle_status"] = "PICKED_UP"
            leg["picked_up_at"] = _now()
            database.save_workflow_record("fulfillment_legs", leg)
            _record_event(request, "PICKED_UP", f"{leg['source_hospital']} picked up {leg['allocated_quantity']} units.", leg)
            _refresh_request(request)
            if request["remaining_quantity"]:
                _match_next(request)
            return _view(request)
        if status == "IN_TRANSIT":
            if leg["source_hospital_id"] != hospital_id or leg["status"] not in ("COMMITTED", "PICKED_UP"):
                raise PermissionError("Only the source hospital can dispatch an accepted or picked-up leg")
            if not database.claim_leg_dispatch(leg_id):
                raise ValueError("This fulfillment leg is already being dispatched or is no longer committed")
            analysis = analyze(database.ACTIVE_DATA)
            batches_by_id = {row["batch_id"]: row for row in database.ACTIVE_DATA.get("inventory", [])}
            source = analysis["hospitals"][leg["source_hospital_id"]]
            destination = analysis["hospitals"][leg["destination_hospital_id"]]
            destination_forecast = next(row for row in analysis["forecasts"]
                                        if row["hospital_id"] == leg["destination_hospital_id"] and row["supply_id"] == leg["supply_id"])
            try:
                route = road_route(source, destination)
            except ValueError:
                _fail_leg_locked(request, leg, "Road route became unavailable before dispatch.")
                return _view(request)
            deadline_minutes = max(1, int(destination_forecast["days_until_stockout"])) * 1440
            if route["duration_minutes"] >= deadline_minutes:
                _fail_leg_locked(request, leg, "Updated road ETA no longer meets the shortage window.")
                return _view(request)
            arrival_date = date.today() + timedelta(days=max(1, (route["duration_minutes"] + 1439) // 1440))
            for allocation in leg.get("source_batch_allocations", []):
                batch = batches_by_id.get(allocation["batch_id"])
                if (not batch or int(batch["quantity"]) < int(allocation["quantity"])
                        or int(batch.get("committed_quantity", 0)) < int(allocation["quantity"])
                        or date.fromisoformat(batch["expiry_date"]) < arrival_date):
                    _fail_leg_locked(request, leg, "Committed batch expired or stock became unavailable before dispatch.")
                    return _view(request)
            consumed_allocations = []
            for allocation in leg.get("source_batch_allocations", []):
                if not database.consume_batch_quantity(allocation["batch_id"], allocation["quantity"]):
                    _fail_leg_locked(request, leg, "Committed batch stock changed during dispatch.", consumed_allocations)
                    return _view(request)
                consumed_allocations.append(allocation)
            database.release_shareable_quantity(leg["source_hospital_id"], leg["supply_id"], leg["allocated_quantity"], delivered=True)
            leg["status"] = "IN_TRANSIT"
            leg["lifecycle_status"] = "IN_TRANSIT"
            leg.setdefault("picked_up_at", _now())
            leg["in_transit_at"] = leg["picked_up_at"]
            leg["dispatched_at"] = _now()
            leg["route_distance_km"] = route["distance_km"]
            leg["estimated_eta_minutes"] = route["duration_minutes"]
            leg["route"] = route["coordinates"]
            _record_event(request, "IN_TRANSIT", f"{leg['allocated_quantity']} units dispatched from {leg['source_hospital']} to {leg['destination_hospital']}.", leg)
        elif status == "ARRIVED":
            if leg["destination_hospital_id"] != hospital_id or leg["status"] != "IN_TRANSIT":
                raise PermissionError("Only the destination hospital can confirm an in-transit leg has arrived")
            leg["status"] = "ARRIVED"
            leg["lifecycle_status"] = "ARRIVED"
            leg["arrived_at"] = _now()
            _record_event(request, "ARRIVED", f"{leg['allocated_quantity']} units arrived at {leg['destination_hospital']}.", leg)
        elif status == "DELIVERED":
            if leg["destination_hospital_id"] != hospital_id or leg["status"] not in ("IN_TRANSIT", "ARRIVED"):
                raise PermissionError("Only the destination hospital can confirm an arrived or in-transit leg")
            _receive_leg(request, leg)
        else:
            raise ValueError("Supported leg status updates are PICKED_UP, IN_TRANSIT, ARRIVED, and DELIVERED")
        database.save_workflow_record("fulfillment_legs", leg)
        _refresh_request(request)
        if leg["status"] == "DELIVERED":
            _record_event(request, "DELIVERED", f"{leg['allocated_quantity']} units received. Remaining need: {request['remaining_quantity']}.", leg)
        if request["remaining_quantity"]:
            _match_next(request)
        return _view(request)


def fail_leg(request_id: str, leg_id: str, hospital_id: str, reason: str) -> dict[str, Any]:
    with _lock:
        request, leg = _find_leg_for_user(request_id, leg_id, hospital_id)
        if leg["status"] not in ("COMMITTED", "PICKED_UP", "IN_TRANSIT", "ARRIVED"):
            raise ValueError("Only an accepted, picked-up, in-transit, or arrived leg can be marked failed")
        _fail_leg_locked(request, leg, reason.strip() or "Source stock, batch, or route became unavailable.")
        return _view(request)
