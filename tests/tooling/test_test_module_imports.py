"""No test module is a library: shared helpers live in ``tests/`` root modules.

``docs/DEVELOPMENT.md`` (Test Layout) keeps helpers shared across directories
in ``conftest.py`` and ``tests/*_helpers.py``. A test that imports another
``test_*`` module couples its collection and setup to that module's private
helpers, so splitting or changing one test file breaks unrelated suites.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    modules: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append((node.lineno, node.module))
            # ``from tests.orca import test_evidence`` imports a test module too.
            modules.extend((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.extend((node.lineno, alias.name) for alias in node.names)
    return modules


def _is_test_module(module: str) -> bool:
    parts = module.split(".")
    return parts[0] == "tests" and parts[-1].startswith("test_")


def test_no_test_module_imports_another_test_module() -> None:
    offenders = [
        f"{path.relative_to(_TESTS.parent)}:{lineno}: {module}"
        for path in sorted(_TESTS.rglob("*.py"))
        if "__pycache__" not in path.parts
        for lineno, module in _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        if _is_test_module(module)
    ]

    assert offenders == [], "move shared helpers to a tests/*_helpers.py module:\n" + "\n".join(
        offenders
    )


def test_the_guard_recognizes_each_import_form() -> None:
    tree = ast.parse(
        "from tests.orca.test_evidence import helper\n"
        "from tests.orca import test_evidence\n"
        "import tests.orca.test_evidence\n"
        "from tests.orca_output_helpers import helper\n"
        "from orca_auto.orca import test_like_name\n"
    )

    flagged = [module for _line, module in _imported_modules(tree) if _is_test_module(module)]

    assert flagged == [
        "tests.orca.test_evidence",
        "tests.orca.test_evidence",
        "tests.orca.test_evidence",
    ]
