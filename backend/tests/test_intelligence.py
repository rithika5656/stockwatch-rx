from __future__ import annotations

import asyncio
import json
import unittest
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import urlsplit

from app.demo_data import build_dataset
from app.engine import RISK_THRESHOLDS, _risk, _simulate_fefo_batches, analyze
from app.assistant_service import AssistantTools, LLMProvider, RuleBasedProvider, get_assistant_provider
from app import database, fulfillment
from app import main as main_module
from app.main import ForecastSimulationPayload, ShareablePoolPayload, SurgeryPayload, TransferSimulationPayload


def _asgi_request(method: str, url: str, body: dict | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], dict]:
    parsed_url = urlsplit(url)
    body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
    request_headers = {"host": "testserver", **(headers or {})}
    if body is not None:
        request_headers.setdefault("content-type", "application/json")
    incoming = [{"type": "http.request", "body": body_bytes, "more_body": False}]
    outgoing: list[dict] = []

    async def receive() -> dict:
        if incoming:
            return incoming.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        outgoing.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": parsed_url.path, "raw_path": parsed_url.path.encode("ascii"),
        "query_string": parsed_url.query.encode("ascii"), "root_path": "",
        "headers": [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in request_headers.items()],
        "client": ("testclient", 123), "server": ("testserver", 80),
    }
    asyncio.run(main_module.app(scope, receive, send))
    response_start = next(message for message in outgoing if message["type"] == "http.response.start")
    response_body = b"".join(message.get("body", b"") for message in outgoing if message["type"] == "http.response.body")
    response_headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in response_start["headers"]}
    return response_start["status"], response_headers, json.loads(response_body or b"{}")


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

    def test_invalid_demo_tokens_are_rejected(self) -> None:
        from app.auth_service import user_from_token

        with self.assertRaisesRegex(ValueError, "invalid or expired"):
            user_from_token("Bearer invalid-token")
        with self.assertRaisesRegex(ValueError, "Authentication is required"):
            user_from_token(None)

    def test_each_hospital_login_keeps_its_own_authenticated_context(self) -> None:
        from app.auth_service import login, user_from_token
        from fastapi import HTTPException

        for hospital_id in ("H001", "H002", "H003"):
            session = login(hospital_id, "demo123")
            self.assertIsNotNone(session)
            current_user = user_from_token(f"Bearer {session['access_token']}")
            self.assertEqual(current_user["hospital_id"], hospital_id)
        with self.assertRaises(HTTPException) as missing:
            main_module.get_current_user(None)
        self.assertEqual(missing.exception.status_code, 401)
        with self.assertRaises(HTTPException) as invalid:
            main_module.get_current_user("Bearer invalid-token")
        self.assertEqual(invalid.exception.status_code, 401)

    def test_advanced_analysis_routes_are_removed(self) -> None:
        retired_paths = {
            "/api/forecast/accuracy", "/api/anomalies", "/api/spillover", "/api/replay/30-day",
        }
        registered_paths = {route.path for route in main_module.app.routes}
        self.assertFalse(retired_paths & registered_paths)

    def test_assistant_inventory_tool_returns_analysis_totals(self) -> None:
        analysis = analyze(build_dataset("normal"))
        tools = AssistantTools(analysis)

        result = tools.call("get_inventory", {"hospital": "H001", "supply": "Normal Saline 500ml"})

        self.assertEqual(result["current_stock"], analysis["inventory_totals"][("H001", "MED001")])
        self.assertEqual(result["supply_id"], "MED001")

    def test_assistant_inventory_tool_handles_unknown_hospital(self) -> None:
        tools = AssistantTools(analyze(build_dataset("normal")))

        result = tools.call("get_inventory", {"hospital": "Unknown Hospital", "supply": "MED001"})

        self.assertEqual(result["error"], "Unknown hospital: Unknown Hospital")

    def test_assistant_uses_rule_fallback_without_llm_key(self) -> None:
        analysis = analyze(build_dataset("normal"))
        with patch.dict("os.environ", {"LLM_API_KEY": "", "LLM_PROVIDER": ""}):
            provider = get_assistant_provider()
            result = provider.answer("Which supplies are at highest risk?", analysis)

        self.assertIsInstance(provider, RuleBasedProvider)
        self.assertEqual(result["mode"], "RULE-BASED")
        self.assertTrue(result["answer"])

    def test_assistant_fallback_resolves_my_hospital_and_partial_supply_name(self) -> None:
        analysis = analyze(build_dataset("normal"))
        scoped = dict(analysis)
        scoped["forecasts"] = [row for row in analysis["forecasts"] if row["hospital_id"] == "H002"]

        result = RuleBasedProvider().answer(
            "What happens if Normal Saline demand increases by 20% at my hospital?", scoped,
        )

        self.assertEqual(result["sources"][0]["tool"], "simulate_demand_increase")
        self.assertEqual(result["sources"][0]["arguments"]["hospital"], "H002")
        self.assertEqual(result["sources"][0]["arguments"]["supply"], "MED001")

    def test_all_suggested_assistant_questions_return_200_for_every_hospital(self) -> None:
        original_data = database.ACTIVE_DATA
        original_cache = main_module._cached_analysis
        original_cache_id = main_module._cached_data_id
        database.ACTIVE_DATA = build_dataset("outbreak")
        main_module._cached_analysis = None
        main_module._cached_data_id = None
        prompts = [
            "Which supplies are at highest risk at my hospital?",
            "Which local batches expire soon?",
            "Why should H002 transfer saline to H001?",
            "What happens if Normal Saline demand increases by 20% at my hospital?",
        ]
        try:
            with patch.dict("os.environ", {"LLM_API_KEY": "", "LLM_PROVIDER": ""}):
                for hospital_id in ("H001", "H002", "H003"):
                    login_status, _, login_response = _asgi_request(
                        "POST", "/api/auth/login", {"hospital_id": hospital_id, "password": "demo123"},
                    )
                    self.assertEqual(login_status, 200)
                    token = login_response["data"]["access_token"]
                    for prompt in prompts:
                        status, _, response = _asgi_request(
                            "POST", "/api/assistant/query", {"question": prompt},
                            {"authorization": f"Bearer {token}"},
                        )
                        self.assertEqual(status, 200, f"{hospital_id}: {prompt}: {response}")
                        self.assertTrue(response["data"]["answer"])
        finally:
            database.ACTIVE_DATA = original_data
            main_module._cached_analysis = original_cache
            main_module._cached_data_id = original_cache_id

    def test_unhandled_api_error_includes_cors_and_visible_message(self) -> None:
        original_data = database.ACTIVE_DATA
        database.ACTIVE_DATA = build_dataset("normal")
        main_module._cached_analysis = None
        main_module._cached_data_id = None
        try:
            login_status, _, login_response = _asgi_request(
                "POST", "/api/auth/login", {"hospital_id": "H001", "password": "demo123"},
            )
            self.assertEqual(login_status, 200)
            token = login_response["data"]["access_token"]
            with patch.object(main_module, "scoped_analysis", side_effect=RuntimeError("visible synthetic failure")):
                status, headers, response = _asgi_request(
                    "GET", "/api/forecast?hospital_id=H001&supply_id=MED001",
                    headers={"authorization": f"Bearer {token}", "origin": "http://localhost:5178"},
                )
            self.assertEqual(status, 500)
            self.assertEqual(headers.get("access-control-allow-origin"), "http://localhost:5178")
            self.assertEqual(response["detail"]["message"], "visible synthetic failure")
        finally:
            database.ACTIVE_DATA = original_data
            main_module._cached_analysis = None
            main_module._cached_data_id = None

    def test_assistant_provider_error_returns_200_fallback(self) -> None:
        original_data = database.ACTIVE_DATA
        database.ACTIVE_DATA = build_dataset("normal")
        main_module._cached_analysis = None
        main_module._cached_data_id = None

        class FailingProvider:
            def answer(self, _question: str, _analysis: dict) -> dict:
                raise RuntimeError("injected assistant failure")

        try:
            login_status, _, login_response = _asgi_request(
                "POST", "/api/auth/login", {"hospital_id": "H001", "password": "demo123"},
            )
            self.assertEqual(login_status, 200)
            token = login_response["data"]["access_token"]
            with patch.object(main_module, "get_assistant_provider", return_value=FailingProvider()):
                status, _, response = _asgi_request(
                    "POST", "/api/assistant/query", {"question": "Which supplies are at highest risk?"},
                    {"authorization": f"Bearer {token}"},
                )
            self.assertEqual(status, 200)
            self.assertEqual(response["data"]["answer"], "I could not answer that")
            self.assertEqual(response["data"]["sources"], [])
        finally:
            database.ACTIVE_DATA = original_data
            main_module._cached_analysis = None
            main_module._cached_data_id = None

    def test_llm_provider_calls_analysis_tool_and_reports_grounded_numbers(self) -> None:
        analysis = analyze(build_dataset("normal"))
        stock = analysis["inventory_totals"][("H001", "MED001")]
        responses = [
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "call-1", "type": "function", "function": {
                    "name": "get_inventory", "arguments": '{"hospital":"H001","supply":"MED001"}',
                },
            }]}}]},
            {"choices": [{"message": {"role": "assistant", "content": f"KMCH has {stock} units in stock."}}]},
        ]
        provider = LLMProvider("test-key", "openai")
        with patch.object(provider, "_request", side_effect=responses):
            result = provider.answer("How much saline does KMCH have?", analysis)

        self.assertEqual(result["mode"], "LLM")
        self.assertIn(str(stock), result["answer"])
        self.assertEqual(result["sources"][0]["tool"], "get_inventory")

    def test_llm_request_error_falls_back_to_rules(self) -> None:
        analysis = analyze(build_dataset("normal"))
        with patch.dict("os.environ", {"LLM_API_KEY": "test-key", "LLM_PROVIDER": "openai"}):
            provider = get_assistant_provider()
        with patch.object(provider, "_request", side_effect=RuntimeError("provider unavailable")):
            result = provider.answer("Which supplies are at highest risk?", analysis)

        self.assertEqual(result["mode"], "RULE-BASED")
        self.assertTrue(result["answer"])

    def test_depletion_uses_configured_safety_thresholds(self) -> None:
        self.assertEqual(_risk(2), "CRITICAL")
        self.assertEqual(_risk(3), "HIGH")
        self.assertEqual(_risk(6), "MEDIUM")
        self.assertEqual(_risk(RISK_THRESHOLDS["medium_days"] + 1), "LOW")

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
        self.assertEqual(rows["outbreak"]["forecast_method"], "weighted_moving_average")
        self.assertNotIn("shortage_probability", rows["outbreak"])

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
            outbound_by_source: dict[tuple[str, str], int] = defaultdict(int)
            surplus_by_source: dict[tuple[str, str], int] = {}
            for item in analysis["transfers"]:
                self.assertGreaterEqual(item["source_remaining_stock"], item["source_safety_stock"])
                self.assertGreater(item["destination_expected_coverage_days"], item["destination_coverage_before_days"])
                self.assertEqual(sum(batch["quantity"] for batch in item["source_batch_allocations"]), item["recommended_quantity"])
                source_key = (item["source_hospital_id"], item["supply_id"])
                outbound_by_source[source_key] += item["recommended_quantity"]
                surplus_by_source[source_key] = item["source_stock_before"] - item["source_reserve"]
            for source_key, quantity in outbound_by_source.items():
                self.assertLessEqual(quantity, max(0, surplus_by_source[source_key]))
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
        self.assertEqual(after["forecast_method"], "weighted_moving_average")
        self.assertNotIn("forecast_uncertainty", after)
        self.assertNotIn("reconciliation", after)
        self.assertGreater(after["surgery_additional_units_next_7_days"], 0)
        self.assertEqual(after["forecast_series"][0]["surgery_demand"], 0)
        self.assertGreater(after["forecast_series"][1]["surgery_demand"], 0)
        self.assertEqual(set(after["forecast_series"][0]), {"day", "demand", "surgery_demand"})
        components = after["forecast_components"]
        self.assertAlmostEqual(
            components["historical_baseline"] + components["forecast_adjustment"] + components["surgery_additional_daily_average"],
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
            pool_simulation = main_module.simulate_nearby_supply(TransferSimulationPayload(
                source_hospital_id="H002", supply_id="MED001", sharing_enabled=True,
                shareable_quantity=300, eta_increase_minutes=0,
            ), kmch_user)["data"]
            self.assertTrue(pool_simulation["feasible"])
            self.assertLessEqual(pool_simulation["recommended_quantity"], 300)
            delayed_simulation = main_module.simulate_nearby_supply(TransferSimulationPayload(
                source_hospital_id="H002", supply_id="MED001", sharing_enabled=True,
                shareable_quantity=300, eta_increase_minutes=3000,
            ), kmch_user)["data"]
            self.assertFalse(delayed_simulation["feasible"])
            disabled_simulation = main_module.simulate_nearby_supply(TransferSimulationPayload(
                source_hospital_id="H002", supply_id="MED001", sharing_enabled=False,
                shareable_quantity=300, eta_increase_minutes=0,
            ), kmch_user)["data"]
            self.assertFalse(disabled_simulation["feasible"])

            report = main_module.weekly_management_report(date.today(), date.today() + timedelta(days=6), kmch_user)["data"]
            self.assertEqual(report["executive_summary"]["upcoming_surgery_cases"], 12)
            self.assertEqual(report["surgeries"][0]["number_of_cases"], 12)
            self.assertEqual(report["surgeries"][0]["supply_impact"][0]["additional_units"], 18.0)

        database.ACTIVE_DATA = original_data
        main_module._cached_analysis = None
        main_module._cached_data_id = None

    def test_multi_source_request_commits_400_then_matches_remaining_200(self) -> None:
        original_data = database.ACTIVE_DATA
        dataset = build_dataset("outbreak")
        for batch in dataset["inventory"]:
            if batch["hospital_id"] in ("H002", "H003") and batch["supply_id"] == "MED001":
                batch["quantity"] += 5000
        source_b = next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H002" and row["supply_id"] == "MED001")
        source_b.update({"shareable_quantity": 400, "enabled": True, "committed_quantity": 0})
        dataset["shareable_pool"].append({
            "pool_id": "POOL-H003-MED001", "hospital_id": "H003", "supply_id": "MED001",
            "shareable_quantity": 250, "enabled": True, "committed_quantity": 0,
        })
        analysis = analyze(dataset)
        next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H003")["enabled"] = False
        database.ACTIVE_DATA = dataset
        main_module._cached_analysis = None
        main_module._cached_data_id = None
        route = {"coordinates": [[11.02, 77.01], [11.04, 77.04]], "distance_km": 6.2,
                 "duration_minutes": 20, "provider": "OSRM", "route_available": True}
        route_calls = []
        def route_for(source, destination):
            route_calls.append((source["hospital_id"], destination["hospital_id"]))
            return route
        try:
            with patch("app.fulfillment.road_route", side_effect=route_for), patch.object(fulfillment, "analyze", return_value=analysis), patch.object(fulfillment, "OFFER_TIMEOUT_SECONDS", 3600):
                request = fulfillment.create_request("H001", "MED001", 600)
                first_leg = request["legs"][0]
                self.assertEqual(first_leg["source_hospital_id"], "H002")
                self.assertEqual(first_leg["allocated_quantity"], 400)
                self.assertEqual(first_leg["status"], "OFFERED")

                source_c = next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H003")
                source_c["enabled"] = True
                committed = fulfillment.accept_leg(request["request_id"], first_leg["leg_id"], "H002")
                self.assertEqual(committed["allocated_quantity"], 400)
                self.assertEqual(committed["fulfilled_quantity"], 0)
                self.assertEqual(committed["status"], "PARTIALLY_FULFILLED")
                self.assertEqual(next(row for row in committed["legs"] if row["leg_id"] == first_leg["leg_id"])["lifecycle_status"], "PICKUP_PENDING")
                self.assertEqual(committed["remaining_quantity"], 200)
                self.assertEqual(source_b["committed_quantity"], 400)
                committed_batches = [batch for batch in dataset["inventory"]
                                     if batch["hospital_id"] == "H002" and batch["supply_id"] == "MED001"]
                self.assertEqual(sum(batch.get("committed_quantity", 0) for batch in committed_batches), 400)
                second_leg = next(row for row in committed["legs"] if row["source_hospital_id"] == "H003")
                self.assertEqual(second_leg["allocated_quantity"], 200)
                self.assertEqual(second_leg["status"], "OFFERED")
                map_edges = main_module.network_summary({"hospital_id": "H001", "role": "hospital_user"})["data"]["edges"]
                mapped_leg = next(row for row in map_edges if row.get("fulfillment_leg_id") == first_leg["leg_id"])
                self.assertEqual(mapped_leg["route"], route["coordinates"])
                self.assertEqual(mapped_leg["leg_status"], "COMMITTED")

                with self.assertRaises(PermissionError):
                    fulfillment.update_leg_status(request["request_id"], first_leg["leg_id"], "H001", "PICKED_UP")
                picked_up = fulfillment.update_leg_status(request["request_id"], first_leg["leg_id"], "H002", "PICKED_UP")
                picked_up_leg = next(row for row in picked_up["legs"] if row["leg_id"] == first_leg["leg_id"])
                self.assertEqual(picked_up_leg["status"], "PICKED_UP")
                self.assertEqual(picked_up["fulfilled_quantity"], 0)
                in_transit = fulfillment.update_leg_status(request["request_id"], first_leg["leg_id"], "H002", "IN_TRANSIT")
                self.assertEqual(next(row for row in in_transit["legs"] if row["leg_id"] == first_leg["leg_id"])["status"], "IN_TRANSIT")
                self.assertEqual(source_b["shareable_quantity"], 0)
                self.assertEqual(source_b["committed_quantity"], 0)
                self.assertEqual(sum(batch.get("committed_quantity", 0) for batch in committed_batches), 0)
                destination_before = sum(batch["quantity"] for batch in dataset["inventory"]
                                         if batch["hospital_id"] == "H001" and batch["supply_id"] == "MED001")
                arrived = fulfillment.update_leg_status(request["request_id"], first_leg["leg_id"], "H001", "ARRIVED")
                self.assertEqual(next(row for row in arrived["legs"] if row["leg_id"] == first_leg["leg_id"])["status"], "ARRIVED")
                delivered = fulfillment.update_leg_status(request["request_id"], first_leg["leg_id"], "H001", "DELIVERED")
                delivered_leg = next(row for row in delivered["legs"] if row["leg_id"] == first_leg["leg_id"])
                self.assertEqual(delivered_leg["status"], "DELIVERED")
                self.assertEqual(sum(batch["quantity"] for batch in dataset["inventory"]
                                     if batch["hospital_id"] == "H001" and batch["supply_id"] == "MED001") - destination_before, 400)
                received = [batch for batch in dataset["inventory"] if batch.get("transfer_leg_id") == first_leg["leg_id"]]
                self.assertEqual(sum(batch["quantity"] for batch in received), 400)
                self.assertTrue(route_calls)
                self.assertEqual(route_calls[0], ("H002", "H001"))
                self.assertTrue(all(destination_id == "H001" for _, destination_id in route_calls))
                self.assertIn(("H003", "H001"), route_calls)
                with self.assertRaises(PermissionError):
                    fulfillment.update_leg_status(request["request_id"], first_leg["leg_id"], "H001", "DELIVERED")
                self.assertEqual(sum(batch["quantity"] for batch in dataset["inventory"]
                                     if batch.get("transfer_leg_id") == first_leg["leg_id"]), 400)

                fulfilled = fulfillment.accept_leg(request["request_id"], second_leg["leg_id"], "H003")
                self.assertEqual(fulfilled["allocated_quantity"], 600)
                self.assertEqual(fulfilled["fulfilled_quantity"], 400)
                self.assertEqual(fulfilled["remaining_quantity"], 0)
                self.assertEqual(fulfilled["status"], "PARTIALLY_DELIVERED")
                self.assertEqual(sum(row["allocated_quantity"] for row in fulfilled["legs"]), 600)
                self.assertEqual(next(row for row in fulfilled["legs"] if row["leg_id"] == second_leg["leg_id"])["lifecycle_status"], "PICKUP_PENDING")
                fulfillment.update_leg_status(request["request_id"], second_leg["leg_id"], "H003", "PICKED_UP")
                fulfillment.update_leg_status(request["request_id"], second_leg["leg_id"], "H003", "IN_TRANSIT")
                fulfillment.update_leg_status(request["request_id"], second_leg["leg_id"], "H001", "ARRIVED")
                fully_delivered = fulfillment.update_leg_status(request["request_id"], second_leg["leg_id"], "H001", "DELIVERED")
                self.assertEqual(fully_delivered["fulfilled_quantity"], 600)
                self.assertEqual(fully_delivered["status"], "FULFILLED")
                events = fully_delivered["timeline"]
                event_names = [row["event"] for row in events]
                for event in ("REQUESTED", "MATCHING", "OFFERED", "ACCEPTED", "REMATCHING", "PICKED_UP", "IN_TRANSIT", "ARRIVED", "DELIVERED"):
                    self.assertIn(event, event_names)
                event_times = [datetime.fromisoformat(row["occurred_at"]) for row in events]
                self.assertEqual(event_times, sorted(event_times))
        finally:
            database.ACTIVE_DATA = original_data
            main_module._cached_analysis = None
            main_module._cached_data_id = None

    def test_request_metadata_tracks_urgency_and_deadline(self) -> None:
        original_data = database.ACTIVE_DATA
        dataset = build_dataset("outbreak")
        source_h2 = next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H002")
        source_h2.update({"shareable_quantity": 400, "enabled": True, "committed_quantity": 0})
        source_h3 = {"pool_id": "POOL-H003-MED001", "hospital_id": "H003", "supply_id": "MED001",
                     "shareable_quantity": 250, "enabled": True, "committed_quantity": 0}
        dataset["shareable_pool"].append(source_h3)
        analysis = analyze(dataset)
        database.ACTIVE_DATA = dataset
        route = {"coordinates": [[11.02, 77.01], [11.04, 77.04]], "distance_km": 6.2,
                 "duration_minutes": 20, "provider": "test", "route_available": True}
        try:
            with patch.object(fulfillment, "analyze", return_value=analysis), patch.object(fulfillment, "road_route", return_value=route):
                request = fulfillment.create_request("H001", "MED001", 600, urgency_level="URGENT", delivery_deadline="2026-10-12")
                self.assertEqual(request["urgency_level"], "URGENT")
                self.assertEqual(request["delivery_deadline"], "2026-10-12")
                self.assertEqual(request["matching_status"], "OFFERED")
        finally:
            database.ACTIVE_DATA = original_data

    def test_rejection_failure_no_source_and_manual_rematch(self) -> None:
        original_data = database.ACTIVE_DATA
        dataset = build_dataset("outbreak")
        for batch in dataset["inventory"]:
            if batch["hospital_id"] in ("H002", "H003") and batch["supply_id"] == "MED001":
                batch["quantity"] += 5000
        source_h2 = next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H002")
        source_h2.update({"shareable_quantity": 400, "enabled": True, "committed_quantity": 0})
        source_h3 = {"pool_id": "POOL-H003-MED001", "hospital_id": "H003", "supply_id": "MED001",
                     "shareable_quantity": 250, "enabled": True, "committed_quantity": 0}
        dataset["shareable_pool"].append(source_h3)
        analysis = analyze(dataset)
        source_h3["enabled"] = False
        database.ACTIVE_DATA = dataset
        route = {"coordinates": [[11.02, 77.01], [11.04, 77.04]], "distance_km": 6.2,
                 "duration_minutes": 20, "provider": "test", "route_available": True}
        try:
            with patch.object(fulfillment, "analyze", return_value=analysis), patch.object(fulfillment, "road_route", return_value=route):
                rejected = fulfillment.create_request("H001", "MED001", 600)
                h2_offer = rejected["legs"][0]
                self.assertEqual(h2_offer["source_hospital_id"], "H002")
                source_h3["enabled"] = True
                after_rejection = fulfillment.reject_leg(rejected["request_id"], h2_offer["leg_id"], "H002")
                self.assertEqual(after_rejection["requested_quantity"], 600)
                self.assertEqual(after_rejection["fulfilled_quantity"], 0)
                self.assertEqual(after_rejection["remaining_quantity"], 600)
                self.assertEqual(after_rejection["rejected_offers_count"], 1)
                self.assertEqual(after_rejection["legs"][-1]["source_hospital_id"], "H003")

                failing = fulfillment.create_request("H001", "MED001", 600)
                h2_leg = next(row for row in failing["legs"] if row["source_hospital_id"] == "H002")
                accepted = fulfillment.accept_leg(failing["request_id"], h2_leg["leg_id"], "H002")
                failed = fulfillment.fail_leg(failing["request_id"], h2_leg["leg_id"], "H002", "test failure")
                self.assertEqual(accepted["allocated_quantity"], 400)
                self.assertEqual(failed["requested_quantity"], 600)
                self.assertEqual(failed["allocated_quantity"], 0)
                self.assertEqual(failed["remaining_quantity"], 600)
                self.assertEqual(failed["failed_transfers_count"], 1)
                failed_leg = next(row for row in failed["legs"] if row["leg_id"] == h2_leg["leg_id"])
                self.assertEqual(failed_leg["lifecycle_status"], "FAILED")
                self.assertEqual(failed_leg["failure_inventory_disposition"], "released_to_source")
                self.assertTrue(any(row["source_hospital_id"] == "H003" and row["status"] == "OFFERED" for row in failed["legs"]))
                self.assertEqual(source_h2["committed_quantity"], 0)

                source_h2["enabled"] = False
                source_h3["enabled"] = False
                unavailable = fulfillment.create_request("H001", "MED001", 600)
                self.assertEqual(unavailable["status"], "NO_SOURCE_AVAILABLE")
                self.assertEqual(unavailable["remaining_quantity"], 600)
                source_h3["enabled"] = True
                retried = fulfillment.rematch_request(unavailable["request_id"], "H001")
                self.assertEqual(retried["matching_status"], "OFFERED")
                self.assertEqual(retried["rematch_count"], 1)
        finally:
            database.ACTIVE_DATA = original_data

    def test_offer_timeout_expires_and_rematches_next_source(self) -> None:
        original_data = database.ACTIVE_DATA
        dataset = build_dataset("outbreak")
        for batch in dataset["inventory"]:
            if batch["hospital_id"] in ("H002", "H003") and batch["supply_id"] == "MED001":
                batch["quantity"] += 5000
        source_h2 = next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H002")
        source_h2.update({"shareable_quantity": 400, "enabled": True, "committed_quantity": 0})
        source_h3 = {"pool_id": "POOL-H003-MED001", "hospital_id": "H003", "supply_id": "MED001",
                     "shareable_quantity": 250, "enabled": True, "committed_quantity": 0}
        dataset["shareable_pool"].append(source_h3)
        analysis = analyze(dataset)
        source_h3["enabled"] = False
        database.ACTIVE_DATA = dataset
        route = {"coordinates": [[11.02, 77.01], [11.04, 77.04]], "distance_km": 6.2,
                 "duration_minutes": 20, "provider": "test", "route_available": True}
        try:
            with patch.object(fulfillment, "analyze", return_value=analysis), patch.object(fulfillment, "road_route", return_value=route), patch.object(fulfillment, "OFFER_TIMEOUT_SECONDS", 1):
                created = fulfillment.create_request("H001", "MED001", 600)
                first_offer = created["legs"][0]
                source_h2["enabled"] = False
                source_h3["enabled"] = True
                active_offer = fulfillment._leg(first_offer["leg_id"])
                active_offer["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
                updated = next(row for row in fulfillment.list_visible_requests("H001") if row["request_id"] == created["request_id"])

                expired = next(row for row in updated["legs"] if row["leg_id"] == first_offer["leg_id"])
                next_offer = updated["legs"][-1]
                self.assertEqual(expired["status"], "EXPIRED")
                self.assertIn("Searching for another supplier", expired["failure_reason"])
                self.assertEqual(next_offer["status"], "OFFERED")
                self.assertEqual(next_offer["source_hospital_id"], "H003")
                self.assertEqual(updated["remaining_quantity"], 600)
                self.assertEqual(updated["expired_offers_count"], 1)
                self.assertEqual(updated["rematch_count"], 1)
        finally:
            database.ACTIVE_DATA = original_data

    def test_in_transit_failure_restores_need_without_restoring_consumed_stock(self) -> None:
        original_data = database.ACTIVE_DATA
        dataset = build_dataset("outbreak")
        for batch in dataset["inventory"]:
            if batch["hospital_id"] in ("H002", "H003") and batch["supply_id"] == "MED001":
                batch["quantity"] += 5000
        source_h2 = next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H002")
        source_h2.update({"shareable_quantity": 400, "enabled": True, "committed_quantity": 0})
        source_h3 = {"pool_id": "POOL-H003-MED001", "hospital_id": "H003", "supply_id": "MED001",
                     "shareable_quantity": 250, "enabled": True, "committed_quantity": 0}
        dataset["shareable_pool"].append(source_h3)
        analysis = analyze(dataset)
        database.ACTIVE_DATA = dataset
        route = {"coordinates": [[11.02, 77.01], [11.04, 77.04]], "distance_km": 6.2,
                 "duration_minutes": 20, "provider": "test", "route_available": True}
        source_batch = next(row for row in dataset["inventory"] if row["hospital_id"] == "H002" and row["supply_id"] == "MED001")
        original_quantity = source_batch["quantity"]
        try:
            with patch.object(fulfillment, "analyze", return_value=analysis), patch.object(fulfillment, "road_route", return_value=route), patch.object(fulfillment, "OFFER_TIMEOUT_SECONDS", 3600):
                request = fulfillment.create_request("H001", "MED001", 600)
                first_leg = request["legs"][0]
                fulfillment.accept_leg(request["request_id"], first_leg["leg_id"], "H002")
                fulfillment.update_leg_status(request["request_id"], first_leg["leg_id"], "H002", "PICKED_UP")
                in_transit = fulfillment.update_leg_status(request["request_id"], first_leg["leg_id"], "H002", "IN_TRANSIT")
                self.assertEqual(source_batch["quantity"], original_quantity - 400)

                failed = fulfillment.fail_leg(request["request_id"], first_leg["leg_id"], "H001", "vehicle route failed")
                failed_leg = next(row for row in failed["legs"] if row["leg_id"] == first_leg["leg_id"])
                self.assertEqual(failed_leg["failure_inventory_disposition"], "lost_or_in_transit")
                self.assertEqual(source_batch["quantity"], original_quantity - 400)
                self.assertEqual(failed["remaining_quantity"], 600)
                self.assertEqual(failed["allocated_quantity"], 0)
                self.assertTrue(any(row["source_hospital_id"] == "H003" and row["status"] == "OFFERED" for row in failed["legs"]))
        finally:
            database.ACTIVE_DATA = original_data

    def test_report_and_copilot_use_live_multi_source_request_data(self) -> None:
        original_data = database.ACTIVE_DATA
        original_cache = main_module._cached_analysis
        original_cache_id = main_module._cached_data_id
        dataset = build_dataset("kmch_to_psg")
        for batch in dataset["inventory"]:
            if batch["hospital_id"] in ("H001", "H003") and batch["supply_id"] == "MED001":
                batch["quantity"] += 5000
        source_h1 = next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H001")
        source_h1.update({"shareable_quantity": 400, "enabled": True, "committed_quantity": 0})
        source_h3 = next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H003")
        source_h3.update({"shareable_quantity": 250, "enabled": True, "committed_quantity": 0})
        analysis = analyze(dataset)
        database.ACTIVE_DATA = dataset
        main_module._cached_analysis = analysis
        main_module._cached_data_id = id(dataset)
        route = {"coordinates": [[11.043, 77.040], [11.018, 77.007]], "distance_km": 6.2,
                 "duration_minutes": 20, "provider": "test", "route_available": True}
        psg_user = {"hospital_id": "H002", "role": "hospital_user"}
        try:
            with patch.object(fulfillment, "analyze", return_value=analysis), patch.object(fulfillment, "road_route", return_value=route), patch.object(main_module, "road_route", return_value=route), patch.object(fulfillment, "OFFER_TIMEOUT_SECONDS", 3600):
                request = fulfillment.create_request("H002", "MED001", 600)
                first_leg = request["legs"][0]
                self.assertEqual(first_leg["source_hospital_id"], "H001")
                accepted = fulfillment.accept_leg(request["request_id"], first_leg["leg_id"], "H001")
                self.assertEqual(accepted["allocated_quantity"], 400)
                self.assertEqual(accepted["remaining_quantity"], 200)
                self.assertEqual(accepted["legs"][-1]["source_hospital_id"], "H003")

                report = main_module.weekly_management_report(date.today(), date.today(), psg_user)["data"]
                metrics = report["executive_summary"]["fulfillment"]
                self.assertEqual(metrics["requests"], 1)
                self.assertEqual(metrics["total_requested_units"], 600)
                self.assertEqual(metrics["total_fulfilled_units"], 0)
                self.assertEqual(metrics["partially_fulfilled_requests"], 1)
                self.assertEqual(metrics["multi_source_fulfillment_count"], 1)
                self.assertEqual(metrics["rematches"], 1)
                self.assertEqual(report["fulfillment_requests"][0]["remaining_quantity"], 200)
                self.assertIsNotNone(metrics["average_matching_time_seconds"])
                self.assertGreater(metrics["average_transfer_eta_minutes"], 0)

                partial = main_module.assistant_query(main_module.AssistantQuery(question="Why is this request partially fulfilled?"), psg_user)["data"]
                multi_source = main_module.assistant_query(main_module.AssistantQuery(question="Why did the system use two hospitals?"), psg_user)["data"]
                self.assertIn("400 units are allocated (0 delivered)", partial["answer"])
                self.assertIn("200 units unresolved", partial["answer"])
                self.assertIn("KMCH 400", multi_source["answer"])
                self.assertIn("Kumaran Medical Center 200", multi_source["answer"])
        finally:
            database.ACTIVE_DATA = original_data
            main_module._cached_analysis = original_cache
            main_module._cached_data_id = original_cache_id

    def test_multi_source_judge_demo_sets_up_400_plus_200_request(self) -> None:
        original_data = database.ACTIVE_DATA
        original_scenario = database.ACTIVE_SCENARIO
        original_backup = database._SCENARIO_POOL_BACKUP
        original_cache = main_module._cached_analysis
        original_cache_id = main_module._cached_data_id
        database.ACTIVE_DATA = build_dataset("outbreak")
        route = {"coordinates": [[11.043, 77.040], [11.018, 77.007]], "distance_km": 6.2,
                 "duration_minutes": 20, "provider": "test", "route_available": True}
        try:
            with patch.object(fulfillment, "road_route", return_value=route):
                demo = main_module.run_multi_source_fulfillment_demo({"hospital_id": "H002", "role": "hospital_user"})["data"]
            request = demo["request"]
            self.assertTrue(demo["demo_ready"])
            self.assertEqual(request["destination_hospital_id"], "H002")
            self.assertEqual(request["requested_quantity"], 600)
            self.assertEqual(len(database.ACTIVE_DATA["supply_requests"]), 1)
            self.assertEqual(request["legs"][0]["source_hospital_id"], "H001")
            self.assertEqual(request["legs"][0]["allocated_quantity"], 400)
            self.assertEqual(next(row["shareable_quantity"] for row in database.ACTIVE_DATA["shareable_pool"] if row["hospital_id"] == "H003"), 250)
            with self.assertRaisesRegex(Exception, "Complete or cancel active"):
                main_module.run_multi_source_fulfillment_demo({"hospital_id": "H002", "role": "hospital_user"})
            with self.assertRaises(Exception):
                main_module.run_multi_source_fulfillment_demo({"hospital_id": "H001", "role": "hospital_user"})
        finally:
            database.ACTIVE_DATA = original_data
            database.ACTIVE_SCENARIO = original_scenario
            database._SCENARIO_POOL_BACKUP = original_backup
            main_module._cached_analysis = original_cache
            main_module._cached_data_id = original_cache_id

    def test_competing_offers_cannot_overcommit_shareable_stock(self) -> None:
        original_data = database.ACTIVE_DATA
        dataset = build_dataset("normal")
        for batch in dataset["inventory"]:
            if batch["hospital_id"] == "H002" and batch["supply_id"] == "MED001":
                batch["quantity"] += 5000
        pool = next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H002")
        pool.update({"shareable_quantity": 400, "enabled": True, "committed_quantity": 0})
        analysis = analyze(dataset)
        database.ACTIVE_DATA = dataset
        route = {"coordinates": [[11.02, 77.01], [11.04, 77.04]], "distance_km": 6.2,
                 "duration_minutes": 20, "provider": "test", "route_available": True}
        try:
            with patch.object(fulfillment, "analyze", return_value=analysis), patch.object(fulfillment, "road_route", return_value=route):
                first = fulfillment.create_request("H001", "MED001", 300)
                second = fulfillment.create_request("H001", "MED001", 200)
                first_offer = first["legs"][0]
                second_offer = second["legs"][0]
                self.assertEqual((first_offer["allocated_quantity"], second_offer["allocated_quantity"]), (300, 200))
                committed_first = fulfillment.accept_leg(first["request_id"], first_offer["leg_id"], "H002")
                committed_second = fulfillment.accept_leg(second["request_id"], second_offer["leg_id"], "H002")

                self.assertEqual(committed_first["allocated_quantity"], 300)
                self.assertEqual(committed_second["allocated_quantity"], 100)
                self.assertEqual(committed_second["remaining_quantity"], 100)
                self.assertEqual(pool["committed_quantity"], 400)
                committed_batches = [row for row in dataset["inventory"] if row["hospital_id"] == "H002" and row["supply_id"] == "MED001"]
                self.assertEqual(sum(row.get("committed_quantity", 0) for row in committed_batches), 400)
                second_batches = next(row for row in committed_second["legs"] if row["leg_id"] == second_offer["leg_id"])["source_batch_allocations"]
                self.assertEqual([row["expiry_date"] for row in second_batches], sorted(row["expiry_date"] for row in second_batches))
                self.assertTrue(all(row["expiry_date"] >= date.today().isoformat() for row in second_batches))
        finally:
            database.ACTIVE_DATA = original_data

    def test_fulfillment_names_follow_source_and_destination_ids_both_directions(self) -> None:
        original_data = database.ACTIVE_DATA
        dataset = build_dataset("normal")
        dataset["supply_requests"] = [
            {"request_id": "SWRX-KMCH-PSG", "destination_hospital_id": "H002", "destination_hospital": "KMCH", "created_at": "2026-10-08T00:00:00Z"},
            {"request_id": "SWRX-PSG-KMCH", "destination_hospital_id": "H001", "destination_hospital": "PSG Hospitals", "created_at": "2026-10-08T00:00:01Z"},
            {"request_id": "SWRX-MISSING", "destination_hospital_id": "H001", "created_at": "2026-10-08T00:00:02Z"},
        ]
        dataset["fulfillment_legs"] = [
            {"leg_id": "LEG-KMCH-PSG", "request_id": "SWRX-KMCH-PSG", "source_hospital_id": "H001",
             "destination_hospital_id": "H002", "source_hospital": "PSG Hospitals", "destination_hospital": "KMCH",
             "created_at": "2026-10-08T00:00:00Z", "allocated_quantity": 400, "status": "IN_TRANSIT"},
            {"leg_id": "LEG-PSG-KMCH", "request_id": "SWRX-PSG-KMCH", "source_hospital_id": "H002",
             "destination_hospital_id": "H001", "source_hospital": "KMCH", "destination_hospital": "PSG Hospitals",
             "created_at": "2026-10-08T00:00:01Z", "allocated_quantity": 200, "status": "IN_TRANSIT"},
            {"leg_id": "LEG-MISSING", "request_id": "SWRX-MISSING", "source_hospital_id": "H404",
             "destination_hospital_id": "H001", "created_at": "2026-10-08T00:00:02Z", "allocated_quantity": 1, "status": "IN_TRANSIT"},
        ]
        database.ACTIVE_DATA = dataset
        try:
            kmch_to_psg = next(row for row in fulfillment.list_visible_requests("H001") if row["request_id"] == "SWRX-KMCH-PSG")
            psg_to_kmch = next(row for row in fulfillment.list_visible_requests("H002") if row["request_id"] == "SWRX-PSG-KMCH")
            self.assertEqual((kmch_to_psg["legs"][0]["source_hospital"], kmch_to_psg["legs"][0]["destination_hospital"]), ("KMCH", "PSG Hospitals"))
            self.assertEqual((psg_to_kmch["legs"][0]["source_hospital"], psg_to_kmch["legs"][0]["destination_hospital"]), ("PSG Hospitals", "KMCH"))
            self.assertEqual(kmch_to_psg["legs"][0]["allocated_quantity"], 400)
            self.assertEqual(psg_to_kmch["legs"][0]["allocated_quantity"], 200)
            missing = next(row for row in fulfillment.list_visible_requests("H001") if row["request_id"] == "SWRX-MISSING")
            self.assertEqual(missing["legs"][0]["source_hospital"], "Source unavailable")
            self.assertEqual(missing["legs"][0]["destination_hospital"], "KMCH")

            route_calls = []
            def capture_route(source, destination):
                route_calls.append((source["hospital_id"], destination["hospital_id"]))
                return {"coordinates": [[source["latitude"], source["longitude"]], [destination["latitude"], destination["longitude"]]],
                        "distance_km": 1, "duration_minutes": 1, "provider": "test", "route_available": True}
            with patch.object(main_module, "road_route", side_effect=capture_route):
                main_module._route_for_ids("H001", "H002")
                main_module._route_for_ids("H002", "H001")
            self.assertEqual(route_calls, [("H001", "H002"), ("H002", "H001")])
        finally:
            database.ACTIVE_DATA = original_data

    def test_kmch_to_psg_scenario_creates_safe_source_direction(self) -> None:
        original_data = database.ACTIVE_DATA
        original_scenario = database.ACTIVE_SCENARIO
        original_pool_backup = database._SCENARIO_POOL_BACKUP
        original_cache = main_module._cached_analysis
        original_cache_id = main_module._cached_data_id
        prior_pool = deepcopy(original_data.get("shareable_pool", []))
        try:
            database.ACTIVE_SCENARIO = "outbreak"
            database._SCENARIO_POOL_BACKUP = None
            scenario_data = database.set_scenario("kmch_to_psg")
            analysis = analyze(scenario_data)
            item = next(row for row in analysis["transfers"]
                        if row["supply_id"] == "MED001" and row["source_hospital_id"] == "H001"
                        and row["destination_hospital_id"] == "H002")
            self.assertEqual(item["recommended_quantity"], 400)
            self.assertGreaterEqual(item["source_remaining_stock"], item["source_reserve"])
            self.assertEqual({(row["hospital_id"], row["shareable_quantity"]) for row in scenario_data["shareable_pool"]}, {("H001", 400), ("H003", 200)})
            second_source = next(row for row in analysis["transfers"]
                                 if row["supply_id"] == "MED001" and row["source_hospital_id"] == "H003"
                                 and row["destination_hospital_id"] == "H002")
            self.assertEqual(second_source["recommended_quantity"], 200)
            self.assertGreaterEqual(second_source["source_remaining_stock"], second_source["source_reserve"])

            transfer_route = {"coordinates": [[11.04, 77.04], [11.02, 77.01]], "distance_km": 5.59,
                              "duration_minutes": 12, "provider": "OSRM", "route_available": True}
            with patch("app.fulfillment.road_route", return_value=transfer_route):
                request = fulfillment.create_request("H002", "MED001", 600)
                first_leg = request["legs"][0]
                self.assertEqual((first_leg["source_hospital_id"], first_leg["destination_hospital_id"], first_leg["allocated_quantity"]), ("H001", "H002", 400))
                partial = fulfillment.accept_leg(request["request_id"], first_leg["leg_id"], "H001")
                self.assertEqual((partial["allocated_quantity"], partial["fulfilled_quantity"], partial["remaining_quantity"]), (400, 0, 200))
                second_leg = partial["legs"][-1]
                self.assertEqual((second_leg["source_hospital_id"], second_leg["destination_hospital_id"], second_leg["allocated_quantity"]), ("H003", "H002", 200))
                final = fulfillment.accept_leg(request["request_id"], second_leg["leg_id"], "H003")
                self.assertEqual((final["allocated_quantity"], final["fulfilled_quantity"], final["remaining_quantity"], final["status"]), (600, 0, 0, "FULLY_ALLOCATED"))
                self.assertEqual(len(fulfillment.list_visible_requests("H002")), 1)
                self.assertEqual(len(fulfillment.list_visible_requests("H001")), 1)
                self.assertEqual(len(fulfillment.list_visible_requests("H003")), 1)

            main_module._cached_analysis = None
            main_module._cached_data_id = None
            route_calls = []
            def capture_route(source, destination):
                route_calls.append((source["hospital_id"], destination["hospital_id"]))
                return {"coordinates": [[source["latitude"], source["longitude"]], [destination["latitude"], destination["longitude"]]],
                        "distance_km": 10.8, "duration_minutes": 15, "provider": "OSRM", "route_available": True}
            with patch.object(main_module, "road_route", side_effect=capture_route):
                public = main_module._public_transfer(item, "H002")
            self.assertEqual((public["source_hospital_id"], public["destination_hospital_id"]), ("H001", "H002"))
            self.assertEqual((public["source_hospital"], public["destination_hospital"]), ("KMCH", "PSG Hospitals"))
            self.assertEqual(route_calls, [("H001", "H002")])

            restored = database.set_scenario("outbreak")
            self.assertEqual(restored["shareable_pool"], prior_pool)
        finally:
            database.ACTIVE_DATA = original_data
            database.ACTIVE_SCENARIO = original_scenario
            database._SCENARIO_POOL_BACKUP = original_pool_backup
            main_module._cached_analysis = original_cache
            main_module._cached_data_id = original_cache_id

    def test_shareable_pool_capacity_validation_and_hospital_isolation(self) -> None:
        from fastapi import HTTPException

        original_data = database.ACTIVE_DATA
        original_scenario = database.ACTIVE_SCENARIO
        original_cache = main_module._cached_analysis
        original_cache_id = main_module._cached_data_id
        kmch = {"hospital_id": "H001", "role": "hospital_user"}
        psg = {"hospital_id": "H002", "role": "hospital_user"}
        try:
            database.ACTIVE_SCENARIO = "outbreak"
            database.ACTIVE_DATA = build_dataset("kmch_to_psg")
            main_module._cached_analysis = None
            main_module._cached_data_id = None

            kmch_rows = main_module.get_shareable_pool(kmch)["data"]
            saline = next(row for row in kmch_rows if row["supply_id"] == "MED001")
            self.assertEqual(saline["hospital_id"], "H001")
            self.assertGreaterEqual(saline["maximum_allowed_shareable"], 700)
            saved = main_module.save_shareable_pool(ShareablePoolPayload(
                supply_id="MED001", shareable_quantity=700, enabled=True,
            ), kmch)["data"]
            self.assertEqual(saved["hospital_id"], "H001")
            self.assertEqual(saved["shareable_quantity"], 700)

            with self.assertRaises(HTTPException) as too_large:
                main_module.save_shareable_pool(ShareablePoolPayload(
                    supply_id="MED001", shareable_quantity=saline["maximum_allowed_shareable"] + 1, enabled=True,
                ), kmch)
            self.assertIn("Maximum shareable quantity is", too_large.exception.detail)
            with self.assertRaises(HTTPException) as negative:
                main_module.save_shareable_pool(ShareablePoolPayload(
                    supply_id="MED001", shareable_quantity=-1, enabled=False,
                ), kmch)
            self.assertIn("cannot be negative", negative.exception.detail)

            psg_rows = main_module.get_shareable_pool(psg)["data"]
            self.assertTrue(psg_rows)
            self.assertEqual({row["hospital_id"] for row in psg_rows}, {"H002"})
            psg_saline = next(row for row in psg_rows if row["supply_id"] == "MED001")
            self.assertEqual(psg_saline["maximum_allowed_shareable"], 0)
            self.assertEqual(psg_saline["shareable_quantity"], 0)
            with self.assertRaises(HTTPException) as no_surplus:
                main_module.save_shareable_pool(ShareablePoolPayload(
                    supply_id="MED001", shareable_quantity=700, enabled=True,
                ), psg)
            self.assertIn("No safe surplus", no_surplus.exception.detail)
        finally:
            database.ACTIVE_DATA = original_data
            database.ACTIVE_SCENARIO = original_scenario
            main_module._cached_analysis = original_cache
            main_module._cached_data_id = original_cache_id

    def test_rejected_offer_automatically_matches_next_source(self) -> None:
        original_data = database.ACTIVE_DATA
        dataset = build_dataset("outbreak")
        for batch in dataset["inventory"]:
            if batch["hospital_id"] in ("H002", "H003") and batch["supply_id"] == "MED001":
                batch["quantity"] += 5000
        source_b = next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H002" and row["supply_id"] == "MED001")
        source_b.update({"shareable_quantity": 400, "enabled": True, "committed_quantity": 0})
        dataset["shareable_pool"].append({
            "pool_id": "POOL-H003-MED001", "hospital_id": "H003", "supply_id": "MED001",
            "shareable_quantity": 250, "enabled": False, "committed_quantity": 0,
        })
        database.ACTIVE_DATA = dataset
        main_module._cached_analysis = None
        main_module._cached_data_id = None
        route = {"coordinates": [[11.02, 77.01], [11.04, 77.04]], "distance_km": 6.2,
                 "duration_minutes": 20, "provider": "OSRM", "route_available": True}
        try:
            with patch("app.fulfillment.road_route", return_value=route):
                request = fulfillment.create_request("H001", "MED001", 600)
                source_c = next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H003")
                source_c["enabled"] = True
                rematched = fulfillment.reject_leg(request["request_id"], request["legs"][0]["leg_id"], "H002")
                self.assertEqual(rematched["fulfilled_quantity"], 0)
                self.assertEqual(rematched["remaining_quantity"], 600)
                self.assertEqual(rematched["status"], "SEARCHING")
                self.assertEqual(rematched["legs"][-1]["source_hospital_id"], "H003")
                self.assertEqual(rematched["legs"][-1]["allocated_quantity"], 250)
                self.assertEqual(rematched["legs"][-1]["status"], "OFFERED")
        finally:
            database.ACTIVE_DATA = original_data
            main_module._cached_analysis = None
            main_module._cached_data_id = None

    def test_committed_source_failure_releases_stock_and_retargets_remaining_need(self) -> None:
        original_data = database.ACTIVE_DATA
        dataset = build_dataset("outbreak")
        for batch in dataset["inventory"]:
            if batch["hospital_id"] in ("H002", "H003") and batch["supply_id"] == "MED001":
                batch["quantity"] += 5000
        source_b = next(row for row in dataset["shareable_pool"] if row["hospital_id"] == "H002" and row["supply_id"] == "MED001")
        source_b.update({"shareable_quantity": 400, "enabled": True, "committed_quantity": 0})
        source_c = {
            "pool_id": "POOL-H003-MED001", "hospital_id": "H003", "supply_id": "MED001",
            "shareable_quantity": 250, "enabled": True, "committed_quantity": 0,
        }
        dataset["shareable_pool"].append(source_c)
        database.ACTIVE_DATA = dataset
        main_module._cached_analysis = None
        main_module._cached_data_id = None
        route = {"coordinates": [[11.02, 77.01], [11.04, 77.04]], "distance_km": 6.2,
                 "duration_minutes": 20, "provider": "OSRM", "route_available": True}
        try:
            with patch("app.fulfillment.road_route", return_value=route):
                request = fulfillment.create_request("H001", "MED001", 600)
                first_leg = request["legs"][0]
                self.assertEqual(first_leg["source_hospital_id"], "H002")
                partially_committed = fulfillment.accept_leg(request["request_id"], first_leg["leg_id"], "H002")
                second_leg = next(row for row in partially_committed["legs"] if row["source_hospital_id"] == "H003")
                self.assertEqual(second_leg["allocated_quantity"], 200)

                rematched = fulfillment.fail_leg(request["request_id"], first_leg["leg_id"], "H002", "Route unavailable")
                self.assertEqual(rematched["fulfilled_quantity"], 0)
                self.assertEqual(rematched["remaining_quantity"], 600)
                self.assertEqual(source_b["committed_quantity"], 0)
                self.assertEqual(source_c["committed_quantity"], 0)
                self.assertEqual(rematched["legs"][-1]["allocated_quantity"], 250)
                self.assertEqual(rematched["legs"][-1]["status"], "OFFERED")

                exhausted = fulfillment.reject_leg(request["request_id"], rematched["legs"][-1]["leg_id"], "H003")
                self.assertEqual(exhausted["status"], "NO_SOURCE_AVAILABLE")
                self.assertEqual(exhausted["fulfilled_quantity"] + exhausted["remaining_quantity"], 600)
        finally:
            database.ACTIVE_DATA = original_data
            main_module._cached_analysis = None
            main_module._cached_data_id = None


if __name__ == "__main__":
    unittest.main()
