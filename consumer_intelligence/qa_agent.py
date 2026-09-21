"""Verification-and-repair agent for a generated Consumer Intelligence dashboard.

After the lens storyboards are built, `run()` cross-checks every lens's JSON
against the tagged articles it came from and against the rules the FE relies on,
repairs what it can with a ladder of fallbacks, and audits again. It repeats for
`CI_QA_ITERATIONS` passes (default 3, never fewer than 2 so every repair is
re-verified) and writes what it found and fixed to `payload["meta"]["qa"]`.

What it checks, and the fallback ladder used to repair each:

  post evidence   embed -> screenshot -> scraped preview card -> designed card
                  (platform/author/date) -> plain quote; a post is never left
                  without something the FE can render and link out from
  post text       must be the poster's own words: a quote that is not in its
                  article's text, or starts mid-fragment, is re-cut from it
  post link       http(s) only; a missing link is recovered from the article
  images          preview image, product photo, screenshot object and profile
                  picture must exist; a broken one is dropped or re-fetched
  logos           real brands only, validated image, brand + competitors present
  product photos  name + category -> name -> category (flagged `generic`)
  prose           a failed narrative is re-written once

Everything is best-effort and bounded: an issue gets `_MAX_ATTEMPTS` tries, a
repair that raises is logged and counted as unfixed, and the agent never raises.
"""

import asyncio
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from file_helpers.s3_file import s3_file

from . import brand_media, logo_resolver, product_images, profile_images, quotes as quote_picker, verbatim_capture
from .storyboard.narrative import write_narrative

logger = logging.getLogger(__name__)

DEFAULT_ITERATIONS = 3
DEFAULT_TIME_BUDGET_SECONDS = 240.0
MIN_PASSES = 2
_MAX_ATTEMPTS = 3
_MAX_REPORTED = 60
_FRAGMENT_RX = re.compile(r"^\S{0,8}\]")  # "com] has ..." — a cut through a "[Amazon.com]" style tag
_GENERIC_PRODUCT_QUERY = "car care products"


@dataclass(frozen=True)
class Issue:
    lens: str
    kind: str
    ref: str
    detail: str
    fixable: bool = True
    target: Any = field(default=None, compare=False, repr=False)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.lens, self.kind, self.ref)


class _Context:
    """Everything one QA run shares: the payload, the source articles and caches."""

    def __init__(self, payload: dict, articles: list[dict], *, network: bool) -> None:
        self.payload = payload
        self.articles = articles
        self.network = network
        self.client: Any = None
        self.attempts: Counter = Counter()
        self.by_url = {str(a.get("url")): a for a in articles if a.get("url")}
        self.reachable: dict[str, bool] = {}
        meta = payload.get("meta") or {}
        self.brands = [b for b in [meta.get("brand"), *(meta.get("competitors") or [])] if b]

    def lenses(self) -> list[tuple[str, dict]]:
        skip = {"meta", "coming_soon"}
        return [(k, v) for k, v in self.payload.items() if k not in skip and isinstance(v, dict)]


# ── helpers ──────────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", str(text or "").lower()).strip()


def _body(article: dict | None) -> str:
    return f"{(article or {}).get('content') or ''} {(article or {}).get('full text') or ''}"


def _walk_quotes(node: Any):
    """Every quote dict under a list keyed `quotes*`."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key.startswith("quotes") and isinstance(value, list):
                yield from (q for q in value if isinstance(q, dict) and q.get("text"))
            else:
                yield from _walk_quotes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_quotes(value)


def evidence_level(quote: dict) -> int:
    """4 embed, 3 screenshot, 2 scraped preview, 1 designed card, 0 plain text."""
    if quote.get("embed"):
        return 4
    if quote.get("screenshot_key"):
        return 3
    if quote.get("preview"):
        return 2
    return 1 if quote.get("platform") else 0


def _is_http(url: Any) -> bool:
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


async def _is_reachable(ctx: _Context, url: str) -> bool:
    if not ctx.network or ctx.client is None:
        return True
    if url not in ctx.reachable:
        ctx.reachable[url] = await logo_resolver._fetch_image(ctx.client, url) is not None  # noqa: SLF001
    return ctx.reachable[url]


async def _key_exists(key: str) -> bool:
    try:
        return bool(await asyncio.to_thread(s3_file.download_file, key))
    except Exception:
        return False


# ── audits ───────────────────────────────────────────────────────────────


async def _audit_quote(ctx: _Context, lens: str, quote: dict) -> list[Issue]:
    ref = f"{quote.get('url') or ''}|{str(quote.get('text'))[:40]}"

    def issue(kind: str, detail: str, fixable: bool = True) -> Issue:
        return Issue(lens, kind, ref, detail, fixable, quote)

    found: list[Issue] = []
    article = ctx.by_url.get(str(quote.get("url") or ""))
    if quote.get("url") and not _is_http(quote["url"]):
        found.append(issue("bad_url", f"non-http link {str(quote['url'])[:40]!r}"))
    elif not quote.get("url"):
        found.append(issue("no_link", "quote has no link to the post", fixable=False))
    text = str(quote["text"])
    if article and _body(article).strip() and (_FRAGMENT_RX.match(text) or _norm(text.rstrip("…")) not in _norm(_body(article))):
        found.append(issue("not_verbatim", "quote is not the poster's own text"))
    level = evidence_level(quote)
    if level == 0:
        found.append(issue("no_card_facts", "no platform to draw a post card from"))
    elif level == 1 and ctx.network and _is_http(quote.get("url")) and not quote.get("evidence_tried"):
        found.append(issue("evidence_upgradable", "only a designed card; embed/screenshot/preview never attempted"))
    key = quote.get("screenshot_key")
    if key and ctx.network and not await _key_exists(key):
        found.append(issue("screenshot_missing", "screenshot object is not in storage"))
    image = (quote.get("preview") or {}).get("image")
    if image and not await _is_reachable(ctx, image):
        found.append(issue("preview_image_broken", "scraped preview image does not load"))
    who = profile_images.identify(article or {"url": quote.get("url"), "author": quote.get("author")})
    if who and not quote.get("avatar_key"):
        found.append(issue("avatar_missing", f"no profile picture for {who[0]}/{who[1]}"))
    return found


def _audit_logos(ctx: _Context, lens: str, storyboard: dict) -> list[Issue]:
    meta = storyboard.get("meta") or {}
    logos = meta.get("logos")
    if not isinstance(logos, dict):
        return []
    known_platforms = logo_resolver._PLATFORM_DOMAINS  # noqa: SLF001
    found = [Issue(lens, "logo_junk", name, f"{name!r} is not a brand", True, storyboard) for name in logos if not logo_resolver.is_entity(name) and name.lower() not in known_platforms]
    named = {str(b).lower() for b in meta.get("competitors") or []} | {str(meta.get("brand") or "").lower()}
    found += [Issue(lens, "logo_missing", b, f"no logo for {b!r}", True, storyboard) for b in ctx.brands if b.lower() in named and b not in logos]
    return found


def _audit_products(lens: str, storyboard: dict) -> list[Issue]:
    products = (storyboard.get("perception") or {}).get("products")
    if not isinstance(products, list):
        return []
    category = (storyboard.get("meta") or {}).get("category") or ""
    return [
        Issue(lens, "product_image_missing", str(p.get("name")), f"no photo for {p.get('name')!r}", True, p)
        for p in products
        if isinstance(p, dict) and not p.get("image") and product_images.product_query(p.get("name"), p.get("brand"), category)
    ]


def _audit_prose(lens: str, storyboard: dict) -> list[Issue]:
    status = str((storyboard.get("meta") or {}).get("narrative") or "")
    return [Issue(lens, "prose_failed", lens, status, True, storyboard)] if status.startswith("failed") else []


async def audit(ctx: _Context) -> list[Issue]:
    """Every issue found across every lens."""
    issues: list[Issue] = []
    for lens, storyboard in ctx.lenses():
        if storyboard.get("status") == "failed":
            issues.append(Issue(lens, "lens_failed", lens, str((storyboard.get("meta") or {}).get("error"))[:120], False))
            continue
        if storyboard.get("status") == "coming_soon":
            continue
        for quote in _walk_quotes(storyboard):
            issues += await _audit_quote(ctx, lens, quote)
        issues += _audit_logos(ctx, lens, storyboard) + _audit_products(lens, storyboard) + _audit_prose(lens, storyboard)
    return issues


# ── repairs (each returns True when it changed something) ─────────────────


async def _fix_bad_url(ctx: _Context, issue: Issue) -> bool:
    quote = issue.target
    needle = _norm(quote["text"].rstrip("…"))[:60]
    article = next((a for a in ctx.articles if needle and needle in _norm(_body(a))), None)
    if article and _is_http(article.get("url")):
        quote["url"] = article["url"]
        return True
    quote.pop("url", None)  # no link is honest; a javascript: link is not
    return True


async def _fix_not_verbatim(ctx: _Context, issue: Issue) -> bool:
    quote = issue.target
    article = ctx.by_url.get(str(quote.get("url") or ""))
    body = (article or {}).get("content") or (article or {}).get("full text")
    if not body:
        return False
    brand = (ctx.payload.get("meta") or {}).get("brand")
    cut = quote_picker.snippet({"content": body}, [brand] if brand else None)
    if not cut or _FRAGMENT_RX.match(cut):
        return False
    quote["text"] = cut
    return True


async def _fix_card_facts(ctx: _Context, issue: Issue) -> bool:
    quote = issue.target
    source = str(quote.get("source") or "")
    quote["platform"] = source.split(" · ")[0] or brand_media.domain_from_url(quote.get("url")) or "Web"
    quote.setdefault("author", "")
    quote.setdefault("date", "")
    return True


async def _fix_evidence(ctx: _Context, issue: Issue) -> bool:
    quote = issue.target
    before = evidence_level(quote)
    if issue.kind == "screenshot_missing":
        quote.pop("screenshot_key", None)
    await verbatim_capture.resolve_evidence([quote])  # embed -> screenshot -> preview
    return evidence_level(quote) > before or issue.kind == "screenshot_missing"


async def _fix_preview_image(ctx: _Context, issue: Issue) -> bool:
    issue.target["preview"].pop("image", None)  # a card without its picture beats a broken image
    return True


async def _fix_avatar(ctx: _Context, issue: Issue) -> bool:
    quote = issue.target
    article = ctx.by_url.get(str(quote.get("url") or "")) or {"url": quote.get("url"), "author": quote.get("author")}
    who = profile_images.identify(article)
    key = await profile_images._resolve(*who) if who else None  # noqa: SLF001
    if key:
        quote["avatar_key"] = key
    return bool(key)


async def _fix_logo_junk(ctx: _Context, issue: Issue) -> bool:
    return issue.target["meta"]["logos"].pop(issue.ref, None) is not None


async def _fix_logo_missing(ctx: _Context, issue: Issue) -> bool:
    found = await logo_resolver.resolve_logos([issue.ref], ctx.articles)
    issue.target["meta"]["logos"].update(found)
    return bool(found)


async def _fix_product_image(ctx: _Context, issue: Issue) -> bool:
    product = issue.target
    category = (ctx.payload.get(issue.lens, {}).get("meta") or {}).get("category") or ""
    attempt = ctx.attempts[issue.key] - 1  # 0 name+category, 1 name only, 2 category only (generic)
    queries = [
        product_images.product_query(product.get("name"), product.get("brand"), category),
        product_images.product_query(product.get("name"), product.get("brand")),
        category or _GENERIC_PRODUCT_QUERY,
    ]
    query = queries[max(0, min(attempt, 2))]
    photo = await brand_media.pexels_photo(query) if query else None
    if photo:
        product["image"] = {**photo, "generic": attempt >= 2}
    return bool(photo)


async def _fix_prose(ctx: _Context, issue: Issue) -> bool:
    await write_narrative(issue.lens, issue.target)
    return not str((issue.target.get("meta") or {}).get("narrative") or "").startswith("failed")


_REPAIRS: dict[str, Callable[[_Context, Issue], Awaitable[bool]]] = {
    "bad_url": _fix_bad_url,
    "not_verbatim": _fix_not_verbatim,
    "no_card_facts": _fix_card_facts,
    "evidence_upgradable": _fix_evidence,
    "screenshot_missing": _fix_evidence,
    "preview_image_broken": _fix_preview_image,
    "avatar_missing": _fix_avatar,
    "logo_junk": _fix_logo_junk,
    "logo_missing": _fix_logo_missing,
    "product_image_missing": _fix_product_image,
    "prose_failed": _fix_prose,
}


async def _repair(ctx: _Context, issue: Issue) -> bool:
    ctx.attempts[issue.key] += 1
    try:
        return await _REPAIRS[issue.kind](ctx, issue)
    except Exception as exc:  # a repair must never sink the dashboard
        logger.warning("QA repair %s failed for %s: %s", issue.kind, issue.lens, exc)
        return False


# Evidence repair (`_fix_evidence`) drives a real headless-browser capture, and
# capture_screenshots already launches one browser and works a whole URL list
# concurrently. Repairing these one issue at a time — the generic `_repair` loop
# below does for every other kind — would launch a separate browser per quote,
# which is what made an early run of this agent take 900s+ against a dataset
# with a dozen dead links. Batched once per pass instead, it costs one browser
# launch no matter how many quotes need it.
_EVIDENCE_KINDS = {"evidence_upgradable", "screenshot_missing"}


async def _repair_evidence_batch(ctx: _Context, issues: list[Issue]) -> list[bool]:
    """One capture pass for every evidence issue in `issues`; returns which fixed, same order."""
    before: list[int] = []
    for issue in issues:
        ctx.attempts[issue.key] += 1
        quote = issue.target
        # Clearing a dangling screenshot_key is itself the fix for that issue, whether
        # or not re-capture then finds something better — same as the single-issue path.
        if issue.kind == "screenshot_missing":
            quote.pop("screenshot_key", None)
        before.append(evidence_level(quote))
    try:
        await verbatim_capture.resolve_evidence([i.target for i in issues])
    except Exception as exc:  # a repair must never sink the dashboard
        logger.warning("QA batched evidence repair failed: %s", exc)
        return [i.kind == "screenshot_missing" for i in issues]
    return [evidence_level(i.target) > before[n] or i.kind == "screenshot_missing" for n, i in enumerate(issues)]


# ── the loop ─────────────────────────────────────────────────────────────


def _iterations(requested: int | None) -> int:
    if requested is not None:
        return requested
    try:
        return int(os.getenv("CI_QA_ITERATIONS", DEFAULT_ITERATIONS))
    except ValueError:
        return DEFAULT_ITERATIONS


def _time_budget() -> float:
    """Wall-clock ceiling for a whole run, so a dataset full of dead links (each a
    real capture attempt) degrades to 'stopped early, here's what got fixed'
    instead of hanging the request that called this."""
    try:
        return float(os.getenv("CI_QA_TIME_BUDGET_SECONDS", DEFAULT_TIME_BUDGET_SECONDS))
    except ValueError:
        return DEFAULT_TIME_BUDGET_SECONDS


async def _repair_pass(ctx: _Context, todo: list[Issue], fixed_by_kind: Counter) -> int:
    """Repairs every issue in `todo`, evidence issues batched into one capture
    pass. Returns how many were fixed."""
    evidence = [i for i in todo if i.kind in _EVIDENCE_KINDS]
    rest = [i for i in todo if i.kind not in _EVIDENCE_KINDS]
    fixed = 0
    if evidence:
        for issue, ok in zip(evidence, await _repair_evidence_batch(ctx, evidence)):
            if ok:
                fixed += 1
                fixed_by_kind[issue.kind] += 1
    for issue in rest:
        if await _repair(ctx, issue):
            fixed += 1
            fixed_by_kind[issue.kind] += 1
    return fixed


async def run(payload: dict, articles: list[dict], *, max_iterations: int | None = None, network: bool = True) -> dict:
    """Audit and repair `payload` in place; returns (and stores in meta.qa) the report."""
    limit = _iterations(max_iterations)
    if limit <= 0:
        return {"skipped": True}
    budget = _time_budget()
    started = time.time()
    ctx = _Context(payload, articles, network=network)
    passes: list[dict] = []
    fixed_by_kind: Counter = Counter()
    timed_out = False
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            ctx.client = client
            for number in range(1, limit + 1):
                if time.time() - started > budget:
                    timed_out = True
                    break
                issues = await audit(ctx)
                todo = [i for i in issues if i.fixable and ctx.attempts[i.key] < _MAX_ATTEMPTS]
                if not todo and number >= MIN_PASSES:
                    passes.append({"pass": number, "found": len(issues), "fixed": 0})
                    break
                fixed = await _repair_pass(ctx, todo, fixed_by_kind)
                passes.append({"pass": number, "found": len(issues), "fixed": fixed})
            remaining = await audit(ctx)
    except Exception as exc:  # the agent never raises
        logger.exception("QA agent aborted: %s", exc)
        return {"aborted": str(exc)}
    report = {
        "iterations": len(passes),
        "timed_out": timed_out,
        "passes": passes,
        "fixed": dict(fixed_by_kind),
        "remaining_count": len(remaining),
        "remaining": [{"lens": i.lens, "kind": i.kind, "detail": i.detail[:120], "fixable": i.fixable} for i in remaining[:_MAX_REPORTED]],
        "elapsed_seconds": round(time.time() - started, 1),
    }
    payload.setdefault("meta", {})["qa"] = report
    return report
