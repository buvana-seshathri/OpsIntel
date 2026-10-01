import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from opsintel.config import Settings
from opsintel.rag import embeddings
from opsintel.rag.documents import chunk_markdown, load_corpus, parse_document
from opsintel.rag.embeddings import FastEmbedEmbedder, HashingEmbedder
from opsintel.rag.search import _or_tsquery, rrf
from opsintel.simulator import SCENARIOS
from tests.test_simulator import DATASETS

ROLES = {"viewer", "responder", "admin"}


def test_corpus_is_valid_and_covers_every_scenario() -> None:
    docs = {d.slug: d for d in load_corpus()}
    for d in docs.values():
        assert d.kind in {"runbook", "postmortem", "policy"}, d.slug
        assert d.acl and set(d.acl) <= ROLES, d.slug
        assert chunk_markdown(d.body), d.slug
    for key in SCENARIOS:
        for slug in DATASETS[key].ground_truth.relevant_runbooks:
            assert slug in docs, (key, slug)
    restricted = [d.slug for d in docs.values() if "viewer" not in d.acl]
    assert restricted, "need at least one restricted document to test ACLs"


def test_parse_document_rejects_missing_front_matter(tmp_path: Path) -> None:
    p = tmp_path / "bad.md"
    p.write_text("# No header\n")
    with pytest.raises(ValueError, match="front matter"):
        parse_document(p)
    p.write_text("---\ntitle: x\n---\nbody")
    with pytest.raises(ValueError, match="acl"):
        parse_document(p)


def test_chunker_follows_headings_and_splits_long_sections() -> None:
    body = "# Title\nintro\n\n## A\nalpha\n\n## B\n" + "\n\n".join(
        f"para {i} " * 30 for i in range(8)
    )
    chunks = chunk_markdown(body, max_chars=500)
    assert chunks[0].heading == "Title" and chunks[0].content == "intro"
    assert chunks[1].heading == "Title > A"
    b_chunks = [c for c in chunks if c.heading == "Title > B"]
    assert len(b_chunks) > 1
    assert all(len(c.content) <= 500 for c in b_chunks)
    # Each split chunk repeats the last paragraph of the previous one for context.
    assert b_chunks[1].content.split("\n\n")[0] == b_chunks[0].content.split("\n\n")[-1]
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_hashing_embedder_is_deterministic_normalised_and_lexical() -> None:
    e = HashingEmbedder()
    a = e.embed_query("payflow connection pool exhausted")
    assert a == e.embed_query("payflow connection pool exhausted")
    assert len(a) == 384 and math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-9)

    def cos(x: list[float], y: list[float]) -> float:
        return sum(i * j for i, j in zip(x, y, strict=True))

    near = e.embed_query("connection pool exhausted on payflow client")
    far = e.embed_query("certificate expired for carrier label")
    assert cos(a, near) > cos(a, far)


def test_fastembed_wrapper(monkeypatch: Any) -> None:
    class FakeModel:
        def __init__(self, name: str) -> None:
            self.name = name

        def embed(self, texts: list[str]) -> Any:
            return (np.full(384, float(len(t))) for t in texts)

        def query_embed(self, text: str) -> Any:
            return iter([np.ones(384)])

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=FakeModel))
    e = FastEmbedEmbedder("BAAI/bge-small-en-v1.5")
    assert e.name == "BAAI/bge-small-en-v1.5"
    assert e.embed_documents(["ab"])[0][:2] == [2.0, 2.0]
    assert e.embed_query("q") == [1.0] * 384
    assert isinstance(embeddings.make_embedder(Settings(embedder="hashing")), HashingEmbedder)


def test_rrf_rewards_agreement() -> None:
    scores = rrf([["a", "b", "c"], ["b", "d"]])
    assert max(scores, key=lambda x: scores[x]) == "b"
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["d"] == pytest.approx(1 / 62)


def test_or_tsquery_handles_error_codes_and_punctuation() -> None:
    assert _or_tsquery("Why? PAYFLOW_POOL_EXHAUSTED on payments-svc!") == (
        "why | payflow | pool | exhausted | on | payments | svc"
    )
    assert _or_tsquery("???") == ""
