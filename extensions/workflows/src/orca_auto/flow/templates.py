from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orca_auto.core.utils.coercion import normalize_text

REACTION_TS_SEARCH_TEMPLATE_ID = "reaction_ts_search"
CONFORMER_SCREENING_TEMPLATE_ID = "conformer_screening"
SCAN_TS_SEARCH_TEMPLATE_ID = "scan_ts_search"

REACTION_TS_SEARCH_SHORTCUT = "ts_search"
CONFORMER_SCREENING_SHORTCUT = "conformer_search"
SCAN_TS_SEARCH_SHORTCUT = "scan_ts"

STANDARD_REACTION_REACTANT_FILENAME = "reactant.xyz"
STANDARD_REACTION_PRODUCT_FILENAME = "product.xyz"
STANDARD_CONFORMER_INPUT_FILENAME = "input.xyz"

# Default ORCA route lines per template. These are creation-time defaults only:
# once a workflow is created its durable request parameters are the single
# source of truth, and stage materialization fails closed when they are absent.
DEFAULT_REACTION_TS_ORCA_ROUTE_LINE = "! r2scan-3c OptTS Freq TightSCF"
DEFAULT_CONFORMER_ORCA_ROUTE_LINE = "! r2scan-3c Opt TightSCF"
DEFAULT_SCAN_ORCA_ROUTE_LINE = "! Opt r2scan-3c TightSCF"
DEFAULT_SCAN_OPTTS_ORCA_ROUTE_LINE = "! OptTS Freq r2scan-3c TightSCF"


@dataclass(frozen=True)
class WorkflowTemplateSpec:
    template_id: str
    cli_shortcut: str
    display_label: str
    scaffold_help: str


WORKFLOW_TEMPLATES: tuple[WorkflowTemplateSpec, ...] = (
    WorkflowTemplateSpec(
        template_id=REACTION_TS_SEARCH_TEMPLATE_ID,
        cli_shortcut=REACTION_TS_SEARCH_SHORTCUT,
        display_label=REACTION_TS_SEARCH_SHORTCUT,
        scaffold_help="Create a reaction TS-search scaffold.",
    ),
    WorkflowTemplateSpec(
        template_id=CONFORMER_SCREENING_TEMPLATE_ID,
        cli_shortcut=CONFORMER_SCREENING_SHORTCUT,
        display_label=CONFORMER_SCREENING_SHORTCUT,
        scaffold_help="Create a conformer-screening scaffold.",
    ),
    WorkflowTemplateSpec(
        template_id=SCAN_TS_SEARCH_TEMPLATE_ID,
        cli_shortcut=SCAN_TS_SEARCH_SHORTCUT,
        display_label=SCAN_TS_SEARCH_SHORTCUT,
        scaffold_help="Create a relaxed-scan TS-search scaffold.",
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
    raise ValueError(
        "workflow_type must be one of: reaction_ts_search, conformer_screening, scan_ts_search"
    )


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
    "DEFAULT_REACTION_TS_ORCA_ROUTE_LINE",
    "DEFAULT_SCAN_OPTTS_ORCA_ROUTE_LINE",
    "DEFAULT_SCAN_ORCA_ROUTE_LINE",
    "REACTION_TS_SEARCH_SHORTCUT",
    "REACTION_TS_SEARCH_TEMPLATE_ID",
    "STANDARD_CONFORMER_INPUT_FILENAME",
    "STANDARD_REACTION_PRODUCT_FILENAME",
    "STANDARD_REACTION_REACTANT_FILENAME",
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
