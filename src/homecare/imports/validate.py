"""Check a care system's visit export (CSV) before any row is used.

An agency moving to a new system brings its old visit records: who visited whom, when they
checked in and out, and when the note was logged. Such exports are messy. Every row is checked
against the agency's plan, and a row that cannot be trusted is quarantined with its reasons,
never repaired by guessing. A row that can be used but needs a person's eye is kept with flags.

The file is checked first: too large, not UTF-8, or not the expected columns, and nothing in
it is used. Plain functions on plain data; storing the result is the app's job.
"""

import csv
import datetime
import io
import re
from typing import Any

COLUMNS = [
    "Visit Ref",
    "Client Ref",
    "Carer Ref",
    "Visit Date",
    "Planned Start",
    "Planned End",
    "Check In",
    "Check Out",
    "Note Logged",
]
MAX_BYTES = 5_000_000
MAX_LINES = 20_000
LATE_START = 30  # minutes after the planned start
SHORT_SHARE = 0.75  # of the planned length
LATE_NOTE = 24 * 60  # minutes after check-out

# Why a row is quarantined.
RAGGED = "wrong number of fields"
UNKNOWN_VISIT = "unknown visit"
UNKNOWN_CLIENT = "unknown client"
WRONG_CLIENT = "client does not match the visit"
UNKNOWN_CARER = "unknown carer"
BAD_DATE = "date is not dd/mm/yyyy"
WRONG_DATE = "date does not match the visit"
BAD_CHECK_IN = "check-in missing or not a time"
NO_CHECK_OUT = "missing check-out"
BAD_CHECK_OUT = "check-out not a time"
OUT_BEFORE_IN = "check-out before check-in"
BAD_NOTE_TIME = "note time not dd/mm/yyyy hh:mm"
COPY = "exact copy of an earlier line"
CONFLICT = "more than one row for this visit"
OVERLAP = "overlaps another visit by the same carer"

# Why a kept row needs a person's eye.
OTHER_CARER = "carer differs from the plan"
LATE = "started over 30 minutes late"
SHORT = "under three quarters of the planned time"
LATE_NOTE_FLAG = "note logged over 24 hours after the visit"
NO_NOTE = "no note logged"

DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


class Refused(ValueError):
    """The whole file is refused; the message says why and how to fix it."""


def read(data: bytes) -> tuple[list[tuple[int, list[str]]], int]:
    """The file's data rows with their line numbers, and how many blank lines were skipped."""
    if len(data) > MAX_BYTES:
        raise Refused(
            f"The file is over {MAX_BYTES // 1_000_000} MB. Split it and import each part."
        )
    try:
        text = data.decode("utf-8-sig")  # a byte order mark, as Excel writes, is allowed
    except UnicodeDecodeError as error:
        raise Refused(
            "The file is not UTF-8. Save it as 'CSV UTF-8' and import it again."
        ) from error
    if text.count("\n") > MAX_LINES:
        raise Refused(f"The file has over {MAX_LINES:,} lines. Split it and import each part.")
    reader = csv.reader(io.StringIO(text))
    rows = []
    blank = 0
    try:
        header = [name.strip() for name in next(reader, [])]
        if header != COLUMNS:
            raise Refused("The columns are not the expected ones: " + ", ".join(COLUMNS) + ".")
        for cells in reader:
            if not any(cell.strip() for cell in cells):
                blank += 1
                continue
            rows.append((reader.line_num, cells))
    except csv.Error as error:  # for example, one cell over csv's 128 KB field limit
        raise Refused(f"Line {reader.line_num} is not readable CSV ({error}).") from error
    return rows, blank


def uk_date(text: str) -> datetime.date | None:
    found = DATE.fullmatch(text.strip())
    if not found:
        return None
    day, month, year = (int(part) for part in found.groups())
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def minutes(text: str) -> int | None:
    found = TIME.fullmatch(text.strip())
    return int(found.group(1)) * 60 + int(found.group(2)) if found else None


def stamp(text: str) -> datetime.datetime | None:
    date_text, _, time_text = text.strip().partition(" ")
    date = uk_date(date_text)
    at = minutes(time_text)
    if date is None or at is None:
        return None
    return datetime.datetime.combine(date, datetime.time(at // 60, at % 60))


def row_problems(cells: list[str], reference: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """What is wrong with one row on its own, and the values read from it."""
    if len(cells) != len(COLUMNS):
        return [RAGGED], {}
    visit_id, client, carer, date_text, _, _, in_text, out_text, note_text = (
        cell.strip() for cell in cells
    )
    problems = []
    visit = reference["visits"].get(visit_id)
    if visit is None:
        problems.append(UNKNOWN_VISIT)
    if client not in reference["clients"]:
        problems.append(UNKNOWN_CLIENT)
    elif visit is not None and client != visit["client"]:
        problems.append(WRONG_CLIENT)
    if carer not in reference["carers"]:
        problems.append(UNKNOWN_CARER)
    date = uk_date(date_text)
    if date is None:
        problems.append(BAD_DATE)
    elif visit is not None and date.isoformat() != visit["date"]:
        problems.append(WRONG_DATE)
    check_in = minutes(in_text)
    check_out = minutes(out_text)
    if check_in is None:
        problems.append(BAD_CHECK_IN)
    if not out_text:
        problems.append(NO_CHECK_OUT)
    elif check_out is None:
        problems.append(BAD_CHECK_OUT)
    if check_in is not None and check_out is not None and check_out <= check_in:
        problems.append(OUT_BEFORE_IN)
    note = stamp(note_text) if note_text else None
    if note_text and note is None:
        problems.append(BAD_NOTE_TIME)
    values = {"visit": visit_id, "client": client, "carer": carer, "date": date,
              "check_in": check_in, "check_out": check_out, "note": note}  # fmt: skip
    return problems, values


def flags(values: dict[str, Any], visit: dict[str, Any]) -> list[str]:
    """What a person should look at on a row that can be used."""
    found = []
    if values["carer"] != visit["carer"]:
        found.append(OTHER_CARER)
    if values["check_in"] > visit["starts"] + LATE_START:
        found.append(LATE)
    if values["check_out"] - values["check_in"] < SHORT_SHARE * visit["duration"]:
        found.append(SHORT)
    if values["note"] is None:
        found.append(NO_NOTE)
    else:
        out = datetime.datetime.combine(values["date"], datetime.time()) + datetime.timedelta(
            minutes=values["check_out"]
        )
        if values["note"] - out > datetime.timedelta(minutes=LATE_NOTE):
            found.append(LATE_NOTE_FLAG)
    return found


def check(rows: list[tuple[int, list[str]]], reference: dict[str, Any]) -> dict[str, Any]:
    """Split the rows into kept (with flags) and quarantined (with reasons and related lines).

    `reference` holds the plan: {"visits": {id: {"client", "carer", "date" (ISO), "starts",
    "duration"}}, "clients": set of IDs, "carers": set of IDs}. Checks run in order: each row on
    its own; then exact copies (the first is kept); then several rows for one visit (none can be
    trusted, so all go); then one carer in two places at once (both go). Only rows that passed
    every check are flagged."""
    quarantined: dict[int, dict[str, Any]] = {}
    passed: list[tuple[int, list[str], dict[str, Any]]] = []
    for line, cells in rows:
        problems, values = row_problems(cells, reference)
        if problems:
            quarantined[line] = {"line": line, "reasons": problems, "related": [], "cells": cells}
        else:
            passed.append((line, cells, values))

    first_copy: dict[tuple[str, ...], int] = {}
    unique = []
    for line, cells, values in passed:
        key = tuple(cell.strip() for cell in cells)
        if key in first_copy:
            quarantined[line] = {"line": line, "reasons": [COPY], "related": [first_copy[key]],
                                 "cells": cells}  # fmt: skip
        else:
            first_copy[key] = line
            unique.append((line, cells, values))

    by_visit: dict[str, list[int]] = {}
    for line, _, values in unique:
        by_visit.setdefault(values["visit"], []).append(line)
    single = []
    for line, cells, values in unique:
        lines = by_visit[values["visit"]]
        if len(lines) > 1:
            others = [other for other in lines if other != line]
            quarantined[line] = {"line": line, "reasons": [CONFLICT], "related": others,
                                 "cells": cells}  # fmt: skip
        else:
            single.append((line, cells, values))

    by_carer_day: dict[tuple[str, datetime.date], list[tuple[int, dict[str, Any]]]] = {}
    for line, _, values in single:
        by_carer_day.setdefault((values["carer"], values["date"]), []).append((line, values))
    clashes: dict[int, list[int]] = {}
    for visits in by_carer_day.values():
        visits.sort(key=lambda item: item[1]["check_in"])
        for (line_a, a), (line_b, b) in zip(visits, visits[1:], strict=False):
            if a["check_out"] > b["check_in"]:
                clashes.setdefault(line_a, []).append(line_b)
                clashes.setdefault(line_b, []).append(line_a)
    kept = []
    for line, cells, values in single:
        if line in clashes:
            quarantined[line] = {"line": line, "reasons": [OVERLAP], "related": clashes[line],
                                 "cells": cells}  # fmt: skip
            continue
        visit = reference["visits"][values["visit"]]
        kept.append({
            "line": line,
            "visit": values["visit"],
            "carer": values["carer"],
            "date": values["date"].isoformat(),
            "check_in": values["check_in"],
            "check_out": values["check_out"],
            "note": values["note"].isoformat(timespec="minutes") if values["note"] else None,
            "flags": flags(values, visit),
        })  # fmt: skip
    return {"kept": kept, "quarantined": [quarantined[line] for line in sorted(quarantined)]}
