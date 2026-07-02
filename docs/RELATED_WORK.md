# Related work and project scope

orca_auto is a runtime and observability layer around ORCA-centered computational
chemistry workflows. It is not a replacement for ORCA, a general workflow engine,
or a chemistry toolkit. This page explains the gap it is designed to fill and how
it relates to neighboring tools.

## Raw ORCA commands and shell scripts

ORCA already provides the electronic-structure engine and input language. For a
single calculation, invoking ORCA directly is often the simplest and clearest
choice. Many groups then add shell scripts for repeated submission, directory
layout, and result copying.

orca_auto is useful when that script layer needs durable state:

- queue entries that survive terminal restarts;
- supervised workers instead of foreground-only runs;
- consistent `job_state.json`, `job_report.json`, and `job_report.md` outputs;
- retry and resume decisions that are recorded rather than implicit;
- a compact activity view across multiple ORCA and workflow jobs.

The design intent is to keep ORCA input files as the chemistry-facing contract
while adding an auditable runtime layer around execution.

## Site schedulers and service managers

Schedulers and service managers such as SLURM, PBS, systemd, and cron are
important infrastructure, but they do not by themselves understand ORCA job
state, retry provenance, selected input files, or chemistry-specific failure
classification.

orca_auto complements this layer. It can be run under systemd on Linux or WSL,
and it records ORCA/job-level state above the process-manager layer. It does not
try to become a cluster scheduler or replace local site policy about cores,
memory, queues, or walltime.

## General workflow engines

General engines such as Snakemake, Nextflow, Parsl, FireWorks, and AiiDA provide
broad workflow abstractions. They are appropriate when a project needs a general
DAG engine, database-backed provenance framework, or multi-code workflow system.

orca_auto is intentionally narrower. Its public surface is a queue-first CLI,
configuration file, worker runtime, and report/state contracts tailored to ORCA
and ORCA-centered reaction/conformer workflows. This smaller scope keeps the
common local/WSL use case easy to inspect and debug, while still leaving room to
export artifacts into broader provenance systems later.

## Chemistry and molecular-toolkit ecosystem

Libraries such as ASE, RDKit, Open Babel, and cclib occupy adjacent roles:
structure manipulation, cheminformatics, file conversion, parsing, or analysis.
They are not direct replacements for a supervised ORCA runtime.

orca_auto may use or interoperate with chemistry tools at the workflow edge, but
its main responsibility is execution orchestration and observable job state, not
molecular modeling algorithms or post-processing analysis APIs.

## xTB and CREST

xTB and CREST remain important for fast pre-screening and conformer-related
workflow stages. In the current public design they are not standalone public
orca_auto surfaces. They are internal workflow stages used to prepare or route
ORCA-centered work while preserving one public CLI and one runtime model.

## Non-goals

orca_auto is not intended to be:

- an electronic-structure engine;
- a replacement for ORCA input design or chemical judgment;
- a cluster scheduler or resource broker;
- a general DAG/workflow engine;
- a GUI or laboratory information-management system;
- a package for crawling literature or managing publication databases;
- a guarantee that a calculation will converge or be chemically meaningful.

The project is successful when it makes ORCA-centered workflows easier to run,
inspect, recover, and curate without hiding the underlying computational
chemistry decisions.
