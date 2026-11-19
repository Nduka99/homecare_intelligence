"""Load and failure checks at the live pod's limits, run on this laptop.

    uv run python scripts/load_check.py          # both checks
    uv run python scripts/load_check.py ramp     # or one of them
    uv run python scripts/load_check.py fill

It builds the serving image from this folder and runs it in a throwaway container with the
pod's limits: 1 CPU, 512 MiB, a read-only root, a 64 MiB in-memory /tmp, and a restart whenever
the app stops. The container listens on 127.0.0.1 only and is removed at the end.

1. Ramp: 10, 30, 60, 120 and 240 visitors at once, 60 s each. Each visitor signs in as a carer
   and sends a note, or as the lead and opens notes (each view is written to the audit log). Each
   journey comes from its own address (Cloudflare's header), as different people would. Passes
   when: no answer of 500 or more other than the busy page; no request fails or waits 30 s; 95%
   of the requests served (not turned away) take at most 0.5 s at 10 and 30 visitors and 2 s
   from 60; the health check, asked every second, answers in 0.25 s for 99% of asks; the
   container's peak memory stays under 384 MiB (75% of its limit) and /tmp under 64 MiB; after
   the ramp the database passes SQLite's integrity check, holds exactly the notes the visitors
   were told were sent, and a whole journey takes under 5 s. Each step also prints the requests
   it served per second, to compare one version's capacity with another's, run back to back.
2. Fill and reset: visitors send the longest notes until the demo's storage is full. Passes
   when the note at the budget gets the "demo is restarting" page, the container restarts once,
   answers again within 30 s with a fresh demo (no notes from visitors), no answer was 500 or
   more other than the busy and restarting pages, and peak memory stayed under 384 MiB.

Each check prints PASS or FAIL; exits 1 if any fails. Needs Docker.
"""

import http.client
import itertools
import re
import statistics
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlencode

IMAGE = "homecare:load"
NAME = "homecare-load"
PORT = 8090
STEPS = [10, 30, 60, 120, 240]
STEP_SECONDS = 60
TIMEOUT = 30.0
MEMORY_GATE = 384 * 1024 * 1024
TMP_GATE = 64 * 1024 * 1024
CF = "cf-connecting-ip"
FORM = "application/x-www-form-urlencoded"
LONGEST = "😀" * 4000  # the longest note a carer can send, in four-byte characters
RESULTS: list[bool] = []
ADDRESSES = itertools.count(1)
LOCAL = threading.local()  # each thread's open connection


def report(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append(ok)
    print("PASS" if ok else "FAIL", name, detail)


def docker(*args: str, check: bool = True) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise SystemExit(f"docker {args[0]} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def start() -> None:
    docker("rm", "--force", NAME, check=False)
    docker("build", "--quiet", "--tag", IMAGE, ".")
    docker("run", "--detach", "--name", NAME, "--memory", "512m", "--cpus", "1",
           "--read-only", "--tmpfs", "/tmp:size=64m", "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges:true", "--restart", "unless-stopped",
           "--publish", f"127.0.0.1:{PORT}:8000", IMAGE)  # fmt: skip
    if not healthy_within(60):
        raise SystemExit("the container did not answer its health check within 60 s")


def healthy_within(seconds: float) -> bool:
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        try:
            if call("GET", "/healthz", {}, "")[0] == 200:
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def in_container(code: str) -> str:
    """Run Python in the app's container (its database and memory are only visible there)."""
    return docker("exec", NAME, "python", "-c", code, check=False)


def peak_memory() -> int:
    found = in_container("print(open('/sys/fs/cgroup/memory.peak').read())")
    return int(found) if found.isdigit() else -1


def tmp_used() -> int:
    found = in_container("import shutil; print(shutil.disk_usage('/tmp').used)")
    return int(found) if found.isdigit() else -1


def database(query: str) -> str:
    opened = "sqlite3.connect('/tmp/homecare-demo.db')"
    return in_container(f"import sqlite3; print({opened}.execute({query!r}).fetchone()[0])")


def call(method: str, path: str, cookies: dict[str, str], form: str,
         address: str = "") -> tuple[int, str, str]:  # fmt: skip
    """One request: status, Location, body. Keeps the cookies it is sent. Each thread keeps one
    connection open and reuses it, as the tunnel does; a new connection per request would run
    this laptop out of ports long before the app runs out of anything."""
    if getattr(LOCAL, "connection", None) is None:
        LOCAL.connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=TIMEOUT)
    connection = LOCAL.connection
    headers = {"cookie": "; ".join(f"{name}={value}" for name, value in cookies.items())}
    if address:
        headers[CF] = address
    if form:
        headers["content-type"] = FORM
    try:
        connection.request(method, path, body=form.encode() if form else None, headers=headers)
        answer = connection.getresponse()
        body = answer.read().decode("utf-8", "replace")
    except OSError:
        connection.close()
        LOCAL.connection = None
        raise
    for name, value in answer.getheaders():
        if name.lower() == "set-cookie":
            pair = value.split(";")[0].split("=", 1)
            cookies[pair[0]] = pair[1]
    return answer.status, answer.getheader("location") or "", body


def new_address() -> str:
    """A different visitor's address for each journey, as Cloudflare would name it."""
    number = next(ADDRESSES)
    return f"10.{number // 62500 % 250}.{number // 250 % 250}.{number % 250}"


def field(page: str, name: str) -> str:
    found = re.search(rf'name="{name}" value="([^"]*)"', page)
    return found.group(1) if found else ""


class Tally:
    """What every visitor saw: latencies of pages served, pages turned away, failures."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.served: list[float] = []
        self.by_page: dict[str, list[float]] = {}  # served latencies by kind of page
        self.busy_pages: dict[str, int] = {}  # busy pages by kind of page
        self.turned_away = 0
        self.failures: list[str] = []
        self.sent: list[str] = []  # idempotency keys of notes acknowledged as sent
        self.restarting = 0

    def slowest(self) -> str:
        """The three kinds of page slowest at the 95th percentile, and where busy pages fell."""
        p95 = {kind: percentile(times, 95) for kind, times in self.by_page.items()}
        top = sorted(p95.items(), key=lambda item: -item[1])[:3]
        shown = ", ".join(f"{kind} {seconds:.2f} s" for kind, seconds in top)
        return f"slowest p95: {shown}; busy pages by page: {self.busy_pages}"

    def request(self, method: str, path: str, cookies: dict[str, str], form: str,
                address: str) -> tuple[int, str, str]:  # fmt: skip
        started = time.monotonic()
        try:
            status, location, body = call(method, path, cookies, form, address)
        except OSError as error:
            with self.lock:
                if not self.restarting:  # refused connections while restarting are expected
                    self.failures.append(f"{method} {path}: {error!r}")
            raise
        took = time.monotonic() - started
        kind = method + " " + re.sub(r"/[^/?]*\d[^/?]*", "/x", path.split("?")[0])
        with self.lock:
            if status == 503 and "The service is busy" in body:
                self.turned_away += 1
                self.busy_pages[kind] = self.busy_pages.get(kind, 0) + 1
            elif status == 503 and "The demo is restarting" in body:
                self.restarting += 1
            elif status >= 500:
                self.failures.append(f"{method} {path}: {status}")
            else:
                self.served.append(took)
                self.by_page.setdefault(kind, []).append(took)
        return status, location, body


def carer_journey(tally: Tally, carer: str, body: str = "Lunch made, ate well.") -> None:
    cookies: dict[str, str] = {}
    address = new_address()
    _, _, page = tally.request("GET", "/signin", cookies, "", address)
    form = urlencode({"csrf": field(page, "csrf"), "user": carer})
    tally.request("POST", "/signin", cookies, form, address)
    _, _, page = tally.request("GET", "/today", cookies, "", address)
    visit = re.search(r'href="/visits/([^/"]+)/note"', page)
    if not visit:
        return
    _, _, page = tally.request("GET", f"/visits/{visit.group(1)}/note", cookies, "", address)
    key = field(page, "key")
    form = urlencode({"csrf": field(page, "csrf"), "key": key, "amends": "", "body": body})
    status, location, _ = tally.request(
        "POST", f"/visits/{visit.group(1)}/notes", cookies, form, address
    )
    if status == 303 and "sent=1" in location:
        with tally.lock:
            tally.sent.append(key)


def lead_journey(tally: Tally) -> None:
    cookies: dict[str, str] = {}
    address = new_address()
    _, _, page = tally.request("GET", "/signin", cookies, "", address)
    tally.request("POST", "/signin", cookies, urlencode({"csrf": field(page, "csrf"),
                                                         "user": "lead"}), address)  # fmt: skip
    _, _, page = tally.request("GET", "/queue", cookies, "", address)
    for note in re.findall(r'href="/notes/([^"?]+)"', page)[:2]:
        tally.request("GET", f"/notes/{note}", cookies, "", address)


def carers() -> list[str]:
    page = call("GET", "/signin", {}, "")[2]
    return re.findall(r"<option>(K\d+)</option>", page)


def visitor(tally: Tally, number: int, ends: float, names: list[str]) -> None:
    while time.monotonic() < ends:
        try:
            if number % 2:
                carer_journey(tally, names[number % len(names)])
            else:
                lead_journey(tally)
        except OSError:
            continue


def health_poll(stop: threading.Event, seen: list[float], failed: list[str]) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            status = call("GET", "/healthz", {}, "")[0]
            if status != 200:
                failed.append(str(status))
            seen.append(time.monotonic() - started)
        except OSError as error:
            failed.append(repr(error))
        stop.wait(1.0)


def percentile(values: list[float], share: int) -> float:
    if len(values) < 2:
        return values[0] if values else 0.0
    return statistics.quantiles(values, n=100)[share - 1]


def ramp() -> None:
    names = carers()
    overall = Tally()
    for count in STEPS:
        tally = Tally()
        stop = threading.Event()
        health: list[float] = []
        health_failed: list[str] = []
        poller = threading.Thread(target=health_poll, args=(stop, health, health_failed))
        poller.start()
        ends = time.monotonic() + STEP_SECONDS
        threads = [threading.Thread(target=visitor, args=(tally, n, ends, names))
                   for n in range(count)]  # fmt: skip
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stop.set()
        poller.join()
        limit = 0.5 if count <= 30 else 2.0
        p95 = percentile(tally.served, 95)
        report(
            not tally.failures and p95 <= limit,
            f"{count} visitors",
            f"served {len(tally.served)} ({len(tally.served) / STEP_SECONDS:.1f} per second), "
            f"turned away {tally.turned_away}, p95 {p95:.2f} s "
            f"(gate {limit} s), failures {len(tally.failures)} {tally.failures[:3]}; "
            + tally.slowest(),
        )
        p99 = percentile(health, 99)
        report(not health_failed and p99 <= 0.25, f"{count} visitors: health check",
               f"p99 {p99:.3f} s over {len(health)} asks, failed {health_failed[:3]}")  # fmt: skip
        overall.sent += tally.sent
    memory, used = peak_memory(), tmp_used()
    report(0 < memory <= MEMORY_GATE, "peak memory", f"{memory / 2**20:.0f} MiB")
    report(0 <= used <= TMP_GATE, "/tmp used", f"{used / 2**20:.1f} MiB")
    report(database("PRAGMA integrity_check") == "ok", "database integrity")
    stored = database("SELECT COUNT(*) FROM notes WHERE source = 'app'")
    report(stored == str(len(overall.sent)), "notes stored = notes acknowledged",
           f"{stored} stored, {len(overall.sent)} acknowledged")  # fmt: skip
    LOCAL.connection = None  # this thread's connection sat idle through the ramp
    started = time.monotonic()
    after = Tally()
    carer_journey(after, names[0])
    took = time.monotonic() - started
    report(len(after.sent) == 1 and took < 5, "a whole journey afterwards", f"{took:.2f} s")


def fill() -> None:
    names = carers()
    tally = Tally()
    memory = [0]
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            memory[0] = max(memory[0], peak_memory())
            stop.wait(2.0)

    sampler = threading.Thread(target=sample)
    sampler.start()

    def sender(number: int) -> None:
        cookies: dict[str, str] = {}
        address = f"10.200.{number}.1"
        try:
            _, _, page = tally.request("GET", "/signin", cookies, "", address)
            form = urlencode({"csrf": field(page, "csrf"), "user": names[number % len(names)]})
            tally.request("POST", "/signin", cookies, form, address)
            _, _, page = tally.request("GET", "/today", cookies, "", address)
            visit = re.search(r'href="/visits/([^/"]+)/note"', page)
            if not visit:
                return
            _, _, page = tally.request("GET", f"/visits/{visit.group(1)}/note", cookies, "",
                                       address)  # fmt: skip
            token = field(page, "csrf")
            while not tally.restarting:
                # A new address for each note, as many visitors would send them.
                fields = {"csrf": token, "key": str(uuid.uuid4()), "amends": "", "body": LONGEST}
                path = f"/visits/{visit.group(1)}/notes"
                tally.request("POST", path, cookies, urlencode(fields), new_address())
        except OSError:
            return

    threads = [threading.Thread(target=sender, args=(n,)) for n in range(20)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    full_at = time.monotonic() - started
    report(tally.restarting >= 1, "the note at the budget gets the restarting page",
           f"after {full_at:.0f} s, {len(tally.served)} answers served")  # fmt: skip
    back = healthy_within(30)
    restarts = docker("inspect", "--format", "{{.RestartCount}}", NAME)
    report(back and restarts == "1", "restarts once and answers within 30 s",
           f"restart count {restarts}")  # fmt: skip
    stored = database("SELECT COUNT(*) FROM notes WHERE source = 'app'")
    report(stored == "0", "the demo is fresh after the restart", f"{stored} visitor notes")
    report(not tally.failures, "no server error while filling", str(tally.failures[:3]))
    stop.set()
    sampler.join()
    report(0 < memory[0] <= MEMORY_GATE, "peak memory while filling",
           f"{memory[0] / 2**20:.0f} MiB")  # fmt: skip


def main() -> int:
    chosen = sys.argv[1:] or ["ramp", "fill"]
    if any(name not in {"ramp", "fill"} for name in chosen):
        print(__doc__)
        return 2
    try:
        for name in chosen:
            start()
            if name == "ramp":
                ramp()
            else:
                fill()
    finally:
        docker("rm", "--force", NAME, check=False)
    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
