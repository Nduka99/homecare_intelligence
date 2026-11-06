"""What each screen shows, read from the database as plain data for the templates.

Nothing here builds HTML: the templates escape every value, because a note or a model's output
must never be able to add markup to a page. Quotes are highlighted by splitting a note into
segments here, so the highlighting needs no markup from either.
"""

import datetime
import json
import sqlite3
from typing import Any

from homecare.notes import routing
from homecare.notes.categories import ACT_THIS_VISIT, ACT_TODAY, CATEGORIES

SAMPLE_SEED = "demo-week"  # the random-check draw for the demo week (routing.sampled)
SLOT_NAMES = {"morning": "Morning", "lunch": "Lunch", "tea": "Tea-time", "bed": "Bedtime"}
LABELS = ("confirmed", "wrong category", "not a concern")
CATEGORIES_SET = set(CATEGORIES)
SECTIONS = [
    (ACT_TODAY, "Act today"),
    (ACT_THIS_VISIT, "Act this visit"),
    (routing.RANDOM_CHECK, "Random checks"),
    (routing.TAMPERING, "Possible tampering"),
]
WHY = {
    routing.FINDING: "an urgent finding",
    routing.RANDOM_CHECK: "a random check of a note with no finding",
    routing.TAMPERING: "possible tampering",
}


def why(reasons: list[str]) -> str:
    """Why a note is in the register, in words."""
    text = "; ".join(WHY[reason] for reason in reasons)
    return text[:1].upper() + text[1:]


def flags(reasons: list[str], found_by: list[str]) -> list[str]:
    """What the lead should know before opening a note: every urgent finding came from a sentence
    read alone (such reads miss context, so they raise more false alarms), or the note carries the
    possible-tampering tag."""
    found = []
    if found_by and all(read == "sentence" for read in found_by):
        found.append("sentence reads only")
    if routing.TAMPERING in reasons:
        found.append("possible tampering")
    return found


READS = {"note": "the whole-note read", "both": "both reads", "sentence": "a sentence read alone"}


def clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def date_of(start: str, day: int) -> datetime.date:
    return datetime.date.fromisoformat(start) + datetime.timedelta(days=day)


def week(start: str, days: list[int]) -> list[dict[str, Any]]:
    """The demo week's days, for the date boxes."""
    found = []
    for day in days:
        date = date_of(start, day)
        long = f"{date.strftime('%A')} {date.day} {date.strftime('%B %Y')}"
        found.append({"day": day, "weekday": date.strftime("%a"), "date": date.day, "long": long})
    return found


def segments(text: str, quotes: list[str]) -> list[dict[str, Any]]:
    """The note as runs of text. Each run lists the finding numbers (from 1) whose quote covers
    it, and `ends` lists those whose quote ends with it, so each number is shown once."""
    cover: list[list[int]] = [[] for _ in text]
    ends: dict[int, list[int]] = {}
    for number, quote in enumerate(quotes, start=1):
        start = text.find(quote) if quote else -1
        if start < 0:
            continue
        for index in range(start, start + len(quote)):
            cover[index].append(number)
        ends.setdefault(start + len(quote) - 1, []).append(number)
    runs: list[dict[str, Any]] = []
    for index, char in enumerate(text):
        if runs and runs[-1]["findings"] == cover[index] and not runs[-1]["ends"]:
            runs[-1]["text"] += char
        else:
            runs.append({"text": char, "findings": cover[index], "ends": []})
        runs[-1]["ends"] = ends.get(index, [])
    return runs


def decided_notes(connection: sqlite3.Connection, visitor: str | None) -> set[str]:
    """Notes with any decision in view: a demo visitor's own, or everyone's in accounts mode."""
    if visitor is None:
        rows = connection.execute("SELECT DISTINCT note FROM decisions")
    else:
        rows = connection.execute(
            "SELECT DISTINCT note FROM decisions WHERE visitor = ?", (visitor,)
        )
    return {row["note"] for row in rows}


def day_queue(connection: sqlite3.Connection, day: int) -> list[dict[str, Any]]:
    """The day's review queue in order (routing.review_queue), each item with its section."""
    results = []
    bands_by_note: dict[str, set[str]] = {}
    for note in connection.execute(
        "SELECT id, tamper FROM notes WHERE day = ? ORDER BY id", (day,)
    ):
        rows = connection.execute(
            "SELECT band FROM findings WHERE note = ? ORDER BY position", (note["id"],)
        ).fetchall()
        findings = [{"band": row["band"]} for row in rows]
        bands_by_note[note["id"]] = {row["band"] for row in rows}
        results.append({"id": note["id"], "findings": findings, "tamper": bool(note["tamper"])})
    queue = routing.review_queue(results, SAMPLE_SEED)
    for item in queue:
        bands = bands_by_note[item["note"]]
        if ACT_TODAY in bands:
            item["section"] = ACT_TODAY
        elif ACT_THIS_VISIT in bands:
            item["section"] = ACT_THIS_VISIT
        else:
            item["section"] = item["reasons"][0]
    return queue


def register(connection: sqlite3.Connection, day: int, visitor: str | None) -> dict[str, Any]:
    """The lead's register for one day: sections in review order, numbered rows, the tally."""
    decided = decided_notes(connection, visitor)
    rows = []
    for number, item in enumerate(day_queue(connection, day), start=1):
        note = connection.execute(
            "SELECT n.id, n.carer, v.slot, v.opens, v.closes, c.name FROM notes n "
            "JOIN visits v ON v.id = n.visit JOIN clients c ON c.id = n.client WHERE n.id = ?",
            (item["note"],),
        ).fetchone()
        urgent = connection.execute(
            "SELECT category, found_by FROM findings WHERE note = ? AND band IN (?, ?) "
            "ORDER BY position",
            (note["id"], ACT_TODAY, ACT_THIS_VISIT),
        ).fetchall()
        categories = list(dict.fromkeys(row["category"] for row in urgent))
        rows.append({
            "number": number,
            "note": note["id"],
            "client": note["name"],
            "carer": note["carer"],
            "visit": SLOT_NAMES[note["slot"]],
            "window": f"{clock(note['opens'])}–{clock(note['closes'])}",
            "categories": categories,
            "why": why(item["reasons"]),
            "flags": flags(item["reasons"], [row["found_by"] for row in urgent]),
            "section": item["section"],
            "decided": note["id"] in decided,
        })  # fmt: skip
    sections = []
    for key, title in SECTIONS:
        members = [row for row in rows if row["section"] == key]
        if members:
            sections.append({"key": key, "title": title, "rows": members})
    done = sum(1 for row in rows if row["decided"])
    return {"sections": sections, "to_decide": len(rows) - done, "decided": done}


def next_note(connection: sqlite3.Connection, note_id: str, visitor: str | None) -> str | None:
    """The first undecided note after this one in its day's register, else the first undecided."""
    day = connection.execute("SELECT day FROM notes WHERE id = ?", (note_id,)).fetchone()["day"]
    order: list[str] = [item["note"] for item in day_queue(connection, day)]
    decided = decided_notes(connection, visitor)
    later = order[order.index(note_id) + 1 :] if note_id in order else order
    for candidate in later + order:
        if candidate not in decided and candidate != note_id:
            return candidate
    return None


def note_detail(
    connection: sqlite3.Connection, note_id: str, visitor: str | None
) -> dict[str, Any]:
    """One note with its visit, findings, highlighted text and the latest decision on each."""
    note = connection.execute(
        "SELECT n.*, v.slot, v.opens, v.closes, c.name, c.details FROM notes n "
        "JOIN visits v ON v.id = n.visit JOIN clients c ON c.id = n.client WHERE n.id = ?",
        (note_id,),
    ).fetchone()
    findings = [dict(row) for row in connection.execute(
        "SELECT * FROM findings WHERE note = ? ORDER BY position", (note_id,)
    )]  # fmt: skip
    latest: dict[Any, dict[str, Any]] = {}
    query = "SELECT * FROM decisions WHERE note = ? ORDER BY id"
    for row in connection.execute(query, (note_id,)):
        if visitor is None or row["visitor"] == visitor:
            latest[row["finding"]] = dict(row)
    for number, finding in enumerate(findings, start=1):
        finding["number"] = number
        finding["read_label"] = READS.get(finding["found_by"], finding["found_by"])
        finding["sentence_only"] = finding["found_by"] == "sentence"
        finding["decision"] = latest.get(finding["id"])
    queued = next((i for i in day_queue(connection, note["day"]) if i["note"] == note_id), None)
    return {
        "id": note_id,
        "day": note["day"],
        "client": note["name"],
        "details": note["details"],
        "carer": note["carer"],
        "visit": SLOT_NAMES[note["slot"]],
        "window": f"{clock(note['opens'])}–{clock(note['closes'])}",
        "segments": segments(note["body"], [f["quote"] for f in findings]),
        "findings": findings,
        "tamper": bool(note["tamper"]),
        "reasons": queued["reasons"] if queued else [],
        "why": why(queued["reasons"]) if queued else "Not in this day's register; opened directly",
        "missed": latest.get(None),
        "categories": list(CATEGORIES),
    }


def visit_sheet(connection: sqlite3.Connection, carer: str, day: int) -> list[dict[str, Any]]:
    """A carer's planned visits for one day, in route order, with their own note if written."""
    sheet = []
    query = (
        "SELECT v.id, v.slot, v.opens, v.closes, v.duration, c.name, c.needs, n.id AS note "
        "FROM visits v JOIN clients c ON c.id = v.client "
        "LEFT JOIN notes n ON n.visit = v.id AND n.carer = v.carer "
        "WHERE v.carer = ? AND v.day = ? ORDER BY v.position"
    )
    for row in connection.execute(query, (carer, day)):
        sheet.append({
            "visit": SLOT_NAMES[row["slot"]],
            "window": f"{clock(row['opens'])}–{clock(row['closes'])}",
            "duration": row["duration"],
            "client": row["name"],
            "needs": json.loads(row["needs"]),
            "note": row["note"],
        })  # fmt: skip
    return sheet
