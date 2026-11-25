"""Turning questions into vectors, locally, so similar questions can be found.

The model runs on the GitHub runner — no API, no cost per call. `fastembed` downloads
it once (about 67 MB for `BAAI/bge-small-en-v1.5`) and the workflow caches it.
`fastembed` is an optional dependency (`pip install -e ".[embeddings]"`): the tests use
a fake embedder and never download anything.

Vectors are stored in the hidden `_embeddings` tab as base64-encoded float32, with the
model name beside each, so a change of model is noticed and everything re-embedded
rather than compared across incompatible vector spaces.
"""

from __future__ import annotations

import base64
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> np.ndarray:
        """Unit-length vectors, one row per text."""
        ...


class FastEmbedEmbedder:
    """A local sentence-embedding model via fastembed."""

    def __init__(self, model: str, *, cache_dir: str, batch_size: int = 64) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as error:  # pragma: no cover - environment problem
            raise RuntimeError(
                "fastembed is not installed. Install the embeddings extra: "
                'pip install -e ".[embeddings]"'
            ) from error
        self.model = model
        self.batch_size = batch_size
        self._engine = TextEmbedding(model_name=model, cache_dir=cache_dir)

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        vectors = np.asarray(
            list(self._engine.embed(texts, batch_size=self.batch_size)), dtype=np.float32
        )
        return normalise(vectors)


def normalise(vectors: np.ndarray) -> np.ndarray:
    """Scale each row to unit length, so a dot product is a cosine similarity."""
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.ndim == 1:
        vectors = vectors[None, :]
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def encode(vector: np.ndarray) -> str:
    """A vector as base64 float32, for a sheet cell."""
    return base64.b64encode(np.asarray(vector, dtype="<f4").tobytes()).decode("ascii")


def decode(text: str) -> np.ndarray:
    """The inverse of :func:`encode`."""
    return np.frombuffer(base64.b64decode(text), dtype="<f4").astype(np.float32)
