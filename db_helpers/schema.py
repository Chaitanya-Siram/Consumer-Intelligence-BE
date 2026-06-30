from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class DASHBOARDS_ENUM(str, Enum):
    media_monitoring = "media_monitoring"
    media_measurement = "media_measurement"
    narrative_intelligence = "narrative_intelligence"
    pr_impact = "pr_impact"
    reputation_index = "reputation_index"


class ChartResult(BaseModel):
    chart_id: str
    title: str
    description: str
    chart_type: str
    data: Any
    series: list = Field(description="for multi-series charts, list of key names in data") 
    x_label: str
    y_label: str
    error: Optional[str] = None