from __future__ import annotations

import os
import stat
import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3:
        return 125
    executable_display = arguments[0]
    input_name = arguments[2]
    try:
        executable_fd = int(arguments[1])
        details = os.fstat(executable_fd)
    except (OSError, ValueError):
        return 125
    if executable_fd < 3 or not stat.S_ISREG(details.st_mode):
        return 125
    if not input_name or os.path.basename(input_name) != input_name or input_name in {".", ".."}:
        return 125
    try:
        release = sys.stdin.buffer.read(1)
    except OSError:
        return 125
    if release != b"1":
        return 125
    os.execve(executable_fd, [executable_display, input_name], dict(os.environ))
    return 125


if __name__ == "__main__":
    raise SystemExit(main())
