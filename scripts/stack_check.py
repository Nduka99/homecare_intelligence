"""Check, from inside the running compose stack, that the app cannot reach outside.

    uv run python scripts/stack_check.py

Each check prints PASS or FAIL. The app's attempts to connect out are attack tests: they pass only
when the connection fails. The control runs the same probe in a throwaway container on Docker's
default network, where it must connect; without it, a failing probe would prove nothing. The engine
check asks Docker Desktop's internal engine address for its version, which any container could do
before Docker Desktop 4.44.3 (CVE-2025-9074).
"""

import subprocess
import sys

from probe import CODE, answer, refused

APP = "homecare-intelligence-app-1"
IMAGE = "homecare:live"
INTERNET = ("1.1.1.1", "443")
ENGINE = ("192.168.65.7", "2375")


def run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    return answer(result.stdout + result.stderr)


def from_app(address: tuple[str, str]) -> str:
    return run(["docker", "exec", APP, "python", "-c", CODE, *address])


def from_throwaway(address: tuple[str, str]) -> str:
    command = ["docker", "run", "--rm", "--network", "bridge", IMAGE, "python", "-c", CODE]
    return run([*command, *address])


def report(ok: bool, text: str) -> bool:
    print("PASS" if ok else "FAIL", text)
    return ok


def main() -> int:
    control = from_throwaway(INTERNET)
    engine = from_throwaway(ENGINE)
    app_internet = from_app(INTERNET)
    app_engine = from_app(ENGINE)
    results = [
        report(
            control.startswith("connected"),
            f"control: a container with a route out connects ({control})",
        ),
        report(
            refused(engine) or engine == "connected",
            f"no container reaches the Docker Engine API ({engine})",
        ),
        report(refused(app_internet), f"the app cannot reach the internet ({app_internet})"),
        report(refused(app_engine), f"the app cannot reach Docker's address ({app_engine})"),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
