"""Agents package.

Houses the interactive data agent that either (1) generates + runs Python in an
E2B sandbox to build a chart from a session's tagged articles, or (2) answers
free-form questions by reading the tagged articles and precomputed dashboard
charts. Provider-agnostic across Claude / Azure OpenAI via `LLM_PROVIDER`.
"""
