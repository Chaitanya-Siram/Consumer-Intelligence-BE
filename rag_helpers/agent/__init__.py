"""LangGraph CRAG-style retrieval agent (ported from RAG_System).

    analyze -> retrieve -> rerank -> grade -> [finish | rewrite -> retrieve ...]

Produces the reranked, relevance-checked context + an ``answerable`` flag. Scoped
to a session (and/or project) instead of RAG_System's per-user tenant.
"""
