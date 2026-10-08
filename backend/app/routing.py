from __future__ import annotations

import json
import os
from functools import lru_cache
from urllib.error import URLError
from urllib.request import Request, urlopen
from typing import Any


@lru_cache(maxsize=64)
def _road_route_cached(source_latitude: float, source_longitude: float, destination_latitude: float, destination_longitude: float) -> dict[str, Any]:
    base_url = os.getenv("OSRM_ROUTING_URL", "https://router.project-osrm.org").rstrip("/")
    coordinates = f"{source_longitude},{source_latitude};{destination_longitude},{destination_latitude}"
    request = Request(
        f"{base_url}/route/v1/driving/{coordinates}?overview=full&geometries=geojson&steps=false",
        headers={"Accept": "application/json", "User-Agent": "StockWatch-RX/1.0"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
    except (TimeoutError, URLError, OSError, json.JSONDecodeError) as error:
        raise ValueError("Road routing service is unavailable") from error
    route = next(iter(payload.get("routes", [])), None)
    coordinates = route.get("geometry", {}).get("coordinates", []) if route else []
    if not route or len(coordinates) < 2:
        raise ValueError("No road route is available between these hospitals")
    return {
        "coordinates": [[latitude, longitude] for longitude, latitude in coordinates],
        "distance_km": round(route["distance"] / 1000, 2),
        "duration_minutes": max(1, round(route["duration"] / 60)),
        "provider": "OSRM",
        "route_available": True,
    }


def road_route(source: dict[str, Any], destination: dict[str, Any]) -> dict[str, Any]:
    return _road_route_cached(
        float(source["latitude"]), float(source["longitude"]),
        float(destination["latitude"]), float(destination["longitude"]),
    )