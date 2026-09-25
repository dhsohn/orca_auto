# Related Work and Project Scope

ORCA_auto provides a reliable runtime and observability layer for ORCA-centered computational chemistry calculations on Linux and WSL.

## Local-first Companion Tools

ORCA_auto connects with neighboring open-source tools:

- **[Chemvas](https://github.com/dhsohn/Chemvas)**: Drawing molecular structures and reaction schemes.
- **ORCA_auto**: Durable queueing, execution management, and observability for quantum chemical calculations.
- **[LLMdocx](https://github.com/dhsohn/LLMdocx)**: Generating research reports, Supporting Information, and paper drafts.

These tools share a versioned `machine.json` schema and standardized output bundles, allowing them to be composed smoothly in local-first research pipelines.

## Shell Scripts vs. ORCA_auto

Direct execution (`orca input.inp > input.out`) can suffice for a single calculation.
For repeated jobs, ORCA_auto provides queue management, process supervision,
recovery, and structured output:

- Background queue execution that persists across terminal closures and reboots.
- Process supervision via systemd rather than fragile foreground scripts.
- Consistent structured artifacts (`job_state.json` and `machine.json`) for downstream automation.
- Explicit failure classification and reproducible recovery procedures.
- Unified activity tracking across multiple jobs.

## Cluster Schedulers and Resource Managers

Cluster schedulers like SLURM and PBS manage compute resources across large clusters. ORCA_auto focuses on workstation/single-node execution (under Linux or WSL) with systemd. It handles ORCA-specific lifecycle management, input selection, output parsing, and failure diagnosis above the OS process layer.

## General Workflow Engines

General workflow engines (such as Nextflow, Snakemake, and AiiDA) provide DAG abstractions across multi-step distributed pipelines. ORCA_auto handles durable submission, execution and reporting for standalone ORCA jobs on Linux/WSL. External tools can consume its documented machine observations; version 7 does not provide workflow orchestration.

## Chemistry Libraries

Libraries like ASE, RDKit, and cclib handle structure generation, file parsing, and cheminformatics. ORCA_auto focuses on execution supervision and lifecycle management, rather than duplicating molecular modeling algorithms.
