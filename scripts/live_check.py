"""Check the live link from outside, the way a visitor reaches it.

    uv run python scripts/live_check.py https://care.example.org

Each check prints PASS or FAIL. The flood check is an attack test: it sends more requests than
the edge's rate-limit rule allows within its 10-second window, from this one address, and
passes only when the edge refuses some of them with 429. Run before the rule exists, it fails;
that is its positive control. It sends only GET /healthz, so it never changes the demo's data.
"""

import http.client
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

# The rule allows 100 requests per 10 seconds from one address; a first visit makes about 31.
FLOOD = 150
CONNECTIONS = 10
WINDOW_SECONDS = 10.0
HEADERS = ["content-security-policy", "x-content-type-options", "referrer-policy"]


def get(conn: http.client.HTTPConnection, path: str) -> http.client.HTTPResponse:
    conn.request("GET", path, headers={"User-Agent": "homecare-live-check"})
    response = conn.getresponse()
    response.read()
    return response


def check_https(host: str) -> bool:
    response = get(http.client.HTTPSConnection(host, timeout=10), "/healthz")
    missing = [name for name in HEADERS if response.getheader(name) is None]
    ok = response.status == 200 and not missing
    print("PASS" if ok else "FAIL", f"https health {response.status}, missing headers {missing}")
    return ok


def check_redirect(host: str) -> bool:
    response = get(http.client.HTTPConnection(host, timeout=10), "/")
    location = response.getheader("location") or ""
    ok = response.status in (301, 308) and location.startswith(f"https://{host}/")
    print("PASS" if ok else "FAIL", f"plain http answers {response.status} to {location!r}")
    return ok


def burst(host: str, count: int) -> list[int]:
    conn = http.client.HTTPSConnection(host, timeout=10)
    return [get(conn, "/healthz").status for _ in range(count)]


def check_flood(host: str) -> bool:
    # One connection takes about 130 ms a request through the tunnel, too slow to fill a
    # window, so the flood goes over parallel connections.
    start = time.monotonic()
    with ThreadPoolExecutor(CONNECTIONS) as pool:
        bursts = list(pool.map(burst, [host] * CONNECTIONS, [FLOOD // CONNECTIONS] * CONNECTIONS))
    seconds = time.monotonic() - start
    counts: dict[int, int] = {}
    for statuses in bursts:
        for status in statuses:
            counts[status] = counts.get(status, 0) + 1
    # Requests spread over more than one window may stay under the limit, so the run must fit.
    fast_enough = seconds < WINDOW_SECONDS
    ok = fast_enough and counts.get(429, 0) > 0
    print("PASS" if ok else "FAIL", f"{FLOOD} requests in {seconds:.1f} s: {counts}")
    return ok


def main() -> int:
    host = urlsplit(sys.argv[1]).hostname or ""
    results = [check_https(host), check_redirect(host), check_flood(host)]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
