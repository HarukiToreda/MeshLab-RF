from __future__ import annotations

import re
import sys
from pathlib import Path


def release_notes(changelog: str, tag: str) -> str:
    version = tag.removeprefix("v")
    heading = re.compile(rf"^##\s+{re.escape(version)}(?:\s+-.*)?$", re.MULTILINE)
    match = heading.search(changelog)
    if match is None:
        raise ValueError(f"CHANGELOG.md has no section for {tag}")

    next_heading = re.search(r"^##\s+", changelog[match.end() :], re.MULTILINE)
    end = len(changelog) if next_heading is None else match.end() + next_heading.start()
    body = changelog[match.end() : end].strip()
    if not body or "- " not in body:
        raise ValueError(f"CHANGELOG.md section for {tag} has no release notes")
    return f"# MeshLab RF {tag}\n\n{body}\n"


if __name__ == "__main__":
    if len(sys.argv) not in {3, 4}:
        raise SystemExit("usage: release_notes.py CHANGELOG.md vX.Y.Z [OUTPUT.md]")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        notes = release_notes(Path(sys.argv[1]).read_text(encoding="utf-8"), sys.argv[2])
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if len(sys.argv) == 4:
        Path(sys.argv[3]).write_text(notes, encoding="utf-8")
    else:
        print(notes, end="")
