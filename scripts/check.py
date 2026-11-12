"""Run every check a change must pass before it is committed.

    uv run python scripts/check.py

The same command runs on a developer's machine and on the CI runner, so both check
exactly the same things. Every step runs, and all failures are listed at the end.
"""

import re
import subprocess
import sys
import tempfile
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
# The carer app's TypeScript (`web/`), run through Node so the same command works everywhere.
TSC = ["node", "node_modules/typescript/bin/tsc"]
BIOME = ["node", "node_modules/@biomejs/biome/bin/biome"]
# Each TypeScript project and the file it compiles to. The compiled files are committed, because
# the serving image has no Node, so they must match a fresh compile exactly.
WEB = {"web/app": "app.js", "web/sw": "sw.js"}
STATIC = Path("src/homecare/app/static")
# Third-party files the app serves, copied unchanged from the npm package package-lock.json pins
# (the serving image has no Node), so each must match its locked original exactly.
VENDORED = {
    "node_modules/leaflet/dist/leaflet.js": "vendor/leaflet/leaflet.js",
    "node_modules/leaflet/dist/leaflet.css": "vendor/leaflet/leaflet.css",
    "node_modules/leaflet/LICENSE": "vendor/leaflet/LICENSE.txt",
}
STEPS = [
    ("format", [PYTHON, "-m", "ruff", "format", "--check", *SOURCES]),
    ("lint", [PYTHON, "-m", "ruff", "check", *SOURCES]),
    ("types", [PYTHON, "-m", "mypy"]),
    ("tests", [PYTHON, "-m", "pytest"]),
    ("app format and lint", [*BIOME, "check", "web"]),
]


def compiled_problems() -> list[str]:
    """Each TypeScript project type-checks and compiles to exactly its committed file."""
    problems = []
    with tempfile.TemporaryDirectory() as folder:
        for project, name in WEB.items():
            if subprocess.run([*TSC, "-p", project, "--outDir", folder]).returncode != 0:
                problems.append(f"{project} does not compile")
                continue
            fresh = (Path(folder) / name).read_text(encoding="utf-8")
            if fresh != (STATIC / name).read_text(encoding="utf-8"):
                problems.append(
                    f"{STATIC / name} is out of date: run `{' '.join(TSC)} -p {project}`"
                )
    return problems


def vendored_problems() -> list[str]:
    """Each vendored file is its locked npm original, unchanged."""
    problems = []
    for original, copy in VENDORED.items():
        if Path(original).read_text(encoding="utf-8") != (STATIC / copy).read_text(
            encoding="utf-8"
        ):
            problems.append(f"{STATIC / copy} differs from {original}: copy it again")
    return problems


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
    print("== app types and compiled files", flush=True)
    for problem in compiled_problems():
        print(problem)
        failed.append("app compile")
    print("== vendored files", flush=True)
    for problem in vendored_problems():
        print(problem)
        failed.append("vendored files")
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
