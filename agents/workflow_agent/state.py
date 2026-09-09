"""Conversational state for the workflow-builder agent.

A plain Pydantic model (not a table) that lives for the duration of one WebSocket
connection and accumulates what the agent learns across turns.
"""
from pydantic import BaseModel, Field


class WorkflowAgentState(BaseModel):
    brand: str = ""
    competitors: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    title: str = ""
    message_themes: list[str] = Field(default_factory=list)
    lenses: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    layout: str = ""
    charts: list[str] = Field(default_factory=list)
    output_format: str = ""
    llm: str = ""
    # The agent's running read of what the user wants, echoed back each turn.
    intent_summary: str = ""
    # Queries offered in the last turn, so a bare "yes" can be attributed to them.
    proposed_queries: list[str] = Field(default_factory=list)
    confirmed_queries: bool = False
    history: list[dict] = Field(default_factory=list)
