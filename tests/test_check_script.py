"""The attribution scan must catch what it claims to catch, and pass clean files."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check", Path(__file__).parents[1] / "scripts" / "check.py"
)
assert spec is not None and spec.loader is not None
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_catches_a_co_author_trailer() -> None:
    problems = check.attribution_problems({"commit abc": "Fix\n\nCo-Authored-By: Claude <x@y>"})
    assert problems == ["commit abc: AI attribution"]


def test_catches_an_agent_name_in_code_but_allows_it_in_the_readme() -> None:
    texts = {"src/a.py": "# written by Codex", "README.md": "Built with help from Codex."}
    assert check.attribution_problems(texts) == [
        "src/a.py: names an AI tool outside a method section"
    ]


def test_clean_files_pass() -> None:
    assert check.attribution_problems({"src/a.py": "def f() -> int:\n    return 1\n"}) == []


def test_only_the_readme_ships_among_markdown_files() -> None:
    assert check.ships("README.md")
    assert check.ships("src/homecare/app/main.py")
    assert not check.ships("docs/notes.md")
    assert not check.ships("NOTES.md")
