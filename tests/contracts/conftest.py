"""Shared runtime for the contract tests: a fake ORCA, an isolated config and the effect log.

``harness`` gives each test its own runs root, admission root, ``orca_auto.yaml``
and fake ORCA, records parent notifications in a ``RecordingChannel`` and
installs the effect log in this process and in every worker child it spawns.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from orca_auto.cli import main as cli_main
from orca_auto.orca.config import load_config
from orca_auto.orca.queue import notifications as queue_notifications
from orca_auto.orca.queue.worker import OrcaQueueWorker
from tests.conftest import RecordingChannel, make_app_cfg, write_config_file
from tests.contracts import effect_log
from tests.contracts.normalize import Normalizer, read_json

H2O_INPUT = """\
! HF STO-3G Opt Freq
%pal nprocs 1 end
%maxcore 500
* xyz 0 1
O 0.000000 0.000000 0.117000
H 0.000000 0.757000 -0.467000
H 0.000000 -0.757000 -0.467000
*
"""

# One fake ORCA for every scenario. ``$1`` is the bound input and stdout the
# output file. ORCA is launched through a pinned descriptor, so ``$0`` does not
# name the script; ``FAKE_ORCA_DIR_ENV`` names its directory. A ``mode`` file
# there selects the next call:
# ``complete`` (default), ``error`` (error termination, exit 3), ``hang``
# (touch ``started`` and sleep) or ``crash`` (SIGKILL the worker child, the
# parent of the launch gate that forked this script).
FAKE_ORCA = """\
#!/bin/sh
here="$ORCA_AUTO_CONTRACT_FAKE_ORCA_DIR"
mode=complete
[ -f "$here/mode" ] && mode=$(cat "$here/mode")
echo "                                 * O   R   C   A *"
echo "Program Version 6.0.1 - RELEASE -"
n=0
while IFS= read -r line || [ -n "$line" ]; do
  n=$((n + 1))
  printf '| %2d> %s\\n' "$n" "$line"
done < "$1"
echo "                *** Geometry Optimization Cycle   1 ***"
echo "FINAL SINGLE POINT ENERGY       -74.965901192"
case "$mode" in
  hang)
    touch "$here/started"
    exec sleep 60
    ;;
  crash)
    read -r _pid _comm _state child _rest < "/proc/$PPID/stat"
    kill -9 "$child"
    exit 1
    ;;
  error)
    echo "ORCA finished by error termination in SCF"
    echo "ORCA FINISHED BY ERROR TERMINATION"
    exit 3
    ;;
esac
cat <<'EOF'
                *** Geometry Optimization Cycle   2 ***
FINAL SINGLE POINT ENERGY       -74.965995310
                    ***********************HURRAY********************
                    ***        THE OPTIMIZATION HAS CONVERGED     ***
                    *************************************************
---------------------------------
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
  O      0.000000    0.000000    0.127000
  H      0.000000    0.751000   -0.472000
  H      0.000000   -0.751000   -0.472000

-----------------------
VIBRATIONAL FREQUENCIES
-----------------------

     0:       0.00 cm**-1
     1:       0.00 cm**-1
     2:       0.00 cm**-1
     3:       0.00 cm**-1
     4:       0.00 cm**-1
     5:       0.00 cm**-1
     6:    2170.10 cm**-1
     7:    4140.20 cm**-1
     8:    4391.40 cm**-1

FINAL SINGLE POINT ENERGY       -74.965995310
TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds 0 msec
                             ****ORCA TERMINATED NORMALLY****
EOF
"""

FAKE_ORCA_DIR_ENV = "ORCA_AUTO_CONTRACT_FAKE_ORCA_DIR"
_SCENARIO_TIMEOUT_SECONDS = 60.0


@dataclasses.dataclass
class Harness:
    """One isolated runtime: config, runs root, admission root and effect log."""

    tmp: Path
    runs: Path
    config: Path
    fake_orca: Path
    log: Path
    channel: RecordingChannel
    capsys: pytest.CaptureFixture[str]
    paths: dict[str | Path, str]
    _normalizer: Normalizer | None = None
    _action_error: Exception | None = None

    @property
    def n(self) -> Normalizer:
        """One placeholder numbering for every golden of the scenario."""
        if self._normalizer is None:
            self._normalizer = Normalizer(self.paths)
        return self._normalizer

    def job(self, name: str, inp_text: str = H2O_INPUT) -> Path:
        job_dir = self.runs / name
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "h2o.inp").write_text(inp_text, encoding="utf-8")
        return job_dir

    def mode(self, mode: str) -> None:
        (self.fake_orca.parent / "mode").write_text(mode, encoding="utf-8")

    def configure(self, **kwargs: Any) -> None:
        write_config_file(
            self.config,
            make_app_cfg(
                self.runs,
                orca_executable=self.fake_orca,
                max_concurrent=1,
                admission_root=self.tmp / "admission",
                **kwargs,
            ),
        )

    def cli(self, *argv: str) -> tuple[int, str, str]:
        self.capsys.readouterr()
        rc = cli_main([*argv, "--config", str(self.config)])
        captured = self.capsys.readouterr()
        return rc, captured.out, captured.err

    def run_worker(self, sleep_fn: Callable[[float], None] | None = None) -> int:
        worker = OrcaQueueWorker(
            load_config(str(self.config)), str(self.config), max_concurrent=1, sleep_fn=sleep_fn
        )
        worker.poll_interval_seconds = 0.05
        rc = worker.run_once(idle_message=None, blocked_message=None)
        _join_notification_senders()
        if self._action_error is not None:
            raise self._action_error
        return rc

    def when_started(self, action: Callable[[], None]) -> Callable[[float], None]:
        """A worker ``sleep_fn`` that runs ``action`` once the fake ORCA is running.

        The worker isolates exceptions of a poll pass, so a failing ``action``
        or the scenario timeout stops the worker and ``run_worker`` re-raises.
        """
        started = self.fake_orca.parent / "started"
        deadline = time.monotonic() + _SCENARIO_TIMEOUT_SECONDS
        done = False

        def sleep(seconds: float) -> None:
            nonlocal done
            if not done and started.exists():
                done = True
                try:
                    action()
                except Exception as exc:
                    self._action_error = exc
                    raise KeyboardInterrupt from exc
            if time.monotonic() > deadline:
                self._action_error = TimeoutError("fake ORCA never started")
                raise KeyboardInterrupt
            time.sleep(seconds)

        return sleep

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = read_json(self.runs / "queue.json")
        return rows


def _join_notification_senders() -> None:
    for thread in threading.enumerate():
        if thread.name.startswith("orca-") and thread.name.endswith("-notification"):
            thread.join(timeout=10)


@pytest.fixture
def harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_fake_orca: Callable[..., Path],
    recording_channel: RecordingChannel,
    capsys: pytest.CaptureFixture[str],
    preserved_signals: None,
) -> Iterator[Harness]:
    for variable in ("FORCE_COLOR", "NO_COLOR", "COLUMNS"):
        monkeypatch.delenv(variable, raising=False)
    log = tmp_path / "effects.jsonl"
    monkeypatch.setenv(effect_log.ENV_VAR, str(log))
    pythonpath = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(p for p in (str(effect_log.SITECUSTOMIZE_DIR), pythonpath) if p),
    )
    monkeypatch.setattr(
        queue_notifications, "notification_channel", lambda *_args: recording_channel
    )
    effect_log.install(monkeypatch.setattr)
    fake_orca = make_fake_orca(FAKE_ORCA, name="bin/fake_orca")
    monkeypatch.setenv(FAKE_ORCA_DIR_ENV, str(fake_orca.parent))
    h = Harness(
        tmp=tmp_path,
        runs=tmp_path / "runs",
        config=tmp_path / "orca_auto.yaml",
        fake_orca=fake_orca,
        log=log,
        channel=recording_channel,
        capsys=capsys,
        paths={tmp_path: "<tmp>"},
    )
    h.configure()
    yield h
    _join_notification_senders()


@pytest.fixture
def shm_scratch_root() -> Iterator[Path]:
    shm = Path("/dev/shm")
    if not shm.is_dir():
        pytest.skip("RAM scratch needs /dev/shm")
    root = Path(tempfile.mkdtemp(prefix="orca-auto-contract-", dir=shm))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
