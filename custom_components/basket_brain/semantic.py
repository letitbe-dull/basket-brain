"""Semantic similarity over product names via a bundled model2vec model.

Degrades to disabled (all calls return None) when the library or the bundled
model can't load, so callers fall back to fuzzy-only and never hard-fail.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .product_utils import meaningful_tokens

_LOGGER = logging.getLogger(__name__)

_MODEL_DIR = Path(__file__).parent / "models" / "potion-base-8M"


def _embed_key(text: str) -> str:
    """Normalise text to the string we actually embed.

    Uses the meaningful tokens (size/packaging stripped) so "Anchor Blue Milk
    2L Bottle" and "anchor blue milk" embed identically. Falls back to a plain
    lowercase when no meaningful tokens survive.
    """
    tokens = meaningful_tokens(text)
    return " ".join(tokens) if tokens else text.lower().strip()


class _Semantic:
    """Lazy-loaded model2vec wrapper with an embedding cache.

    The model loads on first use; if that fails the instance stays disabled for
    the process lifetime. Embeddings are L2-normalised and cached by their
    ``_embed_key`` so the ~250 map names are only encoded once.
    """

    def __init__(self) -> None:
        self._model = None
        self._loaded = False
        self._cache: dict[str, np.ndarray] = {}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            from model2vec import StaticModel

            self._model = StaticModel.from_pretrained(str(_MODEL_DIR))
            _LOGGER.info("semantic: loaded model2vec model from %s", _MODEL_DIR)
        except Exception:
            self._model = None
            _LOGGER.warning(
                "semantic: model2vec model unavailable at %s — "
                "matching falls back to fuzzy-only",
                _MODEL_DIR,
                exc_info=True,
            )

    @property
    def available(self) -> bool:
        """True when the model loaded and semantic scoring is usable."""
        self._ensure_loaded()
        return self._model is not None

    def embed(self, text: str) -> np.ndarray | None:
        """Return the cached L2-normalised embedding for *text*, or None.

        None means the model isn't available — callers treat that as "no
        semantic signal" and rely on fuzzy scoring alone.
        """
        if not self.available:
            return None
        key = _embed_key(text)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        vec = np.asarray(self._model.encode([key])[0], dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        vec = vec / norm if norm else vec
        self._cache[key] = vec
        return vec

    def similarity(self, a: str, b: str) -> float | None:
        """Cosine similarity of two texts in [-1, 1], or None when disabled."""
        va = self.embed(a)
        vb = self.embed(b)
        if va is None or vb is None:
            return None
        return float(np.dot(va, vb))

    def warm(self, texts: list[str]) -> None:
        """Pre-embed and cache a batch of names (the cold-cache one-off).

        Encodes only the not-yet-cached keys in a single batch so seeding the
        ~250 map names is one model call, not 250.
        """
        if not self.available:
            return
        pending: list[str] = []
        seen: set[str] = set()
        for text in texts:
            key = _embed_key(text)
            if key and key not in self._cache and key not in seen:
                seen.add(key)
                pending.append(key)
        if not pending:
            return
        vecs = self._model.encode(pending)
        for key, vec in zip(pending, vecs, strict=True):
            vec = np.asarray(vec, dtype=np.float32)
            norm = float(np.linalg.norm(vec))
            self._cache[key] = vec / norm if norm else vec


_SEMANTIC = _Semantic()


def get_semantic() -> _Semantic:
    """Return the process-wide semantic singleton (lazy, cached)."""
    return _SEMANTIC
