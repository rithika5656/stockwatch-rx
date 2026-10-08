from __future__ import annotations

from datetime import date, timedelta
from typing import Any

HOSPITAL_SEEDS = [
    ("Coimbatore Central Hospital", "Coimbatore", "Tamil Nadu", 11.0168, 76.9558),
    ("Coimbatore Emergency Medical Center", "Coimbatore", "Tamil Nadu", 11.0302, 76.9550),
    ("Coimbatore Regional Hospital", "Coimbatore", "Tamil Nadu", 11.0456, 76.9710),
]

SUPPLY_SEEDS = [
    ("Normal Saline 500ml", "IV Fluids", "bags", 1.0, "high"),
    ("Ringer's Lactate 500ml", "IV Fluids", "bags", 0.7, "high"),
    ("Dextrose 5% 500ml", "IV Fluids", "bags", 0.55, "medium"),
    ("Emergency Antibiotic X", "Antibiotics", "vials", 0.38, "critical"),
    ("Ceftriaxone 1g", "Antibiotics", "vials", 0.75, "high"),
    ("Adrenaline 1mg", "Emergency Medicines", "ampoules", 0.12, "critical"),
    ("Oxygen Mask Adult", "Emergency Medicines", "units", 0.3, "high"),
    ("Surgical Gloves (pair)", "PPE", "pairs", 5.0, "medium"),
    ("Syringe 5ml", "Surgical Supplies", "units", 3.5, "medium"),
    ("Rapid Diagnostic Kit", "Diagnostic Supplies", "kits", 0.45, "high"),
    ("Blood Glucose Strips", "Diagnostic Supplies", "strips", 1.1, "medium"),
    ("Oxygen Tubing Set", "Emergency Medicines", "units", 0.28, "high"),
    ("Antivenom 10ml", "Emergency Medicines", "vials", 0.04, "critical"),
]


def _variation(*values: int) -> float:
    return 0.82 + (sum(values) % 37) / 100


def validate_dataset(dataset: dict[str, Any]) -> None:
    hospital_ids = [item["hospital_id"] for item in dataset["hospitals"]]
    supply_ids = [item["supply_id"] for item in dataset["supplies"]]
    if len(hospital_ids) != len(set(hospital_ids)) or len(supply_ids) != len(set(supply_ids)):
        raise ValueError("Hospital and supply IDs must be unique")
    hospitals, supplies = set(hospital_ids), set(supply_ids)
    for batch in dataset["inventory"]:
        if batch["hospital_id"] not in hospitals or batch["supply_id"] not in supplies:
            raise ValueError(f"Inventory {batch.get('inventory_id')} references an unknown hospital or supply")
        if batch["quantity"] < 0 or batch["safety_stock"] < 0 or batch["reorder_level"] < 0:
            raise ValueError(f"Inventory {batch.get('inventory_id')} contains a negative stock value")
        expiry = date.fromisoformat(batch["expiry_date"])
        date.fromisoformat(batch["last_restock_date"])
        expected_status = "expired" if expiry < date.today() else "active"
        if batch["batch_status"] != expected_status:
            raise ValueError(f"Inventory {batch.get('inventory_id')} has an inconsistent batch status")
    for observation in dataset["demand_history"]:
        if observation["hospital_id"] not in hospitals or observation["supply_id"] not in supplies:
            raise ValueError("Demand history references an unknown hospital or supply")
        if observation["quantity_used"] < 0 or not 0 <= observation["outbreak_signal"] <= 1:
            raise ValueError("Demand history contains invalid consumption or outbreak values")
        date.fromisoformat(observation["date"])


def build_dataset(scenario: str = "redistribution") -> dict[str, Any]:
    today = date.today()
    hospitals = []
    for index, seed in enumerate(HOSPITAL_SEEDS, start=1):
        name, city, state, latitude, longitude = seed
        occupancy = round(0.58 + (index * 13 % 36) / 100, 2)
        bed_capacity = 180 + (index * 47 % 560)
        hospitals.append({
            "hospital_id": f"H{index:03}", "name": name, "city": city, "state": state,
            "latitude": latitude, "longitude": longitude,
            "bed_capacity": bed_capacity,
            "current_occupancy": round(bed_capacity * occupancy), "occupancy_rate": occupancy,
            "emergency_load": round(0.28 + (index * 17 % 65) / 100, 2),
            "criticality_level": "critical" if occupancy >= 0.88 else "high" if occupancy >= 0.78 else "standard",
        })

    supplies = []
    for index, seed in enumerate(SUPPLY_SEEDS, start=1):
        name, category, unit, base_daily, criticality = seed
        supplies.append({
            "supply_id": f"MED{index:03}", "name": name, "category": category,
            "unit": unit, "base_daily_demand": base_daily, "criticality": criticality,
            "shelf_life_days": 365 if category == "Vaccines" else 730 if category in {"Essential Medicines", "Antibiotics", "IV Fluids"} else 1095,
            "alternative_available": index not in {4, 8, 9, 24},
            "alternative_supply_id": {1: "MED002", 2: "MED001", 4: "MED005", 5: "MED004"}.get(index),
            "max_transfer_quantity": 750 if index == 1 else max(100, round(base_daily * 300 * 2)),
        })

    inventory = []
    demand_history = []
    for hospital_index, hospital in enumerate(hospitals, start=1):
        patient_factor = hospital["bed_capacity"] * hospital["occupancy_rate"] / 350
        for supply_index, supply in enumerate(supplies, start=1):
            daily = max(0.6, supply["base_daily_demand"] * patient_factor * _variation(hospital_index, supply_index) * 300)
            if supply_index == 1 and hospital["hospital_id"] == "H001":
                total_stock = 900
                safety_stock = 250
            elif supply_index == 1 and hospital["hospital_id"] == "H002":
                total_stock = 2500
                safety_stock = 800
            elif supply_index == 1 and hospital["hospital_id"] == "H003":
                total_stock = 600
                safety_stock = 500
            else:
                coverage = 17 + (hospital_index * 11 + supply_index * 7) % 43
                total_stock = max(30, round(daily * coverage))
                safety_stock = max(12, round(daily * (5 + (supply_index % 5))))

            expiry_offset = 30 + (hospital_index * 19 + supply_index * 23) % 300
            if scenario == "expiry" and hospital["hospital_id"] in {"H001", "H002", "H003"} and supply_index in {1, 4, 6, 9}:
                expiry_offset = 8 + (hospital_index + supply_index) % 9
            elif hospital["hospital_id"] == "H001" and supply_index == 1:
                expiry_offset = 23
            first_batch = round(total_stock * (0.36 + (hospital_index + supply_index) % 30 / 100))
            first_expiry = today + timedelta(days=expiry_offset)
            second_expiry = today + timedelta(days=expiry_offset + 70 + supply_index % 35)
            supplier_lead_time = 6 + supply_index % 9
            last_restock = today - timedelta(days=(hospital_index * 3 + supply_index * 2) % 30)
            storage = "cold_chain" if supply["category"] == "Vaccines" else "controlled_dry" if supply["category"] in {"Essential Medicines", "Antibiotics"} else "ambient"
            inventory.extend([
                {"inventory_id": f"INV{hospital_index:03}{supply_index:03}A", "hospital_id": hospital["hospital_id"],
                 "supply_id": supply["supply_id"], "batch_id": f"B{hospital_index:03}{supply_index:03}A",
                 "quantity": first_batch, "safety_stock": safety_stock, "expiry_date": first_expiry.isoformat(),
                 "reorder_level": round(safety_stock + daily * supplier_lead_time), "supplier_id": f"SUP{supply_index:03}",
                 "supplier_lead_time": supplier_lead_time, "supplier_lead_days": supplier_lead_time,
                 "last_restock_date": last_restock.isoformat(), "storage_condition": storage,
                 "batch_status": "active" if first_expiry >= today else "expired"},
                {"inventory_id": f"INV{hospital_index:03}{supply_index:03}B", "hospital_id": hospital["hospital_id"],
                 "supply_id": supply["supply_id"], "batch_id": f"B{hospital_index:03}{supply_index:03}B",
                 "quantity": total_stock - first_batch, "safety_stock": safety_stock, "expiry_date": second_expiry.isoformat(),
                 "reorder_level": round(safety_stock + daily * supplier_lead_time), "supplier_id": f"SUP{supply_index:03}",
                 "supplier_lead_time": supplier_lead_time, "supplier_lead_days": supplier_lead_time,
                 "last_restock_date": last_restock.isoformat(), "storage_condition": storage,
                 "batch_status": "active" if second_expiry >= today else "expired"},
            ])

            for day_index in range(90):
                day = today - timedelta(days=89 - day_index)
                weekly = 1 + 0.08 * (1 if day.weekday() in (0, 1) else -1)
                trend = 1 + day_index * ((supply_index % 5) - 2) / 1800
                spike = 1.0
                outbreak = 0.0
                target_spike = hospital["hospital_id"] == "H001" and supply_index in (1, 4, 9)
                if scenario == "outbreak" and target_spike and day_index >= 83:
                    spike, outbreak = 2.6, 0.8
                elif scenario == "critical" and target_spike and day_index >= 76:
                    spike, outbreak = 1.9, 0.75
                elif scenario == "redistribution" and target_spike and day_index >= 83:
                    spike, outbreak = 1.3, 0.55
                noise = 0.91 + ((day_index * 7 + hospital_index * 3 + supply_index * 11) % 20) / 100
                seasonality = weekly * (1.12 if day.month in {6, 7, 8} else 1.0)
                historical_patient_load = min(0.99, hospital["occupancy_rate"] * (1 + outbreak * 0.12))
                demand_history.append({
                    "hospital_id": hospital["hospital_id"], "supply_id": supply["supply_id"],
                    "date": day.isoformat(), "quantity_used": round(daily * seasonality * trend * spike * noise, 1),
                    "patient_load": round(historical_patient_load, 3),
                    "emergency_cases": round(hospital["emergency_load"] * hospital["bed_capacity"] * spike * 0.08),
                    "seasonality_factor": round(seasonality, 3),
                    "outbreak_signal": outbreak,
                })

    dataset = {"hospitals": hospitals, "supplies": supplies, "inventory": inventory, "demand_history": demand_history}
    validate_dataset(dataset)
    return dataset
