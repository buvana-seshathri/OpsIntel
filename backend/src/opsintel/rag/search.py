"""Hybrid retrieval: pgvector cosine search and Postgres full-text search, fused with
Reciprocal Rank Fusion. Access control is part of both SQL queries, so a document the caller
may not read never leaves the database."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from opsintel.db.models import Chunk, Document
from opsintel.rag.embeddings import Embedder

RRF_K = 60


@dataclass
class SearchHit:
    chunk_id: str
    document_id: str
    title: str
    kind: str
    heading: str
    content: str
    score: float
    vector_rank: int | None
    keyword_rank: int | None


class EmbeddingModelMismatch(Exception):
    pass


def rrf(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """score(d) = sum over rankings of 1 / (k + rank), rank starting at 1."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


def _or_tsquery(query: str) -> str:
    # websearch_to_tsquery ANDs every word, so a full-sentence question matches nothing.
    # OR the words instead and let ts_rank_cd reward chunks that cover more of them.
    words = re.findall(r"[a-z0-9]+", query.lower())
    return " | ".join(dict.fromkeys(words))


def hybrid_search(
    session: Session,
    embedder: Embedder,
    query: str,
    roles: list[str],
    k: int = 5,
    kinds: list[str] | None = None,
    candidates: int = 20,
) -> list[SearchHit]:
    models = set(session.scalars(select(Document.embedding_model).distinct()))
    if models and models != {embedder.name}:
        raise EmbeddingModelMismatch(
            f"corpus embedded with {sorted(models)}, query embedder is {embedder.name}; "
            "re-run `opsintel ingest-docs`"
        )

    def scoped(stmt: Select[Any]) -> Select[Any]:
        stmt = stmt.join(Document, Chunk.document_id == Document.id).where(
            Document.acl_roles.overlap(roles)
        )
        if kinds:
            stmt = stmt.where(Document.kind.in_(kinds))
        return stmt.limit(candidates)

    qvec = embedder.embed_query(query)
    vector_ids = list(
        session.scalars(scoped(select(Chunk.id).order_by(Chunk.embedding.cosine_distance(qvec))))
    )

    keyword_ids: list[str] = []
    if tsq_text := _or_tsquery(query):
        tsq = func.to_tsquery("english", tsq_text)
        keyword_ids = list(
            session.scalars(
                scoped(
                    select(Chunk.id)
                    .where(Chunk.tsv.op("@@")(tsq))
                    .order_by(func.ts_rank_cd(Chunk.tsv, tsq).desc(), Chunk.id)
                )
            )
        )

    scores = rrf([vector_ids, keyword_ids])
    top = sorted(scores, key=lambda cid: (-scores[cid], cid))[:k]
    rows = {
        c.id: (c, d)
        for c, d in session.execute(select(Chunk, Document).join(Document).where(Chunk.id.in_(top)))
    }
    return [
        SearchHit(
            chunk_id=cid,
            document_id=rows[cid][1].id,
            title=rows[cid][1].title,
            kind=rows[cid][1].kind,
            heading=rows[cid][0].heading,
            content=rows[cid][0].content,
            score=round(scores[cid], 5),
            vector_rank=vector_ids.index(cid) + 1 if cid in vector_ids else None,
            keyword_rank=keyword_ids.index(cid) + 1 if cid in keyword_ids else None,
        )
        for cid in top
    ]
