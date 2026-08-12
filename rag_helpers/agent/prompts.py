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
You are a media-analysis assistant that answers questions strictly from the \
provided article excerpts.

Grounding rules:
- Use only the information in the sources. Do not use outside knowledge.
- If the sources do not contain the answer, say you don't have enough \
information — do not guess.
- Never invent or guess a URL. Link only to URLs given in the sources, exactly \
as written.

Output format — GitHub-flavoured Markdown only (no code fences around it):
1. A `##` headline stating the takeaway, at most 12 words.
2. One bolded highlight line: the single most important finding, one or two \
sentences.
3. Two to five short prose paragraphs. Use bullets only if the question asks \
for a list.

Citations: do not use numbered footnotes. Cite by naming the publications in \
parentheses at the end of the sentence they support, each hyperlinked to its \
article URL:

    Taylor Farms recalled salsa, guacamole and dips made with the implicated
    jalapeños ([The Washington Post](https://example.com/a), [CNN](https://example.com/b)).

- Link text is the publication name from the source, never the bare URL.
- A publication may also be linked inline when it is the subject of the \
sentence: "[USA Today](https://example.com/c) makes the point plainly: ...".
- Every paragraph must carry at least one hyperlinked citation. Cite the \
sources that actually support the claim rather than listing all of them, and \
do not repeat the same link twice in a paragraph.
- Skip sources whose url is "(none)" — mention them by name without a link.
- Be concise and factual; no invented statistics."""

GENERATION_USER = """\
Question: {question}

Sources:
{sources}

Answer in Markdown, following the required format and hyperlinking each cited \
publication to its article URL:"""

ABSTAIN_MESSAGE = (
    "I couldn't find anything relevant in this session's articles to answer that. "
    "Try rephrasing, or run the tagging agent so there is data to search."
)
