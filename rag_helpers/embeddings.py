"""Embedding model factory (LlamaIndex).

Default is the open-weight ``BAAI/bge-large-en-v1.5`` running locally via
sentence-transformers (no API key, no data egress). BGE retrieval models expect a
query instruction prefix on *queries* (not on documents); LlamaIndex's
``HuggingFaceEmbedding`` applies ``query_instruction`` to query embeddings only,
which matters for retrieval quality.
"""

from __future__ import annotations

from functools import lru_cache

from configs import envs

# Recommended retrieval instruction for the BGE v1.5 English models.
_BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages:"


@lru_cache
def get_embed_model():
    if envs.EMBED_PROVIDER == "huggingface":
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding

        query_instruction = (
            _BGE_QUERY_INSTRUCTION if "bge" in envs.EMBED_MODEL.lower() else None
        )
        return HuggingFaceEmbedding(
            model_name=envs.EMBED_MODEL,
            query_instruction=query_instruction,
        )

    raise ValueError(f"Unknown EMBED_PROVIDER: {envs.EMBED_PROVIDER!r}")
