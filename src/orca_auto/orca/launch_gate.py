from __future__ import annotations

import _signal  # type: ignore[import-not-found]
import os
import stat
import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 4:
        return 125
    executable_display = arguments[1]
    input_name = arguments[3]
    try:
        launch_gate_fd = int(arguments[0])
        executable_fd = int(arguments[2])
        gate_details = os.fstat(launch_gate_fd)
        executable_details = os.fstat(executable_fd)
    except (OSError, ValueError):
        return 125
    if (
        launch_gate_fd < 3
        or executable_fd < 3
        or launch_gate_fd == executable_fd
        or not stat.S_ISREG(gate_details.st_mode)
        or not stat.S_ISREG(executable_details.st_mode)
    ):
        return 125
    if not input_name or os.path.basename(input_name) != input_name or input_name in {".", ".."}:
        return 125
    try:
        release = sys.stdin.buffer.read(1)
    except OSError:
        return 125
    if release != b"1":
        return 125
    try:
        os.close(launch_gate_fd)
        shebang = os.pread(executable_fd, 2, 0) == b"#!"
    except OSError:
        return 125
    environment = dict(os.environ)
    if not shebang:
        os.set_inheritable(executable_fd, False)
        os.execve(executable_fd, [executable_display, input_name], environment)
        return 125

    # Linux cannot fexecve a shebang script through a CLOEXEC descriptor: the
    # interpreter must reopen the script after exec.  Keep the pinned descriptor
    # only in this short-lived group leader and let a forked child open it through
    # the leader's procfs path.  The child closes its inherited copies first, so
    # neither the wrapper nor its descendants retain the launch or executable FDs.
    parent_pid = os.getpid()
    script_path = f"/proc/{parent_pid}/fd/{executable_fd}"
    try:
        child_pid = os.fork()
    except OSError:
        return 125
    if child_pid == 0:
        try:
            os.close(executable_fd)
            os.execve(script_path, [executable_display, input_name], environment)
        except OSError:
            os._exit(126)
    try:
        while True:
            try:
                _, wait_status = os.waitpid(child_pid, 0)
                break
            except InterruptedError:
                continue
    finally:
        os.close(executable_fd)
    exit_code = os.waitstatus_to_exitcode(wait_status)
    if exit_code >= 0:
        return exit_code
    signal_number = -exit_code
    try:
        _signal.signal(signal_number, _signal.SIG_DFL)
    except (OSError, ValueError):
        pass
    os.kill(os.getpid(), signal_number)
    return 128 + signal_number


if __name__ == "__main__":
    raise SystemExit(main())
