#!/usr/bin/env python3
"""Docs-drift guard: fail if a source module has no row in docs/SYSTEM_MAP.md.

Part of the Definition of Done (CLAUDE.md): every source file must be documented in the
System Map. Run locally or in CI:  python scripts/check_docs.py

KISS: a plaintext substring check against the map is enough — no AST, no parsing.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_MAP = REPO_ROOT / "docs" / "SYSTEM_MAP.md"

# Directories whose contents are not individually mapped (documented as a directory row instead).
SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "tests", "scripts",
    "templates", "static", "docs", "data", "work",
}
# Individual files that don't need a row.
SKIP_FILES = {"__init__.py", "conftest.py", "check_docs.py"}


def source_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if rel.parts[0] in SKIP_DIRS:
            continue
        if path.name in SKIP_FILES:
            continue
        files.append(rel)
    return sorted(files)


def main() -> int:
    if not SYSTEM_MAP.exists():
        print(f"ERROR: {SYSTEM_MAP} not found", file=sys.stderr)
        return 2
    map_text = SYSTEM_MAP.read_text(encoding="utf-8")

    missing = [str(rel) for rel in source_files() if f"`{rel.as_posix()}`" not in map_text]
    if missing:
        print("Docs drift — these source files have no row in docs/SYSTEM_MAP.md:")
        for m in missing:
            print(f"  - {m}")
        print("\nAdd a row for each (see the Definition of Done in CLAUDE.md).")
        return 1

    print("OK: every source module is present in docs/SYSTEM_MAP.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
