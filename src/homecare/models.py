"""The local models, listed in models.json: each file's source, revision, size, SHA-256,
licence, role and the llama-server options it runs with. A CPU encoder checkpoint (for the
ranking and tamper-tag studies) is a folder instead, with every file it loads pinned.

The files live in the git-ignored `models/` folder and never ship to the cloud. A model is
served only after its SHA-256 matches, so a swapped or corrupted file never runs. The
llama-server program is found on PATH, or at the path in the `LLAMA_SERVER` environment variable.
"""

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MANIFEST = Path("models.json")
FOLDER = Path("models")
START_TIMEOUT_SECONDS = 600


def read(manifest: Path = MANIFEST) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = json.loads(manifest.read_text(encoding="utf-8"))
    return entries


def entry(name: str, manifest: Path = MANIFEST) -> dict[str, Any]:
    for item in read(manifest):
        if item["name"] == name:
            return item
    raise SystemExit(f"no model named {name!r} in {manifest}")


def sha256(path: Path) -> str:
    """The file's SHA-256, read in chunks (model files run to tens of gigabytes)."""
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def pinned_files(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Each file an entry pins: its one GGUF, or every file of its checkpoint folder."""
    if "files" not in item:
        return [item]
    return [{"file": f"{item['folder']}/{name}", **pin} for name, pin in item["files"].items()]


def problems(entries: list[dict[str, Any]], folder: Path, quick: bool) -> list[str]:
    """What is missing or changed: every file's size, and its SHA-256 unless `quick`."""
    pins = []
    for listed in entries:
        pins.extend(pinned_files(listed))
    found = []
    for item in pins:
        path = folder / item["file"]
        if not path.is_file():
            found.append(f"missing: {item['file']}")
        elif path.stat().st_size != item["bytes"]:
            found.append(f"size differs: {item['file']}")
        elif not quick and sha256(path) != item["sha256"]:
            found.append(f"SHA-256 differs: {item['file']}")
    return found


def server_command(item: dict[str, Any], port: int) -> list[str]:
    program = os.environ.get("LLAMA_SERVER", "llama-server")
    model = str(FOLDER / item["file"])
    host = ["--host", "127.0.0.1", "--port", str(port)]
    return [program, "-m", model, *item["server_args"], *host]


def healthy(url: str) -> bool:
    """Whether llama-server has loaded its model: /health answers 200 when ready."""
    try:
        with urllib.request.urlopen(url + "/health", timeout=5) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


@contextmanager
def served(name: str, port: int, log: Path) -> Iterator[str]:
    """Run llama-server with the named model, after checking its SHA-256; yield its URL and
    stop it afterwards."""
    item = entry(name)
    found = problems([item], FOLDER, quick=False)
    if found:
        raise SystemExit("; ".join(found))
    url = f"http://127.0.0.1:{port}"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as output:
        server = subprocess.Popen(
            server_command(item, port), stdout=output, stderr=subprocess.STDOUT
        )
        try:
            deadline = time.monotonic() + START_TIMEOUT_SECONDS
            while not healthy(url):
                if server.poll() is not None or time.monotonic() > deadline:
                    raise SystemExit(f"llama-server did not start; see {log}")
                time.sleep(2)
            yield url
        finally:
            server.terminate()
            server.wait(timeout=60)
