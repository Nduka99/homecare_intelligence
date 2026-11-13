"""A SYNTHETIC visit export, as an old care system might give it, with known faults planted.

The clean rows come from the demo week's plan: each carer checks in up to 10 minutes after the
planned start (never before the previous visit and the drive are done), stays 90-100% of the
planned time, and logs the note 1-30 minutes after leaving. So no clean row trips any check.

Then faults are planted, one per changed row, and the answer file records what each line must
get: its quarantine reasons and its flags. The answers come from the planting, not from the
checker, so `validate.check` is tested against answers it had no part in.
"""

import csv
import datetime
import io
import random
from collections.abc import Callable
from typing import Any

from homecare.imports import validate as v

SEED = 2028
PLANTED = 3  # rows of each kind of fault
BLANK_LINES = 2
ODD_VISITS = ["V99999", '=HYPERLINK("https://example.invalid","V00001")', "V0"]
ODD_CARERS = ["K99", "<script>alert(1)</script>", "k01"]


def dmy(date: datetime.date) -> str:
    return date.strftime("%d/%m/%Y")


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def clean_rows(seed: dict[str, Any], rng: random.Random) -> list[dict[str, Any]]:
    """One row per visit done by the demo clock, by carer and day, in route order."""
    start = datetime.date.fromisoformat(seed["start"])
    now = seed["now"]
    routes: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for visit in seed["visits"]:
        if visit["carer"] is None:
            continue
        ends = visit["starts"] + visit["duration"]
        if visit["day"] > now["day"] or (visit["day"] == now["day"] and ends > now["minute"]):
            continue
        routes.setdefault((visit["carer"], visit["day"]), []).append(visit)
    rows = []
    for (carer, day), visits in sorted(routes.items()):
        free_from = 0  # the last check-out: the carer cannot arrive before it plus the drive
        for visit in sorted(visits, key=lambda item: item["position"]):
            check_in = max(visit["starts"] + rng.randint(0, 10), free_from + visit["drive"])
            check_out = check_in + round(visit["duration"] * rng.uniform(0.9, 1.0))
            free_from = check_out
            date = start + datetime.timedelta(days=day)
            note = datetime.datetime.combine(date, datetime.time()) + datetime.timedelta(
                minutes=check_out + rng.randint(1, 30)
            )
            cells = [visit["id"], visit["client"], carer, dmy(date), hhmm(visit["starts"]),
                     hhmm(visit["starts"] + visit["duration"]), hhmm(check_in), hhmm(check_out),
                     note.strftime("%d/%m/%Y %H:%M")]  # fmt: skip
            rows.append({"cells": cells, "duration": visit["duration"], "quarantined": [],
                         "flags": [], "added": []})  # fmt: skip
    return rows


def plant(rows: list[dict[str, Any]], carers: list[str], rng: random.Random) -> None:
    """Plant the faults and flags in place. Each changed row records what it must get; a copy or
    a conflicting row is added to its row's `added` list, to follow it in the file."""
    free = list(range(len(rows)))
    rng.shuffle(free)

    def pick(fits: Callable[[int], bool] = lambda index: True) -> dict[str, Any]:
        for position, index in enumerate(free):
            if fits(index):
                del free[position]
                return rows[index]
        raise ValueError("no unchanged row fits")

    def same_day(a: int, b: int) -> bool:
        return bool(rows[a]["cells"][2:4] == rows[b]["cells"][2:4])

    def last_of_day(index: int) -> bool:
        return index + 1 == len(rows) or not same_day(index, index + 1)

    def minutes(row: dict[str, Any], column: int) -> int:
        found = v.minutes(row["cells"][column])
        if found is None:
            raise ValueError(f"not a time: {row['cells'][column]!r}")
        return found

    for _ in range(PLANTED):
        row = pick()
        row["cells"][7] = ""
        row["quarantined"].append(v.NO_CHECK_OUT)

        row = pick()
        row["cells"][6], row["cells"][7] = row["cells"][7], row["cells"][6]
        row["quarantined"].append(v.OUT_BEFORE_IN)

        row = pick()
        row["cells"][6] = row["cells"][6].replace(":", ".")
        row["quarantined"].append(v.BAD_CHECK_IN)

        row = pick()
        day, month, year = row["cells"][3].split("/")
        row["cells"][3] = f"{month}/{day}/{year}"  # US order: a real date, but the wrong one
        row["quarantined"].append(v.WRONG_DATE)

        row = pick()
        row["cells"][1] = "C99"
        row["quarantined"].append(v.UNKNOWN_CLIENT)

        row = pick()
        row["cells"].pop()
        row["quarantined"].append(v.RAGGED)

        row = pick()
        row["added"].append({"cells": list(row["cells"]), "quarantined": [v.COPY], "flags": []})

        row = pick()
        moved = list(row["cells"])
        moved[6] = hhmm(minutes(row, 6) + 15)
        moved[7] = hhmm(minutes(row, 7) + 15)
        row["quarantined"].append(v.CONFLICT)
        row["added"].append({"cells": moved, "quarantined": [v.CONFLICT], "flags": []})

        # One carer in two places: the next visit starts 10 minutes before this one ends.
        first = pick(lambda index: not last_of_day(index) and index + 1 in free)
        second = rows[rows.index(first) + 1]
        free.remove(rows.index(second))
        second["cells"][6] = hhmm(minutes(first, 7) - 10)
        first["quarantined"].append(v.OVERLAP)
        second["quarantined"].append(v.OVERLAP)

        row = pick()
        working = {r["cells"][2] for r in rows if r["cells"][3] == row["cells"][3]}
        row["cells"][2] = rng.choice([carer for carer in carers if carer not in working])
        row["flags"].append(v.OTHER_CARER)

        row = pick(last_of_day)  # nothing after it that day, so moving it clashes with nothing
        row["cells"][6] = hhmm(minutes(row, 6) + 40)
        row["cells"][7] = hhmm(minutes(row, 7) + 40)
        row["flags"].append(v.LATE)

        row = pick()
        row["cells"][7] = hhmm(minutes(row, 6) + round(row["duration"] / 2))
        row["flags"].append(v.SHORT)

        row = pick()
        logged = datetime.datetime.strptime(row["cells"][8], "%d/%m/%Y %H:%M")
        row["cells"][8] = (logged + datetime.timedelta(hours=30)).strftime("%d/%m/%Y %H:%M")
        row["flags"].append(v.LATE_NOTE_FLAG)

        row = pick()
        row["cells"][8] = ""
        row["flags"].append(v.NO_NOTE)

    for odd in ODD_VISITS:
        row = pick()
        row["cells"][0] = odd
        row["quarantined"].append(v.UNKNOWN_VISIT)
    for odd in ODD_CARERS:
        row = pick()
        row["cells"][2] = odd
        row["quarantined"].append(v.UNKNOWN_CARER)


def build(seed: dict[str, Any], rng_seed: int = SEED) -> tuple[str, dict[str, Any]]:
    """The export as CSV text and its answers by line. Line ends are LF, as the repository keeps
    every text file (git would rewrite a CRLF file, and it would no longer match its pin);
    reading Excel's CRLF files is tested on its own."""
    rng = random.Random(rng_seed)
    rows = clean_rows(seed, rng)
    plant(rows, sorted(carer["id"] for carer in seed["carers"]), rng)
    entries: list[dict[str, Any] | None] = []
    for row in rows:
        entries.append(row)
        entries += row["added"]
    for _ in range(BLANK_LINES):
        entries.insert(rng.randrange(1, len(entries)), None)  # None is a blank line
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(v.COLUMNS)
    answers: dict[str, Any] = {
        "about": "SYNTHETIC: what each line of the sample export must get, from the planting",
        "blank": BLANK_LINES,
        "lines": {},
    }
    for line, entry in enumerate(entries, start=2):
        if entry is None:
            buffer.write("\n")
            continue
        writer.writerow(entry["cells"])
        answers["lines"][str(line)] = {
            "quarantined": entry["quarantined"],
            "flags": entry["flags"],
        }
    return buffer.getvalue(), answers
