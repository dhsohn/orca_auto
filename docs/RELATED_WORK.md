# Related Work and Project Scope

ORCA_auto provides a reliable runtime and observability layer for ORCA-centered computational chemistry workflows on Linux and WSL. This page explains how ORCA_auto fits into the broader computational chemistry ecosystem.

## Local-first Companion Tools

ORCA_auto is designed to complement neighboring open-source tools:

- **[Chemvas](https://github.com/dhsohn/Chemvas)**: Drawing molecular structures and reaction schemes.
- **ORCA_auto**: Durable queueing, execution management, and observability for quantum chemical calculations.
- **[LLMdocx](https://github.com/dhsohn/LLMdocx)**: Generating research reports, Supporting Information, and paper drafts.

These tools share a versioned `machine.json` schema and standardized output bundles, allowing them to be composed smoothly in local-first research pipelines.

## Shell Scripts vs. ORCA_auto

For single one-off calculations, running `orca input.inp > input.out` directly is simple and effective. Research groups often build shell scripts for batch runs or directory organization.

ORCA_auto augments this approach with structured, reliable infrastructure:

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

Toolkits like ASE, RDKit, and cclib handle structure generation, file parsing, and cheminformatics. ORCA_auto complements these libraries by providing execution supervision and observable lifecycle management, rather than duplicating molecular modeling algorithms.
