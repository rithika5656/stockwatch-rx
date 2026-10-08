from __future__ import annotations

import hashlib
import secrets
from typing import Any

DEMO_PASSWORD = "demo123"
HOSPITAL_USERS = {
    "H001": {"hospital_id": "H001", "hospital_name": "Kovai Medical Center and Hospital", "role": "hospital_user"},
    "H002": {"hospital_id": "H002", "hospital_name": "PSG Hospitals", "role": "hospital_user"},
    "H003": {"hospital_id": "H003", "hospital_name": "Kumaran Medical Center-Multispeciality Hospital", "role": "hospital_user"},
}
PASSWORD_HASHES = {hospital_id: hashlib.sha256(DEMO_PASSWORD.encode()).hexdigest() for hospital_id in HOSPITAL_USERS}
TOKENS: dict[str, dict[str, Any]] = {}


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def login(hospital_id: str, password: str) -> dict[str, Any] | None:
    user = HOSPITAL_USERS.get(hospital_id.upper())
    if not user or not secrets.compare_digest(PASSWORD_HASHES[hospital_id.upper()], _hash_password(password)):
        return None
    token = f"demo-{secrets.token_urlsafe(24)}"
    TOKENS[token] = dict(user)
    return {"access_token": token, "token_type": "bearer", "user": dict(user), "demo_mode": True}


def user_from_token(token: str | None) -> dict[str, Any]:
    if not token:
        raise ValueError("Authentication is required")
    cleaned = token.removeprefix("Bearer ").strip()
    user = TOKENS.get(cleaned)
    if user:
        return dict(user)
    raise ValueError("Session is invalid or expired; please sign in again")
