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
# CREST's named ensemble outputs, in handoff preference order. The runner walks
# this order, the contract layer deduplicates across it, and the workflow reads
# refusals against it, so the names live here rather than once per layer.
CREST_RETAINED_ENSEMBLE_NAMES = (
    "crest_conformers.xyz",
    "crest_ensemble.xyz",
    "crest_rotamers.xyz",
    "crest_best.xyz",
)
# The first two carry CREST's conformer set, as opposed to the rotamer file and
# the single best structure after them; ``crest_conformers.xyz`` is the 2.x name
# and ``crest_ensemble.xyz`` the 3.x one. They are not mutually exclusive — a
# CREST 3.0.2 job under this machine's run roots wrote both — so a refusal
# naming one of them is not on its own a statement that the conformer set is
# gone. Whether the other one reached the handoff is a separate question, and
# the caller has to ask it.
CREST_PRIMARY_ENSEMBLE_NAMES = CREST_RETAINED_ENSEMBLE_NAMES[:2]

__all__ = [
    "CREST_JOB_MANIFEST_FILE",
    "CREST_PRIMARY_ENSEMBLE_NAMES",
    "CREST_RETAINED_ENSEMBLE_NAMES",
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
