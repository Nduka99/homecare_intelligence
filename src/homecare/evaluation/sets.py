"""The registry of evaluation sets. Copied from PetroKnowledge `evaluation/sets.py` (MIT, same
author).

Each set's files live in the git-ignored `data/eval/<set>/`. The committed registry
(`evaluation/sets.json`) records every file's SHA-256, so a changed set is caught before
anything is scored. A registered hash never changes: a different set gets a new name.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path("data/eval")
REGISTRY = Path("evaluation/sets.json")


def file_hashes(folder: Path) -> dict[str, str]:
    """Relative POSIX path -> SHA-256 of every file under the folder, sorted."""
    return {
        path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def read_registry(registry: Path) -> dict[str, Any]:
    if not registry.exists():
        return {"sets": {}}
    data: dict[str, Any] = json.loads(registry.read_text(encoding="utf-8"))
    return data


def problems(name: str, root: Path, registry: Path) -> list[str]:
    """Every difference between a set's files and its registered hashes."""
    registered = read_registry(registry)["sets"].get(name)
    if registered is None:
        return [f"{name}: not registered"]
    found = file_hashes(root / name)
    listed = registered["files"]
    issues = []
    for path, digest in listed.items():
        if path not in found:
            issues.append(f"{name}/{path}: missing")
        elif found[path] != digest:
            issues.append(f"{name}/{path}: changed since it was registered")
    for path in found:
        if path not in listed:
            issues.append(f"{name}/{path}: not registered")
    return issues


def register(name: str, description: str, root: Path, registry: Path) -> list[str]:
    """Record the hashes of the set's files; return the paths added. Refuses when a registered
    file is missing or changed, so a registered set can never be replaced in place."""
    changed = [p for p in problems(name, root, registry) if "not registered" not in p]
    if changed:
        raise ValueError("; ".join(changed))
    data = read_registry(registry)
    entry = data["sets"].setdefault(name, {"description": description, "files": {}})
    added = [path for path in file_hashes(root / name) if path not in entry["files"]]
    entry["files"] = file_hashes(root / name)
    data["sets"] = dict(sorted(data["sets"].items()))
    registry.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    registry.write_text(text, encoding="utf-8", newline="\n")
    return added


def require(name: str, root: Path, registry: Path) -> Path:
    """The set's folder, only if every file matches its registered hash."""
    found = problems(name, root, registry)
    if found:
        raise ValueError("; ".join(found))
    return root / name
