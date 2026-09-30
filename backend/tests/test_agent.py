"""The LangChain agent loop, driven by a scripted fake chat model."""

from langchain_core.messages import AIMessage, ToolMessage

from backend.domain.models import Location, TransportMode
from backend.services.agent import AgentContext, TripAgent
from backend.tests.conftest import FakeNominatim, FakeOverpass, FakeRouter


class ScriptedLLM:
    """Stands in for ChatGoogleGenerativeAI: returns pre-written AI messages in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []  # the message list at each call

    def bind_tools(self, tools):
        self.tool_names = [t.name for t in tools]
        return self

    def invoke(self, messages):
        self.seen.append(list(messages))
        return self.replies.pop(0)


def call(name, args, cid="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


def ctx(days=1):
    return AgentContext(destination=Location(label="Lisbon", lat=38.72, lon=-9.14), radius_m=5000,
                        origin=Location(label="Hotel", lat=38.716, lon=-9.141), mode=TransportMode.walking,
                        daily_start="10:00", daily_end="20:00", return_to_start=True, day_count=days)


def run(agent, c, allow_questions=True):
    gen = agent.run(c, "Plan it", allow_questions)
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return stop.value, events


def plan_args(ids, day_number=1):
    return {"days": [{"day_number": day_number, "stops": [{"candidate_id": i, "visit_minutes": 60} for i in ids]}],
            "summary": "A nice day"}


def test_agent_finds_checks_and_submits():
    llm = ScriptedLLM([
        call("find_places", {"interests": ["culture"]}),
        call("check_route", {"stops": [{"candidate_id": "node/1", "visit_minutes": 60},
                                       {"candidate_id": "node/2", "visit_minutes": 45}]}),
        call("submit_itinerary", plan_args(["node/1", "node/2", "node/3"])),
    ])
    c = ctx()
    result, events = run(TripAgent(llm, FakeOverpass(), FakeNominatim(), FakeRouter()), c)
    assert result.kind == "plan" and [s.candidate_id for s in result.plan.days[0].stops] == ["node/1", "node/2", "node/3"]
    assert [e["stage"] for e in events] == ["discover", "route"]
    route_msg = llm.seen[2][-1]
    assert isinstance(route_msg, ToolMessage) and "fits_window" in route_msg.content


def test_invalid_submit_gets_errors_then_fixed():
    llm = ScriptedLLM([
        call("find_places", {"interests": ["culture"]}),
        call("submit_itinerary", plan_args(["node/1", "node/999"])),       # unknown id
        call("submit_itinerary", plan_args(["node/1", "node/2"]), cid="c3"),
    ])
    result, _ = run(TripAgent(llm, FakeOverpass(), FakeNominatim(), FakeRouter()), ctx())
    assert result.kind == "plan"
    feedback = llm.seen[2][-1]
    assert isinstance(feedback, ToolMessage) and "node/999" in feedback.content


def test_ask_user_and_plain_reply():
    llm = ScriptedLLM([call("ask_user", {"question": "Vegetarian?", "quick_replies": ["Yes", "No"]})])
    result, _ = run(TripAgent(llm, FakeOverpass(), FakeNominatim(), FakeRouter()), ctx())
    assert result.kind == "question" and result.quick_replies == ["Yes", "No"]
    assert "ask_user" in llm.tool_names

    llm = ScriptedLLM([AIMessage(content="Day 1 has the most walking.")])
    result, _ = run(TripAgent(llm, FakeOverpass(), FakeNominatim(), FakeRouter()), ctx(), allow_questions=False)
    assert result.kind == "reply" and "walking" in result.message
    assert "ask_user" not in llm.tool_names


def test_find_places_includes_must_see_and_reports_missing():
    llm = ScriptedLLM([
        call("find_places", {"interests": ["culture"], "must_see": ["Belem Tower", "Atlantis"]}),
        call("submit_itinerary", plan_args(["way/900", "node/1"])),
    ])
    c = ctx()
    result, _ = run(TripAgent(llm, FakeOverpass(), FakeNominatim(), FakeRouter()), c)
    tool_out = llm.seen[1][-1].content
    assert '"must_see": true' in tool_out and '"not_found": ["Atlantis"]' in tool_out
    assert c.candidates["way/900"].must_see and c.must_see_missing == ["Atlantis"]
    assert result.kind == "plan"


def test_wrong_day_count_rejected():
    llm = ScriptedLLM([
        call("find_places", {"interests": ["culture"]}),
        call("submit_itinerary", plan_args(["node/1"])),  # only 1 of 2 days
        AIMessage(content="I give up"),
    ])
    result, _ = run(TripAgent(llm, FakeOverpass(), FakeNominatim(), FakeRouter()), ctx(days=2))
    assert "exactly 2 day" in llm.seen[2][-1].content
    assert result.kind == "reply"
