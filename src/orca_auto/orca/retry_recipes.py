from __future__ import annotations

from .retry_policy import RetryRecipeName


def apply_retry_recipe(lines: list[str], recipe_name: RetryRecipeName) -> list[str]:
    # Both retry recipe names are route no-ops: "scants_retry" rebuilds inputs
    # from scan-point artifacts (handled in the ScanTS path) and
    # "no_route_rewrite" reuses the input unchanged. Neither performs a
    # route-level rewrite here.
    del lines, recipe_name
    return []
