from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo import ReturnDocument

from .demo_data import build_dataset

load_dotenv()
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
DATABASE_NAME = os.getenv("DATABASE_NAME", "medisupplyiq")
MONGO_CLIENT: MongoClient | None = None
MONGO_DB: Any = None
ACTIVE_DATA: dict[str, Any] = build_dataset("outbreak")
ACTIVE_SCENARIO = "outbreak"
_SCENARIO_POOL_BACKUP: list[dict[str, Any]] | None = None
DATA_SOURCE = "synthetic demo dataset"
SOURCE_COLLECTIONS = ("hospitals", "supplies", "inventory", "demand_history", "surgery_schedules", "shareable_pool")
DERIVED_COLLECTIONS = ("forecast_results", "redistribution_recommendations", "expiry_risks", "shortage_risks", "alerts")
WORKFLOW_COLLECTIONS = {"supply_requests": "request_id", "fulfillment_legs": "leg_id"}


def initialize_database(force_seed: bool = False) -> dict[str, Any]:
    global MONGO_CLIENT, MONGO_DB, ACTIVE_DATA, DATA_SOURCE
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        if not DEMO_MODE:
            raise RuntimeError("MONGODB_URI is required when DEMO_MODE=false")
        ACTIVE_DATA.setdefault("supply_requests", [])
        ACTIVE_DATA.setdefault("fulfillment_legs", [])
        ACTIVE_DATA["_version"] = int(ACTIVE_DATA.get("_version", 0)) + 1
        return ACTIVE_DATA

    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=1500, connectTimeoutMS=1500)
        client.admin.command("ping")
        database = client[DATABASE_NAME]
        seed = build_dataset("outbreak")
        seeded_hospitals = {item["hospital_id"]: item for item in seed["hospitals"]}
        existing_hospitals = list(database["hospitals"].find({}, {"_id": 0, "hospital_id": 1, "name": 1}))
        refresh_seed = force_seed or not existing_hospitals or {
            item.get("hospital_id") for item in existing_hospitals
        } != set(seeded_hospitals) or any(
            seeded_hospitals.get(item.get("hospital_id"), {}).get("name") != item.get("name")
            for item in existing_hospitals
        )
        if refresh_seed:
            for name in SOURCE_COLLECTIONS:
                database[name].delete_many({})
                if seed[name]:
                    database[name].insert_many(seed[name], ordered=False)
        existing = set(database.list_collection_names())
        for name in (*SOURCE_COLLECTIONS, *DERIVED_COLLECTIONS, *WORKFLOW_COLLECTIONS):
            if name not in existing:
                database.create_collection(name)
        ACTIVE_DATA = {
            name: list(database[name].find({}, {"_id": 0}))
            for name in SOURCE_COLLECTIONS
        }
        for name in WORKFLOW_COLLECTIONS:
            ACTIVE_DATA[name] = list(database[name].find({}, {"_id": 0}))
        for pool in ACTIVE_DATA.get("shareable_pool", []):
            pool.setdefault("committed_quantity", 0)
        ACTIVE_DATA["_version"] = int(ACTIVE_DATA.get("_version", 0)) + 1
        MONGO_CLIENT, MONGO_DB = client, database
        DATA_SOURCE = f"MongoDB: {DATABASE_NAME}"
        return ACTIVE_DATA
    except Exception as error:
        if not DEMO_MODE:
            raise RuntimeError(f"MongoDB connection failed: {error}") from error
        DATA_SOURCE = "synthetic demo dataset (MongoDB unavailable)"
        ACTIVE_DATA = build_dataset("outbreak")
        return ACTIVE_DATA


def set_scenario(scenario: str) -> dict[str, Any]:
    global ACTIVE_DATA, ACTIVE_SCENARIO, _SCENARIO_POOL_BACKUP
    updated = build_dataset(scenario)
    updated["surgery_schedules"] = ACTIVE_DATA.get("surgery_schedules", [])
    if scenario == "kmch_to_psg":
        if ACTIVE_SCENARIO != scenario:
            _SCENARIO_POOL_BACKUP = deepcopy(ACTIVE_DATA.get("shareable_pool", []))
    elif ACTIVE_SCENARIO == "kmch_to_psg" and _SCENARIO_POOL_BACKUP is not None:
        updated["shareable_pool"] = _SCENARIO_POOL_BACKUP
        _SCENARIO_POOL_BACKUP = None
    else:
        updated["shareable_pool"] = ACTIVE_DATA.get("shareable_pool", updated["shareable_pool"])
    updated["supply_requests"] = ACTIVE_DATA.get("supply_requests", [])
    updated["fulfillment_legs"] = ACTIVE_DATA.get("fulfillment_legs", [])
    ACTIVE_DATA = updated
    ACTIVE_SCENARIO = scenario
    ACTIVE_DATA["_version"] = int(ACTIVE_DATA.get("_version", 0)) + 1
    if MONGO_DB is not None:
        for name in SOURCE_COLLECTIONS:
            collection = MONGO_DB[name]
            collection.delete_many({})
            if ACTIVE_DATA[name]:
                collection.insert_many(ACTIVE_DATA[name], ordered=False)
    return ACTIVE_DATA


def save_source_record(collection_name: str, record: dict[str, Any], key: str) -> None:
    if collection_name not in SOURCE_COLLECTIONS:
        raise ValueError(f"Unsupported source collection: {collection_name}")
    records = ACTIVE_DATA.setdefault(collection_name, [])
    existing = next((index for index, item in enumerate(records) if item.get(key) == record.get(key)), None)
    if existing is None:
        records.append(record)
    else:
        records[existing] = record
    ACTIVE_DATA["_version"] = int(ACTIVE_DATA.get("_version", 0)) + 1
    if MONGO_DB is not None:
        MONGO_DB[collection_name].replace_one({key: record[key]}, record, upsert=True)


def remove_source_record(collection_name: str, value: str, key: str) -> None:
    if collection_name not in SOURCE_COLLECTIONS:
        raise ValueError(f"Unsupported source collection: {collection_name}")
    ACTIVE_DATA[collection_name] = [item for item in ACTIVE_DATA.get(collection_name, []) if item.get(key) != value]
    ACTIVE_DATA["_version"] = int(ACTIVE_DATA.get("_version", 0)) + 1
    if MONGO_DB is not None:
        MONGO_DB[collection_name].delete_one({key: value})


def save_workflow_record(collection_name: str, record: dict[str, Any]) -> None:
    key = WORKFLOW_COLLECTIONS.get(collection_name)
    if key is None:
        raise ValueError(f"Unsupported workflow collection: {collection_name}")
    records = ACTIVE_DATA.setdefault(collection_name, [])
    existing = next((index for index, item in enumerate(records) if item.get(key) == record.get(key)), None)
    if existing is None:
        records.append(record)
    else:
        records[existing] = record
    if MONGO_DB is not None:
        MONGO_DB[collection_name].replace_one({key: record[key]}, record, upsert=True)


def claim_leg_dispatch(leg_id: str) -> bool:
    leg = next((row for row in ACTIVE_DATA.get("fulfillment_legs", []) if row["leg_id"] == leg_id), None)
    if leg is None or leg.get("status") not in ("COMMITTED", "PICKED_UP"):
        return False
    if MONGO_DB is not None:
        updated = MONGO_DB["fulfillment_legs"].find_one_and_update(
            {"leg_id": leg_id, "status": leg.get("status")},
            {"$set": {"status": "DISPATCHING"}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            return False
    leg["status"] = "DISPATCHING"
    return True


def claim_leg_receipt(leg_id: str) -> bool:
    leg = next((row for row in ACTIVE_DATA.get("fulfillment_legs", []) if row["leg_id"] == leg_id), None)
    if leg is None or leg.get("status") not in ("IN_TRANSIT", "ARRIVED"):
        return False
    if MONGO_DB is not None:
        updated = MONGO_DB["fulfillment_legs"].find_one_and_update(
            {"leg_id": leg_id, "status": leg.get("status")},
            {"$set": {"status": "RECEIVING"}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            return False
    leg["status"] = "RECEIVING"
    return True


def claim_leg_acceptance(leg_id: str) -> bool:
    leg = next((row for row in ACTIVE_DATA.get("fulfillment_legs", []) if row["leg_id"] == leg_id), None)
    if leg is None or leg.get("status") != "OFFERED":
        return False
    if MONGO_DB is not None:
        updated = MONGO_DB["fulfillment_legs"].find_one_and_update(
            {"leg_id": leg_id, "status": "OFFERED"},
            {"$set": {"status": "ACCEPTING"}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            return False
    leg["status"] = "ACCEPTING"
    return True


def reserve_shareable_quantity(hospital_id: str, supply_id: str, quantity: int, safe_surplus: int) -> bool:
    pool = next((row for row in ACTIVE_DATA.get("shareable_pool", [])
                 if row["hospital_id"] == hospital_id and row["supply_id"] == supply_id and row.get("enabled")), None)
    if pool is None:
        return False
    committed = int(pool.get("committed_quantity", 0))
    if committed + quantity > min(int(pool["shareable_quantity"]), safe_surplus):
        return False
    if MONGO_DB is not None:
        updated = MONGO_DB["shareable_pool"].find_one_and_update(
            {"hospital_id": hospital_id, "supply_id": supply_id, "enabled": True,
             "$expr": {"$and": [
                 {"$lte": [{"$add": [{"$ifNull": ["$committed_quantity", 0]}, quantity]}, "$shareable_quantity"]},
                 {"$lte": [{"$add": [{"$ifNull": ["$committed_quantity", 0]}, quantity]}, safe_surplus]},
             ]}},
            {"$inc": {"committed_quantity": quantity}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            return False
        pool["committed_quantity"] = int(updated.get("committed_quantity", 0))
        ACTIVE_DATA["_version"] = int(ACTIVE_DATA.get("_version", 0)) + 1
        return True
    pool["committed_quantity"] = committed + quantity
    ACTIVE_DATA["_version"] = int(ACTIVE_DATA.get("_version", 0)) + 1
    return True


def reserve_batch_quantity(batch_id: str, quantity: int) -> bool:
    batch = next((row for row in ACTIVE_DATA.get("inventory", []) if row["batch_id"] == batch_id), None)
    if batch is None:
        return False
    committed = int(batch.get("committed_quantity", 0))
    if committed + quantity > int(batch["quantity"]):
        return False
    if MONGO_DB is not None:
        updated = MONGO_DB["inventory"].find_one_and_update(
            {"batch_id": batch_id,
             "$expr": {"$lte": [{"$add": [{"$ifNull": ["$committed_quantity", 0]}, quantity]}, "$quantity"]}},
            {"$inc": {"committed_quantity": quantity}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            return False
        batch["committed_quantity"] = int(updated.get("committed_quantity", 0))
        ACTIVE_DATA["_version"] = int(ACTIVE_DATA.get("_version", 0)) + 1
        return True
    batch["committed_quantity"] = committed + quantity
    ACTIVE_DATA["_version"] = int(ACTIVE_DATA.get("_version", 0)) + 1
    return True


def release_batch_quantity(batch_id: str, quantity: int) -> None:
    batch = next((row for row in ACTIVE_DATA.get("inventory", []) if row["batch_id"] == batch_id), None)
    if batch is None:
        return
    if MONGO_DB is not None:
        result = MONGO_DB["inventory"].update_one(
            {"batch_id": batch_id, "committed_quantity": {"$gte": quantity}},
            {"$inc": {"committed_quantity": -quantity}},
        )
        if not result.modified_count:
            return
    batch["committed_quantity"] = max(0, int(batch.get("committed_quantity", 0)) - quantity)
    ACTIVE_DATA["_version"] = int(ACTIVE_DATA.get("_version", 0)) + 1


def consume_batch_quantity(batch_id: str, quantity: int) -> bool:
    batch = next((row for row in ACTIVE_DATA.get("inventory", []) if row["batch_id"] == batch_id), None)
    if batch is None or int(batch.get("committed_quantity", 0)) < quantity or int(batch["quantity"]) < quantity:
        return False
    if MONGO_DB is not None:
        result = MONGO_DB["inventory"].update_one(
            {"batch_id": batch_id, "quantity": {"$gte": quantity}, "committed_quantity": {"$gte": quantity}},
            {"$inc": {"quantity": -quantity, "committed_quantity": -quantity}},
        )
        if not result.modified_count:
            return False
    batch["quantity"] -= quantity
    batch["committed_quantity"] = max(0, int(batch.get("committed_quantity", 0)) - quantity)
    ACTIVE_DATA["_version"] = int(ACTIVE_DATA.get("_version", 0)) + 1
    return True


def release_shareable_quantity(hospital_id: str, supply_id: str, quantity: int, delivered: bool = False) -> None:
    pool = next((row for row in ACTIVE_DATA.get("shareable_pool", [])
                 if row["hospital_id"] == hospital_id and row["supply_id"] == supply_id), None)
    if pool is None:
        return
    change = {"committed_quantity": -quantity}
    if delivered:
        change["shareable_quantity"] = -quantity
    if MONGO_DB is not None:
        result = MONGO_DB["shareable_pool"].update_one(
            {"hospital_id": hospital_id, "supply_id": supply_id,
             "committed_quantity": {"$gte": quantity},
             **({"shareable_quantity": {"$gte": quantity}} if delivered else {})},
            {"$inc": change},
        )
        if not result.modified_count:
            return
    pool["committed_quantity"] = max(0, int(pool.get("committed_quantity", 0)) - quantity)
    if delivered:
        pool["shareable_quantity"] = max(0, int(pool["shareable_quantity"]) - quantity)
    ACTIVE_DATA["_version"] = int(ACTIVE_DATA.get("_version", 0)) + 1


def persist_analysis(analysis: dict[str, Any]) -> None:
    if MONGO_DB is None:
        return
    alerts = [
        {"alert_id": f"SHORT-{index:04}", "type": "shortage", **item}
        for index, item in enumerate(analysis["shortages"], start=1)
    ]
    alerts.extend(
        {"alert_id": f"EXPIRY-{index:04}", "type": "expiry", **item}
        for index, item in enumerate(analysis["expiry_risks"], start=1)
    )
    documents = {
        "forecast_results": analysis["forecasts"],
        "redistribution_recommendations": analysis["transfers"],
        "expiry_risks": analysis["expiry_risks"],
        "shortage_risks": analysis["shortages"],
        "alerts": alerts,
    }
    for name, records in documents.items():
        collection = MONGO_DB[name]
        collection.delete_many({})
        if records:
            collection.insert_many(records, ordered=False)
