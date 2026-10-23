"""Records each step of a support-assistant turn for the Dev Board.

Every step is a plain dict event ({"type": "trace", "kind": ..., "ms": ...}) that is
streamed to the UI as it happens and saved with the turn's summary afterwards.
"""

from time import perf_counter
from typing import Optional


def usage(ai) -> Optional[dict]:
    """Token counts from a LangChain AIMessage, or None when the provider sent none."""
    u = getattr(ai, "usage_metadata", None) or {}
    if not u:
        return None
    inp, out = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    return {"input": inp, "output": out, "total": int(u.get("total_tokens") or inp + out)}


class Tracer:
    def __init__(self, model: Optional[str] = None):
        self.model = model
        self.started = perf_counter()
        self.events: list[dict] = []
        self.cited: list[str] = []  # "[WH-POL-003 p.2]" citations in the final answer

    @staticmethod
    def clock() -> float:
        return perf_counter()

    def add(self, kind: str, label: str, started: Optional[float] = None, **data) -> dict:
        """Record a step. `started` (from clock()) gives the step's latency in ms."""
        now = perf_counter()
        event = {"type": "trace", "kind": kind, "label": label,
                 "ms": round((now - started) * 1000, 1) if started is not None else None,
                 "at_ms": round((now - self.started) * 1000, 1), **data}
        self.events.append(event)
        return event

    def summary(self) -> dict:
        llm = [e for e in self.events if e["kind"] == "llm"]
        counted = [e["tokens"] for e in llm if e.get("tokens")]
        tokens = ({k: sum(t[k] for t in counted) for k in ("input", "output", "total")} if counted else None)
        docs = sorted({c["doc_code"] for e in self.events if e["kind"] == "retrieval" for c in e.get("chunks", [])})
        return {
            "total_ms": round((perf_counter() - self.started) * 1000, 1),
            "model": self.model,
            "llm_calls": len(llm),
            "llm_ms": round(sum(e["ms"] or 0 for e in llm), 1),
            "tokens": tokens,  # None = the provider reported no token usage
            "tokens_partial": bool(counted) and len(counted) < len(llm),
            "tool_calls": sum(1 for e in self.events if e["kind"] == "tool"),
            "retrievals": sum(1 for e in self.events if e["kind"] == "retrieval"),
            "docs_retrieved": docs,
            "citations": self.cited,
        }
