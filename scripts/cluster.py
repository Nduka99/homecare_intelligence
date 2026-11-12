"""Start or remove the local Kubernetes cluster for the live demo.

    uv run python scripts/cluster.py up          # the cluster, the app and its network rules
    uv run python scripts/cluster.py up --live   # and the tunnel: the live link moves here
    uv run python scripts/cluster.py down        # delete the cluster

Needs Docker, kind and kubectl on the PATH. `--live` refuses while compose's tunnel runs (stop it
with `docker compose down`): two connectors of one tunnel must never serve two different apps.
The tunnel's config.yml and key file are read from ~/.cloudflared/homecare (or CLOUDFLARED_DIR,
as in compose.yaml) into a Secret; they never enter the repository, and cert.pem is never read.
`up` can be run again: it rebuilds the image and restarts the app with it.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "k8s"
NAME = "homecare"
CONTEXT = "kind-homecare"
IMAGE = "homecare:live"
COMPOSE_TUNNEL = "homecare-intelligence-tunnel-1"


def run(*command: str, stdin: str | None = None) -> str:
    print("$", " ".join(command), flush=True)
    # kind prints UTF-8 symbols; Windows would otherwise decode them with its own code page.
    result = subprocess.run(
        command, input=stdin, capture_output=True, encoding="utf-8", errors="replace", check=False
    )
    if result.returncode != 0:
        raise SystemExit(f"failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def kubectl(*args: str, stdin: str | None = None) -> str:
    return run("kubectl", "--context", CONTEXT, *args, stdin=stdin)


def tunnel_files() -> list[str]:
    """config.yml and the one key file beside it, as kubectl --from-file arguments."""
    default = Path.home() / ".cloudflared" / "homecare"
    folder = Path(os.environ.get("CLOUDFLARED_DIR", default)).expanduser()
    keys = sorted(folder.glob("*.json"))
    if not (folder / "config.yml").is_file() or len(keys) != 1:
        raise SystemExit(f"{folder} needs config.yml and exactly one key file (*.json)")
    return [f"--from-file=config.yml={folder / 'config.yml'}", f"--from-file={keys[0]}"]


def up(live: bool) -> None:
    if live and run("docker", "ps", "-q", "--filter", f"name={COMPOSE_TUNNEL}").strip():
        raise SystemExit("compose's tunnel is running: stop it first with `docker compose down`")
    commit = run("git", "-C", str(ROOT), "rev-parse", "HEAD").strip()
    run("docker", "build", "--build-arg", f"GIT_COMMIT={commit}", "-t", IMAGE, str(ROOT))
    if NAME not in run("kind", "get", "clusters").split():
        run("kind", "create", "cluster", "--config", str(K8S / "kind.yaml"))
    run("kind", "load", "docker-image", IMAGE, "--name", NAME)
    kubectl("apply", "-f", str(K8S / "namespace.yaml"))
    kubectl("apply", "-f", str(K8S / "network.yaml"), "-f", str(K8S / "app.yaml"))
    # The image tag stays the same, so a restart is what makes the app use a rebuilt image.
    kubectl("-n", NAME, "rollout", "restart", "deployment/app")
    kubectl("-n", NAME, "rollout", "status", "deployment/app", "--timeout=180s")
    if live:
        secret = kubectl(
            "-n", NAME, "create", "secret", "generic", "tunnel", *tunnel_files(),
            "--dry-run=client", "-o", "yaml",
        )  # fmt: skip
        kubectl("apply", "-f", "-", stdin=secret)
        kubectl("apply", "-f", str(K8S / "tunnel.yaml"))
        kubectl("-n", NAME, "rollout", "status", "deployment/tunnel", "--timeout=180s")
    print(kubectl("-n", NAME, "get", "pods", "-o", "wide"))


def down() -> None:
    run("kind", "delete", "cluster", "--name", NAME)


def main() -> None:
    args = sys.argv[1:]
    if args in (["up"], ["up", "--live"]):
        up(live=args == ["up", "--live"])
    elif args == ["down"]:
        down()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
