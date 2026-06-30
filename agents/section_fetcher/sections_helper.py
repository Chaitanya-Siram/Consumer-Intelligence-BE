from agents.chart_generator.llm_client import complete_json
from configs import logger

_SECTIONS_SYSTEM = (
    "You extract the section NAMES defined in a media-monitoring prompt — the labels "
    "articles get sorted into. Return ONLY the names, in the SAME ORDER they appear in "
    "the prompt. Exclude descriptions, examples and brand lists. Respond with JSON only."
)


def extract_section_names(prompt: str) -> list[str]:
    """Use the LLM to pull the ordered section names from a sections prompt.
    Best-effort: returns [] if extraction fails."""
    user = (
        "Sections prompt:\n" + prompt
        + '\n\nReturn JSON {"sections": ["<name>", ...]} preserving prompt order.'
    )
    try:
        data = complete_json(_SECTIONS_SYSTEM, user, max_tokens=512)
        raw = data.get("sections") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for n in raw:
            s = str(n or "").strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        return out
    except Exception:  # noqa: BLE001
        logger.exception("Section-name extraction failed")
        return []