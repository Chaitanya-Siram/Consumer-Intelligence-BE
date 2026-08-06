"""Reranker factory.

Default is a local cross-encoder (``BAAI/bge-reranker-base``) via
sentence-transformers — no API key, no data egress. It reranks the hybrid
retriever's candidates down to ``RERANK_TOP_N`` before generation.
"""

from __future__ import annotations

from functools import lru_cache

from configs import envs


@lru_cache
def get_reranker():
    if envs.RERANK_PROVIDER == "huggingface":
        from llama_index.core.postprocessor import SentenceTransformerRerank

        return SentenceTransformerRerank(
            model=envs.RERANK_MODEL,
            top_n=envs.RERANK_TOP_N,
        )

    raise ValueError(f"Unknown RERANK_PROVIDER: {envs.RERANK_PROVIDER!r}")
