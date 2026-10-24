# Related Work and Project Scope

ORCA_auto manages standalone ORCA calculations on a Linux or WSL workstation: queueing, execution, progress and results.

## Local-first Companion Tools

These independent open-source projects cover different parts of chemistry research:

- **[Chemvas](https://github.com/dhsohn/Chemvas)**: Drawing molecular structures and reaction schemes.
- **ORCA_auto**: Durable queueing, execution management, and observability for quantum chemical calculations.
- **[Chemleaf](https://github.com/dhsohn/Chemleaf)**: Generating research reports, Supporting Information, and paper drafts.

ORCA_auto publishes calculation observations in `machine.json`. Programs that read those results should follow its [public contracts](PUBLIC_CONTRACTS.md). Chemvas uses CDXML for editable drawings; each project owns its file formats and interfaces.

## Shell Scripts vs. ORCA_auto

Direct execution (`orca input.inp > input.out`) can suffice for a single calculation.
For repeated jobs, ORCA_auto provides queue management, process supervision,
recovery, and structured output:

- Background queue execution that persists across terminal closures and reboots.
- Process supervision through systemd.
- Public result observations in `machine.json`; internal recovery metadata in `job_state.json`.
- Explicit failure classification and reproducible recovery procedures.
- Unified activity tracking across multiple jobs.

## Cluster Schedulers and Resource Managers

Cluster schedulers like SLURM and PBS manage compute resources across large clusters. ORCA_auto focuses on workstation/single-node execution (under Linux or WSL) with systemd. It handles ORCA-specific lifecycle management, input selection, output parsing, and failure diagnosis above the OS process layer.

## General Workflow Engines

General workflow engines (such as Nextflow, Snakemake, and AiiDA) provide DAG abstractions across multi-step distributed pipelines. ORCA_auto handles durable submission, execution and reporting for standalone ORCA jobs on Linux/WSL. External tools can consume its documented machine observations; ORCA_auto does not provide workflow orchestration.

## Chemistry Libraries

Libraries like ASE, RDKit, and cclib handle structure generation, file parsing, and cheminformatics. ORCA_auto focuses on execution supervision and lifecycle management, rather than duplicating molecular modeling algorithms.
