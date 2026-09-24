"""Hybrid retrieval over the knowledge chunks: BM25 (lexical) + bge-small
(dense), fused with Reciprocal Rank Fusion.

- BM25 catches exact tokens customers and docs share: "USSD", "KYC0", "PIN",
  "*123#", amounts.
- Dense embeddings catch paraphrase: "how much can I send" vs a section
  called "Daily send limits".
- RRF fuses on rank, not raw score, so the two backends need no score
  calibration against each other (k=60 is the standard constant from the
  original RRF paper, not a tuned value).

Several queries can be searched at once (the customer's original words plus
the router's standalone English rewrite) and all rankings are fused together.
Superseded (archive) chunks are filtered out unless the caller asks for them.

The corpus is ~40 short chunks, so a numpy matrix in memory is the whole
"vector store"; see README for when that stops being true.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Sequence

import numpy as np

from . import config
from .ingest import Chunk

RRF_K = 60
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

STOPWORDS = set(
    "a an the and or of to in on at for from by with is are was were be been it its this that "
    "i me my you your we our he she they them his her do does did can could should would will "
    "how what when where which who why if not no so as than then there here about into".split()
)


def tokenize(text: str) -> List[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for t in tokens:
        if t in STOPWORDS:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]  # fees -> fee, limits -> limit
        out.append(t)
    return out


class BM25:
    def __init__(self, docs: Sequence[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(d)) for d in docs]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg_len = sum(self.lengths) / max(len(self.lengths), 1)
        df = Counter(t for d in self.docs for t in d)
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> np.ndarray:
        q = tokenize(query)
        out = np.zeros(len(self.docs))
        for i, (tf, dl) in enumerate(zip(self.docs, self.lengths)):
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg_len))
            out[i] = s
        return out


@lru_cache(maxsize=1)
def _embedder():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(config.EMBED_MODEL)


@dataclass
class Hit:
    chunk: Chunk
    score: float  # fused RRF score
    dense_sim: float  # best cosine similarity across queries (debug only)


class HybridRetriever:
    def __init__(self, chunks: Sequence[Chunk]):
        self.chunks = [c for c in chunks if c.kind == "knowledge"]
        texts = [c.search_text for c in self.chunks]
        self.bm25 = BM25(texts)
        self.embeddings = np.asarray(
            _embedder().encode(texts, normalize_embeddings=True, show_progress_bar=False), dtype=np.float32
        )

    def search(self, queries: Sequence[str], k: int = 6, include_archive: bool = False) -> List[Hit]:
        queries = [q for q in dict.fromkeys(q.strip() for q in queries) if q]
        allowed = [i for i, c in enumerate(self.chunks) if include_archive or not c.is_archive]
        qvecs = _embedder().encode(
            [BGE_QUERY_PREFIX + q for q in queries], normalize_embeddings=True, show_progress_bar=False
        )

        fused: Dict[int, float] = {i: 0.0 for i in allowed}
        best_sim: Dict[int, float] = {i: -1.0 for i in allowed}
        for q, qvec in zip(queries, qvecs):
            sims = self.embeddings @ qvec
            lexical = self.bm25.scores(q)
            for scores in (sims, lexical):
                ranked = sorted(allowed, key=lambda i: scores[i], reverse=True)
                for rank, i in enumerate(ranked):
                    if scores is lexical and lexical[i] <= 0:
                        break  # no shared terms: no lexical vote
                    fused[i] += 1.0 / (RRF_K + rank + 1)
            for i in allowed:
                best_sim[i] = max(best_sim[i], float(sims[i]))

        top = sorted(allowed, key=lambda i: fused[i], reverse=True)[:k]
        return [Hit(chunk=self.chunks[i], score=fused[i], dense_sim=best_sim[i]) for i in top]
