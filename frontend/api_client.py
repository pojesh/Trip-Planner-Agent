"""Thin HTTP client for the FastAPI backend. The UI never calls external providers directly."""

import json
import os
from pathlib import Path
from typing import Iterator, Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
# On Windows "localhost" tries IPv6 (::1) first and waits ~2 s before falling back to IPv4,
# which made every rerun slow. The backend listens on 127.0.0.1, so talk to it directly.
BASE_URL = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:8000").rstrip("/").replace("//localhost", "//127.0.0.1")

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_STREAM_TIMEOUT = httpx.Timeout(300.0, connect=5.0)
_http = httpx.Client(timeout=_TIMEOUT)  # keep-alive connection reused across reruns


class ApiError(Exception):
    def __init__(self, message: str, code: str = "error", retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable


BACKEND_DOWN = ApiError("Can't reach the planner backend. Start it with `python run.py` "
                        "(or `uvicorn backend.main:app`).", code="backend_down", retryable=True)


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        err = resp.json().get("error", {})
        raise ApiError(err.get("message", "Request failed."), err.get("code", "error"), err.get("retryable", False))
    except (ValueError, AttributeError):
        raise ApiError(f"Request failed ({resp.status_code}).")


def _request(method: str, path: str, **kwargs):
    try:
        resp = _http.request(method, f"{BASE_URL}{path}", **kwargs)
    except httpx.TimeoutException:
        raise ApiError("The planner took too long to respond. Please retry.", "timeout", True)
    except httpx.TransportError:
        raise BACKEND_DOWN
    _raise_for(resp)
    return resp.json() if resp.content else None


def _stream(path: str, payload: dict) -> Iterator[dict]:
    try:
        with _http.stream("POST", f"{BASE_URL}{path}", json=payload, timeout=_STREAM_TIMEOUT) as resp:
            if resp.status_code >= 400:
                resp.read()
                _raise_for(resp)
            for line in resp.iter_lines():
                if line.strip():
                    yield json.loads(line)
    except httpx.TimeoutException:
        raise ApiError("Planning took too long. Please retry.", "timeout", True)
    except httpx.TransportError:
        raise BACKEND_DOWN


# ------------------------------------------------------------------ endpoints

def health() -> dict:
    return _request("GET", "/api/health")


def search_places(query: str, near_bbox: Optional[list[float]] = None) -> list[dict]:
    body = {"query": query, "limit": 5}
    if near_bbox:
        body["near_bbox"] = near_bbox
    return _request("POST", "/api/geocode/search", json=body)


def generate(payload: dict) -> Iterator[dict]:
    return _stream("/api/trips/generate", payload)


def chat(trip_id: str, message: str) -> Iterator[dict]:
    return _stream(f"/api/trips/{trip_id}/chat", {"message": message})


def list_trips() -> list[dict]:
    return _request("GET", "/api/trips")


def get_trip(trip_id: str) -> dict:
    return _request("GET", f"/api/trips/{trip_id}")


def save_trip(trip_id: str, title: Optional[str] = None) -> dict:
    return _request("POST", f"/api/trips/{trip_id}/save", json={"title": title})


def delete_trip(trip_id: str) -> None:
    _request("DELETE", f"/api/trips/{trip_id}")


def update_stop(trip_id: str, day_id: str, stop_id: str, **changes) -> dict:
    return _request("PATCH", f"/api/trips/{trip_id}/days/{day_id}/stops/{stop_id}", json=changes)


def remove_stop(trip_id: str, day_id: str, stop_id: str) -> dict:
    return _request("DELETE", f"/api/trips/{trip_id}/days/{day_id}/stops/{stop_id}")


def add_stop(trip_id: str, day_id: str, candidate_id: str) -> dict:
    return _request("POST", f"/api/trips/{trip_id}/days/{day_id}/stops", json={"candidate_id": candidate_id})


def recalculate(trip_id: str, day_id: str) -> dict:
    return _request("POST", f"/api/trips/{trip_id}/days/{day_id}/recalculate")
