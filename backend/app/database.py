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
SOURCE_COLLECTIONS = ("hospitals", "supplies", "inventory", "demand_history")
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
        if force_seed or database["hospitals"].count_documents({}) == 0:
            for name in ("hospitals", "supplies", "inventory", "demand_history"):
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
    ACTIVE_DATA = build_dataset(scenario)
    if MONGO_DB is not None:
        for name in SOURCE_COLLECTIONS:
            collection = MONGO_DB[name]
            collection.delete_many({})
            if ACTIVE_DATA[name]:
                collection.insert_many(ACTIVE_DATA[name], ordered=False)
    return ACTIVE_DATA


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
