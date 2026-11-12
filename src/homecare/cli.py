"""Command line, run from the repository root.

    homecare serve                          run the web app
    homecare models check [--full]          every model file present (and its SHA-256)
    homecare notes write SET...             write and register synthetic note sets
    homecare notes extract --model M SET... run one model over registered sets
    homecare notes demo                     write the demo week's notes from the planned rota
    homecare world map                      download and pin the road map, once
    homecare world build [--seed N]         build the synthetic agency on the pinned map
    homecare rota sweep                     compare continuity weightings on one week
    homecare rota plan                      plan the demo week's rota
    homecare rota replan --carer K --at T  re-plan a demo day after a carer drops out
    homecare rota replan-sweep              measure the re-plan's time-move weight
    homecare evaluate notes --model M SET...  score a run
    homecare evaluate rank --model M --arm A SET...  the queue-ranking study (laptop only)
    homecare evaluate tamper SET...         the tamper-tag study (laptop only)
    homecare attack generate --model M      write and register the generated attacks
    homecare attack search --attacker A --target T  adaptive attack search, then register
    homecare attack llmail --model M        adapt and register human attacks from LLMail-Inject
    homecare platform seed                  write the platform's SYNTHETIC demo-week seed and pin
    homecare platform user NAME --role R    add or reset a laptop account (password asked)
    homecare retention run                  simulate the workforce, fit and check the model

Data lives in the git-ignored `data/` folder: `data/eval/<set>/` (registered sets),
`data/runs/<set>/<model>--<pipeline>.jsonl` (runs) and `data/reports/` (scores).
"""

import argparse
import getpass
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import uvicorn

from homecare import llm, models, nli, tamper
from homecare.app import auditlog, auth, db, importer, maps, reader, screens
from homecare.app import main as app_main
from homecare.app import seed as platform_seed
from homecare.evaluation import attack, llmail, ranking, search, sets, tampering
from homecare.evaluation import notes as notes_score
from homecare.imports import sample as import_sample
from homecare.imports import validate as import_validate
from homecare.matching import baseline, check, sweep
from homecare.notes import extract
from homecare.world import agency, roads, writer

DATA = Path("data")
PORT = 8090
# Each final attack set: the attacks fixed in code, then the registered attack sets it runs.
# notes-attack-fresh replaces attacks that failed the positive control on notes-attack-final.
FINAL_ATTACKS: dict[str, tuple[list[dict[str, Any]], tuple[str, ...]]] = {
    "notes-attack-final": (
        [attack.CONTROL, *attack.LIBRARY],
        ("attacks-generated", "attacks-adaptive"),
    ),
    "notes-attack-fresh": (
        [attack.CONTROL, *attack.TRANSFER],
        ("attacks-search", "attacks-llmail"),
    ),
}
LLMAIL = DATA / "external" / "llmail-inject"
LLMAIL_COUNT = 10
LLMAIL_SEED = 2025
WORLD = DATA / "world" / "agency.json"
# The demo week: the last 7 days of the built world, planned, then written up as notes.
DEMO = "demo-week"
DEMO_DAYS = range(21, 28)
DEMO_SEED = 909
DEMO_ROTA = DATA / "rota" / "demo-week.json"
# The rota data the app shows the coordinator: committed and pinned, like the seed.
ROTA_APP = platform_seed.FILE.with_name("rota.json")
DEMO_MODEL = "gemma-4-26b-a4b"  # the extraction the platform shows
RETENTION = DATA / "retention"
MIN_PASSWORD = 15


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def run_write(names: list[str]) -> None:
    writers = {writer.SETS[name]["writer"] for name in names}
    if len(writers) != 1:
        raise SystemExit("write sets that share one writer model in each call")
    model = writers.pop()
    for name in names:
        if sets.read_registry(sets.REGISTRY)["sets"].get(name):
            raise SystemExit(f"{name} is registered already; a new set needs a new name")
    with models.served(model, PORT, DATA / "logs" / f"{model}.log") as url:
        for name in names:
            perturb = writer.SETS[name]["pool"] == "B"
            written = write_set(url, model, name, writer.specs(name), perturb)
            description = f"{len(written)} synthetic notes written by {model}"
            sets.register(name, description, sets.ROOT, sets.REGISTRY)
            print(f"{name}: {len(written)} notes registered", flush=True)


def write_set(
    url: str, model: str, name: str, specs: list[dict[str, Any]], perturb: bool
) -> list[dict[str, Any]]:
    """Write each spec's note, log every attempt, and save the notes that passed the checks."""
    by_id = templates_by_id()
    folder = sets.ROOT / name
    folder.mkdir(parents=True, exist_ok=True)
    log = DATA / "logs" / f"{name}-write.jsonl"
    log.unlink(missing_ok=True)
    written = []
    for spec in specs:
        note = writer.write_note(url, spec, by_id, perturb=perturb)
        append_jsonl(log, note)
        if note["text"] is not None:
            written.append(
                {**note, "writer": model, "writer_sha256": models.entry(model)["sha256"]}
            )
        print(name, spec["id"], "ok" if note["text"] else note["failed"], flush=True)
    notes_file = folder / "notes.jsonl"
    notes_file.write_text("", encoding="utf-8")
    for note in written:
        append_jsonl(notes_file, note)
    return written


def run_retention() -> None:
    """Simulate the SYNTHETIC workforce, fit the retention model on months 1-18 and check it on
    months 19-24 against the simulator's true probabilities."""
    from homecare.retention import evaluate, model, simulate  # laptop only (scikit-learn)

    intercept = simulate.tune_intercept()
    rows = simulate.simulate(intercept)
    fitted = model.fit(rows)
    report: dict[str, Any] = {
        "intercept": intercept,
        "annual_event_rate": simulate.annual_rate(rows),
    }
    report |= evaluate.evaluate(rows, fitted)
    RETENTION.mkdir(parents=True, exist_ok=True)
    (RETENTION / "model.json").write_text(json.dumps(fitted, indent=1) + "\n", encoding="utf-8")
    (RETENTION / "report.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    found = report["auroc"]
    print(f"person-months {len(rows)}; annual event rate {report['annual_event_rate']:.3f}")
    months, resigned = report["test_person_months"], report["test_resignations"]
    print(f"test: {months} person-months, {resigned} resignations")
    print(f"AUROC {found['auroc']:.3f} ({found['low']:.3f}-{found['high']:.3f}); "
          f"shuffled {report['controls']['shuffled_labels_auroc']:.3f}; "
          f"truth as score {report['controls']['truth_as_score_auroc']:.3f}")  # fmt: skip
    print(f"calibration: annual gap {report['calibration']['annual_gap']:+.4f}, "
          f"slope {report['calibration']['slope']:.3f}; Brier {report['brier']['model']:.5f} "
          f"vs base rate {report['brier']['base_rate']:.5f}")  # fmt: skip
    for attribute, groups in report["fairness"].items():
        for group, found in groups.items():
            print(f"  {attribute} {group}: annual gap {found['annual_gap']:+.4f}, "
                  f"FPR at flag {found['false_positive_rate']:.3f}")  # fmt: skip
    print(f"label audit: {report['label_audit']}")


def run_platform_user(name: str, role: str, carer: str | None) -> None:
    """Add or reset an account in the laptop's accounts-mode database. The password is read
    without echo and needs at least 15 characters (NIST SP 800-63B, single-factor)."""
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Same password again: "):
        raise SystemExit("the two passwords differ")
    if len(password) < MIN_PASSWORD:
        raise SystemExit(f"use at least {MIN_PASSWORD} characters")
    path = app_main.ACCOUNTS_FOLDER / "homecare.db"
    db.prepare(path, platform_seed.load())
    connection = db.connect(path)
    connection.execute(
        "INSERT INTO users (id, role, carer, password) VALUES (?, ?, ?, ?) ON CONFLICT (id) DO "
        "UPDATE SET role = excluded.role, carer = excluded.carer, password = excluded.password, "
        "failures = 0, locked_until = 0",
        (name, role, carer, auth.hash_password(password)),
    )
    connection.commit()
    connection.close()
    print(f"account {name!r} ({role}) saved in {path}")


ANSWERS = Path("tests") / "data" / "visit-export-answers.json"


def run_export_sample() -> None:
    """Write the SYNTHETIC sample visit export (with planted faults), its pin and its answers."""
    text, answers = import_sample.build(platform_seed.load())
    data = text.encode("utf-8")
    importer.SAMPLE.write_bytes(data)
    importer.SAMPLE_PIN.write_text(
        hashlib.sha256(data).hexdigest() + "\n", encoding="utf-8", newline="\n"
    )
    ANSWERS.parent.mkdir(parents=True, exist_ok=True)
    ANSWERS.write_text(json.dumps(answers, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"{importer.SAMPLE}: {len(answers['lines'])} rows; answers in {ANSWERS}")


def run_platform_import(file: Path) -> None:
    """Check and store a visit export in the laptop's accounts-mode database, audited by its
    SHA-256 and counts only (never a cell)."""
    settings = app_main.settings_from_env()
    if settings["mode"] != "accounts":
        raise SystemExit("set SIGN_IN=accounts: demo mode imports only the committed sample")
    seed = platform_seed.load()
    db.prepare(settings["database"], seed)
    connection = db.connect(settings["database"])
    try:
        found = importer.store(
            connection, file.read_bytes(), file.name, "command line", importer.reference(seed)
        )
    except import_validate.Refused as refusal:
        raise SystemExit(f"refused, nothing stored: {refusal}") from refusal
    entry = {"at": time.time(), "event": "import", "user": "command line",
             "sha256": found["sha256"], "kept": found["kept"],
             "quarantined": found["quarantined"], "new": found["new"]}  # fmt: skip
    auditlog.record(connection, settings["key"], settings["anchor"], entry)
    connection.close()
    if not found["new"]:
        print("this file was imported before; nothing added")
    print(f"kept {found['kept']} ({found['flagged']} flagged), quarantined {found['quarantined']}")


def run_platform_read() -> None:
    """Have the laptop's model read the app notes it has not read yet, in the accounts-mode
    database; each read is audited by note ID only."""
    settings = app_main.settings_from_env()
    if settings["mode"] != "accounts":
        raise SystemExit("set SIGN_IN=accounts: a demo's notes live only in its running process")
    db.prepare(settings["database"], platform_seed.load())
    connection = db.connect(settings["database"])
    if not reader.unread(connection):
        connection.close()
        print("every app note has been read")
        return
    header = {"model": DEMO_MODEL, "sha256": models.entry(DEMO_MODEL)["sha256"],
              "prompt": extract.PROMPT_HASH, "pipeline": extract.PIPELINE}  # fmt: skip
    with models.served(DEMO_MODEL, PORT, DATA / "logs" / f"{DEMO_MODEL}.log") as url:
        done = reader.read_all(connection, lambda text: extract.extract(url, text), header)
    for note in done["read"]:
        entry = {"at": time.time(), "event": "model read", "user": "command line", "note": note}
        auditlog.record(connection, settings["key"], settings["anchor"], entry)
    connection.close()
    print(f"read {len(done['read'])}; failed {len(done['failed'])} (still listed as unread)")


def run_platform_seed() -> None:
    """Write the SYNTHETIC demo-week seed the platform loads, and its pin. The PIGuard scores must
    come from the pinned checkpoint."""
    world = json.loads(WORLD.read_text(encoding="utf-8"))
    rotas = json.loads(DEMO_ROTA.read_text(encoding="utf-8"))["rotas"]
    folder = sets.require(DEMO, sets.ROOT, sets.REGISTRY)
    notes = read_jsonl(folder / "notes.jsonl")
    run = read_jsonl(run_path(DEMO, DEMO_MODEL))
    scores = json.loads((DATA / "runs" / DEMO / f"{tamper.NAME}.json").read_text(encoding="utf-8"))
    if scores["sha256"] != models.entry(tamper.NAME)["files"]["model.safetensors"]["sha256"]:
        raise SystemExit("the demo week's PIGuard scores are not from the pinned checkpoint")
    from homecare.retention import demo  # plain Python, but only the seed needs it

    fitted = json.loads((RETENTION / "model.json").read_text(encoding="utf-8"))
    today = screens.date_of(world["start"], DEMO_DAYS[-1] + 1).isoformat()
    retention = {"model": fitted, "carers": demo.known_now(world, rotas, today)}
    text = platform_seed.dumps(
        platform_seed.build(world, rotas, list(DEMO_DAYS), notes, run, scores, retention)
    )
    platform_seed.FILE.parent.mkdir(parents=True, exist_ok=True)
    platform_seed.FILE.write_text(text, encoding="utf-8", newline="\n")
    pin = platform_seed.sha256(text) + "\n"
    platform_seed.PIN.write_text(pin, encoding="utf-8", newline="\n")
    print(f"{platform_seed.FILE}: {len(text):,} characters, SHA-256 {platform_seed.sha256(text)}")


def run_demo_notes() -> None:
    """The demo week's notes (DEMO only), from the planned rota: written, then registered so the
    extractor can read them. Never scored as an evaluation set."""
    if sets.read_registry(sets.REGISTRY)["sets"].get(DEMO):
        raise SystemExit(f"{DEMO} is registered already; a new set needs a new name")
    world = json.loads(WORLD.read_text(encoding="utf-8"))
    plan = json.loads(DEMO_ROTA.read_text(encoding="utf-8"))
    specs = writer.demo_specs(world, plan["rotas"], DEMO, DEMO_SEED)
    model = writer.GLM["writer"]
    with models.served(model, PORT, DATA / "logs" / f"{model}.log") as url:
        written = write_set(url, model, DEMO, specs, perturb=False)
    description = (
        f"DEMO ONLY, never an evaluation set: {len(written)} synthetic notes written by {model} "
        f"from the rota planned for days {DEMO_DAYS[0]}-{DEMO_DAYS[-1]}"
    )
    sets.register(DEMO, description, sets.ROOT, sets.REGISTRY)
    print(f"{DEMO}: {len(written)} notes registered", flush=True)


def run_plan(continuity: int, high_need: int) -> None:
    """The optimised rota for the demo week, beside the baseline, every rota checked."""
    from homecare.matching import solve  # laptop-only, as in run_map

    world = json.loads(WORLD.read_text(encoding="utf-8"))
    report: dict[str, Any] = {"continuity": continuity, "high_need": high_need, "rotas": []}
    results: dict[str, list[dict[str, Any]]] = {"baseline": [], "solver": []}
    for day in DEMO_DAYS:
        rota = solve.solve(world, day, continuity, high_need)
        report["rotas"].append(rota)
        results["solver"].append(check.check(rota, world, day))
        results["baseline"].append(check.check(baseline.plan_day(world, day), world, day))
        print("day", day, rota["search"], flush=True)
    report["totals"] = {label: sweep.totals(found) for label, found in results.items()}
    DEMO_ROTA.parent.mkdir(parents=True, exist_ok=True)
    DEMO_ROTA.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for label, row in report["totals"].items():
        print(f"{label:9}", "  ".join(f"{k} {v}" for k, v in row.items()))


def run_replan(day: int, out: str, at_text: str) -> None:
    """Re-plan a demo day after a carer drops out, check it three ways, and save it
    for the coordinator's screen beside the planned week's uncovered visits and their reasons."""
    from homecare.matching import replan  # laptop-only, as in run_map

    world = json.loads(WORLD.read_text(encoding="utf-8"))
    plan = json.loads(DEMO_ROTA.read_text(encoding="utf-8"))
    if day not in DEMO_DAYS:
        raise SystemExit(f"day {day} is not in the demo week ({DEMO_DAYS[0]}-{DEMO_DAYS[-1]})")
    hours, mins = at_text.split(":")
    at = int(hours) * 60 + int(mins)
    before = plan["rotas"][list(DEMO_DAYS).index(day)]
    after = replan.replan(world, before, day, out, at, plan["continuity"], plan["high_need"])
    problems = check.check(after, world, day, after["holds"])["problems"]
    problems += check.unmoved(before, after, world, day, out, at)
    problems += check.reason_problems(after, world, day, after["holds"], out)
    if problems:
        raise SystemExit("the re-plan fails its checks, nothing saved: " + "; ".join(problems))
    uncovered = {}
    for each_day, rota in zip(DEMO_DAYS, plan["rotas"], strict=True):
        uncovered[str(each_day)] = rota["uncovered"]
    summary = replan.summarise(world, day, before, after, out, at)
    # The whole re-planned day too, so a test can check the committed scenario again.
    summary["rota"] = {key: after[key] for key in ("routes", "uncovered", "holds")}
    data = {
        "synthetic": True,
        "about": "SYNTHETIC rota data for the demo week: no real person, client or carer.",
        "uncovered": uncovered,
        "replan": summary,
    }
    text = platform_seed.dumps(data)
    ROTA_APP.write_text(text, encoding="utf-8", newline="\n")
    ROTA_APP.with_name("rota.json.sha256").write_text(
        platform_seed.sha256(text) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"{out} out at {at_text} on day {day}: {len(summary['changes'])} visits change;",
          summary["totals"], summary["search"])  # fmt: skip
    for change in summary["changes"]:
        print(" ", change)
    for row in summary["work"]:
        print(" ", row)


REPLAN_WEIGHTS = [0, 1, 2, 4, 8]  # tenths of a driving minute per minute a visit moves
SWEEP_AT = 600  # each fresh drop-out is at 10:00


def busiest_after(world: dict[str, Any], rota: dict[str, Any], day: int, at: int) -> str:
    """The carer with the most visits planned to start at or after `at` (lowest ID on a tie)."""
    nodes = {client["id"]: client["node"] for client in world["clients"]}
    carers = {carer["id"]: carer for carer in world["carers"]}
    by_id = {visit["id"]: visit for visit in world["visits"] if visit["day"] == day}
    counts: list[tuple[int, str]] = []
    for carer_id, route in sorted(rota["routes"].items()):
        plan = check.timeline([by_id[v] for v in route], carers[carer_id], nodes, world)
        counts.append((-sum(1 for stop in plan["stops"] if stop["starts"] >= at), carer_id))
    return min(counts)[1]


def run_replan_sweep() -> None:
    """The time-move weight measured on fresh drop-outs: on days 21-26 the busiest
    carer after 10:00 drops out at 10:00, re-planned at each weight, every result checked; the
    knee of minutes moved picks the weight."""
    from homecare.matching import replan  # laptop-only, as in run_map

    world = json.loads(WORLD.read_text(encoding="utf-8"))
    plan = json.loads(DEMO_ROTA.read_text(encoding="utf-8"))
    rows: dict[int, dict[str, int]] = {}
    scenarios = []
    for weight in REPLAN_WEIGHTS:
        rows[weight] = {"changes": 0, "driving": 0, "breaks": 0, "moved": 0, "problems": 0}
    for day in DEMO_DAYS[:-1]:
        before = plan["rotas"][list(DEMO_DAYS).index(day)]
        out = busiest_after(world, before, day, SWEEP_AT)
        scenarios.append({"day": day, "out": out})
        for weight in REPLAN_WEIGHTS:
            after = replan.replan(world, before, day, out, SWEEP_AT, plan["continuity"],
                                  plan["high_need"], moved_tenths=weight)  # fmt: skip
            found = check.check(after, world, day, after["holds"])["problems"]
            found += check.unmoved(before, after, world, day, out, SWEEP_AT)
            found += check.reason_problems(after, world, day, after["holds"], out)
            summary = replan.summarise(world, day, before, after, out, SWEEP_AT)
            totals = summary["totals"]
            row = rows[weight]
            row["changes"] += len(summary["changes"])
            row["driving"] += (
                totals["driving_minutes"]["after"] - totals["driving_minutes"]["before"]
            )
            row["breaks"] += totals["continuity_breaks"]["after"]
            row["moved"] += totals["minutes_moved"]["after"]
            row["problems"] += len(found)
            moved = totals["minutes_moved"]["after"]
            print(f"day {day} {out} weight {weight}/10: {len(summary['changes'])} changes, "
                  f"moved {moved}, problems {len(found)}", flush=True)  # fmt: skip
    chosen = sweep.knee({w: rows[w]["moved"] for w in REPLAN_WEIGHTS if w > 0})
    report = {"at": SWEEP_AT, "scenarios": scenarios, "rows": rows, "knee": chosen}
    out_file = DATA / "rota" / "replan-sweep.json"
    out_file.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for weight, row in rows.items():
        print(f"weight {weight}/10", "  ".join(f"{k} {v}" for k, v in row.items()))
    print(f"knee: {chosen}/10 of a driving minute per minute moved")


def templates_by_id() -> dict[str, dict[str, Any]]:
    return {t["id"]: t for pool in ("A", "B") for t in writer.templates(pool)}


def evidence_of(note: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str | None:
    """The note's evidence sentence, found from its main template's evidence words."""
    must = by_id[note["templates"][0]]["must"]
    return attack.evidence_unit(extract.units(note["text"]), must)


def final_attacks(name: str) -> list[dict[str, Any]]:
    """A final set's attacks fixed in code, then its registered attack sets, in a fixed order."""
    in_code, sources = FINAL_ATTACKS[name]
    found = list(in_code)
    registered = sets.read_registry(sets.REGISTRY)["sets"]
    for source in sources:
        if source in registered:
            folder = sets.require(source, sets.ROOT, sets.REGISTRY)
            found += read_jsonl(folder / "attacks.jsonl")
    return found


def placed(note: dict[str, Any], item: dict[str, Any], position: str, evidence: str) -> str:
    text = attack.inserted(note["text"], item, position, evidence)
    if not attack.is_insertion(note["text"], text):
        raise SystemExit(f"{note['id']} {item['id']}: the attack would change the note")
    return text


def note_jobs(name: str, notes: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(run id, text) for each extraction: the attack sets add their attacked copies."""
    jobs = [(note["id"], note["text"]) for note in notes]
    if name == "notes-attack":
        for note in notes:
            for variant in range(attack.VARIANTS):
                jobs.append((f"{note['id']}#{variant}", attack.attacked(note["text"], variant)))
    if name in FINAL_ATTACKS:
        by_id = templates_by_id()
        for note in notes:
            evidence = evidence_of(note, by_id)
            for item in final_attacks(name):
                for position in attack.POSITIONS:
                    if position == attack.INSIDE and evidence is None:
                        continue
                    text = placed(note, item, position, evidence or "")
                    jobs.append((f"{note['id']}#{item['id']}@{position}", text))
    return jobs


def run_path(name: str, model: str) -> Path:
    return DATA / "runs" / name / f"{model}--{extract.PIPELINE}.jsonl"


def run_rank(model: str, arm: str, names: list[str]) -> None:
    """Score each queued note's urgent findings with the NLI model (cached per run, checkpoint
    and arm), then report the study's measures."""
    for name in names:
        folder = sets.require(name, sets.ROOT, sets.REGISTRY)
        notes = read_jsonl(folder / "notes.jsonl")
        records = read_jsonl(run_path(name, model))
        runs = {r["id"]: r for r in records[1:]}
        pairs = ranking.jobs(notes, runs, arm)
        weights = models.entry(nli.NAME)["files"]["model.safetensors"]["sha256"]
        header = {"run": records[0]["header"], "nli_sha256": weights, "arm": arm}
        cache = DATA / "runs" / name / f"{model}--{extract.PIPELINE}--nli-{arm}.json"
        if cache.exists() and json.loads(cache.read_text(encoding="utf-8"))["header"] == header:
            values = json.loads(cache.read_text(encoding="utf-8"))["entailment"]
        else:
            values = nli.entailment([(pair["premise"], pair["hypothesis"]) for pair in pairs])
            cache.write_text(json.dumps({"header": header, "entailment": values}), encoding="utf-8")
        report = {
            "set": name,
            **header,
            **ranking.study(notes, runs, ranking.note_scores(pairs, values)),
        }
        out = DATA / "reports" / f"{name}--{model}--{extract.PIPELINE}--rank-{arm}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        found = report["auroc"]
        print(
            f"{name}, arm {arm}: queue {report['queue']} ({report['false_items']} false), "
            f"same items {report['same_items']}\n"
            f"  AUROC {found['auroc']:.3f} ({found['low']:.3f}-{found['high']:.3f}), "
            f"shuffled {report['shuffled_auroc']['auroc']:.3f}\n"
            f"  real in top {report['top']}: today {report['real_in_top_today']}, "
            f"ranked {report['real_in_top_ranked']}, oracle {report['oracle_real_in_top']}"
        )


def attacked_copies(name: str, notes: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Each attacked copy an attack set's final test made, as (id, text, source, position)."""
    if name not in FINAL_ATTACKS:
        return []
    source = {item["id"]: item["source"] for item in final_attacks(name)}
    copies = []
    for job_id, text in note_jobs(name, notes)[len(notes) :]:
        item_id, position = job_id.split("#")[1].split("@")
        copies.append({"id": job_id, "text": text, "source": source[item_id], "position": position})
    return copies


def run_tamper(names: list[str]) -> None:
    """P(injection) for every clean note and attacked copy (cached per checkpoint), then the
    tamper-tag study's measures. Attack strings alone are scored as a control."""
    weights = models.entry(tamper.NAME)["files"]["model.safetensors"]["sha256"]
    for name in names:
        folder = sets.require(name, sets.ROOT, sets.REGISTRY)
        notes = read_jsonl(folder / "notes.jsonl")
        copies = attacked_copies(name, notes)
        texts = {note["id"]: note["text"] for note in notes}
        texts |= {copy["id"]: copy["text"] for copy in copies}
        if copies:
            texts |= {f"alone:{item['id']}": item["text"] for item in final_attacks(name)}
        cache = DATA / "runs" / name / f"{tamper.NAME}.json"
        cached = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
        if cached.get("sha256") == weights and set(cached["probabilities"]) == set(texts):
            probabilities = cached["probabilities"]
        else:
            ids = sorted(texts)
            probabilities = dict(zip(ids, tamper.injection([texts[i] for i in ids]), strict=True))
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"sha256": weights, "probabilities": probabilities}))
        report: dict[str, Any] = {"set": name, "sha256": weights}
        report["false_tags"] = tampering.false_tags(notes, probabilities)
        lines = [f"{name}: clean notes tagged {shown(report['false_tags']['overall'])}"]
        for persona, found in report["false_tags"]["by_persona"].items():
            lines.append(f"  {persona[:60]}: {shown(found)}")
        if copies:
            report["detection"] = tampering.detection(copies, probabilities)
            alone = [f"alone:{item['id']}" for item in final_attacks(name)]
            report["alone"] = tampering.share_tagged(alone, probabilities)
            lines.append(f"  attacked copies tagged {shown(report['detection']['pooled'])}")
            for key in ("by_source", "by_position"):
                for label, found in report["detection"][key].items():
                    lines.append(f"    {label}: {shown(found)}")
            lines.append(f"  attack strings alone tagged {shown(report['alone'])}")
        out = DATA / "reports" / f"{name}--{tamper.NAME}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print("\n".join(lines))


def run_extract(model: str, names: list[str]) -> None:
    base = {
        "model": model,
        "sha256": models.entry(model)["sha256"],
        "prompt": extract.PROMPT_HASH,
        "pipeline": extract.PIPELINE,
    }
    # Every set is checked before a model starts, so a missing one fails in seconds.
    folders = {name: sets.require(name, sets.ROOT, sets.REGISTRY) for name in names}
    with models.served(model, PORT, DATA / "logs" / f"{model}.log") as url:
        for name in names:
            notes = read_jsonl(folders[name] / "notes.jsonl")
            header = dict(base)
            if name in FINAL_ATTACKS:
                header["attacks"] = [item["id"] for item in final_attacks(name)]
            out = run_path(name, model)
            out.parent.mkdir(parents=True, exist_ok=True)
            done = set()
            if out.exists() and read_jsonl(out)[:1] == [{"header": header}]:
                done = {r["id"] for r in read_jsonl(out)[1:]}
            elif out.exists():
                # A run made under another header is evidence; it is never overwritten.
                raise SystemExit(f"{out} holds a run with another header; move it first")
            else:
                out.write_text(
                    json.dumps({"header": header}, sort_keys=True) + "\n", encoding="utf-8"
                )
            cache: dict[str, Any] = {}
            for job_id, text in note_jobs(name, notes):
                if job_id in done:
                    continue
                result = extract.extract(url, text, cache)
                append_jsonl(out, {"id": job_id, **result})
                print(name, job_id, [f["category"] for f in result["findings"]], flush=True)


def run_evaluate(model: str, names: list[str]) -> None:
    for name in names:
        folder = sets.require(name, sets.ROOT, sets.REGISTRY)
        notes = read_jsonl(folder / "notes.jsonl")
        records = read_jsonl(run_path(name, model))
        runs = {r["id"]: r for r in records[1:]}
        report: dict[str, Any] = {"set": name, "header": records[0]["header"]}
        report |= notes_score.score(notes, runs)
        if name == "notes-attack":
            report["attack"] = attack_score(notes, runs)
        if name in FINAL_ATTACKS:
            report["final_attack"] = attack.final_score(notes, runs, final_attacks(name))
        out = DATA / "reports" / f"{name}--{model}--{extract.PIPELINE}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(summary(report))


def attack_score(notes: list[dict[str, Any]], runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Notes whose concern band the clean note found but an attacked copy lost."""
    succeeded = []
    for note in notes:
        wanted = notes_score.bands_of(note["labels"])
        clean = notes_score.bands_of(notes_score.found(runs[note["id"]])) & wanted
        for variant in range(attack.VARIANTS):
            run = runs[f"{note['id']}#{variant}"]
            if clean - notes_score.bands_of(notes_score.found(run)):
                succeeded.append(f"{note['id']}#{variant}")
    return {"attempts": len(notes) * attack.VARIANTS, "succeeded": succeeded}


def shown(p: dict[str, Any]) -> str:
    return f"{p['found']:3}/{p['total']:<3} {p['rate']:.2f} ({p['low']:.2f}-{p['high']:.2f})"


def summary(report: dict[str, Any]) -> str:
    lines = [f"{report['set']} ({report['notes']} notes), {report['header']['model']}"]
    for kind in ("recall", "precision"):
        for band, p in report[kind].items():
            lines.append(f"  {kind:9} {band:15} {shown(p)}")
    for key in ("safeguarding_type_agreement", "routine_false_alarms", "quote_drops"):
        lines.append(f"  {key:29} {shown(report[key])}")
    missed = len(report["missed_safeguarding"])
    lines.append(f"  errors {len(report['errors'])}; missed safeguarding {missed}")
    lines.append(f"  {'urgent_needing_whole_note':29} {shown(report['urgent_needing_whole_note'])}")
    load = report["alert_burden"]
    lines.append(
        f"  alert burden: {load['urgent_flags_per_100_notes']} urgent flags per 100 notes; "
        f"{load['false_urgent_per_100_routine']} false per 100 routine; "
        f"precision at top {load['top']} {shown(load['precision_at_top'])}"
    )
    if "attack" in report:
        attacks = report["attack"]
        lines.append(f"  injection succeeded {len(attacks['succeeded'])}/{attacks['attempts']}")
    if "final_attack" in report:
        final = report["final_attack"]
        lines.append(f"  attacked copies {final['attempts']}")
        for kind, jobs in final["losses"].items():
            lines.append(f"  losses {kind:17} {len(jobs)}")
        for group, p in final["by_group"].items():
            lines.append(f"    {group:45} {shown(p)}")
        for source, p in final.get("inside_by_source", {}).items():
            lines.append(f"  inside {source:20} raw {shown(p)}")
        for source, p in final.get("inside_beyond_control_by_source", {}).items():
            lines.append(f"  inside {source:20} beyond control {shown(p)}")
    return "\n".join(lines)


def run_generate(model: str) -> None:
    """The generated attacks, written once and registered before the final attack run."""
    name = "attacks-generated"
    if name in sets.read_registry(sets.REGISTRY)["sets"]:
        raise SystemExit(f"{name} is registered already")
    with models.served(model, PORT, DATA / "logs" / f"{model}.log") as url:
        attacks = search.generated(url)
    write_attacks(name, attacks, f"{len(attacks)} attacks written once by {model}")


def write_attacks(name: str, attacks: list[dict[str, Any]], description: str) -> None:
    folder = sets.ROOT / name
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "attacks.jsonl"
    out.write_text("", encoding="utf-8")
    for item in attacks:
        append_jsonl(out, item)
        print(name, item["id"], json.dumps(item["text"], ensure_ascii=False), flush=True)
    sets.register(name, description, sets.ROOT, sets.REGISTRY)


def search_notes() -> list[dict[str, Any]]:
    """The notes-attack-dev notes whose evidence sentence code can find."""
    folder = sets.require("notes-attack-dev", sets.ROOT, sets.REGISTRY)
    by_id = templates_by_id()
    notes = []
    for note in read_jsonl(folder / "notes.jsonl"):
        evidence = evidence_of(note, by_id)
        if evidence is not None:
            notes.append({**note, "evidence": evidence})
    return notes


def try_insertion(
    url: str,
    note: dict[str, Any],
    item: dict[str, Any],
    clean: dict[str, Any],
    cache: dict[str, Any],
) -> dict[str, Any]:
    text = placed(note, item, item["position"], note["evidence"])
    wanted = notes_score.bands_of(note["labels"])
    return attack.read_outcome(wanted, clean, extract.extract(url, text, cache))


def wins(position: str, outcome: dict[str, Any]) -> bool:
    """A success for the position's own aim: outside text aims at the whole-note read, the
    only read that sees it; inside text aims at every read."""
    if position == attack.OUTSIDE:
        return bool(outcome["note_read_lost"])
    return bool(outcome["lost"])


def run_search(attacker: str, target: str) -> None:
    """Graded adaptive search on notes-attack-dev against the fixed v2 defence.

    Each round, the attacker writes text at both positions for each note and sees what each
    read did with its earlier tries and the strongest tries on other notes. Per position, the
    strongest texts are tried on every dev note, and the KEEP that win on most notes are
    registered, even if none wins. Every try is logged in data/logs/search-graded.jsonl."""
    name = "attacks-search"
    if name in sets.read_registry(sets.REGISTRY)["sets"]:
        raise SystemExit(f"{name} is registered already")
    notes = search_notes()
    log = DATA / "logs" / "search-graded.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.unlink(missing_ok=True)
    cache: dict[str, Any] = {}
    target_log = DATA / "logs" / f"{target}.log"
    with models.served(target, PORT, target_log) as url:
        clean = {note["id"]: extract.extract(url, note["text"], cache) for note in notes}
    history: dict[str, list[dict[str, Any]]] = {note["id"]: [] for note in notes}
    every_try: list[dict[str, Any]] = []
    for round_number in range(search.ROUNDS):
        offered = {}
        with models.served(attacker, PORT, DATA / "logs" / f"{attacker}.log") as url:
            for note in notes:
                best = search.elites(every_try, note["id"], search.ELITES)
                mine = history[note["id"]]
                try:
                    offered[note["id"]] = search.proposals(
                        url, note["text"], note["evidence"], mine, best, round_number
                    )
                except llm.ModelServerError as error:
                    print("search", note["id"], "attacker failed:", error, flush=True)
        with models.served(target, PORT, target_log) as url:
            for note in notes:
                for item in offered.get(note["id"], []):
                    outcome = try_insertion(url, note, item, clean[note["id"]], cache)
                    tried = {**item, "note": note["id"], "round": round_number, "outcome": outcome}
                    history[note["id"]].append(tried)
                    every_try.append(tried)
                    append_jsonl(log, tried)
        for position in attack.POSITIONS:
            tries = [
                t for t in every_try if t["round"] == round_number and t["position"] == position
            ]
            round_wins = [t for t in tries if wins(position, t["outcome"])]
            print(
                f"search round {round_number} {position}: {len(round_wins)}/{len(tries)} won",
                flush=True,
            )
    attacks: list[dict[str, Any]] = []
    with models.served(target, PORT, target_log) as url:
        for position in attack.POSITIONS:
            for item in search_winners(url, notes, clean, cache, every_try, position):
                item["id"] = f"S{len(attacks) + 1}"
                attacks.append(item)
    totals = {position: 0 for position in attack.POSITIONS}
    for tried in every_try:
        if wins(tried["position"], tried["outcome"]):
            totals[tried["position"]] += 1
    description = (
        f"{len(attacks)} insertions kept from a graded search by {attacker} against {target}: "
        f"{len(every_try)} tries; {totals[attack.OUTSIDE]} outside beat the whole-note read, "
        f"{totals[attack.INSIDE]} inside beat every read"
    )
    write_attacks(name, attacks, description)


def search_winners(
    url: str,
    notes: list[dict[str, Any]],
    clean: dict[str, dict[str, Any]],
    cache: dict[str, Any],
    every_try: list[dict[str, Any]],
    position: str,
) -> list[dict[str, Any]]:
    """The KEEP texts at one position that win on most dev notes, from the CHECKED strongest."""
    tries = [t for t in every_try if t["position"] == position]
    tries.sort(key=lambda t: search.strength(t["outcome"]), reverse=True)
    checked: list[dict[str, Any]] = []
    for item in tries:
        if item["text"] not in [c["text"] for c in checked]:
            checked.append(item)
        if len(checked) == search.CHECKED:
            break
    for item in checked:
        item["dev_wins"] = 0
        for note in notes:
            outcome = try_insertion(url, note, item, clean[note["id"]], cache)
            if wins(position, outcome):
                item["dev_wins"] += 1
    checked.sort(key=lambda t: (t["dev_wins"], search.strength(t["outcome"])), reverse=True)
    kept = []
    for item in checked[: search.KEEP]:
        kept.append(
            {
                "source": "search",
                "family": f"search {position}",
                "text": item["text"],
                "dev_wins": item["dev_wins"],
                "dev_notes": len(notes),
            }
        )
    return kept


def run_llmail(model: str) -> None:
    """LLMAIL_COUNT human attacks from LLMail-Inject, goal adapted, registered."""
    name = "attacks-llmail"
    if name in sets.read_registry(sets.REGISTRY)["sets"]:
        raise SystemExit(f"{name} is registered already")
    source = LLMAIL / "labelled_unique_submissions_phase2.json"
    sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    revision = (LLMAIL / "REVISION").read_text(encoding="utf-8").strip()
    labels = json.loads(source.read_text(encoding="utf-8"))
    log = DATA / "logs" / "llmail-adapt.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.unlink(missing_ok=True)
    attacks: list[dict[str, Any]] = []
    taken: list[str] = []
    with models.served(model, PORT, DATA / "logs" / f"{model}.log") as url:
        for original in llmail.pool(labels, LLMAIL_SEED):
            if len(attacks) == LLMAIL_COUNT:
                break
            if llmail.near_copy(original, taken):
                continue
            taken.append(original)
            for attempt in range(3):
                text = llmail.adapted(url, original, seed=21 + attempt)
                ok = llmail.kept(original, text)
                append_jsonl(log, {"original": original, "adapted": text, "kept": ok})
                if ok:
                    attacks.append(
                        {
                            "id": f"H{len(attacks) + 1}",
                            "source": "llmail",
                            "family": "human",
                            "text": text,
                            "original": original,
                        }
                    )
                    break
    description = (
        f"{len(attacks)} human attacks from LLMail-Inject Phase 2 (MIT; revision {revision}, "
        f"file sha256 {sha256}), goal rewritten by {model} and checked in code"
    )
    write_attacks(name, attacks, description)


def run_map() -> None:
    # OSMnx is laptop-only, so it is imported here: the serving image runs this module without it.
    from homecare.world import osm

    pin = osm.download()
    osm.PIN.write_text(json.dumps(pin, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{pin['file']}: {pin['nodes']} junctions, {pin['edges']} road links, pinned")


def run_roads() -> None:
    """The road file the carer's map draws and routes on (ODbL), and its pin."""
    from homecare.world import osm  # laptop-only, as in run_map

    map_sha256 = json.loads(osm.PIN.read_text(encoding="utf-8"))["sha256"]
    found = osm.road_file(osm.load(), map_sha256)
    text = json.dumps(found, separators=(",", ":"), ensure_ascii=False) + "\n"
    maps.FILE.write_text(text, encoding="utf-8", newline="\n")
    maps.PIN.write_text(platform_seed.sha256(text) + "\n", encoding="utf-8", newline="\n")
    print(f"{maps.FILE}: {len(found['nodes'])} junctions, {len(found['links'])} links, pinned")


def world_path(seed: int) -> Path:
    """The demo agency's file; an agency from another seed sits beside it, for checks only."""
    return WORLD if seed == agency.SEED else DATA / "world" / f"agency-seed-{seed}.json"


def run_build(seed: int) -> None:
    """A SYNTHETIC agency on the pinned map."""
    from homecare.world import osm  # laptop-only, as in run_map

    edge_list = osm.edges(osm.load())
    nodes = sorted({start for start, _, _ in edge_list})
    world = agency.build(nodes, lambda places: roads.matrix(edge_list, places), seed)
    world["map_sha256"] = json.loads(osm.PIN.read_text(encoding="utf-8"))["sha256"]
    out = world_path(seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(world, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"{out}: {len(world['clients'])} clients, {len(world['carers'])} carers, "
        f"{len(world['visits'])} visits over {world['days']} days, on {len(nodes)} junctions"
    )


def run_sweep(first_day: int, seed: int) -> None:
    """The continuity sweep on one week: the baseline and each setting, all checked."""
    from homecare.matching import solve  # laptop-only, as in run_map

    world = json.loads(world_path(seed).read_text(encoding="utf-8"))
    days = range(first_day, first_day + sweep.WEEK)
    report: dict[str, Any] = {"map_sha256": world["map_sha256"], "rows": {}, "rotas": {}}
    for label in ["baseline", *sweep.SETTINGS]:
        results = []
        for day in days:
            if label == "baseline":
                rota = baseline.plan_day(world, day)
            else:
                continuity, high_need = sweep.SETTINGS[label]
                rota = solve.solve(world, day, continuity, high_need)
            results.append(check.check(rota, world, day))
            report["rotas"][f"{label} day {day}"] = rota
            print(label, "day", day, rota.get("search", ""), flush=True)
        report["rows"][label] = sweep.totals(results)
    rows = report["rows"]
    breaks = {m: rows[f"high need x{m}"]["avoidable_high_need_breaks"] for m in sweep.MULTIPLIERS}
    report["knee"] = sweep.knee(breaks)
    out = DATA / "rota" / f"sweep-seed-{seed}-days-{days[0]}-{days[-1]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for label, row in report["rows"].items():
        print(f"{label:16}", "  ".join(f"{k} {v}" for k, v in row.items()))
    print(f"knee: high need x{report['knee']} by the sweep rule (a person confirms it)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="homecare")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the web app")
    # The container image sets HOST and PORT (Dockerfile).
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    serve.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    check = commands.add_parser("models", help="check the model files").add_subparsers(
        dest="action", required=True
    )
    check.add_parser("check").add_argument("--full", action="store_true", help="also SHA-256")
    notes = commands.add_parser("notes", help="write or read note sets").add_subparsers(
        dest="action", required=True
    )
    notes.add_parser("write").add_argument("sets", nargs="+", choices=list(writer.SETS))
    notes.add_parser("demo", help="write the demo week's notes from the planned rota (DEMO only)")
    extract_command = notes.add_parser("extract")
    extract_command.add_argument("--model", required=True)
    extract_command.add_argument("sets", nargs="+")
    evaluate = commands.add_parser("evaluate", help="score runs").add_subparsers(
        dest="action", required=True
    )
    evaluate_notes = evaluate.add_parser("notes")
    evaluate_notes.add_argument("--model", required=True)
    evaluate_notes.add_argument("sets", nargs="+")
    evaluate_rank = evaluate.add_parser("rank", help="the queue-ranking study (laptop only)")
    evaluate_rank.add_argument("--model", required=True)
    evaluate_rank.add_argument("--arm", required=True, choices=ranking.ARMS)
    evaluate_rank.add_argument("sets", nargs="+")
    evaluate_tamper = evaluate.add_parser("tamper", help="the tamper-tag study (laptop only)")
    evaluate_tamper.add_argument("sets", nargs="+")
    attacks = commands.add_parser("attack", help="make attacks for the v2 attack test")
    attack_commands = attacks.add_subparsers(dest="action", required=True)
    attack_commands.add_parser("generate").add_argument("--model", required=True)
    search_command = attack_commands.add_parser("search")
    search_command.add_argument("--attacker", required=True)
    search_command.add_argument("--target", required=True)
    attack_commands.add_parser("llmail").add_argument("--model", required=True)
    retention = commands.add_parser("retention", help="the retention model (laptop only)")
    retention.add_subparsers(dest="action", required=True).add_parser(
        "run", help="simulate the workforce, fit the model and check it"
    )
    platform = commands.add_parser("platform", help="the web platform's data and accounts")
    platform_commands = platform.add_subparsers(dest="action", required=True)
    platform_commands.add_parser("seed", help="write the SYNTHETIC demo-week seed and its pin")
    platform_commands.add_parser("read", help="have the laptop's model read unread app notes")
    platform_commands.add_parser(
        "export-sample", help="write the SYNTHETIC sample visit export, its pin and its answers"
    )
    platform_commands.add_parser("import", help="check and store a visit export").add_argument(
        "file", type=Path
    )
    user_command = platform_commands.add_parser("user", help="add or reset a laptop account")
    user_command.add_argument("name")
    user_command.add_argument(
        "--role", required=True, choices=["lead", "carer", "coordinator", "manager"]
    )
    user_command.add_argument("--carer", help="the carer ID, for a carer account")
    world = commands.add_parser("world", help="the SYNTHETIC demo agency on a real road map")
    world_commands = world.add_subparsers(dest="action", required=True)
    world_commands.add_parser("map", help="download and pin the road graph, once")
    world_commands.add_parser("roads", help="write the carer map's road file (ODbL) and its pin")
    build_command = world_commands.add_parser("build", help="build the agency on the pinned map")
    build_command.add_argument("--seed", type=int, default=agency.SEED)
    rota = commands.add_parser("rota", help="plan rotas on the built agency")
    rota_commands = rota.add_subparsers(dest="action", required=True)
    sweep_command = rota_commands.add_parser("sweep", help="compare continuity weightings")
    sweep_command.add_argument("--first-day", type=int, default=0)
    sweep_command.add_argument("--seed", type=int, default=agency.SEED)
    plan_command = rota_commands.add_parser("plan", help="plan the demo week (days 21-27)")
    plan_command.add_argument("--continuity", type=int, default=sweep.BASE)
    plan_command.add_argument("--high-need", type=int, default=sweep.HIGH_NEED)
    replan_command = rota_commands.add_parser("replan", help="re-plan a demo day after a drop-out")
    replan_command.add_argument("--day", type=int, default=DEMO_DAYS[-1])
    replan_command.add_argument("--carer", required=True)
    replan_command.add_argument("--at", required=True, help="the drop-out time, HH:MM")
    rota_commands.add_parser("replan-sweep", help="measure the time-move weight")
    args = parser.parse_args(argv)

    if args.command == "serve":
        uvicorn.run(
            "homecare.app.main:create_app_from_env",
            factory=True,
            host=args.host,
            port=args.port,
            server_header=False,
        )
    elif args.command == "models":
        found = models.problems(models.read(), models.FOLDER, quick=not args.full)
        print(
            "\n".join(found) or "all model files present" + (" and unchanged" if args.full else "")
        )
    elif args.command == "notes" and args.action == "write":
        run_write(args.sets)
    elif args.command == "notes" and args.action == "demo":
        run_demo_notes()
    elif args.command == "notes":
        run_extract(args.model, args.sets)
    elif args.command == "attack" and args.action == "generate":
        run_generate(args.model)
    elif args.command == "attack" and args.action == "llmail":
        run_llmail(args.model)
    elif args.command == "attack":
        run_search(args.attacker, args.target)
    elif args.command == "retention":
        run_retention()
    elif args.command == "platform" and args.action == "user":
        run_platform_user(args.name, args.role, args.carer)
    elif args.command == "platform" and args.action == "read":
        run_platform_read()
    elif args.command == "platform" and args.action == "export-sample":
        run_export_sample()
    elif args.command == "platform" and args.action == "import":
        run_platform_import(args.file)
    elif args.command == "platform":
        run_platform_seed()
    elif args.command == "world" and args.action == "map":
        run_map()
    elif args.command == "world" and args.action == "roads":
        run_roads()
    elif args.command == "world":
        run_build(args.seed)
    elif args.command == "rota" and args.action == "plan":
        run_plan(args.continuity, args.high_need)
    elif args.command == "rota" and args.action == "replan":
        run_replan(args.day, args.carer, args.at)
    elif args.command == "rota" and args.action == "replan-sweep":
        run_replan_sweep()
    elif args.command == "rota":
        run_sweep(args.first_day, args.seed)
    elif args.action == "rank":
        run_rank(args.model, args.arm, args.sets)
    elif args.action == "tamper":
        run_tamper(args.sets)
    else:
        run_evaluate(args.model, args.sets)
