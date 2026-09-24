from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_docs_parity.py"

EN = """# Title

Intro with a [link](INSTALLATION.md) and an [anchor](#setup).

## Setup

| Key | Value |
|-----|-------|
| a   | 1     |
| b   | 2     |

```bash
# Create the config
orca_auto init --config ~/orca_auto.yaml
```

### Details

Text.
"""

KO = """# 제목

[링크](INSTALLATION.ko.md)와 [앵커](#설정)가 있는 소개.

## 설정

| 키  | 값    |
|-----|-------|
| a   | 1     |
| b   | 2     |

```bash
# 설정 파일 생성
orca_auto init --config ~/orca_auto.yaml
```

### 상세

본문.
"""


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docs_parity", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


parity = _load_module()


def _write_pair(directory: Path, name: str, english: str, korean: str) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    en_path = directory / f"{name}.md"
    ko_path = directory / f"{name}.ko.md"
    en_path.write_text(english, encoding="utf-8")
    ko_path.write_text(korean, encoding="utf-8")
    return en_path, ko_path


def test_aligned_pair_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_pair(tmp_path / "docs", "GUIDE", EN, KO)

    assert parity.main([str(tmp_path)]) == 0
    assert "1 pairs aligned" in capsys.readouterr().out


def test_heading_order_mismatch_fails_naming_the_pair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pair(tmp_path / "docs", "GUIDE", EN, KO.replace("### 상세", "## 상세"))

    assert parity.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "GUIDE.md <-> " in out and "GUIDE.ko.md" in out
    assert "GUIDE.md:17 heading level 3" in out
    assert "GUIDE.ko.md:17 heading level 2" in out


def test_table_row_mismatch_fails_naming_the_pair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pair(tmp_path / "docs", "GUIDE", EN, KO.replace("| b   | 2     |\n", ""))

    assert parity.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "GUIDE.ko.md" in out
    assert "GUIDE.md:7 table (3 rows) vs GUIDE.ko.md:7 table (2 rows)" in out


def test_code_block_command_mismatch_fails_naming_the_pair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pair(
        tmp_path / "docs",
        "GUIDE",
        EN,
        KO.replace("--config ~/orca_auto.yaml", "--config ~/설정.yaml"),
    )

    assert parity.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "GUIDE.ko.md" in out
    assert "code block [bash] (2 lines)" in out
    assert "code line 2 differs" in out


def test_code_block_language_and_length_are_compared(tmp_path: Path) -> None:
    _, ko_path = _write_pair(tmp_path / "docs", "GUIDE", EN, KO.replace("```bash", "```text"))
    assert parity.main([str(tmp_path)]) == 1

    ko_path.write_text(KO.replace("# 설정 파일 생성\n", ""), encoding="utf-8")
    assert parity.main([str(tmp_path)]) == 1


def test_missing_trailing_element_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pair(tmp_path / "docs", "GUIDE", EN, KO.replace("### 상세\n", ""))

    assert parity.main([str(tmp_path)]) == 1
    assert "has no counterpart (5 vs 4 elements)" in capsys.readouterr().out


def test_relative_links_are_compared_with_ko_suffix_folded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pair(tmp_path / "docs", "GUIDE", EN, KO.replace("INSTALLATION.ko.md", "RUNTIME.md"))

    assert parity.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "links only in GUIDE.md: INSTALLATION.md" in out
    assert "links only in GUIDE.ko.md: RUNTIME.md" in out


def test_in_page_anchor_count_is_compared_but_slug_is_not(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pair(tmp_path / "docs", "GUIDE", EN, KO.replace("[앵커](#설정)", "앵커"))

    assert parity.main([str(tmp_path)]) == 1
    assert "in-page anchor links: GUIDE.md has 1, GUIDE.ko.md has 0" in capsys.readouterr().out


def test_structure_inside_code_fences_is_not_counted(tmp_path: Path) -> None:
    fenced = "# T\n\n```text\n# not a heading\n| not | a table |\n[x](y.md)\n```\n"
    structure = parity.parse_markdown(_write_pair(tmp_path, "F", fenced, fenced)[0])

    assert [element.kind for element in structure.elements] == ["heading", "code block"]
    assert structure.relative_links == set()


def test_setext_heading_and_thematic_break_are_distinguished(tmp_path: Path) -> None:
    text = "Title\n=====\n\nSection\n-------\n\nprose\n\n---\n\n- item\n---\n"
    structure = parity.parse_markdown(_write_pair(tmp_path, "S", text, text)[0])

    assert [element.key for element in structure.elements] == [(1,), (2,)]


def test_pairs_lists_discovered_pairs_by_convention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pair(tmp_path / "docs", "B", EN, KO)
    _write_pair(tmp_path, "A", EN, KO)
    (tmp_path / "docs" / "ORPHAN.ko.md").write_text("# x\n", encoding="utf-8")
    (tmp_path / "docs" / "ENGLISH_ONLY.md").write_text("# x\n", encoding="utf-8")
    _write_pair(tmp_path / ".venv" / "site", "IGNORED", EN, "# other\n")

    assert parity.main(["--pairs", str(tmp_path)]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert len(listed) == 2
    assert listed[0].endswith("A.md <-> " + str(tmp_path / "A.ko.md"))
    assert "docs" in listed[1] and "B.md <-> " in listed[1]


def test_explicit_file_argument_resolves_its_sibling(tmp_path: Path) -> None:
    en_path, ko_path = _write_pair(tmp_path, "A", EN, KO)

    assert parity.main([str(ko_path)]) == 0
    assert parity.main([str(en_path), str(ko_path)]) == 0
    ko_path.unlink()
    assert parity.main([str(en_path)]) == 2


def test_no_pairs_is_an_error(tmp_path: Path) -> None:
    assert parity.main([str(tmp_path)]) == 2


def test_verbose_lists_the_structure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_pair(tmp_path, "A", EN, KO)

    assert parity.main(["--verbose", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "heading level 1 'Title'" in out
    assert "table (3 rows)" in out
    assert "code block [bash] (2 lines)" in out
    assert "links: INSTALLATION.md" in out
    assert "  OK" in out


def test_cli_exit_status_and_report(tmp_path: Path) -> None:
    _write_pair(tmp_path, "A", EN, KO.replace("## 설정", "# 설정"))

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "[docs-parity] MISMATCH" in result.stdout
    assert "A.md:5 heading level 2 'Setup' vs A.ko.md:5 heading level 1" in result.stdout
    assert "1 of 1 pairs out of sync" in result.stdout
