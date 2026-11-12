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
from homecare.retention import model as retention_model

SAMPLE_SEED = "demo-week"  # the random-check draw for the demo week (routing.sampled)
SLOT_NAMES = {"morning": "Morning", "lunch": "Lunch", "tea": "Tea-time", "bed": "Bedtime"}
LABELS = ("confirmed", "wrong category", "not a concern")
NOTHING_MISSED = "nothing"  # section 4's "I read the whole note: nothing missed"
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


def same_concern(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two findings of one category whose quotes overlap, one inside the other after folding
    case and spacing: the whole-note read and a sentence read reporting the same concern."""
    if a["category"] != b["category"]:
        return False
    first = " ".join(a["quote"].casefold().split())
    second = " ".join(b["quote"].casefold().split())
    return first in second or second in first


def group_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings as the decision items the lead sees: near-duplicates of one concern become one
    item. A group keeps the longest quote and every member (each still a stored finding), the
    members' reasons, which reads found it, and the first member's id as the form's name. The
    stored findings and every evaluation set are untouched: this is how they are shown."""
    groups: list[dict[str, Any]] = []
    for finding in findings:
        home = next((g for g in groups if same_concern(g, finding)), None)
        if home is None:
            groups.append({**finding, "members": [finding]})
            continue
        home["members"].append(finding)
        if len(finding["quote"]) > len(home["quote"]):
            home["quote"] = finding["quote"]
    for group in groups:
        reads = {member["found_by"] for member in group["members"]}
        group["found_by"] = "both" if len(reads) > 1 or "both" in reads else reads.pop()
        reasons = []
        for member in group["members"]:
            if member["reason"] not in reasons:
                reasons.append(member["reason"])
        group["reasons"] = reasons
    return groups


# In demo mode a visitor sees the seed's notes and their own app notes, never another visitor's.
# In accounts mode (no visitor) app notes have no visitor, so everyone signed in sees them.
VISIBLE = "(n.visitor IS NULL OR n.visitor = ?)"
# A seed note was read by the model on the laptop; an app note only once it has a reading.
READ = "(n.source = 'seed' OR EXISTS (SELECT 1 FROM readings r WHERE r.note = n.id))"
# Seed notes in their file's order, then app notes as they arrived.
ARRIVAL = "n.written IS NOT NULL, n.written, n.id"


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


def day_queue(
    connection: sqlite3.Connection, day: int, visitor: str | None
) -> list[dict[str, Any]]:
    """The day's review queue in order (routing.review_queue), each item with its section. Only
    notes the model has read can be queued; the rest are listed as unread (`unread`)."""
    results = []
    bands_by_note: dict[str, set[str]] = {}
    query = (
        f"SELECT n.id, n.tamper FROM notes n WHERE n.day = ? AND {VISIBLE} AND {READ} "
        f"ORDER BY {ARRIVAL}"
    )
    for note in connection.execute(query, (day, visitor)):
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


def unread(connection: sqlite3.Connection, day: int, visitor: str | None) -> list[dict[str, Any]]:
    """The day's app notes the model has not read yet, as they arrived. The model runs only on
    the laptop, so a person must see these some other way."""
    query = (
        "SELECT n.id, n.carer, n.amends, v.slot, v.opens, v.closes, c.name FROM notes n "
        "JOIN visits v ON v.id = n.visit JOIN clients c ON c.id = n.client "
        f"WHERE n.day = ? AND n.source = 'app' AND {VISIBLE} AND NOT {READ} ORDER BY {ARRIVAL}"
    )
    rows = []
    for row in connection.execute(query, (day, visitor)):
        rows.append({
            "note": row["id"],
            "client": row["name"],
            "carer": row["carer"],
            "visit": SLOT_NAMES[row["slot"]],
            "window": f"{clock(row['opens'])}–{clock(row['closes'])}",
            "addition": row["amends"] is not None,
        })  # fmt: skip
    return rows


def register(connection: sqlite3.Connection, day: int, visitor: str | None) -> dict[str, Any]:
    """The lead's register for one day: the app notes the model has not read yet come first,
    since nothing has checked them; then the sections in review order. Rows are numbered through,
    and the tally counts every row."""
    decided = decided_notes(connection, visitor)
    first = unread(connection, day, visitor)
    for number, row in enumerate(first, start=1):
        row["number"] = number
        row["decided"] = row["note"] in decided
    rows = []
    for number, item in enumerate(day_queue(connection, day, visitor), start=len(first) + 1):
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
    done = sum(1 for row in rows + first if row["decided"])
    return {
        "sections": sections,
        "to_decide": len(rows) + len(first) - done,
        "decided": done,
        "unread": first,
    }


def next_note(connection: sqlite3.Connection, note_id: str, visitor: str | None) -> str | None:
    """The first undecided note after this one in its day's register, else the first undecided."""
    day = connection.execute("SELECT day FROM notes WHERE id = ?", (note_id,)).fetchone()["day"]
    order: list[str] = [row["note"] for row in unread(connection, day, visitor)]
    order += [item["note"] for item in day_queue(connection, day, visitor)]
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
    groups = group_findings(findings)
    for number, group in enumerate(groups, start=1):
        group["number"] = number
        group["read_label"] = READS.get(group["found_by"], group["found_by"])
        group["sentence_only"] = group["found_by"] == "sentence"
        group["decision"] = latest.get(group["id"])
    queue = day_queue(connection, note["day"], visitor)
    queued = next((item for item in queue if item["note"] == note_id), None)
    additions = connection.execute(
        f"SELECT n.id FROM notes n WHERE n.amends = ? AND {VISIBLE} ORDER BY {ARRIVAL}",
        (note_id, visitor),
    ).fetchall()
    reading = connection.execute(
        "SELECT extraction FROM readings WHERE note = ?", (note_id,)
    ).fetchone()
    read = note["source"] == "seed" or reading is not None
    if queued:
        reason = why(queued["reasons"])
    elif not read:
        reason = "Not read by the model yet"
    else:
        reason = "Not in this day's register; opened directly"
    return {
        "id": note_id,
        "from_app": note["source"] == "app",
        "read": read,
        "reading": json.loads(reading["extraction"]) if reading else None,
        "amends": note["amends"],
        "additions": [row["id"] for row in additions],
        "day": note["day"],
        "client": note["name"],
        "details": note["details"],
        "carer": note["carer"],
        "visit": SLOT_NAMES[note["slot"]],
        "window": f"{clock(note['opens'])}–{clock(note['closes'])}",
        "segments": segments(note["body"], [g["quote"] for g in groups]),
        "findings": groups,
        "tamper": bool(note["tamper"]),
        "reasons": queued["reasons"] if queued else [],
        "why": reason,
        "missed": latest.get(None),
        "categories": list(CATEGORIES),
    }


def own_notes(
    connection: sqlite3.Connection, carer: str, visit: str, visitor: str | None
) -> list[dict[str, Any]]:
    """A carer's notes for one visit, first note first, then any additions as they arrived."""
    query = (
        f"SELECT n.id, n.amends FROM notes n WHERE n.visit = ? AND n.carer = ? AND {VISIBLE} "
        f"ORDER BY n.amends IS NOT NULL, {ARRIVAL}"
    )
    return [dict(row) for row in connection.execute(query, (visit, carer, visitor))]


def started(visit: dict[str, Any], now: dict[str, int]) -> bool:
    """Whether a planned visit has begun by the demo's clock: only then may it have a note."""
    return bool(visit["day"] < now["day"] or (visit["day"] == now["day"]
                                              and visit["starts"] <= now["minute"]))  # fmt: skip


def carer_day(
    connection: sqlite3.Connection,
    carer: str,
    day: int,
    visitor: str | None,
    plan: dict[str, dict[str, Any]],
    now: dict[str, int],
) -> dict[str, Any]:
    """A carer's planned visits for one day in route order: when to leave and arrive, the drive,
    the client's needs and the carer's own notes. Where to go and what to write are kept apart:
    `next` is the first visit not yet begun, and `to_write` lists the visits begun with no note.
    `written` counts the visits that have one. `plan` is the seed's visits by ID, with their
    planned arrival and drive minutes; `now` is the demo's clock."""
    visits = []
    query = (
        "SELECT v.id, v.slot, v.opens, v.closes, v.duration, v.client, c.name, c.needs "
        "FROM visits v JOIN clients c ON c.id = v.client "
        "WHERE v.carer = ? AND v.day = ? ORDER BY v.position"
    )
    for number, row in enumerate(connection.execute(query, (carer, day)), start=1):
        planned = plan[row["id"]]
        notes = own_notes(connection, carer, row["id"], visitor)
        visits.append({
            "number": number,
            "id": row["id"],
            "client_id": row["client"],
            "visit": SLOT_NAMES[row["slot"]],
            "window": f"{clock(row['opens'])}–{clock(row['closes'])}",
            "duration": row["duration"],
            "client": row["name"],
            "needs": json.loads(row["needs"]),
            "arrive": clock(planned["starts"]),
            "leave": clock(planned["starts"] - planned["drive"]),
            "drive": planned["drive"],
            "notes": notes,
            "written": bool(notes),
            "started": started(planned, now),
        })  # fmt: skip
    to_come = [visit for visit in visits if not visit["started"]]
    return {
        "visits": visits,
        "next": to_come[0] if to_come else None,
        "to_write": [visit for visit in visits if visit["started"] and not visit["written"]],
        "written": sum(1 for visit in visits if visit["written"]),
    }


FLAG_SHARE = 0.10  # the top tenth are suggested for a conversation


def team(retention: dict[str, Any]) -> list[dict[str, Any]]:
    """Every working carer, most likely to resign first, with the top reasons in plain words. A
    suggestion for a conversation, never a decision about a person."""
    fitted = retention["model"]
    rows = []
    for carer, known in retention["carers"].items():
        chance = retention_model.score(fitted, known)
        reasons = [text for text, _ in retention_model.reasons(fitted, known)]
        rows.append({"carer": carer, "chance": chance, "hours": known["hours"], "reasons": reasons})
    rows.sort(key=lambda row: (-row["chance"], row["carer"]))
    suggested = max(1, round(FLAG_SHARE * len(rows)))
    for index, row in enumerate(rows):
        row["suggested"] = index < suggested
        row["in_100"] = f"{100 * row['chance']:.1f}"
    return rows


SHOWN_CELLS = (0, 1, 2, 3, 6, 7)  # visit, client, carer, date, check in, check out


def import_report(connection: sqlite3.Connection) -> dict[str, Any] | None:
    """The latest visit import: its file, counts, the rows held in quarantine (cells exactly as
    the file had them) and the kept rows with flags."""
    found = connection.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 1").fetchone()
    if found is None:
        return None
    report = dict(found)
    stamp = datetime.datetime.fromtimestamp(found["imported"], datetime.UTC)
    report["imported"] = stamp.strftime("%d %B %Y %H:%M")
    held = []
    for row in connection.execute(
        "SELECT * FROM quarantine WHERE import = ? ORDER BY line", (found["id"],)
    ):
        cells = json.loads(row["cells"])
        held.append({
            "line": row["line"],
            "reasons": json.loads(row["reasons"]),
            "related": json.loads(row["related"]),
            "cells": [cells[index] if index < len(cells) else "" for index in SHOWN_CELLS],
        })  # fmt: skip
    flagged = []
    for row in connection.execute(
        "SELECT * FROM actuals WHERE import = ? AND flags != '[]' ORDER BY line", (found["id"],)
    ):
        flagged.append({
            "line": row["line"],
            "flags": json.loads(row["flags"]),
            "visit": row["visit"],
            "carer": row["carer"],
            "date": datetime.date.fromisoformat(row["date"]).strftime("%d/%m/%Y"),
            "check_in": clock(row["check_in"]),
            "check_out": clock(row["check_out"]),
        })  # fmt: skip
    report["held"] = held
    report["flagged_rows"] = flagged
    return report


# The planner's reasons, as a coordinator would say them.
REASON_WORDS = {
    "needs two carers (not planned yet)": "Needs two carers: the planner does not pair carers yet",
    "no skilled carer": "No carer working has the skills",
    "no carer of the preferred gender": "No carer working is of the client's preferred gender",
    "outside every shift": "Outside every shift",
    "too far": "Too far for any carer working",
    "clashes with other visits": "Fits no carer's day: it clashes with their other visits",
    "no free carer can reach it in time": "No free carer can reach it in time",
}


def worded(reason: str) -> str:
    return REASON_WORDS.get(reason, reason[:1].upper() + reason[1:])


def day_visits(connection: sqlite3.Connection, day: int) -> list[dict[str, Any]]:
    """A day's visits with their clients, in each carer's route order, unplanned ones last."""
    query = (
        "SELECT v.id, v.client, v.slot, v.opens, v.closes, v.duration, v.carer, v.position, "
        "c.name FROM visits v JOIN clients c ON c.id = v.client WHERE v.day = ? "
        "ORDER BY v.carer IS NULL, v.carer, v.position, v.opens, v.id"
    )
    return [dict(row) for row in connection.execute(query, (day,))]


def rota_day(
    connection: sqlite3.Connection,
    day: int,
    plan: dict[str, dict[str, Any]],
    reasons: dict[str, str],
    chosen: str | None,
) -> dict[str, Any]:
    """The coordinator's day: each carer working with their visits and hours, every uncovered
    visit with its reason in words, and the chosen carer's visits in route order. `plan` is the
    seed's visits by ID, with their planned starts; `reasons` the planner's, by visit."""
    routes: dict[str, list[dict[str, Any]]] = {}
    uncovered = []
    for row in day_visits(connection, day):
        visit = {
            "id": row["id"],
            "client": row["name"],
            "client_id": row["client"],
            "visit": SLOT_NAMES[row["slot"]],
            "window": f"{clock(row['opens'])}–{clock(row['closes'])}",
        }
        if row["carer"] is None:
            uncovered.append(visit | {"reason": worded(reasons.get(row["id"], "not planned"))})
            continue
        starts = plan[row["id"]]["starts"]
        visit |= {"starts": clock(starts), "ends": clock(starts + row["duration"])}
        routes.setdefault(row["carer"], []).append(visit)
    team = []
    for carer, visits in sorted(routes.items()):
        team.append({"carer": carer, "visits": len(visits), "from": visits[0]["starts"],
                     "to": visits[-1]["ends"]})  # fmt: skip
    # One reason stated once, with its visits under it, most common first.
    by_reason: dict[str, list[dict[str, Any]]] = {}
    for visit in uncovered:
        by_reason.setdefault(visit["reason"], []).append(visit)
    groups = [{"reason": reason, "visits": found} for reason, found in by_reason.items()]
    groups.sort(key=lambda group: -len(group["visits"]))
    return {
        "team": team,
        "covered": sum(len(visits) for visits in routes.values()),
        "uncovered": uncovered,
        "groups": groups,
        "chosen": chosen if chosen in routes else None,
        "route": routes.get(chosen or "", []),
    }


def hours(minutes: int) -> str:
    return f"{minutes // 60} h {minutes % 60:02d} min"


def shift(was: int, now: int) -> str:
    """How far a visit moved, in words."""
    if now == was:
        return "same time"
    return f"{abs(now - was)} min {'later' if now > was else 'earlier'}"


def replan_view(connection: sqlite3.Connection, saved: dict[str, Any]) -> dict[str, Any]:
    """The saved re-plan as the coordinator reads it: each visit that changes carer (or is left
    uncovered, with why) and each that keeps its carer at a new time, every move in words beside
    the visit's window; each carer whose day changes; and the totals before and after."""
    visits = {row["id"]: row for row in day_visits(connection, saved["day"])}
    out = saved["out"]

    def described(visit_id: str) -> dict[str, Any]:
        row = visits[visit_id]
        window = f"{clock(row['opens'])}–{clock(row['closes'])}"
        return {"client": row["name"], "visit": SLOT_NAMES[row["slot"]], "window": window}

    changes = []
    for change in saved["changes"]:
        moved = change["starts"] is not None
        changes.append(described(change["visit"]) | {
            "before": change["before"],
            "was": clock(change["was"]),
            "after": change["after"],
            "starts": clock(change["starts"]) if moved else None,
            "shift": shift(change["was"], change["starts"]) if moved else None,
            "made_room": change["before"] != out,
            "reason": worded(change["reason"]) if change["reason"] else None,
        })  # fmt: skip
    retimed = []
    for item in saved["retimed"]:
        retimed.append(described(item["visit"]) | {
            "carer": item["carer"],
            "was": clock(item["was"]),
            "starts": clock(item["starts"]),
            "shift": shift(item["was"], item["starts"]),
        })  # fmt: skip
    work = []
    for row in saved["work"]:
        if row["carer"] == out:
            continue
        work.append({
            "carer": row["carer"],
            "before": hours(row["before"]),
            "after": hours(row["after"]),
            "added": row["after"] - row["before"],
            "left": row["limit"] - row["after"],
            "limit": hours(row["limit"]),
            "reordered": row["reordered"],
        })  # fmt: skip
    work.sort(key=lambda row: (-row["added"], row["carer"]))
    totals = saved["totals"]
    return {
        "day": saved["day"],
        "out": out,
        "at": clock(saved["at"]),
        "changes": changes,
        "retimed": retimed,
        "from_out": sum(1 for c in changes if c["after"] and not c["made_room"]),
        "made_room": sum(1 for c in changes if c["after"] and c["made_room"]),
        "left": [change for change in changes if not change["after"]],
        "already": totals["uncovered"]["before"],
        "work": work,
        "driving": totals["driving_minutes"],
        "breaks": totals["continuity_breaks"],
        "moved": totals["minutes_moved"]["after"],
    }
