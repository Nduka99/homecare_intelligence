"""HL7's FHIR validator, pinned, for checking exports against UK Core (laptop only, Windows).

`install` downloads the validator and a Java runtime into `tools/fhir/` (git-ignored) and checks
each file against the SHA-256 its publisher lists. Nothing is installed system-wide. `validate`
checks both again before every run, then runs the validator offline (`-tx n/a`: no record is sent
to a terminology server) and returns its issues as plain dicts.
"""

import hashlib
import json
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from homecare.models import sha256

FOLDER = Path("tools") / "fhir"
# Eclipse Temurin JRE 21 (GPLv2 with the Classpath Exception); the validator needs Java 17 or later.
JAVA: dict[str, Any] = {
    "file": "OpenJDK21U-jre_x64_windows_hotspot_21.0.13_9.zip",
    "size": 49106397,
    "url": "https://github.com/adoptium/temurin21-binaries/releases/download/"
    "jdk-21.0.13%2B9/OpenJDK21U-jre_x64_windows_hotspot_21.0.13_9.zip",
    "sha256": "2332c0b4cff6bed722da3567c91d5e47568bcba863fa4945d0a8d6fc76158628",
}
# HL7's validator (Apache-2.0), release 7.0.3 of 21 October 2026.
VALIDATOR: dict[str, Any] = {
    "file": "validator_cli.jar",
    "size": 204631233,
    "url": "https://github.com/hapifhir/org.hl7.fhir.core/releases/download/7.0.3/validator_cli.jar",
    "sha256": "57aa43fb40cec5de41def81c927725fd85908eeb71509b7f9313e04d9b8ec79b",
}
UK_CORE = "fhir.r4.ukcore.stu2#2.0.2"
# The runtime's files that run the validator, checked against the pinned zip before every run.
JAVA_FILES = ("bin/java.exe", "lib/modules")


def download(item: dict[str, Any], attempts: int = 10) -> None:
    """Fetch one pinned file. The release host sometimes ends a long download early, so a short
    file is resumed from where it stopped (an HTTP Range request), then its SHA-256 is checked."""
    path = FOLDER / item["file"]
    if path.exists() and sha256(path) == item["sha256"]:
        return
    part = path.with_name(path.name + ".part")
    part.unlink(missing_ok=True)
    for _ in range(attempts):
        done = part.stat().st_size if part.exists() else 0
        if done >= item["size"]:
            break
        request = urllib.request.Request(item["url"], headers={"Range": f"bytes={done}-"})
        with urllib.request.urlopen(request, timeout=60) as response, part.open("ab") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
    if not part.exists() or sha256(part) != item["sha256"]:
        part.unlink(missing_ok=True)
        raise SystemExit(f"{item['file']}: SHA-256 differs from the pinned one; not installed")
    part.replace(path)


def runtime_root(archive: zipfile.ZipFile) -> str:
    """The zip's single top folder, such as `jdk-21.0.13+9-jre`."""
    return archive.namelist()[0].split("/")[0]


def install() -> None:
    FOLDER.mkdir(parents=True, exist_ok=True)
    download(JAVA)
    download(VALIDATOR)
    with zipfile.ZipFile(FOLDER / JAVA["file"]) as archive:
        archive.extractall(FOLDER / "jre")


def java_path() -> Path:
    with zipfile.ZipFile(FOLDER / JAVA["file"]) as archive:
        return FOLDER / "jre" / runtime_root(archive) / "bin" / "java.exe"


def member_sha256(archive: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(name) as member:
        while chunk := member.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def problems() -> list[str]:
    """Why the installed validator or runtime cannot be trusted; empty when both match."""
    found = []
    for item in (JAVA, VALIDATOR):
        path = FOLDER / item["file"]
        if not path.exists():
            return [f"{path} is missing: run `uv run homecare fhir install-validator`"]
        if sha256(path) != item["sha256"]:
            found.append(f"{path}: SHA-256 differs from the pinned one")
    if found:
        return found
    with zipfile.ZipFile(FOLDER / JAVA["file"]) as archive:
        root = runtime_root(archive)
        for name in JAVA_FILES:
            path = FOLDER / "jre" / root / name
            if not path.exists() or sha256(path) != member_sha256(archive, f"{root}/{name}"):
                found.append(f"{path} differs from the pinned runtime")
    return found


def installed() -> bool:
    return all((FOLDER / str(item["file"])).exists() for item in (JAVA, VALIDATOR))


def issues(outcome: dict[str, Any]) -> list[dict[str, str]]:
    """Every issue in the validator's results: one OperationOutcome, or a Bundle of them for
    several files, each naming its file in an extension."""
    if outcome["resourceType"] == "Bundle":
        outcomes = [item["resource"] for item in outcome.get("entry", [])]
    else:
        outcomes = [outcome]
    found = []
    for result in outcomes:
        file = next((ext.get("valueString", "") for ext in result.get("extension", [])
                     if ext["url"].endswith("operationoutcome-file")), "")  # fmt: skip
        for issue in result.get("issue", []):
            found.append({
                "file": Path(file).name,
                "severity": issue["severity"],
                "where": ", ".join(issue.get("expression", issue.get("location", []))),
                "message": issue.get("details", {}).get("text", issue.get("diagnostics", "")),
            })  # fmt: skip
    return found


def validate(paths: list[Path]) -> list[dict[str, str]]:
    """Validate FHIR R4 JSON files against the base specification and UK Core, offline."""
    found = problems()
    if found:
        raise SystemExit("; ".join(found))
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "outcome.json"
        command = [str(java_path()), "-jar", str(FOLDER / VALIDATOR["file"]),
                   *[str(path) for path in paths], "-version", "4.0.1", "-ig", UK_CORE,
                   "-tx", "n/a", "-output", str(out)]  # fmt: skip
        run = subprocess.run(command, capture_output=True, text=True, timeout=900)
        if not out.exists():
            raise SystemExit(f"the validator wrote no results:\n{run.stdout[-3000:]}")
        return issues(json.loads(out.read_text(encoding="utf-8")))
