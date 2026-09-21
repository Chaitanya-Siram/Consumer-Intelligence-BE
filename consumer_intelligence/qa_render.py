"""Does the dashboard actually draw its pictures? A real-browser check.

`qa_agent` proves the data is right: every poster picture the payload points at
is a real, servable image. This proves the other half — that the frontend
requests those pictures and the browser renders them in the charts — by opening
the dashboards in headless Chromium, the way a user would, and inspecting the
page's <img> elements.

Only the lenses whose screens draw poster pictures (quote cards, PR author
tables, influencer cards) are opened, one URL per tab (their tabs are
URL-synced). For each: how many of the payload's pictures showed up, and which
images — poster pictures or brand logos alike — are on the page but did not
load. The caller's own bearer token is put in the browser's localStorage so the
app treats it as that user; nothing is stored or sent anywhere else.

A picture the payload has but no tab showed is reported as a note, not a
failure: some sit behind a control the check does not click (PR Research's
"Earlier" period toggle).
"""

import base64
import json
import logging
from urllib.parse import parse_qs, urlsplit

from . import profile_images, qa_agent

logger = logging.getLogger(__name__)

# payload lens key -> the FE route segment (router/nav.js `paths`)
LENS_ROUTES = {
    "pr_research": "pr-research",
    "social_research": "social-research",
    "social_listening": "social-listening",
    "social_audit": "social-audit",
}
AVATAR_PATH = "/consumer-intelligence/profile-image"
_PAGE_TIMEOUT_MS = 45000
_IDLE_TIMEOUT_MS = 15000
_SCROLL_STEP_PX = 600
_SCROLL_PAUSE_MS = 120

# Every <img> on the page (images below the fold are lazy-loaded, so the page is
# scrolled first) and whether the browser managed to draw it.
_COLLECT_IMAGES_JS = """async () => {
  for (let y = 0; y < document.body.scrollHeight; y += %d) {
    window.scrollTo(0, y);
    await new Promise(r => setTimeout(r, %d));
  }
  window.scrollTo(0, 0);
  await new Promise(r => setTimeout(r, 400));
  return [...document.images].map(i => ({src: i.currentSrc || i.src, ok: i.complete && i.naturalWidth > 0}));
}""" % (_SCROLL_STEP_PX, _SCROLL_PAUSE_MS)


def avatar_keys(storyboard: dict) -> set[str]:
    """The poster-picture keys the frontend is expected to draw. A quote shown
    as an embed, a screenshot or a scraped preview card carries the poster's
    face inside that picture, so only the designed post card (evidence level 1)
    draws the separate avatar; author/influencer rows always do."""
    return {
        row["avatar_key"]
        for row, _ in profile_images._people_rows(storyboard)  # noqa: SLF001
        if row.get("avatar_key") and ("text" not in row or qa_agent.evidence_level(row) == 1)
    }


def tab_ids(storyboard: dict) -> list[str]:
    return [t["id"] for t in storyboard.get("tabs") or [] if isinstance(t, dict) and t.get("id")]


def _key_of(src: str) -> str | None:
    if AVATAR_PATH not in src:
        return None
    values = parse_qs(urlsplit(src).query).get("key")
    return values[0] if values else None


def _org_id(token: str) -> str | None:
    """The `org_id` claim of the caller's JWT (read, not verified — the backend
    already accepted the token)."""
    try:
        body = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return str(claims["org_id"]) if claims.get("org_id") is not None else None
    except Exception:
        return None


def summarise(expected: set[str], images: list[dict]) -> dict:
    """Pure: what the pages drew, against what the payload said to draw.
    `images` is every {src, ok} collected across a lens's tabs."""
    drawn: set[str] = set()
    failed: set[str] = set()
    for image in images:
        key = _key_of(image["src"])
        if image["ok"]:
            if key:
                drawn.add(key)
        else:
            failed.add(image["src"])
    not_shown = sorted(expected - drawn)
    passed = not failed and (not expected or bool(drawn & expected))
    return {
        "status": "pass" if passed else "fail",
        "pictures_in_payload": len(expected),
        "pictures_rendered": len(drawn & expected),
        "images_failed_to_load": sorted(failed)[:20],
        "note": f"{len(not_shown)} picture(s) in the data were not visible on any tab (may sit behind a control)" if not_shown and passed else "",
    }


async def check_rendering(payload: dict, *, frontend_url: str, project_id: int, session_id: int, token: str) -> dict:
    """Open each avatar-drawing lens's tabs in a real browser and report what
    rendered. Never raises; a browser that can't start returns {"error": ...}."""
    targets = {lens: sb for lens, sb in payload.items() if lens in LENS_ROUTES and isinstance(sb, dict) and sb.get("tabs")}
    if not targets:
        return {"status": "skipped", "reason": "no lens in this dashboard is one whose screens draw poster pictures"}
    try:
        from playwright.async_api import async_playwright

        base = frontend_url.rstrip("/")
        init_script = f"localStorage.setItem('auth_token', {json.dumps(token)});"
        org = _org_id(token)
        if org:
            init_script += f"localStorage.setItem('organization_id', {json.dumps(org)});"
        lenses: dict[str, dict] = {}
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context(viewport={"width": 1440, "height": 900})
                await context.add_init_script(init_script)
                page = await context.new_page()
                for lens, storyboard in targets.items():
                    images: list[dict] = []
                    tabs = tab_ids(storyboard) or [""]
                    for tab in tabs:
                        url = f"{base}/{project_id}/sessions/{session_id}/{LENS_ROUTES[lens]}" + (f"/{tab}" if tab else "")
                        try:
                            await page.goto(url, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT_MS)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=_IDLE_TIMEOUT_MS)
                            except Exception:
                                pass  # a page that never goes quiet is still inspected
                            images += await page.evaluate(_COLLECT_IMAGES_JS)
                        except Exception as exc:
                            logger.info("render check could not open %s: %s", url, exc)
                            images.append({"src": f"[page did not open] {url}", "ok": False})
                    lenses[lens] = {**summarise(avatar_keys(storyboard), images), "tabs_checked": len(tabs)}
            finally:
                await browser.close()
    except Exception as exc:
        logger.warning("render check unavailable: %s", exc)
        return {"status": "error", "error": str(exc)[:200]}
    status = "pass" if all(v["status"] == "pass" for v in lenses.values()) else "fail"
    return {"status": status, "lenses": lenses}
