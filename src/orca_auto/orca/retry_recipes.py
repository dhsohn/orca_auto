from __future__ import annotations

from typing import Callable, List

from .input_blocks import ensure_route_keywords, set_block_key_value
from .resource_directives import increase_maxcore
from .retry_policy import RetryRecipeName

RetryRecipe = Callable[[List[str]], List[str]]


def apply_retry_recipe(lines: List[str], recipe_name: RetryRecipeName | int) -> List[str]:
    if recipe_name == "scants_retry" or recipe_name == "no_route_rewrite":
        return []
    recipe = RETRY_RECIPES.get(recipe_name)
    if recipe is None:
        return ["no_recipe_applied"]
    return recipe(lines)


def retry_step_1(lines: List[str]) -> List[str]:
    actions: List[str] = []
    if ensure_route_keywords(lines, ["TightSCF", "SlowConv"]):
        actions.append("route_add_tightscf_slowconv")
    if set_block_key_value(lines, "scf", "MaxIter", "300"):
        actions.append("scf_maxiter_300")
    return actions


def retry_step_2(lines: List[str]) -> List[str]:
    changed = set_geom_retry_keys(lines, max_iter="300")
    return ["geom_hessian_and_maxiter"] if changed else []


def retry_step_3(lines: List[str]) -> List[str]:
    actions: List[str] = []
    if increase_maxcore(lines):
        actions.append("maxcore_increased")
    if ensure_route_keywords(lines, ["LooseOpt"]):
        actions.append("route_add_looseopt")
    return actions


def retry_step_4(lines: List[str]) -> List[str]:
    actions: List[str] = []
    if set_geom_retry_keys(lines, max_iter="500"):
        actions.append("geom_hessian_and_maxiter_500")
    if increase_maxcore(lines):
        actions.append("maxcore_increased")
    if ensure_route_keywords(lines, ["TightSCF", "SlowConv"]):
        actions.append("route_add_tightscf_slowconv")
    return actions


def set_geom_retry_keys(lines: List[str], *, max_iter: str) -> bool:
    changed = False
    changed |= set_block_key_value(lines, "geom", "Calc_Hess", "true")
    changed |= set_block_key_value(lines, "geom", "Recalc_Hess", "5")
    changed |= set_block_key_value(lines, "geom", "MaxIter", max_iter)
    return changed


RETRY_RECIPES: dict[int, RetryRecipe] = {
    1: retry_step_1,
    2: retry_step_2,
    3: retry_step_3,
    4: retry_step_4,
}
