"""Verbatim excerpt picker for Consumer Intelligence storyboards.

Every quote a storyboard shows must be a real excerpt from an article's
`content` (or `summary`/`title` when content is empty), attributed to the
article's platform and host. Nothing here calls an LLM.

Lifted from brand_intel._brand_snippet so every lens shares one picker.
"""

import re

from . import cohorts
from .brand_media import domain_from_url

_BREAKS = re.compile(r">>+|[\r\n]+|•|\|")
_SENTENCES = re.compile(r"[^.!?]*[.!?]|[^.!?]+$")

DEFAULT_CHARS = 240
# "In their words" blocks show this many posts, strongest first.
TOP_POSTS = 5
MIN_CHARS = 40


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for segment in _BREAKS.split(text or ""):
        for match in _SENTENCES.findall(segment):
            s = match.strip()
            if s:
                out.append(s)
    return out


def _text(article: dict) -> str:
    # "full text" is the poster's own words; the LLM `summary` only stands in when
    # the upload had no body, because a quote must never be a paraphrase.
    return str(article.get("content") or article.get("full text") or article.get("summary") or article.get("title") or "")


def snippet(article: dict, needles: list[str] | None = None, *, chars: int = DEFAULT_CHARS) -> str:
    """Up to two consecutive sentences, preferring ones that name a needle
    (brand, theme keyword). Falls back to the opening sentences."""
    sents = sentences(_text(article))
    if not sents:
        return ""
    lowered = [n.lower() for n in (needles or []) if n]
    hits = [s for s in sents if any(n in s[:160].lower() for n in lowered)] if lowered else []
    pick = hits[:2] if hits else sents[:2]
    text = " ".join(pick).strip()
    if len(text) > chars:
        text = text[:chars].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def truncate(text: str, chars: int = 90) -> str:
    """Word-boundary-safe truncation with an ellipsis; never cuts mid-word."""
    text = text or ""
    if len(text) <= chars:
        return text
    return text[:chars].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def source(article: dict) -> str:
    """"Forums · reddit.com" / "Online News · autoblog.com" / "Review"."""
    plat = cohorts.platform(article)
    host = article.get("domain") or article.get("domain_name") or domain_from_url(article.get("url"))
    if host and host.lower() not in plat.lower():
        return f"{plat} · {host}"
    return plat


def pick(articles: list[dict], *, needles: list[str] | None = None, limit: int = 3, chars: int = DEFAULT_CHARS, prefer: str | None = None) -> list[dict]:
    """[{"text", "source", "url"}] — distinct, needle-bearing when possible.

    `prefer` = "Negative"/"Positive" ranks that sentiment first (issues want
    the complaints, health wants the praise)."""
    scored: list[tuple[tuple, dict]] = []
    lowered = [n.lower() for n in (needles or []) if n]
    for a in articles:
        text = snippet(a, needles, chars=chars)
        if len(text) < MIN_CHARS:
            continue
        has_needle = any(n in text.lower() for n in lowered) if lowered else False
        tone_match = prefer is not None and a.get("sentiment") == prefer
        authored = bool(str(a.get("author") or "").strip())
        rank = (tone_match, has_needle, authored, -abs(len(text) - 170))
        scored.append((rank, {
            "text": text,
            "source": source(a),
            "url": a.get("url") or "",
            # Facts for the designed post card the FE draws when the live post
            # can't be embedded or captured.
            "platform": cohorts.platform(a),
            "author": str(a.get("author") or "").strip(),
            "date": str(a.get("date") or "")[:10],
        }))
    scored.sort(key=lambda t: t[0], reverse=True)
    out, seen = [], set()
    for _, q in scored:
        key = q["text"][:60].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= limit:
            break
    return out


def one(articles: list[dict], **kw) -> dict | None:
    rows = pick(articles, limit=1, **kw)
    return rows[0] if rows else None
