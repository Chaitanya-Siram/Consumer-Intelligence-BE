"""Prompt templates for the agent nodes and final generation."""

from __future__ import annotations

ANALYZE_PROMPT = """\
You rewrite a user's latest question into a standalone search query and extract \
any metadata filters it implies.

Conversation so far:
{history}

Latest question: {question}

Return ONLY a JSON object with these keys (use null when not present):
- "standalone_query": the question rewritten to stand on its own (resolve \
pronouns/references using the conversation).
- "author": an author name to filter results by, or null.
- "date_from": an ISO-8601 lower-bound date (YYYY-MM-DD) implied by the question, or null.
- "date_to": an ISO-8601 upper-bound date (YYYY-MM-DD) implied by the question, or null.

JSON:"""

GRADE_PROMPT = """\
You are grading whether the retrieved context can answer the user's question.

Question: {question}

Retrieved context:
{context}

Can this context answer the question? Reply with a single word: "yes" or "no"."""

REWRITE_PROMPT = """\
The previous search did not find relevant results. Rewrite the query to be \
broader and clearer so it retrieves useful articles. Return ONLY the rewritten query.

Original question: {question}
Rewritten query:"""

# System prompt for the final, grounded answer.
GENERATION_SYSTEM = """\
You are a helpful assistant that answers questions strictly from the provided \
article excerpts. Follow these rules:
- Use only the information in the sources below. Do not use outside knowledge.
- Cite sources inline using bracketed numbers like [1], [2] that match the \
source list.
- If the sources do not contain the answer, say you don't have enough \
information — do not guess.
- Be concise and factual."""

GENERATION_USER = """\
Question: {question}

Sources:
{sources}

Answer (with inline [n] citations):"""

ABSTAIN_MESSAGE = (
    "I couldn't find anything relevant in this session's articles to answer that. "
    "Try rephrasing, or run the tagging agent so there is data to search."
)
