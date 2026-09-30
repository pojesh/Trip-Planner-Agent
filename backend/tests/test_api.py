"""End-to-end API tests with fake providers and a temporary SQLite database."""

import json

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.deps import get_plan_service
from backend.services.plan_service import PlanService
from backend.tests.conftest import FakeNominatim, FakeOverpass, FakeRouter, trip_request_json
from backend.tests.test_agent import ScriptedLLM, call
from backend.services.agent import TripAgent


@pytest.fixture
def make_client(repo, monkeypatch):
    monkeypatch.setattr(main, "get_repo", lambda: repo)

    def _make(agent_llm=None):
        overpass, router = FakeOverpass(), FakeRouter()
        agent = TripAgent(agent_llm, overpass, FakeNominatim(), router) if agent_llm else None
        svc = PlanService(repo, FakeNominatim(), overpass, router, agent)
        main.app.dependency_overrides[get_plan_service] = lambda: svc
        return TestClient(main.app), svc
    yield _make
    main.app.dependency_overrides.clear()


def events(resp):
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def generate(client, **overrides):
    with client:
        resp = client.post("/api/trips/generate", json=trip_request_json(**overrides))
    return events(resp)


def test_health_and_geocode(make_client):
    client, _ = make_client()
    with client:
        assert client.get("/api/health").json()["status"] == "ok"
        res = client.post("/api/geocode/search", json={"query": "Lisbon"}).json()
        assert res[0]["name"] == "Lisbon"


def test_generate_builtin_edit_save_delete(make_client, repo):
    client, svc = make_client()
    evs = generate(client)
    assert [e["type"] for e in evs][-1] == "result"
    trip = evs[-1]["trip"]
    assert trip["status"] == "generated" and len(trip["days"]) == 2
    assert trip["constraints"]["planner"] == "built-in"
    day = trip["days"][0]
    assert day["stops"] and len(day["legs"]) == len(day["stops"]) + 1  # includes return leg
    assert all(s["source_url"].startswith("https://www.openstreetmap.org/") for s in day["stops"])

    with client:
        assert client.get("/api/trips").json() == []  # drafts are not listed

        # move the last stop first: only changed legs are re-routed
        last = day["stops"][-1]
        r = client.patch(f"/api/trips/{trip['id']}/days/{day['id']}/stops/{last['id']}", json={"move_to": 1}).json()
        assert r["trip"]["days"][0]["stops"][0]["id"] == last["id"]
        assert 0 < r["rerouted_legs"] < len(day["legs"])

        # remove a stop
        first = r["trip"]["days"][0]["stops"][0]
        r = client.delete(f"/api/trips/{trip['id']}/days/{day['id']}/stops/{first['id']}").json()
        assert all(s["id"] != first["id"] for s in r["trip"]["days"][0]["stops"])

        # note + unknown candidate
        s0 = r["trip"]["days"][0]["stops"][0]
        r = client.patch(f"/api/trips/{trip['id']}/days/{day['id']}/stops/{s0['id']}", json={"note": "Buy tickets"}).json()
        assert r["trip"]["days"][0]["stops"][0]["user_note"] == "Buy tickets"
        bad = client.post(f"/api/trips/{trip['id']}/days/{day['id']}/stops", json={"candidate_id": "node/nope"})
        assert bad.status_code == 404 and bad.json()["error"]["code"] == "unknown_candidate"

        saved = client.post(f"/api/trips/{trip['id']}/save", json={"title": "Lisbon weekend"}).json()
        assert saved["status"] == "saved" and saved["title"] == "Lisbon weekend"
        listed = client.get("/api/trips").json()
        assert [t["id"] for t in listed] == [trip["id"]]
        reopened = client.get(f"/api/trips/{trip['id']}").json()
        assert reopened["days"][0]["stops"][0]["user_note"] == "Buy tickets"
        assert reopened["days"][0]["legs"][0]["geometry"]

        assert client.delete(f"/api/trips/{trip['id']}").status_code == 204
        assert client.get(f"/api/trips/{trip['id']}").status_code == 404


def test_invalid_request_rejected(make_client):
    client, _ = make_client()
    with client:
        r = client.post("/api/trips/generate", json=trip_request_json(end_date="2026-09-01"))
    assert r.status_code == 422 and "end date" in r.json()["error"]["message"]


def test_generate_with_agent_question_then_plan(make_client):
    llm = ScriptedLLM([
        call("find_places", {"interests": ["culture"]}),
        call("ask_user", {"question": "Do you want a long lunch?", "quick_replies": ["Yes", "No"]}),
    ])
    client, _ = make_client(llm)
    evs = generate(client)
    assert evs[-1]["type"] == "question" and evs[-1]["quick_replies"] == ["Yes", "No"]

    plan = {"days": [{"day_number": n, "stops": [{"candidate_id": f"node/{i}", "visit_minutes": 60}
                                                 for i in ids]} for n, ids in ((1, (1, 2, 3)), (2, (6, 7, 8)))],
            "summary": "Two easy days"}
    llm.replies = [call("find_places", {"interests": ["culture"]}), call("submit_itinerary", plan)]
    evs = generate(client, clarifications=[{"question": "Do you want a long lunch?", "answer": "No"}])
    trip = evs[-1]["trip"]
    assert trip["constraints"]["planner"] == "gemini" and trip["agent_summary"] == "Two easy days"
    assert [m["role"] for m in trip["chat"]] == ["assistant", "user", "assistant"]


def test_question_before_any_search_reaches_the_user(make_client):
    llm = ScriptedLLM([call("ask_user", {"question": "Museums or outdoors first?", "quick_replies": ["Museums", "Outdoors"]})])
    client, _ = make_client(llm)
    evs = generate(client)
    assert evs[-1] == {"type": "question", "question": "Museums or outdoors first?",
                       "quick_replies": ["Museums", "Outdoors"]}


def test_agent_failure_falls_back(make_client):
    class Broken(ScriptedLLM):
        def invoke(self, messages):
            raise RuntimeError("quota")
    client, _ = make_client(Broken([]))
    evs = generate(client)
    assert evs[-1]["type"] == "result" and evs[-1]["trip"]["constraints"]["planner"] == "built-in"


def test_chat_updates_plan(make_client):
    llm = ScriptedLLM([
        call("find_places", {"interests": ["culture"]}),
        call("submit_itinerary", {"days": [{"day_number": 1, "stops": [{"candidate_id": "node/1", "visit_minutes": 60},
                                                                     {"candidate_id": "node/2", "visit_minutes": 60},
                                                                     {"candidate_id": "node/3", "visit_minutes": 60}]}],
                                  "summary": "Day planned"}),
    ])
    client, _ = make_client(llm)
    trip = generate(client, end_date="2026-10-01")[-1]["trip"]
    llm.replies = [call("submit_itinerary", {"days": [{"day_number": 1, "stops": [
        {"candidate_id": "node/2", "visit_minutes": 60}, {"candidate_id": "node/4", "visit_minutes": 30}]}],
        "summary": "Swapped in a lighter day"})]
    with client:
        resp = client.post(f"/api/trips/{trip['id']}/chat", json={"message": "lighter please"})
    last = events(resp)[-1]
    assert last["type"] == "result" and last["changed"]
    assert [s["osm_id"] for s in last["trip"]["days"][0]["stops"]] == ["2", "4"]
    assert last["trip"]["chat"][-1]["content"] == "Swapped in a lighter day"
