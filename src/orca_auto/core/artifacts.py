from __future__ import annotations

RUN_STATE_FILE = "job_state.json"
RUN_REPORT_JSON_FILE = "machine.json"
RUN_REPORT_HTML_FILE = "job_report.html"
MAX_RUN_ARTIFACT_JSON_BYTES = 64 * 1024 * 1024
WORKFLOW_REPORT_HTML_FILE = "workflow_report.html"
SI_BLOCK_MD_FILE = "si_block.md"
WORKFLOW_SI_MD_FILE = "workflow_si.md"
QUEUE_FILE = "queue.json"
XTB_JOB_MANIFEST_FILE = "xtb_job.yaml"
CREST_JOB_MANIFEST_FILE = "crest_job.yaml"

__all__ = [
    "CREST_JOB_MANIFEST_FILE",
    "MAX_RUN_ARTIFACT_JSON_BYTES",
    "QUEUE_FILE",
    "RUN_REPORT_HTML_FILE",
    "RUN_REPORT_JSON_FILE",
    "RUN_STATE_FILE",
    "SI_BLOCK_MD_FILE",
    "WORKFLOW_REPORT_HTML_FILE",
    "WORKFLOW_SI_MD_FILE",
    "XTB_JOB_MANIFEST_FILE",
]
