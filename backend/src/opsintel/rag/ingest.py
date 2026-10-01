"""Idempotent ingestion: unchanged documents are skipped, changed ones re-chunked and
re-embedded, and documents removed from the corpus are deleted."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from opsintel.db.models import Chunk, Document
from opsintel.rag.documents import CORPUS_DIR, chunk_markdown, load_corpus
from opsintel.rag.embeddings import Embedder


@dataclass
class IngestReport:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    chunks: int = 0


def ingest_corpus(
    session: Session, embedder: Embedder, directory: Path = CORPUS_DIR
) -> IngestReport:
    report = IngestReport()
    docs = load_corpus(directory)
    existing = {d.id: d for d in session.scalars(select(Document))}

    for doc in docs:
        doc_id = f"doc_{doc.slug}"
        current = existing.pop(doc_id, None)
        if (
            current is not None
            and current.content_hash == doc.content_hash
            and current.embedding_model == embedder.name
        ):
            report.unchanged += 1
            continue

        chunks = chunk_markdown(doc.body)
        vectors = embedder.embed_documents(
            [f"{doc.title}\n{c.heading}\n{c.content}" for c in chunks]
        )
        if current is None:
            report.added += 1
        else:
            report.updated += 1
            session.execute(delete(Chunk).where(Chunk.document_id == doc_id))
        session.merge(
            Document(
                id=doc_id,
                slug=doc.slug,
                title=doc.title,
                kind=doc.kind,
                services=doc.services,
                acl_roles=doc.acl,
                content_hash=doc.content_hash,
                embedding_model=embedder.name,
            )
        )
        session.flush()
        session.add_all(
            Chunk(
                id=f"chk_{doc.slug}_{c.ordinal:02d}",
                document_id=doc_id,
                ordinal=c.ordinal,
                heading=c.heading,
                content=c.content,
                embedding=vec,
            )
            for c, vec in zip(chunks, vectors, strict=True)
        )
        report.chunks += len(chunks)

    for stale in existing.values():
        session.delete(stale)
        report.removed += 1
    session.flush()
    return report
