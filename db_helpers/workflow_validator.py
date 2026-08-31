"""Validation and cleaning for the workflow designer graph's data nodes."""
from typing import Any

SOURCE_TYPE_FILE = "file"
SOURCE_TYPE_API = "api"
SOURCE_TYPES = (SOURCE_TYPE_FILE, SOURCE_TYPE_API)

# Fields that belong to only one source type; the other type's are dropped.
_FILE_ONLY_FIELDS = ("file_upload_id",)
_API_ONLY_FIELDS = ("data_sources", "queries", "api", "query")

KEYWORD_FIELDS = ("brandKeywords", "competitorKeywords", "messageKeywords")


class WorkflowValidationError(ValueError):
    """Raised when a workflow's data nodes are missing or misusing fields."""


def _non_empty_strings(value: Any) -> list[str]:
    """The non-blank strings in a list value (empty list for anything else)."""
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _clean_data_node(node: dict[str, Any]) -> dict[str, Any]:
    """Validate one data node and return it with the other source type's fields removed.

    Args:
        node: A node dict whose type is "data".

    Returns:
        The node with irrelevant source-type fields stripped.
    """
    node_id = node.get("id") or "<no id>"
    data = node.get("data")
    if not isinstance(data, dict):
        raise WorkflowValidationError(f"Data node {node_id} has no data object.")

    source_type = data.get("sourceType")
    if source_type not in SOURCE_TYPES:
        raise WorkflowValidationError(
            f"Data node {node_id} has sourceType {source_type!r}; "
            f"expected one of {', '.join(SOURCE_TYPES)}."
        )

    if source_type == SOURCE_TYPE_FILE:
        file_upload_id = data.get("file_upload_id")
        if not isinstance(file_upload_id, str) or not file_upload_id.strip():
            raise WorkflowValidationError(
                f"Data node {node_id} has sourceType 'file' but no file_upload_id."
            )
        data["file_upload_id"] = file_upload_id.strip()
        drop = _API_ONLY_FIELDS
    else:
        data_sources = _non_empty_strings(data.get("data_sources"))
        queries = _non_empty_strings(data.get("queries"))
        if not data_sources:
            raise WorkflowValidationError(f"Data node {node_id} needs at least one data source.")
        if not queries:
            raise WorkflowValidationError(f"Data node {node_id} needs at least one query.")
        data["data_sources"] = data_sources
        data["queries"] = queries
        data.pop("api", None)
        drop = _FILE_ONLY_FIELDS

    for field in drop:
        data.pop(field, None)

    node["data"] = data
    return node


def validate_workflow(workflow: Any) -> dict[str, Any]:
    """Validate a workflow graph's data nodes and strip each one's unused fields.

    Args:
        workflow: The workflow graph (nodes + edges) as posted by the designer.

    Returns:
        The workflow with its data nodes cleaned.
    """
    if not isinstance(workflow, dict):
        raise WorkflowValidationError("Workflow must be an object with nodes and edges.")

    nodes = workflow.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise WorkflowValidationError("Workflow has no nodes.")

    data_nodes = [n for n in nodes if isinstance(n, dict) and n.get("type") == "data"]
    if not data_nodes:
        raise WorkflowValidationError("Workflow has no data node.")

    for node in data_nodes:
        _clean_data_node(node)
    return workflow


def collect_file_upload_ids(workflow: dict[str, Any]) -> list[str]:
    """The file_upload_ids referenced by the workflow's file data nodes."""
    ids: list[str] = []
    for node in workflow.get("nodes", []):
        if not isinstance(node, dict) or node.get("type") != "data":
            continue
        data = node.get("data") or {}
        if data.get("sourceType") == SOURCE_TYPE_FILE and data.get("file_upload_id"):
            ids.append(data["file_upload_id"])
    return ids


def collect_keywords(workflow: dict[str, Any]) -> dict[str, list[str]]:
    """Merge the keyword lists off the data nodes plus the analysis nodes'
    competitorKeywords, de-duplicated in first-seen order.

    Args:
        workflow: A validated workflow graph.

    Returns:
        Dict with brand_keywords, competitor_keywords and message_keywords.
    """
    merged: dict[str, list[str]] = {field: [] for field in KEYWORD_FIELDS}
    seen: dict[str, set[str]] = {field: set() for field in KEYWORD_FIELDS}
    for node in workflow.get("nodes", []):
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type not in ("data", "analysis"):
            continue
        data = node.get("data") or {}
        # Analysis nodes carry only competitorKeywords.
        fields = KEYWORD_FIELDS if node_type == "data" else ("competitorKeywords",)
        for field in fields:
            for keyword in _non_empty_strings(data.get(field)):
                if keyword not in seen[field]:
                    seen[field].add(keyword)
                    merged[field].append(keyword)
    return {
        "brand_keywords": merged["brandKeywords"],
        "competitor_keywords": merged["competitorKeywords"],
        "message_keywords": merged["messageKeywords"],
    }


# Shown as the request body example in Swagger.
WORKFLOW_EXAMPLE = {
    "nodes": [
        {
            "id": "data_33",
            "type": "data",
            "position": {"x": 60, "y": 145},
            "data": {
                "label": "Data",
                "sourceType": "file",
                "file_upload_id": "32784y238hey3g3y2tf",
                "brandKeywords": ["Apple"],
                "messageKeywords": ["innovation", "product launches"],
                "competitorKeywords": ["Samsung"],
            },
        },
        {
            "id": "data_34",
            "type": "data",
            "position": {"x": 60, "y": 160},
            "data": {
                "label": "Data",
                "sourceType": "api",
                "brandKeywords": ["Apple"],
                "messageKeywords": ["market trends", "technology"],
                "competitorKeywords": ["Samsung"],
                "data_sources": ["google_rss"],
                "queries": ['"Apple AND Samsung"'],
            },
        },
        {
            "id": "analysis_35",
            "type": "analysis",
            "position": {"x": 380, "y": 40},
            "data": {
                "label": "Analysis",
                "lens": "media_monitoring",
                "llm": "openai",
                "skill": "",
                "competitorKeywords": ["Samsung"],
            },
        },
        {
            "id": "review_37",
            "type": "review",
            "position": {"x": 700, "y": 40},
            "data": {"label": "Review", "flag": 50, "auto": 75, "requiresSignOff": False},
        },
        {
            "id": "assembly_38",
            "type": "assembly",
            "position": {"x": 1020, "y": 145},
            "data": {
                "label": "Dashboard Builder",
                "clientName": "Apple",
                "charts": ["volume", "sentiment", "share_of_voice", "themes"],
                "layout": "Classic",
            },
        },
        {
            "id": "output_39",
            "type": "output",
            "position": {"x": 1340, "y": 145},
            "data": {
                "label": "Output",
                "format": "Dashboard",
                "projectName": "Apple vs Samsung Media Monitoring",
                "projectDescription": "Media monitoring comparing Apple and Samsung.",
            },
        },
    ],
    "edges": [
        {"id": "e_data_33_analysis_35", "source": "data_33", "target": "analysis_35", "invalid": False},
        {"id": "e_data_34_analysis_35", "source": "data_34", "target": "analysis_35", "invalid": False},
        {"id": "e_analysis_35_review_37", "source": "analysis_35", "target": "review_37", "invalid": False},
        {"id": "e_review_37_assembly_38", "source": "review_37", "target": "assembly_38", "invalid": False},
        {"id": "e_assembly_38_output_39", "source": "assembly_38", "target": "output_39", "invalid": False},
    ],
}

# Swagger request-body examples. These go on Body(openapi_examples=...) rather than
# the models' json_schema_extra because Pydantic alphabetizes anything it puts
# through JSON-Schema generation, which scrambles the node field order.
CREATE_EXAMPLE = {
    "default": {
        "summary": "Workflow with a file and an api data node",
        "value": {"project_id": 1, "workflow": WORKFLOW_EXAMPLE},
    }
}
WORKFLOW_EXAMPLE_BODY = {
    "default": {
        "summary": "Workflow with a file and an api data node",
        "value": {"workflow": WORKFLOW_EXAMPLE},
    }
}