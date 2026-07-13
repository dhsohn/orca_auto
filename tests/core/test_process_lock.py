from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orca_auto.core.utils.process_lock import (
    _claim_stale_lock,
    _write_lock_payload,
    acquire_file_lock,
    current_process_start_ticks,
    is_process_alive,
    parse_lock_info,
    process_start_ticks,
)


def _active_lock_error(lock_pid: int, _lock_info: dict, lock_path: Path) -> RuntimeError:
    return RuntimeError(f"active:{lock_pid}:{lock_path.name}")


def _unreadable_lock_error(lock_path: Path) -> RuntimeError:
    return RuntimeError(f"unreadable:{lock_path.name}")


def _timeout_error(lock_path: Path, timeout_seconds: int) -> RuntimeError:
    return RuntimeError(f"timeout:{lock_path.name}:{timeout_seconds}")


class TestParseLockInfo(unittest.TestCase):
    def test_parse_lock_info_returns_empty_shape_for_read_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            with patch("orca_auto.core.utils.process_lock.Path.read_text", side_effect=OSError):
                info = parse_lock_info(lock_path)

        self.assertEqual(
            info,
            {"pid": None, "started_at": None, "process_start_ticks": None, "boot_id": None},
        )

    def test_parse_lock_info_returns_empty_shape_for_empty_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text("", encoding="utf-8")

            info = parse_lock_info(lock_path)

        self.assertEqual(
            info,
            {"pid": None, "started_at": None, "process_start_ticks": None, "boot_id": None},
        )

    def test_parse_lock_info_accepts_numeric_strings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": "4321",
                        "started_at": "2026-03-22T00:00:00+00:00",
                        "process_start_ticks": "987",
                        "boot_id": "boot-a",
                    }
                ),
                encoding="utf-8",
            )

            info = parse_lock_info(lock_path)

        self.assertEqual(info["pid"], 4321)
        self.assertEqual(info["started_at"], "2026-03-22T00:00:00+00:00")
        self.assertEqual(info["process_start_ticks"], 987)
        self.assertEqual(info["boot_id"], "boot-a")

    def test_parse_lock_info_returns_empty_shape_for_malformed_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text("not-json", encoding="utf-8")

            info = parse_lock_info(lock_path)

        self.assertEqual(
            info,
            {"pid": None, "started_at": None, "process_start_ticks": None, "boot_id": None},
        )

    def test_parse_lock_info_ignores_invalid_pid_and_tick_strings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": "not-a-number",
                        "process_start_ticks": "not-a-number",
                    }
                ),
                encoding="utf-8",
            )

            info = parse_lock_info(lock_path)

        self.assertIsNone(info["pid"])
        self.assertIsNone(info["process_start_ticks"])


class TestWriteLockPayload(unittest.TestCase):
    def test_write_lock_payload_publishes_full_payload_atomically(self) -> None:
        # Regression: the lock must never be observable empty by a racing reader.
        # It is published via a hard link, so at the instant lock_path can appear
        # the payload is already fully written to the linked source.
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            observed: list[str] = []
            real_link = os.link

            def spy_link(src: str, dst: str, *, follow_symlinks: bool = True) -> None:
                observed.append(Path(src).read_text(encoding="utf-8"))
                real_link(src, dst, follow_symlinks=follow_symlinks)

            with patch("orca_auto.core.utils.process_lock.os.link", side_effect=spy_link):
                self.assertTrue(_write_lock_payload(lock_path, json.dumps({"pid": 111})))

            self.assertTrue(observed, "lock was not published via a hard link")
            self.assertTrue(observed[0].strip(), "lock was published while empty")
            self.assertEqual(parse_lock_info(lock_path)["pid"], 111)

    def test_write_lock_payload_loser_does_not_clobber_or_litter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            self.assertTrue(_write_lock_payload(lock_path, json.dumps({"pid": 111})))
            # A contender that loses the create race returns False without clobbering.
            self.assertFalse(_write_lock_payload(lock_path, json.dumps({"pid": 222})))
            self.assertEqual(parse_lock_info(lock_path)["pid"], 111)
            # No temp file is left behind by either the winner or the loser.
            leftovers = sorted(p.name for p in Path(td).iterdir() if p.name != "run.lock")
            self.assertEqual(leftovers, [])


class TestIsProcessAlive(unittest.TestCase):
    def test_is_process_alive_returns_false_for_non_positive_pid(self) -> None:
        self.assertFalse(is_process_alive(0))

    def test_is_process_alive_returns_true_on_permission_error(self) -> None:
        with patch("orca_auto.core.utils.process_lock.os.kill", side_effect=PermissionError):
            self.assertTrue(is_process_alive(1234))

    def test_is_process_alive_returns_true_on_unknown_oserror(self) -> None:
        with patch("orca_auto.core.utils.process_lock.os.kill", side_effect=OSError):
            self.assertTrue(is_process_alive(1234))


class TestProcessStartTicks(unittest.TestCase):
    def test_process_start_ticks_returns_none_for_non_positive_pid(self) -> None:
        self.assertIsNone(process_start_ticks(0))

    def test_process_start_ticks_parses_field_22_from_proc_stat(self) -> None:
        raw_stat = "1234 (orca worker) " + " ".join(["S"] + ["0"] * 18 + ["999"] + ["0"] * 2)
        with patch("orca_auto.core.utils.process_lock.Path.read_text", return_value=raw_stat):
            ticks = process_start_ticks(1234)

        self.assertEqual(ticks, 999)

    def test_process_start_ticks_returns_none_for_short_proc_stat(self) -> None:
        with patch(
            "orca_auto.core.utils.process_lock.Path.read_text", return_value="1234 (orca) S 0 0"
        ):
            ticks = process_start_ticks(1234)

        self.assertIsNone(ticks)

    def test_process_start_ticks_returns_none_for_empty_stat(self) -> None:
        with patch("orca_auto.core.utils.process_lock.Path.read_text", return_value=""):
            ticks = process_start_ticks(1234)

        self.assertIsNone(ticks)

    def test_process_start_ticks_returns_none_for_invalid_starttime_field(self) -> None:
        raw_stat = "1234 (orca worker) " + " ".join(["S"] + ["0"] * 18 + ["not-an-int"] + ["0"] * 2)
        with patch("orca_auto.core.utils.process_lock.Path.read_text", return_value=raw_stat):
            ticks = process_start_ticks(1234)

        self.assertIsNone(ticks)

    def test_current_process_start_ticks_reads_current_pid(self) -> None:
        with patch(
            "orca_auto.core.utils.process_lock.process_start_ticks", return_value=555
        ) as mock_ticks:
            ticks = current_process_start_ticks()

        self.assertEqual(ticks, 555)
        mock_ticks.assert_called_once_with(os.getpid())


class TestAcquireFileLock(unittest.TestCase):
    def test_acquire_file_lock_writes_and_removes_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            logger = logging.getLogger("test.process_lock.acquire")

            with acquire_file_lock(
                lock_path=lock_path,
                lock_payload_obj={"pid": 123},
                parse_lock_info_fn=parse_lock_info,
                is_process_alive_fn=lambda _pid: False,
                process_start_ticks_fn=lambda _pid: None,
                logger=logger,
                acquired_log_template="acquired %s",
                released_log_template="released %s",
                stale_pid_reuse_log_template="reuse %d %d %s %s",
                stale_lock_log_template="stale %d %s",
                active_lock_error_builder=_active_lock_error,
                unreadable_lock_error_builder=_unreadable_lock_error,
                timeout_error_builder=_timeout_error,
            ):
                self.assertTrue(lock_path.exists())
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["pid"], 123)

            self.assertFalse(lock_path.exists())

    def test_acquire_file_lock_removes_stale_dead_pid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text(json.dumps({"pid": 4321}), encoding="utf-8")

            with acquire_file_lock(
                lock_path=lock_path,
                lock_payload_obj={"pid": 999},
                parse_lock_info_fn=parse_lock_info,
                is_process_alive_fn=lambda _pid: False,
                process_start_ticks_fn=lambda _pid: None,
                logger=logging.getLogger("test.process_lock.dead"),
                acquired_log_template="acquired %s",
                released_log_template="released %s",
                stale_pid_reuse_log_template="reuse %d %d %s %s",
                stale_lock_log_template="stale %d %s",
                active_lock_error_builder=_active_lock_error,
                unreadable_lock_error_builder=_unreadable_lock_error,
                timeout_error_builder=_timeout_error,
            ):
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["pid"], 999)

    def test_acquire_file_lock_raises_for_active_owner_without_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text(json.dumps({"pid": 4321}), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, r"active:4321:run\.lock"):
                with acquire_file_lock(
                    lock_path=lock_path,
                    lock_payload_obj={"pid": 999},
                    parse_lock_info_fn=parse_lock_info,
                    is_process_alive_fn=lambda _pid: True,
                    process_start_ticks_fn=lambda _pid: None,
                    logger=logging.getLogger("test.process_lock.active"),
                    acquired_log_template="acquired %s",
                    released_log_template="released %s",
                    stale_pid_reuse_log_template="reuse %d %d %s %s",
                    stale_lock_log_template="stale %d %s",
                    active_lock_error_builder=_active_lock_error,
                    unreadable_lock_error_builder=_unreadable_lock_error,
                    timeout_error_builder=_timeout_error,
                ):
                    pass

    def test_acquire_file_lock_times_out_when_owner_stays_active(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text(json.dumps({"pid": 4321}), encoding="utf-8")

            with (
                patch(
                    "orca_auto.core.utils.process_lock.time.monotonic",
                    side_effect=[0.0, 0.25, 1.25],
                ),
                patch("orca_auto.core.utils.process_lock.time.sleep", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, r"timeout:run\.lock:1"):
                    with acquire_file_lock(
                        lock_path=lock_path,
                        lock_payload_obj={"pid": 999},
                        parse_lock_info_fn=parse_lock_info,
                        is_process_alive_fn=lambda _pid: True,
                        process_start_ticks_fn=lambda _pid: None,
                        logger=logging.getLogger("test.process_lock.timeout"),
                        acquired_log_template="acquired %s",
                        released_log_template="released %s",
                        stale_pid_reuse_log_template="reuse %d %d %s %s",
                        stale_lock_log_template="stale %d %s",
                        timeout_seconds=1,
                        active_lock_error_builder=_active_lock_error,
                        unreadable_lock_error_builder=_unreadable_lock_error,
                        timeout_error_builder=_timeout_error,
                    ):
                        pass

    def test_acquire_file_lock_treats_pid_reuse_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text(
                json.dumps({"pid": 4321, "process_start_ticks": 111}),
                encoding="utf-8",
            )

            with acquire_file_lock(
                lock_path=lock_path,
                lock_payload_obj={"pid": 999},
                parse_lock_info_fn=parse_lock_info,
                is_process_alive_fn=lambda _pid: True,
                process_start_ticks_fn=lambda _pid: 222,
                logger=logging.getLogger("test.process_lock.pid_reuse"),
                acquired_log_template="acquired %s",
                released_log_template="released %s",
                stale_pid_reuse_log_template="reuse %d %d %s %s",
                stale_lock_log_template="stale %d %s",
                active_lock_error_builder=_active_lock_error,
                unreadable_lock_error_builder=_unreadable_lock_error,
                timeout_error_builder=_timeout_error,
            ):
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["pid"], 999)

    def test_acquire_file_lock_treats_cross_boot_owner_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 4321,
                        "process_start_ticks": 111,
                        "boot_id": "boot-a",
                    }
                ),
                encoding="utf-8",
            )
            alive_calls: list[int] = []

            def record_alive_probe(pid: int) -> bool:
                alive_calls.append(pid)
                return True

            with acquire_file_lock(
                lock_path=lock_path,
                lock_payload_obj={"pid": 999, "boot_id": "boot-b"},
                parse_lock_info_fn=parse_lock_info,
                is_process_alive_fn=record_alive_probe,
                process_start_ticks_fn=lambda _pid: 111,
                boot_id_fn=lambda: "boot-b",
                logger=logging.getLogger("test.process_lock.cross_boot"),
                acquired_log_template="acquired %s",
                released_log_template="released %s",
                stale_pid_reuse_log_template="reuse %d %d %s %s",
                stale_lock_log_template="stale %d %s",
                active_lock_error_builder=_active_lock_error,
                unreadable_lock_error_builder=_unreadable_lock_error,
                timeout_error_builder=_timeout_error,
            ):
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["pid"], 999)
                self.assertEqual(payload["boot_id"], "boot-b")

            self.assertEqual(alive_calls, [])

    def test_acquire_file_lock_treats_unreadable_owner_as_stale_with_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text("not-json", encoding="utf-8")

            with acquire_file_lock(
                lock_path=lock_path,
                lock_payload_obj={"pid": 777},
                parse_lock_info_fn=parse_lock_info,
                is_process_alive_fn=lambda _pid: True,
                process_start_ticks_fn=lambda _pid: None,
                logger=logging.getLogger("test.process_lock.unreadable"),
                acquired_log_template="acquired %s",
                released_log_template="released %s",
                stale_pid_reuse_log_template="reuse %d %d %s %s",
                stale_lock_log_template="stale %d %s",
                timeout_seconds=1,
                active_lock_error_builder=_active_lock_error,
                unreadable_lock_error_builder=_unreadable_lock_error,
                timeout_error_builder=_timeout_error,
            ):
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["pid"], 777)

    def test_acquire_file_lock_uses_default_timeout_error_when_builder_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text(json.dumps({"pid": 4321}), encoding="utf-8")

            with (
                patch(
                    "orca_auto.core.utils.process_lock.time.monotonic",
                    side_effect=[0.0, 0.25, 1.25],
                ),
                patch("orca_auto.core.utils.process_lock.time.sleep", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, r"timed out after 1s"):
                    with acquire_file_lock(
                        lock_path=lock_path,
                        lock_payload_obj={"pid": 999},
                        parse_lock_info_fn=parse_lock_info,
                        is_process_alive_fn=lambda _pid: True,
                        process_start_ticks_fn=lambda _pid: None,
                        logger=logging.getLogger("test.process_lock.default_timeout"),
                        acquired_log_template="acquired %s",
                        released_log_template="released %s",
                        stale_pid_reuse_log_template="reuse %d %d %s %s",
                        stale_lock_log_template="stale %d %s",
                        timeout_seconds=1,
                    ):
                        pass


class TestStaleLockClaimAndOwnedRelease(unittest.TestCase):
    def test_stale_claim_leaves_a_fresh_live_lock_untouched(self) -> None:
        # The unlink-then-recreate race: a contender judged the lock stale, but
        # by the time it acts another process has already replaced it with a
        # fresh, live lock. Recovery re-verifies under the flock and must not
        # move or remove the live lock at any point.
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text(json.dumps({"pid": 4321}), encoding="utf-8")

            claimed = _claim_stale_lock(
                lock_path,
                parse_lock_info_fn=parse_lock_info,
                is_process_alive_fn=lambda _pid: True,
                process_start_ticks_fn=lambda _pid: None,
                logger=logging.getLogger("test.process_lock.claim"),
                stale_pid_reuse_log_template="reuse %d %d %s %s",
            )

            self.assertFalse(claimed)
            self.assertTrue(lock_path.exists())
            self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8"))["pid"], 4321)

    def test_stale_claim_discards_a_dead_owner_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            lock_path.write_text(json.dumps({"pid": 4321}), encoding="utf-8")

            claimed = _claim_stale_lock(
                lock_path,
                parse_lock_info_fn=parse_lock_info,
                is_process_alive_fn=lambda _pid: False,
                process_start_ticks_fn=lambda _pid: None,
                logger=logging.getLogger("test.process_lock.claim"),
                stale_pid_reuse_log_template="reuse %d %d %s %s",
            )

            self.assertTrue(claimed)
            self.assertFalse(lock_path.exists())

    def test_release_leaves_a_foreign_lock_alone(self) -> None:
        # If our lock was stolen and re-created by another owner, exiting the
        # context must not delete the new owner's lock.
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"

            with acquire_file_lock(
                lock_path=lock_path,
                lock_payload_obj={"pid": 123},
                parse_lock_info_fn=parse_lock_info,
                is_process_alive_fn=lambda _pid: False,
                process_start_ticks_fn=lambda _pid: None,
                logger=logging.getLogger("test.process_lock.release"),
                acquired_log_template="acquired %s",
                released_log_template="released %s",
                stale_pid_reuse_log_template="reuse %d %d %s %s",
                stale_lock_log_template="stale %d %s",
                active_lock_error_builder=_active_lock_error,
                unreadable_lock_error_builder=_unreadable_lock_error,
                timeout_error_builder=_timeout_error,
            ):
                lock_path.write_text(json.dumps({"pid": 777}), encoding="utf-8")

            self.assertTrue(lock_path.exists())
            self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8"))["pid"], 777)
