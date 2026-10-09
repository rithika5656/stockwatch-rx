from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from .engine import _risk, _simulate_depletion


load_dotenv()

_DISCLAIMER = "Synthetic demo data; verify operational decisions with local teams."
_NUMBER_PATTERN = re.compile(r"(?<![\w])[-+]?\d[\d,]*(?:\.\d+)?%?(?![\w])")


class AssistantProvider(Protocol):
    name: str

    def answer(self, question: str, analysis: dict[str, Any]) -> dict[str, Any]: ...


def _parse_prompt(question: str) -> tuple[str, list[dict[str, Any]]]:
    try:
        payload = json.loads(question)
    except (TypeError, json.JSONDecodeError):
        return question.strip(), []
    if not isinstance(payload, dict) or not isinstance(payload.get("question"), str):
        return question.strip(), []
    history = []
    for message in payload.get("history", [])[-6:]:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            continue
        text = message.get("text")
        if isinstance(text, str):
            history.append({
                "role": message["role"],
                "text": text[:3000],
                "sources": message.get("sources", []) if message["role"] == "assistant" else [],
            })
    return payload["question"].strip(), history


def _normal(value: str) -> str:
    return " ".join(value.casefold().split())


def _entity_map(analysis: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    items = analysis.get("hospitals" if kind == "hospital" else "supplies", {})
    if isinstance(items, list):
        id_key = "hospital_id" if kind == "hospital" else "supply_id"
        return {str(item[id_key]): item for item in items if id_key in item}
    return items if isinstance(items, dict) else {}


def _resolve_entity(query: str | None, entities: dict[str, dict[str, Any]], kind: str) -> tuple[str | None, dict[str, Any] | None]:
    if not isinstance(query, str) or not query.strip():
        return None, None
    normalized = _normal(query)
    name_fields = ("name", "display_name", "hospital", "displayName") if kind == "hospital" else ("name", "display_name")
    exact = []
    partial = []
    for entity_id, item in entities.items():
        aliases = {str(entity_id), *(str(item[field]) for field in name_fields if item.get(field))}
        for alias in aliases:
            candidate = _normal(alias)
            if candidate == normalized:
                exact.append((entity_id, item))
                break
            if len(normalized) >= 3 and (candidate in normalized or normalized in candidate):
                partial.append((entity_id, item))
                break
    matches = exact or partial
    unique = {entity_id: item for entity_id, item in matches}
    if len(unique) == 1:
        entity_id, item = next(iter(unique.items()))
        return str(entity_id), item
    return None, None


def _find_mentioned_entity(text: str, entities: dict[str, dict[str, Any]], kind: str) -> str | None:
    normalized = _normal(text)
    name_fields = ("name", "display_name", "hospital", "displayName") if kind == "hospital" else ("name", "display_name")
    aliases = []
    for entity_id, item in entities.items():
        aliases.append((str(entity_id), str(entity_id)))
        aliases.extend((str(item[field]), str(entity_id)) for field in name_fields if item.get(field))
    for alias, entity_id in sorted(aliases, key=lambda pair: len(pair[0]), reverse=True):
        candidate = _normal(alias)
        if candidate and candidate in normalized:
            return entity_id
    words = normalized.split()
    for size in range(min(4, len(words)), 0, -1):
        for start in range(len(words) - size + 1):
            phrase = " ".join(words[start:start + size])
            if len(phrase) < 3:
                continue
            entity_id, _ = _resolve_entity(phrase, entities, kind)
            if entity_id is not None:
                return entity_id
    return None


def _filtered_rows(
    rows: list[dict[str, Any]], analysis: dict[str, Any], hospital: str | None, supply: str | None,
) -> list[dict[str, Any]] | dict[str, str]:
    hospital_id = None
    supply_id = None
    if hospital:
        hospital_id, _ = _resolve_entity(hospital, _entity_map(analysis, "hospital"), "hospital")
        if hospital_id is None:
            return {"error": f"Unknown hospital: {hospital}"}
    if supply:
        supply_id, _ = _resolve_entity(supply, _entity_map(analysis, "supply"), "supply")
        if supply_id is None:
            return {"error": f"Unknown supply: {supply}"}
    return [
        row for row in rows
        if (hospital_id is None or row.get("hospital_id") == hospital_id
            or row.get("source_hospital_id") == hospital_id
            or row.get("destination_hospital_id") == hospital_id)
        and (supply_id is None or row.get("supply_id") == supply_id)
    ]


def _tool_schema(name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


_TOOLS = [
    _tool_schema("get_shortages", "Read current shortage risks from analysis.", {
        "hospital": {"type": "string"}, "supply": {"type": "string"},
    }),
    _tool_schema("get_expiry_risks", "Read current expiry risks from analysis.", {
        "hospital": {"type": "string"}, "supply": {"type": "string"},
    }),
    _tool_schema("get_priorities", "Read current hospital supply priorities from analysis.", {
        "hospital": {"type": "string"}, "supply": {"type": "string"},
    }),
    _tool_schema("get_transfers", "Read existing safe transfer recommendations.", {
        "hospital": {"type": "string"}, "supply": {"type": "string"},
    }),
    _tool_schema("get_inventory", "Read existing inventory totals and batches for a hospital and supply.", {
        "hospital": {"type": "string"}, "supply": {"type": "string"},
    }, ["hospital", "supply"]),
    _tool_schema("get_nearby_supplies", "Read existing local transfer recommendations for a supply.", {
        "supply": {"type": "string"},
    }, ["supply"]),
    _tool_schema("simulate_demand_increase", "Use the existing deterministic forecast simulation for a demand increase.", {
        "hospital": {"type": "string"},
        "supply": {"type": "string"},
        "percent": {"type": "number", "minimum": 0, "maximum": 200},
    }, ["hospital", "supply", "percent"]),
]


class AssistantTools:
    """Small read-only adapters over the current analysis response."""

    def __init__(self, analysis: dict[str, Any]):
        self.analysis = analysis
        self._functions = {
            "get_shortages": self.get_shortages,
            "get_expiry_risks": self.get_expiry_risks,
            "get_priorities": self.get_priorities,
            "get_transfers": self.get_transfers,
            "get_inventory": self.get_inventory,
            "get_nearby_supplies": self.get_nearby_supplies,
            "simulate_demand_increase": self.simulate_demand_increase,
        }

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        function = self._functions.get(name)
        if function is None:
            return {"error": f"Unknown tool: {name}"}
        try:
            result = function(**(arguments or {}))
            return result if isinstance(result, dict) else {"items": result, "count": len(result)}
        except Exception:
            return {"error": "The requested analysis is unavailable."}

    def _rows(self, key: str, hospital: str | None = None, supply: str | None = None) -> dict[str, Any]:
        rows = self.analysis.get(key, [])
        result = _filtered_rows(rows, self.analysis, hospital, supply)
        if isinstance(result, dict):
            return result
        return {"items": result[:20], "count": len(result)}

    def get_shortages(self, hospital: str | None = None, supply: str | None = None) -> dict[str, Any]:
        return self._rows("shortages", hospital, supply)

    def get_expiry_risks(self, hospital: str | None = None, supply: str | None = None) -> dict[str, Any]:
        result = self._rows("expiry_risks", hospital, supply)
        if result.get("error"):
            return result
        at_risk = [row for row in result["items"] if float(row.get("expected_waste", 0) or 0) > 0]
        return {"items": at_risk, "count": len(at_risk)}

    def get_priorities(self, hospital: str | None = None, supply: str | None = None) -> dict[str, Any]:
        return self._rows("priorities", hospital, supply)

    def get_transfers(self, hospital: str | None = None, supply: str | None = None) -> dict[str, Any]:
        return self._rows("transfers", hospital, supply)

    def get_inventory(self, hospital: str, supply: str) -> dict[str, Any]:
        hospital_id, hospital_row = _resolve_entity(hospital, _entity_map(self.analysis, "hospital"), "hospital")
        if hospital_id is None:
            return {"error": f"Unknown hospital: {hospital}"}
        supply_id, supply_row = _resolve_entity(supply, _entity_map(self.analysis, "supply"), "supply")
        if supply_id is None:
            return {"error": f"Unknown supply: {supply}"}
        key = (hospital_id, supply_id)
        if key not in self.analysis.get("inventory_totals", {}):
            return {"error": "Inventory for that hospital is not available in this session."}
        batches = self.analysis.get("batches", {}).get(key, [])
        return {
            "hospital_id": hospital_id,
            "hospital": hospital_row.get("display_name") or hospital_row.get("name", hospital_id),
            "supply_id": supply_id,
            "supply": supply_row.get("name", supply_id),
            "current_stock": self.analysis["inventory_totals"][key],
            "safety_stock": self.analysis.get("safety_totals", {}).get(key, 0),
            "batches": [{
                "batch_id": batch.get("batch_id"),
                "quantity": batch.get("quantity", 0),
                "expiry_date": batch.get("expiry_date"),
                "batch_status": batch.get("batch_status"),
            } for batch in batches[:20]],
        }

    def get_nearby_supplies(self, supply: str) -> dict[str, Any]:
        supply_id, _ = _resolve_entity(supply, _entity_map(self.analysis, "supply"), "supply")
        if supply_id is None:
            return {"error": f"Unknown supply: {supply}"}
        rows = [row for row in self.analysis.get("transfers", []) if row.get("supply_id") == supply_id]
        return {"items": rows[:20], "count": len(rows)}

    def simulate_demand_increase(self, hospital: str, supply: str, percent: float) -> dict[str, Any]:
        hospital_id, _ = _resolve_entity(hospital, _entity_map(self.analysis, "hospital"), "hospital")
        if hospital_id is None:
            return {"error": f"Unknown hospital: {hospital}"}
        supply_id, _ = _resolve_entity(supply, _entity_map(self.analysis, "supply"), "supply")
        if supply_id is None:
            return {"error": f"Unknown supply: {supply}"}
        try:
            increase = float(percent)
        except (TypeError, ValueError):
            return {"error": "Demand increase must be a number from 0 to 200 percent."}
        if not 0 <= increase <= 200:
            return {"error": "Demand increase must be a number from 0 to 200 percent."}
        forecast = next((row for row in self.analysis.get("forecasts", [])
                         if row.get("hospital_id") == hospital_id and row.get("supply_id") == supply_id), None)
        if forecast is None:
            return {"error": "Forecast for that hospital and supply is not available in this session."}
        adjusted_demand = forecast["forecast_daily_demand"] * (1 + increase / 100)
        depletion = _simulate_depletion(forecast["current_stock"], forecast["safety_stock"], adjusted_demand, 0)
        return {
            "hospital_id": hospital_id,
            "hospital": forecast["hospital"],
            "supply_id": supply_id,
            "supply": forecast["supply"],
            "percent_increase": increase,
            "current_forecast_daily_demand": forecast["forecast_daily_demand"],
            "adjusted_daily_demand": round(adjusted_demand, 1),
            "days_to_safety_stock": depletion["days_to_safety_stock"],
            "days_to_zero_stock": depletion["days_to_zero_stock"],
            "risk_level": _risk(depletion["days_to_safety_stock"]),
        }


def _is_tamil(text: str) -> bool:
    return bool(re.search(r"[\u0B80-\u0BFF]", text))


def _format_tool_answer(name: str, result: dict[str, Any], tamil: bool = False) -> str:
    if result.get("error"):
        if tamil:
            return f"தரவைப் பெற முடியவில்லை: {result['error']}"
        return result["error"]
    items = result.get("items", [])
    if name == "get_inventory":
        if tamil:
            return f"{result['hospital']} - {result['supply']}: இருப்பு {result['current_stock']} units; பாதுகாப்பு இருப்பு {result['safety_stock']} units."
        return (f"{result['hospital']} has {result['current_stock']} units of {result['supply']} in stock; "
                f"safety stock is {result['safety_stock']} units.")
    if name == "simulate_demand_increase":
        if tamil:
            return (f"{result['hospital']} இல் {result['supply']} தேவையை {result['percent_increase']}% உயர்த்தினால், "
                    f"தினசரி தேவை {result['adjusted_daily_demand']} units ஆகும். பாதுகாப்பு இருப்பு {result['days_to_safety_stock']} நாட்களில் எட்டும்; "
                    f"முழு இருப்பு {result['days_to_zero_stock']} நாட்களில் தீரும்.")
        return (f"A {result['percent_increase']}% demand increase at {result['hospital']} would raise {result['supply']} use to "
                f"{result['adjusted_daily_demand']} units/day. Safety stock is reached in {result['days_to_safety_stock']} days; "
                f"physical stock reaches zero in {result['days_to_zero_stock']} days.")
    if not items:
        if name == "get_expiry_risks":
            return "ஆபத்தில் உள்ள batches இல்லை." if tamil else "No batches at risk."
        return "தொடர்புடைய பதிவுகள் இல்லை." if tamil else "No matching records were found."
    if name in ("get_shortages", "get_priorities"):
        lines = [f"{row.get('hospital')}: {row.get('supply')} — {row.get('days_until_stockout')} days, {row.get('risk_level', row.get('priority'))}" for row in items[:5]]
        return ("பற்றாக்குறை முன்னுரிமைகள்: " if tamil else "Current shortage priorities: ") + "; ".join(lines)
    if name == "get_expiry_risks":
        lines = [f"{row.get('supply')} at {row.get('hospital')}: {row.get('expected_waste')} units, expires in {row.get('days_until_expiry')} days" for row in items[:5]]
        return ("காலாவதி அபாயங்கள்: " if tamil else "Expiry risks: ") + "; ".join(lines)
    if name in ("get_transfers", "get_nearby_supplies"):
        lines = []
        for row in items[:5]:
            minutes = row.get("estimated_transport_minutes")
            route = f"ETA {minutes} minutes" if minutes is not None else "route unverified"
            lines.append(f"{row.get('recommended_quantity')} {row.get('supply')} from {row.get('source_hospital')} to {row.get('destination_hospital')} ({route})")
        return ("அருகிலுள்ள பரிந்துரைகள்: " if tamil else "Nearby transfer recommendations: ") + "; ".join(lines)
    return str(result)


def _mentioned_entity(question: str, analysis: dict[str, Any], kind: str) -> str | None:
    return _find_mentioned_entity(question, _entity_map(analysis, kind), kind)


class RuleBasedProvider:
    name = "RULE-BASED"

    def answer(self, question: str, analysis: dict[str, Any]) -> dict[str, Any]:
        current_question, _ = _parse_prompt(question)
        normalized = current_question.casefold()
        tamil = _is_tamil(current_question)
        tools = AssistantTools(analysis)
        hospital = _mentioned_entity(current_question, analysis, "hospital")
        if hospital is None and any(phrase in normalized for phrase in ("my hospital", "my facility", "என் மருத்துவமனை")):
            scoped_ids = {row.get("hospital_id") for row in analysis.get("forecasts", []) if row.get("hospital_id")}
            if len(scoped_ids) == 1:
                hospital = next(iter(scoped_ids))
        supply = _mentioned_entity(current_question, analysis, "supply")
        if any(term in normalized for term in ("partially fulfilled", "remaining need", "மீதமுள்ள தேவை")):
            request = next((row for row in analysis.get("visible_supply_requests", [])
                            if row.get("remaining_quantity", 0) > 0 and row.get("allocated_quantity", 0) > 0), None)
            if request:
                matching = ("An eligible source offer is open for the remaining quantity."
                            if request.get("matching_status") == "OFFERED" else
                            "The system is searching eligible nearby sources for the remaining quantity.")
                answer = (f"{request['destination_hospital']} requested {request['requested_quantity']:,} units of {request['supply']}. "
                          f"{request['allocated_quantity']:,} units are allocated ({request['fulfilled_quantity']:,} delivered), "
                          f"leaving {request['remaining_quantity']:,} units unresolved. {matching}")
                source = {"tool": "get_supply_request", "request_id": request["request_id"],
                          "result": {key: request.get(key) for key in ("requested_quantity", "allocated_quantity", "fulfilled_quantity", "remaining_quantity")}}
                return {"answer": answer, "sources": [source], "mode": self.name, "disclaimer": _DISCLAIMER}
        if any(term in normalized for term in ("two hospitals", "multiple hospitals", "இரண்டு மருத்துவமனை")):
            request = next((row for row in analysis.get("visible_supply_requests", [])
                            if len({leg["source_hospital_id"] for leg in row.get("legs", [])
                                    if leg.get("status") not in ("REJECTED", "EXPIRED", "CANCELLED")}) > 1), None)
            if request:
                source_totals: dict[str, int] = {}
                for leg in request["legs"]:
                    if leg.get("status") not in ("REJECTED", "EXPIRED", "CANCELLED"):
                        source_totals[leg["source_hospital"]] = source_totals.get(leg["source_hospital"], 0) + int(leg["allocated_quantity"])
                sources_text = ", ".join(f"{name} {quantity:,}" for name, quantity in source_totals.items())
                answer = (f"{request['destination_hospital']} requested {request['requested_quantity']:,} units of {request['supply']}. "
                          f"The matched sources are {sources_text}; the request was split across eligible hospitals.")
                source = {"tool": "get_supply_request", "request_id": request["request_id"], "result": source_totals}
                return {"answer": answer, "sources": [source], "mode": self.name, "disclaimer": _DISCLAIMER}
        tool_name = "get_shortages"
        arguments: dict[str, Any] = {}
        if any(term in normalized for term in ("what if", "what happens if", "increase", "rises", "demand increase", "உயர்ந்தால்", "அதிகரித்தால்")):
            percent_match = re.search(r"(\d{1,3})\s*%", normalized)
            if not hospital or not supply or not percent_match:
                answer = "Please specify a hospital, supply, and demand increase percentage." if not tamil else "மருத்துவமனை, பொருள், மற்றும் தேவை அதிகரிப்பு சதவீதத்தைக் குறிப்பிடவும்."
                return {"answer": answer, "sources": [], "mode": self.name, "disclaimer": _DISCLAIMER}
            tool_name = "simulate_demand_increase"
            arguments = {"hospital": hospital, "supply": supply, "percent": int(percent_match.group(1))}
        elif any(term in normalized for term in ("expir", "wast", "காலாவதி", "வீணா")):
            tool_name = "get_expiry_risks"
            arguments = {key: value for key, value in (("hospital", hospital), ("supply", supply)) if value}
        elif any(term in normalized for term in ("priority", "priorit", "critical", "முன்னுரிமை")):
            tool_name = "get_priorities"
            arguments = {key: value for key, value in (("hospital", hospital), ("supply", supply)) if value}
        elif any(term in normalized for term in ("nearby", "near me", "local supply", "அருகில்")):
            tool_name = "get_nearby_supplies"
            if supply:
                arguments = {"supply": supply}
            else:
                answer = "Please name a supply to check nearby recommendations." if not tamil else "அருகிலுள்ள பரிந்துரைகளைப் பார்க்கும் பொருளின் பெயரைக் குறிப்பிடவும்."
                return {"answer": answer, "sources": [], "mode": self.name, "disclaimer": _DISCLAIMER}
        elif any(term in normalized for term in ("inventory", "in stock", "stock at", "batch", "இருப்பு")) and (hospital or supply):
            tool_name = "get_inventory"
            if not hospital or not supply:
                answer = "Please specify both a hospital and a supply." if not tamil else "மருத்துவமனை மற்றும் பொருளைக் குறிப்பிடவும்."
                return {"answer": answer, "sources": [], "mode": self.name, "disclaimer": _DISCLAIMER}
            arguments = {"hospital": hospital, "supply": supply}
        elif any(term in normalized for term in ("transfer", "redistribut", "move stock", "மாற்று")):
            tool_name = "get_transfers"
            arguments = {key: value for key, value in (("hospital", hospital), ("supply", supply)) if value}
        elif hospital or supply:
            tool_name = "get_shortages"
            arguments = {key: value for key, value in (("hospital", hospital), ("supply", supply)) if value}

        result = tools.call(tool_name, arguments)
        source = {"tool": tool_name, "arguments": arguments, "result": result}
        answer = _format_tool_answer(tool_name, result, tamil)
        return {"answer": answer, "sources": [source], "mode": self.name, "disclaimer": _DISCLAIMER}


def _numeric_values(value: Any) -> set[Decimal]:
    values: set[Decimal] = set()
    if isinstance(value, bool):
        return values
    if isinstance(value, (int, float)):
        try:
            values.add(Decimal(str(value)).normalize())
        except InvalidOperation:
            pass
    elif isinstance(value, dict):
        for nested in value.values():
            values.update(_numeric_values(nested))
    elif isinstance(value, list):
        for nested in value:
            values.update(_numeric_values(nested))
    return values


def _numbers_are_grounded(answer: str, sources: list[dict[str, Any]], history: list[dict[str, Any]]) -> bool:
    allowed: set[Decimal] = set()
    for source in sources:
        allowed.update(_numeric_values(source.get("result")))
    for message in history:
        for source in message.get("sources", []):
            if isinstance(source, dict):
                allowed.update(_numeric_values(source.get("result")))
    for token in _NUMBER_PATTERN.findall(answer):
        try:
            number = Decimal(token.rstrip("%").replace(",", "")).normalize()
        except InvalidOperation:
            return False
        if number not in allowed:
            return False
    return True


class LLMProvider:
    name = "LLM"

    def __init__(self, api_key: str, provider: str):
        self.api_key = api_key
        self.provider = provider
        base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        self.model = os.getenv("LLM_MODEL", "gpt-4o-mini")
        self.fallback = RuleBasedProvider()

    def _request(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "tools": _TOOLS,
            "tool_choice": "auto",
            "temperature": 0.1,
        }).encode("utf-8")
        request = Request(self.endpoint, data=body, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def answer(self, question: str, analysis: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._answer(question, analysis)
        except Exception:
            return self.fallback.answer(question, analysis)

    def _answer(self, question: str, analysis: dict[str, Any]) -> dict[str, Any]:
        current_question, history = _parse_prompt(question)
        messages: list[dict[str, Any]] = [{
            "role": "system",
            "content": (
                "You are StockWatch-RX assistant. Reply in the same language as the latest user message, "
                "especially Tamil or English. Use the provided read-only tools for factual operational data. "
                "Never invent, estimate, or state a number unless it appears in a tool result from this session. "
                "Do not claim to change inventory, requests, or any other data. If a tool says an entity or "
                "record is unavailable, say so plainly. Keep answers concise."
            ),
        }]
        scoped_ids = {row.get("hospital_id") for row in analysis.get("forecasts", []) if row.get("hospital_id")}
        if len(scoped_ids) == 1:
            hospital_id = next(iter(scoped_ids))
            hospital = analysis.get("hospitals", {}).get(hospital_id, {})
            hospital_name = hospital.get("display_name") or hospital.get("name") or hospital_id
            messages.append({
                "role": "system",
                "content": f"Authenticated hospital context: {hospital_name} ({hospital_id}). Resolve 'my hospital' to this hospital.",
            })
        for message in history[-6:]:
            messages.append({"role": message["role"], "content": message["text"]})
            if message["role"] == "assistant" and message.get("sources"):
                prior_results = [
                    {"tool": source.get("tool"), "result": source.get("result")}
                    for source in message["sources"] if isinstance(source, dict) and "result" in source
                ]
                if prior_results:
                    messages.append({
                        "role": "system",
                        "content": "Prior read-only tool results for this conversation: " + json.dumps(prior_results, ensure_ascii=False),
                    })
        messages.append({"role": "user", "content": current_question})
        tools = AssistantTools(analysis)
        sources: list[dict[str, Any]] = []
        for _ in range(5):
            response = self._request(messages)
            choice = response["choices"][0]["message"]
            calls = choice.get("tool_calls") or []
            if not calls:
                answer = choice.get("content") or ""
                if not _numbers_are_grounded(answer, sources, history):
                    return self.fallback.answer(question, analysis)
                return {"answer": answer, "sources": sources, "mode": self.name, "disclaimer": _DISCLAIMER}
            messages.append(choice)
            for call in calls:
                function = call.get("function", {})
                name = function.get("name", "")
                arguments = json.loads(function.get("arguments") or "{}")
                result = tools.call(name, arguments)
                sources.append({"tool": name, "arguments": arguments, "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(result, ensure_ascii=False),
                })
        return self.fallback.answer(question, analysis)


def get_assistant_provider() -> AssistantProvider:
    try:
        api_key = os.getenv("LLM_API_KEY", "").strip()
        provider = os.getenv("LLM_PROVIDER", "").strip().casefold()
        if api_key and provider in {"openai", "openai-compatible"}:
            return LLMProvider(api_key, provider)
    except Exception:
        pass
    return RuleBasedProvider()
