"""The local cluster's manifests keep the lock-down the attack tests rely on.

These read the files only; `scripts/cluster_check.py` attacks the running cluster.
"""

from pathlib import Path

K8S = Path(__file__).resolve().parents[1] / "k8s"
MANIFESTS = ["namespace.yaml", "app.yaml", "tunnel.yaml", "network.yaml"]


def text(name: str) -> str:
    return (K8S / name).read_text(encoding="utf-8")


def test_nothing_opens_a_way_in_except_the_tunnel() -> None:
    every = "".join(text(name) for name in [*MANIFESTS, "kind.yaml"])
    for word in ["hostPort", "NodePort", "LoadBalancer", "kind: Ingress", "extraPortMappings"]:
        assert word not in every
    for word in ["hostNetwork", "hostPID", "hostIPC", "hostPath", "privileged"]:
        assert word not in every


def test_every_image_is_pinned_or_local() -> None:
    for name in [*MANIFESTS, "kind.yaml"]:
        for line in text(name).splitlines():
            if line.strip().startswith("image:") and "homecare:live" not in line:
                assert "@sha256:" in line, line
    # The app's image is never pulled: it is the hash-locked build, loaded into the nodes.
    assert "imagePullPolicy: Never" in text("app.yaml")


def test_both_workloads_are_locked_down() -> None:
    for name in ["app.yaml", "tunnel.yaml"]:
        manifest = text(name)
        assert manifest.count("automountServiceAccountToken: false") == 2
        assert "runAsNonRoot: true" in manifest
        assert "allowPrivilegeEscalation: false" in manifest
        assert "readOnlyRootFilesystem: true" in manifest
        assert "drop: [ALL]" in manifest
        assert "type: RuntimeDefault" in manifest
        assert "limits:" in manifest


def test_the_namespace_enforces_restricted_pod_security() -> None:
    assert "pod-security.kubernetes.io/enforce: restricted\n" in text("namespace.yaml")


def test_traffic_is_denied_unless_a_rule_opens_it() -> None:
    rules = text("network.yaml")
    assert "  podSelector: {}\n  policyTypes: [Ingress, Egress]\n" in rules
    # The app has an inbound rule only; no rule lets it send anything.
    assert rules.count("policyTypes: [Egress]") == 1
    assert "name: tunnel-out" in rules
