# Fake ORCA smoke example

Runs an end-to-end ORCA_auto queue pass without a licensed ORCA installation, for onboarding, release checks, and changes that do not alter real-ORCA execution or output parsing.

Run from the repository root:

```bash
bash examples/fake_orca_smoke/run.sh
```

The script:

1. creates a temporary runtime directory;
2. writes a small fake ORCA executable that prints a normal-termination marker;
3. writes a minimal `orca_auto.yaml` pointing at the fake executable;
4. submits `water_opt.inp` through `orca_auto run-dir`;
5. runs one queue-worker poll;
6. asserts that the queue entry and private `job_state.json` completed and that
   public `machine.json` reports a consumable completion, verified by the
   repository's test-tree reader `tests/contracts/report_verifier.py`, then
   follows the verified result to the confined generation output and checks the
   normal-termination marker.

Pass an explicit work directory if you want to inspect the generated files after
success:

```bash
bash examples/fake_orca_smoke/run.sh /tmp/orca_auto_fake_smoke_demo
```

This script does not test a real ORCA/OpenMPI installation or chemical correctness; see `docs/VALIDATION.md` when real-engine behavior changes.
