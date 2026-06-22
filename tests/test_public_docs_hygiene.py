from __future__ import annotations

from pathlib import Path

PUBLIC_DOC_ROOTS = ("README.md", "docs", "systemd/README.md")
LOCAL_PATH_PATTERNS = (
    "/home/daehyupsohn",
    "/mnt/c/Users/",
    "C:/Users/",
    "C:\\Users\\",
)


def _public_doc_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for relative in PUBLIC_DOC_ROOTS:
        path = repo_root / relative
        if path.is_file():
            files.append(path)
            continue
        files.extend(sorted(path.rglob("*.md")))
    return files


def test_public_docs_do_not_link_to_local_workstation_paths() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []

    for path in _public_doc_files(repo_root):
        text = path.read_text(encoding="utf-8")
        for pattern in LOCAL_PATH_PATTERNS:
            if pattern in text:
                offenders.append(f"{path.relative_to(repo_root)} contains {pattern!r}")

    assert offenders == []
