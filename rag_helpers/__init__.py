"""RAG plumbing ported from the standalone RAG_System (LlamaIndex + pgvector).

Local open-weight embeddings (BAAI/bge-large-en-v1.5) and reranker
(BAAI/bge-reranker-base) via sentence-transformers, a hybrid PGVectorStore backed
by the app's own Postgres, and a session/project-scoped retriever. All heavy
llama-index imports are lazy (inside the factory functions) so importing this
package does not require the RAG dependencies to be installed until a factory is
actually called.
"""
