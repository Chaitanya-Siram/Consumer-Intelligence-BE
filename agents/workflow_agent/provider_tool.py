"""The agent's data-provider tool: which providers an organization can actually fetch from."""
from sqlalchemy.orm import Session

from db_helpers.repository.data_provider_keys_db import get_org_active_data_providers

def get_active_providers(db: Session, org_id: int) -> dict[str, str]:
    """The org's active data providers, limited to labels the fetcher honors.

    Args:
        db: Database session.
        org_id: Owning organization id.

    Returns:
        Dict of provider display name to label.
    """
    providers = get_org_active_data_providers(db, org_id)
    return {
        name: label
        for name, label in providers.items()
    }


def format_providers_block(providers: dict[str, str]) -> str:
    """Render the provider list plus its fetch-time caveats for the system prompt.

    Only names are shown. Labels are an internal routing detail, so keeping them out
    of the prompt is what stops the agent echoing them at the user.

    Args:
        providers: Provider display name to label.

    Returns:
        A prompt section listing the selectable providers.
    """
    if providers:
        lines = "\n".join(f"  - {name}" for name in sorted(providers))
    else:
        lines = "  (none configured)"
    return (
        "ACTIVE DATA PROVIDERS — the only sources available to this organization:\n"
        f"{lines}\n"
        "Refer to these by name exactly as written above, including any punctuation. "
        "Never invent a shorter or tidier name for one. A source not listed is not "
        "enabled — if the user asks for it, say so and offer what is listed.\n"
        "Every selected provider receives the SAME query list, so one query must work "
        "across all of them."
    )


def resolve_provider_names(names: list[str], providers: dict[str, str]) -> list[str]:
    """Map provider names the model chose onto the labels the fetcher dispatches on.

    Accepts a label too, since the model occasionally returns one despite the prompt.

    Args:
        names: Provider names (or labels) selected this turn.
        providers: Provider display name to label.

    Returns:
        The matching labels, de-duplicated in first-seen order.
    """
    by_name = {name.strip().lower(): label for name, label in providers.items()}
    by_label = {label.strip().lower(): label for label in providers.values()}

    labels: list[str] = []
    for raw in names:
        key = str(raw or "").strip().lower()
        label = by_name.get(key) or by_label.get(key)
        if label and label not in labels:
            labels.append(label)
    return labels


def provider_name(label: str, providers: dict[str, str]) -> str:
    """The display name for a label, falling back to the label itself."""
    for name, value in providers.items():
        if value == label:
            return name
    return label
