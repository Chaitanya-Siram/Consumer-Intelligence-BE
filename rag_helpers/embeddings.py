"""Embedding model factory (LlamaIndex).

Two providers:

* ``huggingface`` — open-weight ``BAAI/bge-large-en-v1.5`` running locally via
  sentence-transformers (no API key, no data egress). BGE retrieval models expect a
  query instruction prefix on *queries* (not on documents); LlamaIndex's
  ``HuggingFaceEmbedding`` applies ``query_instruction`` to query embeddings only,
  which matters for retrieval quality.
* ``nvidia`` — NVIDIA NIM hosted embeddings. ``NVIDIAEmbedding`` sends the
  ``input_type`` ("query" vs "passage") the embedqa models need, so no manual
  instruction prefix is required.

Both defaults are 1024-dim, so ``EMBED_DIM`` (and the pgvector column) stays the
same across providers.
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

    if envs.EMBED_PROVIDER == "nvidia":
        from llama_index.embeddings.nvidia import NVIDIAEmbedding

        if not envs.NVIDIA_EMBED_API_KEY:
            raise ValueError("NVIDIA_EMBED_API_KEY is required when EMBED_PROVIDER=nvidia")

        return NVIDIAEmbedding(
            model=envs.NVIDIA_EMBED_MODEL,
            base_url=envs.NVIDIA_EMBED_API_URL,
            api_key=envs.NVIDIA_EMBED_API_KEY,
            embed_batch_size=envs.NVIDIA_EMBED_BATCH_SIZE,
            truncate="END",
        )

    raise ValueError(f"Unknown EMBED_PROVIDER: {envs.EMBED_PROVIDER!r}")
