"""Policy search over the local Chroma index, with relevance scores and timings for the Dev Board."""

import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Optional

from backend.errors import AppError
from backend.support import ingest


@dataclass
class Chunk:
    cite: str  # "[WH-POL-003 p.2]": how the agent must cite it
    doc_code: str
    title: str
    version: str
    page: int
    score: float  # cosine relevance, 0..1
    relevant: bool  # score >= threshold
    text: str
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


class PolicyRetriever:
    """Opens (and if needed builds) the index lazily, so the app starts fast."""

    def __init__(self, data_dir: Path, chroma_dir: Path, model_name: str, threshold: float, top_k: int,
                 embeddings=None):
        self.data_dir, self.chroma_dir = Path(data_dir), Path(chroma_dir)
        self.model_name, self.threshold, self.top_k = model_name, threshold, top_k
        self._embeddings = embeddings
        self._store = None
        self.manifest: Optional[dict] = None
        self._lock = threading.Lock()

    def _ensure(self, force: bool = False):
        with self._lock:
            if self._store is None or force:
                try:
                    if self._embeddings is None:
                        self._embeddings = ingest.make_embeddings(self.model_name)
                    self._store, self.manifest = ingest.build_index(
                        self.data_dir, self.chroma_dir, self._embeddings, self.model_name, force=force)
                except ImportError as e:
                    raise AppError("rag_unavailable", f"The policy search needs extra packages ({e.name}). "
                                   "Run `pip install -r requirements.txt`.", http_status=503)
            return self._store

    def reindex(self) -> dict:
        self._ensure(force=True)
        return self.stats()

    def stats(self) -> dict:
        self._ensure()
        m = self.manifest or {}
        return {"chunks": m.get("chunks", 0), "pdfs": len(m.get("files", {})), "docs": m.get("docs", []),
                "built_at": m.get("built_at"), "model": self.model_name, "threshold": self.threshold,
                "data_dir": str(self.data_dir)}

    def search(self, query: str, trip_type: Optional[str] = None, doc_code: Optional[str] = None,
               k: Optional[int] = None) -> tuple[list[Chunk], dict]:
        """Top-k chunks (best first) and timings {"embed_ms", "query_ms"}."""
        store = self._ensure()
        conditions = []
        if trip_type in ("domestic", "international"):
            conditions.append({trip_type: True})
        if doc_code:
            conditions.append({"doc_code": doc_code.strip().upper()})
        where = None if not conditions else conditions[0] if len(conditions) == 1 else {"$and": conditions}

        t0 = perf_counter()
        vector = self._embeddings.embed_query(query)
        t1 = perf_counter()
        hits = store.similarity_search_by_vector_with_relevance_scores(vector, k=k or self.top_k, filter=where)
        t2 = perf_counter()

        chunks = []
        for doc, distance in hits:
            m = doc.metadata
            score = round(1 - float(distance), 3)  # cosine distance -> similarity
            chunks.append(Chunk(cite=f"[{m.get('doc_code')} p.{m.get('page')}]", doc_code=m.get("doc_code", ""),
                                title=m.get("title", ""), version=m.get("version", ""), page=int(m.get("page", 0)),
                                score=score, relevant=score >= self.threshold, text=doc.page_content,
                                source=m.get("source", "")))
        return chunks, {"embed_ms": round((t1 - t0) * 1000, 1), "query_ms": round((t2 - t1) * 1000, 1),
                        "filter": where}
