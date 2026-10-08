from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from pymongo import MongoClient

from .demo_data import build_dataset

load_dotenv()
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
DATABASE_NAME = os.getenv("DATABASE_NAME", "medisupplyiq")
MONGO_CLIENT: MongoClient | None = None
MONGO_DB: Any = None
ACTIVE_DATA: dict[str, Any] = build_dataset("outbreak")
DATA_SOURCE = "synthetic demo dataset"
SOURCE_COLLECTIONS = ("hospitals", "supplies", "inventory", "demand_history", "surgery_schedules", "shareable_pool")
DERIVED_COLLECTIONS = ("forecast_results", "redistribution_recommendations", "expiry_risks", "shortage_risks", "alerts")


def initialize_database(force_seed: bool = False) -> dict[str, Any]:
    global MONGO_CLIENT, MONGO_DB, ACTIVE_DATA, DATA_SOURCE
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        if not DEMO_MODE:
            raise RuntimeError("MONGODB_URI is required when DEMO_MODE=false")
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
        for name in (*SOURCE_COLLECTIONS, *DERIVED_COLLECTIONS):
            if name not in existing:
                database.create_collection(name)
        ACTIVE_DATA = {
            name: list(database[name].find({}, {"_id": 0}))
            for name in SOURCE_COLLECTIONS
        }
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
    global ACTIVE_DATA
    updated = build_dataset(scenario)
    updated["surgery_schedules"] = ACTIVE_DATA.get("surgery_schedules", [])
    updated["shareable_pool"] = ACTIVE_DATA.get("shareable_pool", updated["shareable_pool"])
    ACTIVE_DATA = updated
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
    if MONGO_DB is not None:
        MONGO_DB[collection_name].replace_one({key: record[key]}, record, upsert=True)


def remove_source_record(collection_name: str, value: str, key: str) -> None:
    if collection_name not in SOURCE_COLLECTIONS:
        raise ValueError(f"Unsupported source collection: {collection_name}")
    ACTIVE_DATA[collection_name] = [item for item in ACTIVE_DATA.get(collection_name, []) if item.get(key) != value]
    if MONGO_DB is not None:
        MONGO_DB[collection_name].delete_one({key: value})


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
