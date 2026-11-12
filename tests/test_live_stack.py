"""The live demo's compose file keeps the app off the host network.

The public link must reach the app only through the outbound tunnel. A published port would let
anyone on the same network reach it directly, around the edge's rate limit.
"""

from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[1] / "compose.yaml"


def test_no_service_is_published_on_the_host() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "ports:" not in text
    assert "network_mode" not in text


def test_both_containers_are_locked_down() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert text.count("read_only: true") == 2
    assert text.count("cap_drop: [ALL]") == 2
    assert text.count("no-new-privileges:true") == 2


def test_only_the_tunnel_can_reach_outside() -> None:
    # The app's one network is internal (no route out); the tunnel alone joins the outside one.
    text = COMPOSE.read_text(encoding="utf-8")
    assert "networks: [inside]\n" in text
    assert "networks: [inside, outside]\n" in text
    assert "  inside:\n    internal: true\n" in text


def test_the_tunnel_image_is_pinned_by_digest() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "cloudflare/cloudflared:2026.10.7@sha256:" in text
