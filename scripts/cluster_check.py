"""Attack the local cluster's defences, one check for each promise the files in k8s/ make:

    ingress     only the tunnel's pods reach the app
    egress      the app cannot connect anywhere
    tunnel      the tunnel reaches only the cluster's DNS and Cloudflare's tunnel port, on public
                addresses
    privileges  nothing privileged runs (Pod Security "restricted")
    read-only   the app's files are read-only, except /tmp and the /dev/shm every pod gets
    API         pods cannot use the Kubernetes API
    lost node   a lost worker heals (resilience, not security)

    uv run python scripts/cluster_check.py          # every check but the lost node
    uv run python scripts/cluster_check.py --node   # and the lost node: stops the app's worker

Run after `scripts/cluster.py up`. Each attack prints PASS only when it fails. Then the defence is
switched off for a moment (printed as "sabotaged") and the same attack must succeed, so a check
that cannot fail is caught. The defence is put back from the files in k8s/ and the attack is run
again. Expected results come from the rules as written in k8s/, never from the cluster. Probe pods
use the app's image (it has Python) and are deleted at the end.

Run it before `up --live` where possible. While the tunnel is live, switching the rules off and on
makes the live link fail for a few seconds (12 Nov 2026: 3 of 70 requests timed out during the
ingress check; 90 of 90 were served with no rule changes), and --node takes the app away for
about 80 seconds.
"""

import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from probe import CODE, answer, refused

K8S = Path(__file__).resolve().parents[1] / "k8s"
CONTEXT = "kind-homecare"
NS = "homecare"
IMAGE = "homecare:live"
INTERNET = ("1.1.1.1", "443")
WEB = ("1.1.1.1", "80")
ENGINE = ("192.168.65.7", "2375")
CLOUDFLARE = ("region1.v2.argotunnel.com", "7844")
LOOKUP = ("example.com.", "dns")
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
# Prints "probe: written" or "probe: OSError <errno>" for the path it is given.
WRITE = """
import sys
try:
    with open(sys.argv[1], "w") as f:
        f.write("x")
    print("probe: written")
except OSError as error:
    print("probe:", type(error).__name__, error.errno)
"""
HAS_TOKEN = f"import os; print('probe: token' if os.path.exists('{TOKEN_FILE}') else 'probe: none')"
# A listener on a private address, for the tunnel to try.
LISTEN = (
    "import socket\ns = socket.create_server(('', 7844))\nwhile True:\n    s.accept()[0].close()"
)
REFUSED = "violates PodSecurity"
RESULTS: list[bool] = []


def kubectl(*args: str, stdin: str | None = None, timeout: int = 120) -> str:
    """Run kubectl and return its output and errors together."""
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, *args],
        input=stdin,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return result.stdout + result.stderr


def run_in(namespace: str, pod: str, code: str, *args: str) -> str:
    return answer(
        kubectl("-n", namespace, "exec", pod, "--", "python", "-c", code, *args, timeout=90)
    )


def probe(namespace: str, pod: str, target: tuple[str, str]) -> str:
    return run_in(namespace, pod, CODE, *target)


def report(ok: bool, text: str) -> None:
    print("PASS" if ok else "FAIL", text, flush=True)
    RESULTS.append(ok)


def pod(name: str, namespace: str, labels: dict[str, str], command: list[str]) -> dict[str, Any]:
    """A pod that meets Pod Security "restricted", running the app's image."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "terminationGracePeriodSeconds": 0,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "probe",
                    "image": IMAGE,
                    "imagePullPolicy": "Never",
                    "command": command,
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {"limits": {"cpu": "200m", "memory": "64Mi"}},
                }
            ],
        },
    }


def start_probe_pods() -> None:
    sleep = ["python", "-c", "import time; time.sleep(3600)"]
    inside = pod("probe-inside", NS, {"role": "probe"}, sleep)
    tunnel = pod("probe-tunnel", NS, {"role": "probe", "app": "tunnel"}, sleep)
    # Outside the namespace, with a writable root and the default account's token: the controls.
    outside = pod("probe-outside", "default", {"role": "probe"}, sleep)
    outside["spec"]["containers"][0]["securityContext"]["readOnlyRootFilesystem"] = False
    listener = pod("listener", "default", {"role": "probe"}, ["python", "-c", LISTEN])
    for manifest in [inside, tunnel, outside, listener]:
        kubectl("apply", "-f", "-", stdin=json.dumps(manifest))
    for namespace, name in [
        (NS, "probe-inside"),
        (NS, "probe-tunnel"),
        ("default", "probe-outside"),
    ]:
        kubectl("-n", namespace, "wait", "--for=condition=Ready", f"pod/{name}", "--timeout=120s")
    kubectl("-n", "default", "wait", "--for=condition=Ready", "pod/listener", "--timeout=120s")


def jsonpath(namespace: str, kind: str, path: str, *selector: str) -> str:
    return kubectl("-n", namespace, "get", kind, *selector, "-o", f"jsonpath={path}").strip()


def restore_network() -> None:
    kubectl("-n", NS, "delete", "networkpolicy", "--all")
    kubectl("apply", "-f", str(K8S / "network.yaml"))


def restored(name: str, attacks: Callable[[], list[str]]) -> None:
    """Put the rules back, then run the attacks until they fail again, for up to a minute.

    Rules deleted and re-created in quick succession can take a few seconds to hold again (seen
    12 Nov), so the first tries after a restore may connect. The tries and the seconds from the
    restore to the start of the try that held are reported; the rules must hold within a minute.
    """
    restore_network()
    start = time.monotonic()
    tries = 0
    while True:
        tries += 1
        began = round(time.monotonic() - start)
        again = attacks()
        if all(refused(a) for a in again) or time.monotonic() - start > 60:
            break
    held = all(refused(a) for a in again)
    report(held, f"{name} restored (try {tries}, {began} s after): {again}")


def check_ingress(app_ip: str, service_ip: str) -> None:
    def attacks() -> list[str]:
        return [
            probe(NS, "probe-inside", (app_ip, "8000")),
            probe(NS, "probe-inside", (service_ip, "8000")),
            probe("default", "probe-outside", (app_ip, "8000")),
            probe("default", "probe-outside", (service_ip, "8000")),
        ]

    allowed = probe(NS, "probe-tunnel", (service_ip, "8000"))
    report(allowed.startswith("connected"), f"ingress: the tunnel's pods reach the app ({allowed})")
    found = attacks()
    report(all(refused(a) for a in found), f"ingress: other pods cannot reach the app {found}")
    kubectl("-n", NS, "delete", "networkpolicy", "default-deny", "app-from-tunnel")
    sabotaged = attacks()
    report(
        all(a.startswith("connected") for a in sabotaged),
        f"ingress sabotaged, no policies: {sabotaged}",
    )
    restored("ingress", attacks)


def check_egress(app_pod: str) -> None:
    def attacks() -> list[str]:
        return [probe(NS, app_pod, target) for target in [INTERNET, ENGINE, LOOKUP]]

    found = attacks()
    report(
        all(refused(a) for a in found), f"egress: the app cannot call out or look up names {found}"
    )
    kubectl("-n", NS, "delete", "networkpolicy", "default-deny")
    sabotaged = [probe(NS, app_pod, INTERNET), probe(NS, app_pod, LOOKUP)]
    report(
        all(a.startswith("connected") for a in sabotaged),
        f"egress sabotaged, no default deny: {sabotaged}",
    )
    restored("egress", attacks)


def check_tunnel(listener_ip: str) -> None:
    def attacks() -> list[str]:
        return [probe(NS, "probe-tunnel", WEB), probe(NS, "probe-tunnel", (listener_ip, "7844"))]

    allowed = probe(NS, "probe-tunnel", CLOUDFLARE)
    report(allowed.startswith("connected"), f"tunnel: it reaches Cloudflare on 7844 ({allowed})")
    found = attacks()
    report(all(refused(a) for a in found), f"tunnel: it reaches nothing else {found}")
    open_egress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "tunnel-out", "namespace": NS},
        "spec": {
            "podSelector": {"matchLabels": {"app": "tunnel"}},
            "policyTypes": ["Egress"],
            "egress": [{}],
        },
    }
    kubectl("-n", NS, "delete", "networkpolicy", "tunnel-out")
    kubectl("apply", "-f", "-", stdin=json.dumps(open_egress))
    sabotaged = attacks()
    report(
        all(a.startswith("connected") for a in sabotaged),
        f"tunnel sabotaged, open egress: {sabotaged}",
    )
    restored("tunnel", attacks)


def privileged_pods() -> dict[str, dict[str, Any]]:
    """One pod per forbidden setting, each otherwise allowed, so each refusal has one cause."""
    command = ["python", "-c", "pass"]
    root = pod("test-root", NS, {}, command)
    root["spec"]["securityContext"].update({"runAsNonRoot": False, "runAsUser": 0})
    privileged = pod("test-privileged", NS, {}, command)
    # The API itself refuses privileged with allowPrivilegeEscalation false, so both change.
    privileged["spec"]["containers"][0]["securityContext"].update(
        {"privileged": True, "allowPrivilegeEscalation": True}
    )
    host_path = pod("test-host-path", NS, {}, command)
    host_path["spec"]["volumes"] = [{"name": "host", "hostPath": {"path": "/"}}]
    host_network = pod("test-host-network", NS, {}, command)
    host_network["spec"]["hostNetwork"] = True
    return {
        "root": root,
        "privileged": privileged,
        "host path": host_path,
        "host network": host_network,
    }


def dry_run(manifest: dict[str, Any]) -> str:
    return kubectl("create", "--dry-run=server", "-f", "-", stdin=json.dumps(manifest))


def check_privileges() -> None:
    control = dry_run(pod("test-allowed", NS, {}, ["python", "-c", "pass"]))
    report(
        REFUSED not in control and "created" in control,
        "privileges control: a restricted pod is allowed",
    )
    found = {name: REFUSED in dry_run(m) for name, m in privileged_pods().items()}
    report(all(found.values()), f"privileges: privileged pods are refused {found}")
    label = "pod-security.kubernetes.io/enforce=privileged"
    kubectl("label", "namespace", NS, label, "--overwrite")
    sabotaged = {name: "created" in dry_run(m) for name, m in privileged_pods().items()}
    report(
        all(sabotaged.values()),
        f"privileges sabotaged, namespace set to privileged, accepted: {sabotaged}",
    )
    kubectl("apply", "-f", str(K8S / "namespace.yaml"))
    again = {name: REFUSED in dry_run(m) for name, m in privileged_pods().items()}
    report(all(again.values()), f"privileges restored: {again}")


def check_read_only(app_pod: str) -> None:
    found = {
        path: run_in(NS, app_pod, WRITE, path)
        for path in ["/var/tmp/probe", "/app/probe", "/probe"]
    }
    report(
        all(a == "OSError 30" for a in found.values()),
        f"read-only: the app's disk is read-only {found}",
    )
    allowed = run_in(NS, app_pod, WRITE, "/tmp/probe")
    report(allowed == "written", f"read-only: the app can write its /tmp ({allowed})")
    control = run_in("default", "probe-outside", WRITE, "/var/tmp/probe")
    report(
        control == "written", f"read-only control, no read-only root: /var/tmp written ({control})"
    )


def can_i(account: str, verb: str, resource: str) -> str:
    who = f"system:serviceaccount:{NS}:{account}"
    return kubectl("auth", "can-i", verb, resource, "-n", NS, "--as", who).strip()


def check_api(app_pod: str) -> None:
    token = run_in(NS, app_pod, HAS_TOKEN)
    control = run_in("default", "probe-outside", HAS_TOKEN)
    report(token == "none", f"API: no API token in the app's pod ({token})")
    report(control == "token", f"API control, the default account's pod has one ({control})")
    asks = [
        (a, v, r) for a in ["app", "tunnel"] for v, r in [("get", "secrets"), ("create", "pods")]
    ]
    found = {f"{a} {v} {r}": can_i(a, v, r) for a, v, r in asks}
    report(all(answer == "no" for answer in found.values()), f"API: no API permissions {found}")
    kubectl("-n", NS, "create", "serviceaccount", "test-reader")
    kubectl("-n", NS, "create", "role", "test-reader", "--verb=get", "--resource=secrets")
    binding = ["--role=test-reader", f"--serviceaccount={NS}:test-reader"]
    kubectl("-n", NS, "create", "rolebinding", "test-reader", *binding)
    sabotaged = can_i("test-reader", "get", "secrets")
    report(
        sabotaged == "yes",
        f"API sabotaged, an account with a role can read Secrets ({sabotaged})",
    )
    kubectl("-n", NS, "delete", "rolebinding,role,serviceaccount", "test-reader")


def ready_app() -> tuple[str, str]:
    """The ready app pod's name and node, or empty strings."""
    ready = '{.status.conditions[?(@.type=="Ready")].status}'
    path = "{range .items[*]}{.metadata.name} {.spec.nodeName} " + ready + '{"\\n"}{end}'
    for line in jsonpath(NS, "pods", path, "-l", "app=app").splitlines():
        name, node, ready = (line.split() + ["", "", ""])[:3]
        if ready == "True":
            return name, node
    return "", ""


def check_lost_node() -> None:
    _, node = ready_app()
    print(f"     stopping {node}, which runs the app", flush=True)
    subprocess.run(["docker", "stop", node], capture_output=True, check=True)
    start = time.monotonic()
    moved = ""
    while time.monotonic() - start < 300:
        name, where = ready_app()
        if name and where != node:
            moved = f"{name} on {where}"
            break
        time.sleep(2)
    seconds = round(time.monotonic() - start)
    report(bool(moved), f"lost node: the app is ready on another node after {seconds} s ({moved})")
    subprocess.run(["docker", "start", node], capture_output=True, check=True)
    kubectl("wait", "--for=condition=Ready", f"node/{node}", "--timeout=180s")


def main() -> int:
    start_probe_pods()
    try:
        app_pod, _ = ready_app()
        app_ip = jsonpath(NS, "pod", "{.status.podIP}", app_pod)
        service_ip = jsonpath(NS, "service", "{.spec.clusterIP}", "app")
        listener_ip = jsonpath("default", "pod", "{.status.podIP}", "listener")
        check_ingress(app_ip, service_ip)
        check_egress(app_pod)
        check_tunnel(listener_ip)
        check_privileges()
        check_read_only(app_pod)
        check_api(app_pod)
        if "--node" in sys.argv[1:]:
            check_lost_node()
    finally:
        restore_network()
        kubectl("apply", "-f", str(K8S / "namespace.yaml"))
        kubectl("-n", NS, "delete", "pod", "probe-inside", "probe-tunnel", "--wait=false")
        kubectl("-n", "default", "delete", "pod", "probe-outside", "listener", "--wait=false")
    passed = sum(RESULTS)
    print(f"{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
