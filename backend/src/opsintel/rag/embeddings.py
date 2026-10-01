"""Text embedders. Groq has no embeddings endpoint, so embeddings are computed locally.

- `FastEmbedEmbedder`: BAAI/bge-small-en-v1.5 through fastembed (ONNX on CPU, no torch).
  Downloads ~130 MB from HuggingFace on first use.
- `HashingEmbedder`: deterministic feature hashing of words and word pairs. Needs no
  network or model, so tests and offline environments use it. It is lexical, not semantic.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Protocol

from opsintel.config import Settings, get_settings

DIM = 384  # both embedders produce 384-dim vectors; the column is vector(384)


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


_TOKEN = re.compile(r"[a-z0-9]+")


class HashingEmbedder:
    name = "hashing-v1"
    dim = DIM

    def _vector(self, text: str) -> list[float]:
        words = _TOKEN.findall(text.lower())
        features = words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]
        counts: dict[str, int] = {}
        for f in features:
            counts[f] = counts.get(f, 0) + 1
        vec = [0.0] * self.dim
        for feature, n in counts.items():
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign * (1.0 + math.log(n))
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class FastEmbedEmbedder:
    dim = DIM

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        from fastembed import TextEmbedding  # heavy import; only when selected

        self.name = model_name
        self._model: Any = TextEmbedding(model_name)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        # bge models are trained with a query prefix; fastembed applies it here.
        return [float(x) for x in next(iter(self._model.query_embed(text)))]


def make_embedder(settings: Settings | None = None) -> Embedder:
    s = settings or get_settings()
    if s.embedder == "hashing":
        return HashingEmbedder()
    return FastEmbedEmbedder(s.embedding_model)
