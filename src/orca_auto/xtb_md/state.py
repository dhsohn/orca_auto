from __future__ import annotations

from orca_auto.core.artifacts import (
    JOB_REPORT_JSON_FILE,
    JOB_REPORT_MD_FILE,
    JOB_STATE_FILE,
    XTB_MD_JOB_MANIFEST_FILE,
)
from orca_auto.core.state import engine as _engine_state

STATE_FILE_NAME = JOB_STATE_FILE
REPORT_JSON_FILE_NAME = JOB_REPORT_JSON_FILE
REPORT_MD_FILE_NAME = JOB_REPORT_MD_FILE

_EXPORTS = _engine_state.create_engine_state_module_exports(
    _engine_state.EngineStateModuleSpec(
        state_file_name=STATE_FILE_NAME,
        report_json_file_name=REPORT_JSON_FILE_NAME,
        report_md_file_name=REPORT_MD_FILE_NAME,
        manifest_file_name=XTB_MD_JOB_MANIFEST_FILE,
        engine="xtb_md",
        report_title="orca_auto xTB-MD Report",
        selected_input_label="Selected XYZ Snapshot",
    )
)

write_state = _EXPORTS.write_state
write_report_json = _EXPORTS.write_report_json
write_report_md_lines = _EXPORTS.write_report_md_lines
load_state = _EXPORTS.load_state
load_report_json = _EXPORTS.load_report_json


__all__ = [
    "REPORT_JSON_FILE_NAME",
    "REPORT_MD_FILE_NAME",
    "STATE_FILE_NAME",
    "load_report_json",
    "load_state",
    "write_report_json",
    "write_report_md_lines",
    "write_state",
]
