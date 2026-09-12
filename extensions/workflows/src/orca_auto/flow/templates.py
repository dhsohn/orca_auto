from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orca_auto.core.utils.coercion import normalize_text

CONFORMER_SCREENING_TEMPLATE_ID = "conformer_screening"

CONFORMER_SCREENING_SHORTCUT = "conformer_search"

STANDARD_CONFORMER_INPUT_FILENAME = "input.xyz"

# Default ORCA route lines per template. These are creation-time defaults only:
# once a workflow is created its durable request parameters are the single
# source of truth, and stage materialization fails closed when they are absent.
DEFAULT_CONFORMER_ORCA_ROUTE_LINE = "! r2scan-3c Opt TightSCF"


@dataclass(frozen=True)
class WorkflowTemplateSpec:
    template_id: str
    cli_shortcut: str
    display_label: str
    scaffold_help: str


WORKFLOW_TEMPLATES: tuple[WorkflowTemplateSpec, ...] = (
    WorkflowTemplateSpec(
        template_id=CONFORMER_SCREENING_TEMPLATE_ID,
        cli_shortcut=CONFORMER_SCREENING_SHORTCUT,
        display_label=CONFORMER_SCREENING_SHORTCUT,
        scaffold_help="Create a conformer-screening scaffold.",
    ),
)

WORKFLOW_TEMPLATE_BY_ID = {template.template_id: template for template in WORKFLOW_TEMPLATES}
WORKFLOW_TEMPLATE_IDS = frozenset(WORKFLOW_TEMPLATE_BY_ID)
WORKFLOW_SCAFFOLD_SHORTCUTS = tuple(
    (template.cli_shortcut, template.template_id, template.scaffold_help)
    for template in WORKFLOW_TEMPLATES
)


def normalize_workflow_template_id(value: Any) -> str:
    text = normalize_text(value).lower()
    if text in WORKFLOW_TEMPLATE_IDS:
        return text
    raise ValueError("workflow_type must be conformer_screening")


def workflow_template_id_or_none(value: Any) -> str | None:
    text = normalize_text(value).lower()
    return text if text in WORKFLOW_TEMPLATE_IDS else None


def workflow_template_spec(value: Any) -> WorkflowTemplateSpec | None:
    template_id = workflow_template_id_or_none(value)
    return WORKFLOW_TEMPLATE_BY_ID.get(template_id or "")


def workflow_template_label(template_name: Any, *, default: str = "workflow") -> str:
    text = normalize_text(template_name)
    template = workflow_template_spec(text)
    if template is not None:
        return template.display_label
    return text or default


def workflow_template_shortcut(workflow_type: Any, *, default: str = "workflow") -> str:
    template = workflow_template_spec(workflow_type)
    if template is not None:
        return template.cli_shortcut
    return default


__all__ = [
    "CONFORMER_SCREENING_SHORTCUT",
    "CONFORMER_SCREENING_TEMPLATE_ID",
    "DEFAULT_CONFORMER_ORCA_ROUTE_LINE",
    "STANDARD_CONFORMER_INPUT_FILENAME",
    "WORKFLOW_SCAFFOLD_SHORTCUTS",
    "WORKFLOW_TEMPLATE_BY_ID",
    "WORKFLOW_TEMPLATE_IDS",
    "WORKFLOW_TEMPLATES",
    "WorkflowTemplateSpec",
    "normalize_workflow_template_id",
    "workflow_template_id_or_none",
    "workflow_template_label",
    "workflow_template_shortcut",
    "workflow_template_spec",
]
