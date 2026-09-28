# ADR 0012: Positive scientific completion evidence

- Status: Accepted
- Date: 2026-09-28

## Problem

A normal ORCA termination could complete an Opt without an explicit convergence
verdict. An earlier SCF failure stayed sticky even after explicit convergence.
The machine observation exposed execution status without scientific measurements.
Synthetic success fixtures could therefore test control flow with insufficient
chemical evidence.

## Decision

The streaming output analyzer owns positive completion evidence and last explicit
SCF/optimization verdicts. It requires finite final energy, convergence for Opt/TS,
and the final frequency section when requested. The common frequency scanner and
energy grammar remain shared with reporting. Missing evidence fails the run.

The results-bundle's producer-specific results object gains science; the common
v1 envelope stays unchanged. The report parses receipted input bytes once and
hashes the output bytes actually decoded. Missing measurements are null. Only
unconstrained optimized structures with matching harmonic evidence receive a
stationary-point label. Historical terminal reports are never rewritten.

## Verification and limits

Scientific completion regressions distinguish absent evidence, SCF recovery and
terminal failure. Authentic ORCA 6.1.1 fixtures cover SP, Opt+Freq, nonconverged
Opt and TS+IRC, with provenance and independent expected measurements. Report
tests reject mismatched evidence, and the normal validation/package gates and
bounded real-engine acceptance apply.

IRC currently establishes path-driver evidence only. No complete validation of
MD, NEB, compound jobs or global stability is claimed. Other ORCA versions remain
unverified. Public consumers must handle absent fields in historical reports.
