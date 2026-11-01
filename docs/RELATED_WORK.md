# Related Work and Project Scope

ORCA_auto manages standalone ORCA calculations on a Linux or WSL workstation: queueing, execution, progress and results.

## Local-first Companion Tools

These independent open-source projects cover different parts of chemistry research:

- **[Chemvas](https://github.com/dhsohn/Chemvas)**: Drawing molecular structures and reaction schemes.
- **ORCA_auto**: Durable queueing, execution management, and observability for quantum chemical calculations.
- **[Chemleaf](https://github.com/dhsohn/Chemleaf)**: Generating research reports, Supporting Information, and paper drafts.

ORCA_auto publishes calculation observations in `machine.json`. Programs that read those results should follow its [public contracts](PUBLIC_CONTRACTS.md). Chemvas uses CDXML for editable drawings; each project owns its file formats and interfaces.

## Shell Scripts vs. ORCA_auto

Direct execution (`orca input.inp > input.out`) suffices for a single run. For repeated jobs, ORCA_auto adds:

- Disk-backed queueing across terminal disconnects and reboots.
- Process supervision through systemd.
- Structured observations in `machine.json` and internal recovery state in `job_state.json`.
- Failure classification without automatic reruns of failed calculations.
- Queue status tracking across jobs.

## Cluster Schedulers and Resource Managers

Cluster schedulers (SLURM, PBS) allocate nodes across multi-host clusters. ORCA_auto targets single-host Linux and WSL workstations under systemd, handling ORCA input binding, output analysis, and recovery above OS process supervision.

## General Workflow Engines

Workflow engines (Nextflow, Snakemake, AiiDA) orchestrate multi-step DAG pipelines. ORCA_auto runs standalone ORCA jobs only; external tools can consume its documented `machine.json` observations.

## Chemistry Libraries

Libraries such as ASE, RDKit, and cclib handle structure generation, file parsing, and cheminformatics. ORCA_auto supervises ORCA execution rather than implementing molecular modeling algorithms.
