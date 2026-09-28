"""Generation reports of one ORCA job.

``publication.write_report_files`` writes them into the verified execution
generation: ``job_report.html`` composed from the selected input's facets
(``composer``), ``si_block.md`` (``si``) and ``machine.json`` (assembled by
``orca.machine_observation``). Non-stationary path and dynamics jobs (plain
NEB, MD) get no HTML report. Report generation must never break run
finalization, so every HTML and SI error is logged and swallowed.
"""
