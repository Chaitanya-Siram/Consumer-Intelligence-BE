from typing import Optional
from pydantic import BaseModel, Field


class TaggedArticleUpdate(BaseModel):
    """A partial update for one tagged article.

    Only the AI-tagged fields are accepted — the article body/metadata (title,
    content, date, reach, …) cannot be edited here; unknown fields are ignored.
    Every field except `id` is optional, so callers send only what changed.
    `id` is the primary key (e.g. "A1").
    """
    id: str
    sentiment: Optional[str] = None
    theme: Optional[str] = None
    summary: Optional[str] = None
    sentiment_confidence: Optional[float] = Field(default=None, ge=0, le=100)  # 0–100 percent; stored as a 0–1 float
    theme_confidence: Optional[float] = Field(default=None, ge=0, le=100)
    section_category_confidence: Optional[float] = Field(default=None, ge=0, le=100)
    relevancy_confidence: Optional[float] = Field(default=None, ge=0, le=100)
    relevancy_reason: Optional[str] = None
    xai_theme_reason: Optional[str] = None
    xai_sentiment_reason: Optional[str] = None
    priority_watch: Optional[bool] = None
    section: Optional[str] = None
    # Relation links (editable in the review page): the id of the main article
    # this one is syndicated-from / similar-to. Empty string clears the link.
    syndication_of: Optional[str] = None
    similar_of: Optional[str] = None
    brand_of_interest: Optional[list[str]] = None
    competitors: Optional[list[str]] = None
    other_competitors: Optional[list[str]] = None
    peoples: Optional[list[str]] = None
    countries: Optional[list[str]] = None
    organizations: Optional[list[str]] = None



class NewTaggedArticle(BaseModel):
    """A manually-added article for the review table — body fields plus tags.
    A fresh `A{n}` id is assigned server-side. Either title or content is required."""
    title: str
    content: str
    date: str
    url: str
    reach: Optional[int] = None
    sentiment: Optional[str] = None
    theme: Optional[str] = None
    summary: Optional[str] = None
    sentiment_confidence: Optional[float] = Field(default=None, ge=0, le=100)
    theme_confidence: Optional[float] = Field(default=None, ge=0, le=100)
    section_category_confidence: Optional[float] = Field(default=None, ge=0, le=100)
    relevancy_confidence: Optional[float] = Field(default=None, ge=0, le=100)
    relevancy_reason: Optional[str] = None
    xai_theme_reason: Optional[str] = None
    xai_sentiment_reason: Optional[str] = None
    priority_watch: Optional[bool] = None
    section: Optional[str] = None
    brand_of_interest: Optional[list[str]] = Field(default=None, min_length=1, max_length=1)
    competitors: Optional[list[str]] = None
    other_competitors: Optional[list[str]] = None
    peoples: Optional[list[str]] = None
    countries: Optional[list[str]] = None
    organizations: Optional[list[str]] = None


class ApproveRequest(BaseModel):
    """Mark a set of articles approved (or not) by id.

    `for_monitoring` selects which approval flag to set: the Media Monitoring
    review popup approves into `is_approved_for_monitoring`, every other review
    into `is_approved`. The two are independent."""
    ids: list[str]
    is_approved: bool = True
    for_monitoring: bool = False


class FetchArticleRequest(BaseModel):
    """Fetch a single article by URL and AI-tag it (preview, not yet saved)."""
    url: str
