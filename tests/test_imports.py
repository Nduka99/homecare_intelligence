"""The visit import: every fault planted in the SYNTHETIC sample export is quarantined with its
reason and every planted flag is raised, with no clean row touched (the planting's answer file
is the independent oracle); a file that cannot be used stores nothing; the same file twice adds
nothing; cells are shown as text and never logged; only the coordinator sees imports. Each test
was checked by breaking its protection on purpose."""

import json
from pathlib import Path
from typing import Any

import pytest

from homecare.app import auditlog, db, importer, seed
from homecare.imports import validate
from test_screens import csrf_of, make, sign_in

ANSWERS = Path(__file__).parent / "data" / "visit-export-answers.json"


def outcome(result: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    found = {}
    for row in result["quarantined"]:
        found[str(row["line"])] = {"quarantined": row["reasons"], "flags": []}
    for row in result["kept"]:
        found[str(row["line"])] = {"quarantined": [], "flags": row["flags"]}
    return found


def test_every_planted_fault_and_flag_is_found_and_no_clean_row_is_touched() -> None:
    answers = json.loads(ANSWERS.read_text(encoding="utf-8"))
    rows, blank = validate.read(importer.sample())
    result = validate.check(rows, importer.reference(seed.load()))
    assert outcome(result) == answers["lines"]
    assert blank == answers["blank"]
    planted = [line for line, wanted in answers["lines"].items() if any(wanted.values())]
    assert len(planted) >= 50  # the oracle is not empty: 16 kinds of fault and flag, 3 or more each


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (b"x" * (validate.MAX_BYTES + 1), "over 5 MB"),
        (b"Visit Ref,Client Ref\nV1,C1\n", "columns"),
        (",".join(validate.COLUMNS).encode() + b"\nV00001,Caf\xe9,K01\n", "not UTF-8"),
        ((",".join(validate.COLUMNS) + "\n" * (validate.MAX_LINES + 1)).encode(), "lines"),
        ((",".join(validate.COLUMNS) + "\nV1," + "x" * 200_000 + "\n").encode(), "not readable"),
    ],
)
def test_a_file_that_cannot_be_used_stores_nothing(tmp_path: Path, data: bytes, why: str) -> None:
    db.prepare(tmp_path / "h.db", seed.load())
    connection = db.connect(tmp_path / "h.db")
    with pytest.raises(validate.Refused, match=why):
        importer.store(connection, data, "bad.csv", "test", importer.reference(seed.load()))
    for table in ["imports", "actuals", "quarantine"]:
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


def test_the_committed_sample_has_lf_line_ends_so_git_keeps_its_bytes() -> None:
    """The repository stores text with LF line ends; a CRLF sample would be rewritten on commit
    and fail its pin on every fresh clone (found 6 Nov, before it shipped)."""
    assert b"\r" not in importer.sample()


def test_an_excel_byte_order_mark_and_blank_lines_are_accepted() -> None:
    text = "﻿" + ",".join(validate.COLUMNS) + "\r\n\r\nV1,C1,K1,01/01/2026,,,,,\r\n"
    rows, blank = validate.read(text.encode("utf-8"))
    assert blank == 1 and [line for line, _ in rows] == [3]


def test_the_same_file_twice_adds_nothing(tmp_path: Path) -> None:
    db.prepare(tmp_path / "h.db", seed.load())
    connection = db.connect(tmp_path / "h.db")
    reference = importer.reference(seed.load())
    first = importer.store(connection, importer.sample(), "a.csv", "test", reference)
    again = importer.store(connection, importer.sample(), "renamed.csv", "test", reference)
    assert first["new"] and not again["new"] and again["id"] == first["id"]
    assert connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1
    count = connection.execute("SELECT COUNT(*) FROM actuals").fetchone()[0]
    assert count == first["kept"]


def test_a_changed_sample_stops_the_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    changed = tmp_path / "visit-export.csv"
    changed.write_bytes(importer.SAMPLE.read_bytes().replace(b"K01", b"K02", 1))
    monkeypatch.setattr(importer, "SAMPLE", changed)
    with pytest.raises(SystemExit, match="differs from its pin"):
        make(tmp_path)


def test_the_coordinator_sees_the_report_with_cells_as_text(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    sign_in(client, "coordinator")
    page = client.get("/imports").text
    assert "Quarantine" in page and "Kept, with a flag" in page
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "=HYPERLINK(&#34;https://example.invalid&#34;" in page  # shown, never a formula sink


def test_only_the_coordinator_opens_imports(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    for user in ["lead", "manager", "K01"]:
        sign_in(client, user)
        assert client.get("/imports").status_code == 403
    client.post("/signout", data={"csrf": csrf_of(client.get("/today").text)})
    assert client.get("/imports", follow_redirects=False).headers["location"] == "/signin"


def test_an_import_is_audited_by_hash_and_counts_never_cells(tmp_path: Path) -> None:
    make(tmp_path)
    connection = db.connect(tmp_path / "demo.db")
    entries = [record["entry"] for record in auditlog.chain(connection)]
    imports = [entry for entry in entries if entry["event"] == "import"]
    assert len(imports) == 1
    assert set(imports[0]) == {"at", "event", "user", "sha256", "kept", "quarantined"}
    text = json.dumps(entries)
    for cell in ["HYPERLINK", "script", "V0", "C99"]:
        assert cell not in text


def test_imported_rows_cannot_be_edited_or_deleted(tmp_path: Path) -> None:
    make(tmp_path)
    connection = db.connect(tmp_path / "demo.db")
    for statement in ["UPDATE actuals SET check_in = 0", "DELETE FROM quarantine",
                      "UPDATE imports SET kept = 0"]:  # fmt: skip
        with pytest.raises(Exception, match="append-only"):
            connection.execute(statement)
