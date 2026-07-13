# Fake ORCA smoke example

This example proves the orca_auto queue lifecycle without requiring a licensed
ORCA installation. It is useful for contributor onboarding, release checks, and
PRs that change project hygiene, docs, queue reporting, or packaging without
changing true ORCA numerical behavior.

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
6. asserts that the queue entry, `job_state.json`, and `job_report.json` all
   report completion, then follows `artifacts.last_out_path` to the confined
   generation output and verifies the normal-termination marker.

Pass an explicit work directory if you want to inspect the generated files after
success:

```bash
bash examples/fake_orca_smoke/run.sh /tmp/orca_auto_fake_smoke_demo
```

This smoke does not prove that a real ORCA/OpenMPI/site-scheduler installation
works, and it does not validate chemical correctness. Use the manual acceptance
checklist in `docs/VALIDATION.md` for changes that depend on real-engine
semantics.
