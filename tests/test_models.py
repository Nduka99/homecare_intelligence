"""A swapped model file is refused before it is served."""

import hashlib
from pathlib import Path

from homecare import models


def entry_for(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {"file": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def test_an_unchanged_file_passes(tmp_path: Path) -> None:
    (tmp_path / "m.gguf").write_bytes(b"weights")
    assert models.problems([entry_for(tmp_path / "m.gguf")], tmp_path, quick=False) == []


def test_a_same_size_swap_is_caught_by_the_hash_not_the_size(tmp_path: Path) -> None:
    (tmp_path / "m.gguf").write_bytes(b"weights")
    pinned = entry_for(tmp_path / "m.gguf")
    (tmp_path / "m.gguf").write_bytes(b"WEIGHTS")
    assert models.problems([pinned], tmp_path, quick=True) == []  # the size check alone misses it
    assert models.problems([pinned], tmp_path, quick=False) == ["SHA-256 differs: m.gguf"]


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    pinned = {"file": "gone.gguf", "bytes": 1, "sha256": "0" * 64}
    assert models.problems([pinned], tmp_path, quick=False) == ["missing: gone.gguf"]


def test_a_changed_file_inside_a_checkpoint_folder_is_caught(tmp_path: Path) -> None:
    (tmp_path / "enc").mkdir()
    (tmp_path / "enc" / "model.safetensors").write_bytes(b"weights")
    (tmp_path / "enc" / "tokenizer.json").write_bytes(b"{}")
    files = {}
    for name in ("model.safetensors", "tokenizer.json"):
        pin = entry_for(tmp_path / "enc" / name)
        files[name] = {"bytes": pin["bytes"], "sha256": pin["sha256"]}
    checkpoint = {"folder": "enc", "files": files}
    assert models.problems([checkpoint], tmp_path, quick=False) == []
    (tmp_path / "enc" / "tokenizer.json").write_bytes(b"[]")
    assert models.problems([checkpoint], tmp_path, quick=False) == [
        "SHA-256 differs: enc/tokenizer.json"
    ]


def test_every_pinned_model_names_its_licence_and_role() -> None:
    for item in models.read():
        assert item["licence"] and item["role"]
        for pin in models.pinned_files(item):
            assert len(pin["sha256"]) == 64
