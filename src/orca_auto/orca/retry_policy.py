from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .input_blocks import file_route_lines

_ROUTE_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?")
_RETRY_STEM_RE = re.compile(r"\.retry\d+$", re.IGNORECASE)
_TS_TOKENS = frozenset({"OPTTS", "NEB-TS"})
_FREQ_TOKENS = frozenset({"FREQ", "NUMFREQ", "ANFREQ"})


@dataclass(frozen=True)
class RetryPolicy:
    name: str
    max_retries: int


_RETRY_POLICIES: dict[str, RetryPolicy] = {
    # ScanTS owns the only automatic artifact restart path.  It uses scan-point
    # artifacts to build continuation/reverse-scan inputs, not a generic .xyz/.gbw
    # rerun.
    "scants": RetryPolicy(name="scants", max_retries=3),
    # Standalone OPTTS/NEB-TS retries are intentionally disabled. Failed TS-search
    # artifacts are not reused, and Hessian hardening is left as an explicit user
    # choice rather than an automatic retry policy.
    "standalone_ts": RetryPolicy(name="standalone_ts", max_retries=0),
    "opt_freq": RetryPolicy(name="opt_freq", max_retries=0),
    "opt": RetryPolicy(name="opt", max_retries=0),
    "freq": RetryPolicy(name="freq", max_retries=0),
    "single_point": RetryPolicy(name="single_point", max_retries=0),
}


def retry_policy_for_input(inp_path: Path) -> RetryPolicy:
    tokens = _route_tokens(inp_path)
    if "SCANTS" in tokens:
        return _RETRY_POLICIES["scants"]
    if tokens & _TS_TOKENS:
        return _RETRY_POLICIES["standalone_ts"]
    has_opt = "OPT" in tokens
    has_freq = bool(tokens & _FREQ_TOKENS)
    if has_opt and has_freq:
        return _RETRY_POLICIES["opt_freq"]
    if has_opt:
        return _RETRY_POLICIES["opt"]
    if has_freq:
        return _RETRY_POLICIES["freq"]
    return _RETRY_POLICIES["single_point"]


def effective_max_retries(inp_path: Path, *, configured_max_retries: int) -> int:
    configured = max(0, int(configured_max_retries))
    if configured == 0:
        return 0
    return retry_policy_for_input(inp_path).max_retries


def retry_input_path(selected_inp: Path, retry_number: int) -> Path:
    base_stem = _RETRY_STEM_RE.sub("", selected_inp.stem)
    if not base_stem:
        base_stem = selected_inp.stem
    return selected_inp.with_name(f"{base_stem}.retry{retry_number:02d}.inp")


def resume_checkpoint_input_path(current_inp: Path) -> Path:
    return current_inp.with_name(f"{current_inp.stem}.resume.inp")


def _route_tokens(inp_path: Path) -> set[str]:
    route = "\n".join(line[1:] for line in file_route_lines(inp_path))
    return {match.group(0).upper() for match in _ROUTE_WORD_RE.finditer(route)}


__all__ = [
    "RetryPolicy",
    "effective_max_retries",
    "resume_checkpoint_input_path",
    "retry_input_path",
    "retry_policy_for_input",
]
