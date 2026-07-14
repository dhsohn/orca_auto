"""Developer smoke suites and local review packets.

The canonical source-checkout entrypoint is ``orca_auto smoke``.  The retained
``scripts/smoke.sh`` wrapper pins a specific worktree for CI and parallel
development checkouts.
"""

from .catalog import SmokeScenario, scenarios_for_profile
from .runner import run_smoke_suite

__all__ = ["SmokeScenario", "run_smoke_suite", "scenarios_for_profile"]
