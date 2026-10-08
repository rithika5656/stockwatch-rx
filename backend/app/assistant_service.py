from __future__ import annotations

import re
from typing import Any, Protocol

from .engine import _risk, _simulate_depletion


class AssistantProvider(Protocol):
    name: str

    def answer(self, question: str, analysis: dict[str, Any]) -> dict[str, Any]: ...


class RuleBasedProvider:
    name = "Demo AI Mode"

    def answer(self, question: str, analysis: dict[str, Any]) -> dict[str, Any]:
        normalized = question.strip().lower()
        if any(phrase in normalized for phrase in ("what if", "what happens if", "what would happen if")) and any(term in normalized for term in ("demand", "increase", "rises")):
            percent_match = re.search(r"(\d{1,3})\s*%", normalized)
            increase_percent = min(200, int(percent_match.group(1))) if percent_match else 20
            hospital_match = re.search(r"\bhospital\s+([a-z])\b", normalized)
            hospital_id = f"H{ord(hospital_match.group(1)) - 96:03}" if hospital_match else "H003"
            supply = next((item for item in analysis["supplies"].values() if item["name"].lower() in normalized), None)
            supply = supply or analysis["supplies"].get("MED001")
            forecast = next((item for item in analysis["forecasts"] if item["hospital_id"] == hospital_id and item["supply_id"] == supply["supply_id"]), None)
            if forecast:
                adjusted_demand = forecast["forecast_daily_demand"] * (1 + increase_percent / 100)
                simulation = _simulate_depletion(forecast["current_stock"], forecast["safety_stock"], adjusted_demand, 0)
                adjusted_risk = _risk(simulation["days_to_safety_stock"], forecast["shortage_probability"])
                response = (f"A {increase_percent}% demand increase at {forecast['hospital']} would raise {forecast['supply']} use from "
                            f"{forecast['forecast_daily_demand']:.1f} to {adjusted_demand:.1f} units/day. The projected safety-stock breach "
                            f"would be in {simulation['days_to_safety_stock']} days ({adjusted_risk}); physical stock reaches zero in "
                            f"{simulation['days_to_zero_stock']} days. This is a scenario calculation, not a new observed forecast.")
                sources = [{"tool": "get_forecast", "hospital_id": hospital_id, "supply_id": supply["supply_id"], "demand_increase_percent": increase_percent}]
            else:
                response, sources = "No forecast exists for that hospital and supply combination.", []
        elif ("top 5" in normalized or "top five" in normalized) and any(term in normalized for term in ("transfer", "redistribut", "recommendation")):
            top = analysis["transfers"][:5]
            response = "Top redistribution actions: " + "; ".join(f"{item['recommended_quantity']:,} {item['supply']} from {item['source_hospital']} to {item['destination_hospital']} (score {item['priority_score']}/100)" for item in top) if top else "No feasible redistribution actions meet the current safety and expiry constraints."
            sources = [{"tool": "get_redistribution_recommendations", "results": len(top)}]
        elif ("saline" in normalized or "normal saline" in normalized) and not any(term in normalized for term in ("transfer", "redistribut", "move stock")):
            supply_id = "MED001"
            total = sum(value for (_hospital_id, selected_supply), value in analysis["inventory_totals"].items() if selected_supply == supply_id)
            matching = [item for item in analysis["shortages"] if item["supply_id"] == supply_id]
            response = f"There are {total:,} bags of Normal Saline 500ml across {len(analysis['hospitals'])} hospitals. {len(matching)} hospitals are currently flagged for shortage risk."
            sources = [{"tool": "get_inventory_summary", "total_units": total}, {"tool": "get_shortage_risks", "matching_hospitals": len(matching)}]
        elif any(term in normalized for term in ("transfer", "redistribut", "move stock")):
            match = next((item for item in analysis["transfers"] if ("hospital a" in normalized and "hospital c" in normalized and item["supply_id"] == "MED001") or ("saline" in normalized and item["supply_id"] == "MED001")), None)
            match = match or (analysis["transfers"][0] if analysis["transfers"] else None)
            if match:
                response = f"Recommend moving {match['recommended_quantity']:,} units of {match['supply']} from {match['source_hospital']} to {match['destination_hospital']}. {match['reason']} Estimated transport is {match['estimated_transport_hours']} hours."
                sources = [{"tool": "get_redistribution_recommendations", "recommendation_id": match["recommendation_id"], "priority_score": match["priority_score"]}]
            else:
                response, sources = "No safe redistribution match is available under the current stock and safety-stock constraints.", []
        elif "expir" in normalized or "wast" in normalized:
            top = sorted(analysis["expiry_risks"], key=lambda item: item["expected_waste"], reverse=True)[:5]
            response = "Highest expiry exposure: " + "; ".join(f"{item['expected_waste']:,} units of {item['supply']} at {item['hospital']} (batch {item['batch_id']}, {item['days_until_expiry']} days left)" for item in top) if top else "No batches currently meet the expiry-risk thresholds."
            sources = [{"tool": "get_expiry_risks", "results": len(top)}]
        elif any(term in normalized for term in ("priority", "first", "emergency")):
            top = analysis["priorities"][:5]
            response = "Priority order: " + "; ".join(f"{item['hospital']} needs {item['supply']} (score {item['priority_score']}/100, safety-stock breach in {item['days_until_stockout']} days)" for item in top) if top else "No supply allocation priorities are active."
            sources = [{"tool": "get_priority_supplies", "results": len(top)}]
        else:
            top = analysis["shortages"][:5]
            response = "Highest shortage risks: " + "; ".join(f"{item['supply']} at {item['hospital']} breaches safety stock in {item['days_until_stockout']} days and is physically depleted in {item['days_until_zero_stock']} days ({item['risk_level'].lower()})" for item in top) if top else "No active shortage risks are identified in this demo scenario."
            sources = [{"tool": "get_shortage_risks", "results": len(top)}]
        return {
            "answer": response,
            "mode": self.name,
            "sources": sources,
            "disclaimer": "Synthetic demo data; verify operational decisions with local teams.",
        }


def get_assistant_provider() -> AssistantProvider:
    # Provider implementations can be swapped here without changing API contracts.
    return RuleBasedProvider()
