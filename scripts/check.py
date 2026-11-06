"""Run every check a change must pass before it is committed.

    uv run python scripts/check.py

The same command runs on a developer's machine and on the CI runner, so both check
exactly the same things. Every step runs, and all failures are listed at the end.
"""

import re
import subprocess
import sys
from pathlib import Path

# Files describing how the work was done may name the AI tools used; no other shipped
# file may. "Cursor" is left out: database drivers have cursors.
METHOD_FILES = {"README.md"}
# The scanner and its test hold the patterns they search for, so they are skipped.
SCANNER_FILES = {"scripts/check.py", "tests/test_check_script.py"}
AGENT_NAME = re.compile(r"\b(claude|codex|gemini|copilot)\b", re.IGNORECASE)
ATTRIBUTION = re.compile(
    r"co-authored-by|generated with \[?(claude|codex|gemini|copilot)", re.IGNORECASE
)

PYTHON = sys.executable
SOURCES = ["src", "tests", "scripts"]
# The container installs from this file, so it must match uv.lock exactly.
SERVE_LOCK = Path("requirements/serve.lock")
EXPORT = ["uv", "export", "--no-default-groups", "--no-emit-project", "--no-header", "--format"]
EXPORT += ["requirements-txt", "--quiet"]
STEPS = [
    ("format", [PYTHON, "-m", "ruff", "format", "--check", *SOURCES]),
    ("lint", [PYTHON, "-m", "ruff", "check", *SOURCES]),
    ("types", [PYTHON, "-m", "mypy"]),
    ("tests", [PYTHON, "-m", "pytest"]),
]


def attribution_problems(texts: dict[str, str]) -> list[str]:
    """Paths (or commit messages) that would ship AI attribution, with the reason."""
    problems = []
    for path, text in texts.items():
        if path in SCANNER_FILES:
            continue
        if ATTRIBUTION.search(text):
            problems.append(f"{path}: AI attribution")
        elif AGENT_NAME.search(text) and path not in METHOD_FILES:
            problems.append(f"{path}: names an AI tool outside a method section")
    return problems


def git_lines(*args: str) -> list[str]:
    output = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    return [line for line in output.stdout.splitlines() if line]


def ships(path: str) -> bool:
    """Whether a file can be published: no Markdown but the README ever is (the private
    notes and coordination files are Markdown)."""
    return not path.endswith(".md") or Path(path).name == "README.md"


def files_to_commit() -> dict[str, str]:
    """Files git would commit that can ship; ignored files never ship, so they are not
    scanned."""
    texts = {}
    for path in git_lines("ls-files", "--cached", "--others", "--exclude-standard"):
        if Path(path).is_file() and ships(path):
            texts[path] = Path(path).read_bytes().decode("utf-8", errors="ignore")
    return texts


def main() -> int:
    failed = []
    for name, command in STEPS:
        print(f"== {name}", flush=True)
        if subprocess.run(command).returncode != 0:
            failed.append(name)
    print("== serve lock", flush=True)
    exported = subprocess.run(EXPORT, capture_output=True, text=True, check=True).stdout
    if exported != SERVE_LOCK.read_text(encoding="utf-8"):
        print(f"{SERVE_LOCK} is out of date: rerun `{' '.join(EXPORT)} -o {SERVE_LOCK}`")
        failed.append("serve lock")
    print("== attribution", flush=True)
    for problem in attribution_problems(files_to_commit()):
        print(problem)
        failed.append("attribution")
    if failed:
        print("FAILED: " + ", ".join(dict.fromkeys(failed)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
