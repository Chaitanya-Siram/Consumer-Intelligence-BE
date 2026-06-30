"""Conversational state for the query-builder intake agent.

A plain Pydantic model (not a SQLAlchemy table) — it lives for the duration of a
WebSocket conversation and accumulates everything the agent learns across turns.
"""
from typing import List

from pydantic import BaseModel, Field


class AgentState(BaseModel):
    stage: int = 1
    brand: str = ""
    topics: List[str] = Field(default_factory=list)
    geography: str = ""
    query_groups: List[dict] = Field(default_factory=list)
    # Competitors to monitor — seeded from live web research + the user's queries
    # when the Competitors stage opens, then refined by the user's selection.
    competitors: List[str] = Field(default_factory=list)
    confirmed_intent: bool = False
    confirmed_queries: bool = False
    confirmed_competitors: bool = False
    history: List[dict] = Field(default_factory=list)
