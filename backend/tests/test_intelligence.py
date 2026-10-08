from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import patch

from app.demo_data import build_dataset
from app.engine import RISK_THRESHOLDS, _risk, _simulate_fefo_batches, analyze
from app import database
from app import main as main_module
from app.main import ForecastSimulationPayload, ShareablePoolPayload, SurgeryPayload


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

    def test_scheduled_surgeries_adjust_forecast_and_depletion(self) -> None:
        dataset = build_dataset("outbreak")
        surgery = {
            "surgery_id": "SURG-TEST", "hospital_id": "H001",
            "scheduled_date": (date.today() + timedelta(days=1)).isoformat(),
            "surgery_type": "general_surgery", "number_of_cases": 12, "status": "scheduled",
        }
        baseline = analyze(dataset)
        adjusted = analyze(dataset, surgery_schedules=[surgery])
        baseline_row = next(row for row in baseline["forecasts"] if (row["hospital_id"], row["supply_id"]) == ("H001", "MED001"))
        adjusted_row = next(row for row in adjusted["forecasts"] if (row["hospital_id"], row["supply_id"]) == ("H001", "MED001"))

        self.assertGreater(adjusted_row["surgery_additional_demand"], 0)
        self.assertGreater(adjusted_row["forecast_daily_demand"], baseline_row["forecast_daily_demand"])
        self.assertGreater(adjusted_row["simulation"][1]["forecast_demand"], baseline_row["simulation"][1]["forecast_demand"])
        cancelled = analyze(dataset, surgery_schedules=[{**surgery, "status": "cancelled"}])
        cancelled_row = next(row for row in cancelled["forecasts"] if (row["hospital_id"], row["supply_id"]) == ("H001", "MED001"))
        self.assertEqual(cancelled_row["forecast_daily_demand"], baseline_row["forecast_daily_demand"])

    def test_transfers_respect_opt_in_shareable_quantity(self) -> None:
        analysis = analyze(build_dataset("outbreak"), shareable_pool=[{
            "hospital_id": "H002", "supply_id": "MED001", "shareable_quantity": 100, "enabled": True,
        }])
        transfers = [row for row in analysis["transfers"] if row["source_hospital_id"] == "H002" and row["supply_id"] == "MED001"]

        self.assertTrue(transfers)
        self.assertTrue(all(row["recommended_quantity"] <= 100 for row in transfers))
        self.assertTrue(all(row["source_remaining_stock"] >= row["source_reserve"] for row in transfers))
        disabled = analyze(build_dataset("outbreak"), shareable_pool=[{
            "hospital_id": "H002", "supply_id": "MED001", "shareable_quantity": 100, "enabled": False,
        }])
        self.assertFalse(any(row["source_hospital_id"] == "H002" and row["supply_id"] == "MED001" for row in disabled["transfers"]))

    def test_public_hospital_seed_metadata_is_stable(self) -> None:
        hospitals = build_dataset()["hospitals"]
        self.assertEqual([item["display_name"] for item in hospitals], ["KMCH", "PSG Hospitals", "Kumaran Medical Center"])
        self.assertEqual((hospitals[0]["latitude"], hospitals[0]["longitude"]), (11.0430079, 77.0406057))
        self.assertTrue(all(item["demo_data_flag"] and item["active"] for item in hospitals))

    def test_hospital_user_only_sees_own_operations_and_public_hospital_locations(self) -> None:
        database.ACTIVE_DATA = build_dataset("outbreak")
        main_module._cached_analysis = None
        main_module._cached_data_id = None
        user = {"hospital_id": "H002", "role": "hospital_user"}
        scoped = main_module.scoped_analysis(user)

        self.assertEqual({row["hospital_id"] for row in scoped["forecasts"]}, {"H002"})
        self.assertGreater(len(scoped["shortages"]), 0)
        self.assertGreater(len(scoped["transfers"]), 0)
        self.assertEqual(set(scoped["hospitals"]), {"H001", "H002", "H003"})
        self.assertNotIn(("H001", "MED001"), scoped["inventory_totals"])
        self.assertNotIn(("H003", "MED001"), scoped["batches"])

        database.ACTIVE_DATA = build_dataset("outbreak")
        main_module._cached_analysis = None
        main_module._cached_data_id = None

    def test_schedule_pool_nearby_and_weekly_report_api_flow(self) -> None:
        original_data = database.ACTIVE_DATA
        database.ACTIVE_DATA = build_dataset("outbreak")
        main_module._cached_analysis = None
        main_module._cached_data_id = None
        kmch_user = {"hospital_id": "H001", "role": "hospital_user"}
        psg_user = {"hospital_id": "H002", "role": "hospital_user"}
        before = main_module.forecast("H001", "MED001", 14, kmch_user)["data"]
        simulation = main_module.simulate_surgery_forecast(ForecastSimulationPayload(surgery=SurgeryPayload(
            scheduled_date=date.today() + timedelta(days=1), surgery_type="general_surgery",
            number_of_cases=12, expected_duration_minutes=120,
        )), kmch_user)["data"]
        self.assertGreater(simulation["adjusted_forecast_daily_demand"], simulation["baseline_forecast_daily_demand"])
        main_module.create_surgery(SurgeryPayload(
            scheduled_date=date.today() + timedelta(days=1), surgery_type="general_surgery",
            number_of_cases=12, expected_duration_minutes=120,
        ), kmch_user)
        after = main_module.forecast("H001", "MED001", 14, kmch_user)["data"]

        self.assertGreater(after["forecast_daily_demand"], before["forecast_daily_demand"])
        self.assertGreater(after["surgery_additional_units_next_7_days"], 0)
        self.assertEqual(after["forecast_series"][0]["surgery_demand"], 0)
        self.assertGreater(after["forecast_series"][1]["surgery_demand"], 0)
        components = after["forecast_components"]
        self.assertAlmostEqual(
            components["historical_baseline"] + components["existing_model_adjustments"] + components["surgery_additional_daily_average"],
            components["adjusted_daily_forecast"],
            places=1,
        )
        self.assertEqual(len(main_module.surgeries(kmch_user)["data"]), 1)

        main_module.save_shareable_pool(ShareablePoolPayload(supply_id="MED001", shareable_quantity=100, enabled=True), psg_user)
        with patch.object(main_module, "road_route", return_value={
            "coordinates": [[11.0188398, 77.0073136], [11.0430079, 77.0406057]],
            "distance_km": 5.8, "duration_minutes": 19, "provider": "OSRM", "route_available": True,
        }):
            matches = main_module.nearby_supplies("MED001", kmch_user)["data"]
            self.assertTrue(matches)
            psg_match = next(row for row in matches if row["hospital_id"] == "H002")
            self.assertEqual(psg_match["shareable_quantity"], 100)
            self.assertEqual(psg_match["road_distance_km"], 5.8)
            self.assertNotIn("total_stock", psg_match)
            self.assertNotIn("safety_reserve", psg_match)
            self.assertTrue(psg_match["feasible"])
            request = main_module.request_transfer(psg_match["recommendation_id"], kmch_user)["data"]
            self.assertEqual(request["status"], "requested")

            report = main_module.weekly_management_report(date.today(), date.today() + timedelta(days=6), kmch_user)["data"]
            self.assertEqual(report["executive_summary"]["upcoming_surgery_cases"], 12)
            self.assertEqual(report["surgeries"][0]["number_of_cases"], 12)
            self.assertEqual(report["surgeries"][0]["supply_impact"][0]["additional_units"], 18.0)

        database.ACTIVE_DATA = original_data
        main_module._cached_analysis = None
        main_module._cached_data_id = None


if __name__ == "__main__":
    unittest.main()
