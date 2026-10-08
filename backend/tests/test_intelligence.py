from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import date, timedelta

from app.demo_data import build_dataset
from app.engine import RISK_THRESHOLDS, _risk, _simulate_fefo_batches, analyze


class IntelligenceTests(unittest.TestCase):
    def test_mock_records_have_references_and_nonnegative_values(self) -> None:
        dataset = build_dataset("normal")
        hospital_ids = {item["hospital_id"] for item in dataset["hospitals"]}
        supply_ids = {item["supply_id"] for item in dataset["supplies"]}
        self.assertEqual(len(hospital_ids), 3)
        self.assertGreaterEqual(len(supply_ids), 10)
        self.assertGreaterEqual(len(dataset["inventory"]), 60)
        self.assertGreaterEqual(len(dataset["demand_history"]), 3_000)
        for batch in dataset["inventory"]:
            self.assertIn(batch["hospital_id"], hospital_ids)
            self.assertIn(batch["supply_id"], supply_ids)
            self.assertGreaterEqual(batch["quantity"], 0)
            date.fromisoformat(batch["expiry_date"])
        for observation in dataset["demand_history"]:
            self.assertIn(observation["hospital_id"], hospital_ids)
            self.assertIn(observation["supply_id"], supply_ids)
            self.assertGreaterEqual(observation["quantity_used"], 0)
            date.fromisoformat(observation["date"])

    def test_depletion_uses_configured_safety_thresholds(self) -> None:
        self.assertEqual(_risk(2, 0.1), "CRITICAL")
        self.assertEqual(_risk(3, 0.1), "HIGH")
        self.assertEqual(_risk(6, 0.1), "MEDIUM")
        self.assertEqual(_risk(RISK_THRESHOLDS["medium_days"] + 1, 0.99), "LOW")

    def test_outbreak_increases_demand_and_moves_safety_breach_earlier(self) -> None:
        target_key = ("H001", "MED001")
        rows = {}
        for scenario in ("normal", "outbreak"):
            result = analyze(build_dataset(scenario))
            rows[scenario] = next(item for item in result["forecasts"] if (item["hospital_id"], item["supply_id"]) == target_key)
        self.assertGreater(rows["outbreak"]["forecast_daily_demand"], rows["normal"]["forecast_daily_demand"])
        self.assertLess(rows["outbreak"]["days_until_stockout"], rows["normal"]["days_until_stockout"])
        self.assertEqual(rows["outbreak"]["risk_level"], "CRITICAL")
        self.assertGreaterEqual(rows["outbreak"]["days_until_zero_stock"], rows["outbreak"]["days_until_stockout"])

    def test_fefo_simulation_consumes_earliest_lot_first(self) -> None:
        today = date.today()
        lots = [
            {"batch_id": "A", "quantity": 200, "expiry_date": (today + timedelta(days=10)).isoformat()},
            {"batch_id": "B", "quantity": 200, "expiry_date": (today + timedelta(days=20)).isoformat()},
        ]
        result = _simulate_fefo_batches(lots, daily_demand=10, trend=0, today=today)
        self.assertEqual(result["A"], {"expected_usage": 100, "expected_waste": 100})
        self.assertEqual(result["B"], {"expected_usage": 100, "expected_waste": 100})

    def test_expired_quantity_is_not_usable_stock(self) -> None:
        dataset = build_dataset("normal")
        baseline = analyze(dataset)
        expired = deepcopy(next(item for item in dataset["inventory"] if item["hospital_id"] == "H001" and item["supply_id"] == "MED001"))
        expired.update({"inventory_id": "EXPIRED-TEST", "batch_id": "EXPIRED-TEST", "quantity": 5000,
                        "expiry_date": (date.today() - timedelta(days=2)).isoformat(), "batch_status": "expired"})
        dataset["inventory"].append(expired)
        updated = analyze(dataset)
        before = next(item["current_stock"] for item in baseline["forecasts"] if item["hospital_id"] == "H001" and item["supply_id"] == "MED001")
        after = next(item["current_stock"] for item in updated["forecasts"] if item["hospital_id"] == "H001" and item["supply_id"] == "MED001")
        expiry_row = next(item for item in updated["expiry_risks"] if item["batch_id"] == "EXPIRED-TEST")
        self.assertEqual(before, after)
        self.assertEqual(expiry_row["expected_waste"], 5000)

    def test_transfer_is_dynamic_safe_and_fefo_ordered(self) -> None:
        transfers_by_scenario = {}
        for scenario in ("normal", "outbreak"):
            analysis = analyze(build_dataset(scenario))
            for item in analysis["transfers"]:
                self.assertGreaterEqual(item["source_remaining_stock"], item["source_safety_stock"])
                self.assertGreater(item["destination_expected_coverage_days"], item["destination_coverage_before_days"])
                self.assertEqual(sum(batch["quantity"] for batch in item["source_batch_allocations"]), item["recommended_quantity"])
            transfers_by_scenario[scenario] = next(
                item for item in analysis["transfers"]
                if item["source_hospital_id"] == "H002" and item["destination_hospital_id"] == "H001" and item["supply_id"] == "MED001"
            )
        self.assertNotEqual(transfers_by_scenario["normal"]["recommended_quantity"], transfers_by_scenario["outbreak"]["recommended_quantity"])
        self.assertLessEqual(transfers_by_scenario["outbreak"]["recommended_quantity"], transfers_by_scenario["outbreak"]["destination_need"])

    def test_priority_components_are_explainable_and_sum_to_score(self) -> None:
        analysis = analyze(build_dataset("outbreak"))
        item = next(row for row in analysis["priorities"] if row["hospital_id"] == "H001" and row["supply_id"] == "MED001")
        self.assertEqual(sum(item["score_components"].values()), item["priority_score"])
        self.assertIn("alternative_availability", item["score_components"])
        self.assertIn("supply_criticality", item["score_components"])
        self.assertTrue(item["reasons"])


if __name__ == "__main__":
    unittest.main()
