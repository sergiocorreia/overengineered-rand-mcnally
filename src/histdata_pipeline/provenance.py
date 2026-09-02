"""Hashing, atomic publication, and local dependency revision helpers."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")


def git_revision(path: Path) -> dict[str, object]:
    """Return a commit plus a content hash for tracked and untracked changes."""
    resolved = path.resolve()
    try:
        commit = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(resolved), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        diff = subprocess.run(
            ["git", "-C", str(resolved), "diff", "--binary", "HEAD"],
            check=True,
            capture_output=True,
        ).stdout
        untracked = subprocess.run(
            ["git", "-C", str(resolved), "ls-files", "--others", "--exclude-standard", "-z"],
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
    except (OSError, subprocess.CalledProcessError):
        return {"path": str(resolved), "commit": None, "dirty": None, "tree_hash": None}

    dirty_hash = hashlib.sha256()
    dirty_hash.update(diff)
    for raw_name in sorted(name for name in untracked if name):
        relative = Path(os.fsdecode(raw_name))
        candidate = resolved / relative
        dirty_hash.update(raw_name)
        if candidate.is_file():
            dirty_hash.update(hashlib.sha256(candidate.read_bytes()).digest())
    return {
        "path": str(resolved),
        "commit": commit,
        "dirty": bool(status),
        "tree_hash": dirty_hash.hexdigest() if status else None,
    }
