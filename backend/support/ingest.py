"""Builds the local Chroma index from the policy PDFs in rag/data.

PDFs are read with PyMuPDF, split into chunks, embedded with a local sentence-transformers
model and stored in rag/chroma. A manifest of file hashes skips the rebuild when nothing
changed.

    python -m backend.support.ingest           # build if the PDFs changed
    python -m backend.support.ingest --force   # always rebuild
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

COLLECTION = "wayfinder_policies"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

# Header line the policy PDFs carry on every page (pages without it fall back to the file name):
# Doc: WH-POL-003 | Title: Cancellation and Refund Policy | Version: v1 | Effective: 2026-04-01 | Applies to: all
HEADER_RE = re.compile(
    r"^\s*Doc:\s*(?P<doc_code>[^|]+?)\s*\|\s*Title:\s*(?P<title>[^|]+?)\s*\|\s*Version:\s*(?P<version>[^|]+?)"
    r"\s*\|\s*Effective:\s*(?P<effective_date>[^|]+?)\s*\|\s*Applies to:\s*(?P<applies_to>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE)
FOOTER_MARK = "is a fictional company"


def _fallback_meta(path: Path) -> dict:
    """Metadata for a PDF without the standard header line."""
    code = path.stem.split("_")[0].upper()
    return {"doc_code": code, "title": path.stem.replace("_", " "), "version": "",
            "effective_date": "", "applies_to": "all"}


def pdf_chunks(path: Path) -> list:
    """Split one PDF into LangChain Documents with doc_code / title / version / page metadata."""
    import pymupdf
    from langchain_core.documents import Document
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    docs, meta = [], None
    with pymupdf.open(path) as pdf:
        for page_no, page in enumerate(pdf, start=1):
            text = page.get_text()
            m = HEADER_RE.search(text)
            if m:
                meta = {k: v.strip() for k, v in m.groupdict().items()}
            page_meta = meta or _fallback_meta(path)
            body = "\n".join(line for line in text.splitlines()
                             if not HEADER_RE.match(line) and FOOTER_MARK not in line)
            body = re.sub(r"\n{3,}", "\n\n", body).strip()
            applies = page_meta["applies_to"].lower()
            for i, piece in enumerate(splitter.split_text(body)):
                docs.append(Document(page_content=piece, metadata={
                    **page_meta, "page": page_no, "chunk": i, "source": path.name,
                    "domestic": "all" in applies or "domestic" in applies,
                    "international": "all" in applies or "international" in applies,
                }))
    return docs


def chunk_id(doc) -> str:
    m = doc.metadata
    return f"{m['doc_code']}-p{m['page']}-c{m['chunk']}"


def file_hashes(data_dir: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(data_dir).glob("*.pdf"))}


def load_manifest(chroma_dir: Path) -> dict | None:
    path = Path(chroma_dir) / "manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def make_embeddings(model_name: str):
    """Local, offline sentence-transformers embeddings (downloaded once on first use)."""
    from langchain_huggingface import HuggingFaceEmbeddings
    return HuggingFaceEmbeddings(model_name=model_name, encode_kwargs={"normalize_embeddings": True})


def open_store(chroma_dir: Path, embeddings):
    from langchain_chroma import Chroma
    return Chroma(collection_name=COLLECTION, embedding_function=embeddings, persist_directory=str(chroma_dir),
                  collection_metadata={"hnsw:space": "cosine"})


def build_index(data_dir: Path, chroma_dir: Path, embeddings, model_name: str, force: bool = False):
    """(Re)build the index if the PDFs or the embedding model changed. Returns (store, manifest)."""
    chroma_dir = Path(chroma_dir)
    chroma_dir.mkdir(parents=True, exist_ok=True)
    hashes = file_hashes(data_dir)
    manifest = load_manifest(chroma_dir)
    store = open_store(chroma_dir, embeddings)
    if not force and manifest and manifest.get("files") == hashes and manifest.get("model") == model_name:
        return store, manifest

    store.delete_collection()
    store = open_store(chroma_dir, embeddings)
    docs = [d for name in hashes for d in pdf_chunks(Path(data_dir) / name)]
    for i in range(0, len(docs), 128):
        batch = docs[i:i + 128]
        store.add_documents(batch, ids=[chunk_id(d) for d in batch])
    manifest = {
        "files": hashes, "model": model_name, "chunks": len(docs),
        "docs": sorted({f"{d.metadata['doc_code']} - {d.metadata['title']}" for d in docs}),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (chroma_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("indexed %d chunks from %d PDFs", len(docs), len(hashes))
    return store, manifest


if __name__ == "__main__":
    import argparse

    from backend.config import get_settings

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Index the policy PDFs in rag/data into Chroma.")
    parser.add_argument("--force", action="store_true", help="rebuild even if nothing changed")
    s = get_settings()
    _, info = build_index(s.rag_data_path, s.chroma_path, make_embeddings(s.embedding_model), s.embedding_model,
                          force=parser.parse_args().force)
    print(f"{info['chunks']} chunks from {len(info['files'])} PDFs ({info['built_at']}):")
    for d in info["docs"]:
        print("  -", d)
