from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RetryRecipeName = Literal["scants_retry", "no_route_rewrite"]

_ROUTE_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?")
_TS_TOKENS = {"OPTTS", "NEB-TS"}
_FREQ_TOKENS = {"FREQ", "NUMFREQ", "ANFREQ"}


@dataclass(frozen=True)
class RetryPolicy:
    name: str
    max_retries: int
    recipes: tuple[RetryRecipeName, ...]

    def recipe_for_retry(self, retry_number: int) -> RetryRecipeName:
        if not self.recipes:
            return "no_route_rewrite"
        index = max(1, int(retry_number)) - 1
        if index >= len(self.recipes):
            return self.recipes[-1]
        return self.recipes[index]


_RETRY_POLICIES: dict[str, RetryPolicy] = {
    # ScanTS owns the only automatic artifact restart path.  It uses scan-point
    # artifacts to build continuation/reverse-scan inputs, not a generic .xyz/.gbw
    # rerun.
    "scants": RetryPolicy(
        name="scants",
        max_retries=2,
        recipes=("scants_retry", "scants_retry"),
    ),
    # Standalone OPTTS/NEB-TS retries are intentionally disabled. Failed TS-search
    # artifacts are not reused, and Hessian hardening is left as an explicit user
    # choice rather than an automatic retry policy.
    "standalone_ts": RetryPolicy(
        name="standalone_ts",
        max_retries=0,
        recipes=(),
    ),
    "opt_freq": RetryPolicy(name="opt_freq", max_retries=0, recipes=()),
    "opt": RetryPolicy(name="opt", max_retries=0, recipes=()),
    "freq": RetryPolicy(name="freq", max_retries=0, recipes=()),
    "single_point": RetryPolicy(
        name="single_point",
        max_retries=0,
        recipes=(),
    ),
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


def retry_recipe_name_for_input(inp_path: Path, retry_number: int) -> RetryRecipeName:
    return retry_policy_for_input(inp_path).recipe_for_retry(retry_number)


def _route_tokens(inp_path: Path) -> set[str]:
    route = _route_text(inp_path)
    return {match.group(0).upper() for match in _ROUTE_WORD_RE.finditer(route)}


def _route_text(inp_path: Path) -> str:
    routes: list[str] = []
    try:
        with inp_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("!"):
                    routes.append(stripped[1:])
    except OSError:
        return ""
    return "\n".join(routes)


__all__ = [
    "RetryPolicy",
    "RetryRecipeName",
    "effective_max_retries",
    "retry_policy_for_input",
    "retry_recipe_name_for_input",
]
