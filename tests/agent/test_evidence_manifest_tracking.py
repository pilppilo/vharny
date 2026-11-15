"""Evidence manifests must list files that exist and travel with git."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence"


def _path_values(value: object) -> set[str]:
    """Collect bundle-relative path strings from a manifest value."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        path = value.get("path")
        if isinstance(path, str):
            return {path}
    return set()


def _listed_paths(manifest: dict[str, object]) -> set[str]:
    """Collect every bundle-relative path a manifest lists."""
    paths: set[str] = set()
    files = manifest.get("files")
    if isinstance(files, dict):
        paths.update(str(name) for name in files)
    derived = manifest.get("derived_files")
    if isinstance(derived, dict):
        paths.update(value for value in derived.values() if isinstance(value, str))
    for key in ("journal", "artifacts"):
        paths.update(_path_values(manifest.get(key)))
    for key in ("journals", "artifact_stores"):
        entries = manifest.get(key)
        if isinstance(entries, dict):
            for entry in entries.values():
                paths.update(_path_values(entry))
    return paths


def _tracked_files() -> set[str]:
    """Return every repository-relative path git currently tracks."""
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return set(completed.stdout.split("\0"))


def _tracks(path: str, tracked: set[str]) -> bool:
    """Return whether git tracks the path or any file beneath it."""
    prefix = path + "/"
    return any(entry == path or entry.startswith(prefix) for entry in tracked)


def test_evidence_manifest_files_exist_and_are_tracked() -> None:
    """No manifest may silently reference a missing or untracked path."""
    tracked = _tracked_files()
    problems: list[str] = []
    for manifest_path in sorted(EVIDENCE.glob("**/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            problems.append(f"{manifest_path}: not a JSON object")
            continue
        for relative in sorted(_listed_paths(manifest)):
            path = manifest_path.parent / relative
            repo_relative = path.relative_to(ROOT).as_posix()
            if not path.exists():
                problems.append(f"{repo_relative}: missing")
            elif path.is_dir() and not _tracks(repo_relative, tracked):
                problems.append(f"{repo_relative}: no tracked files")
            elif not path.is_dir() and not _tracks(repo_relative, tracked):
                problems.append(f"{repo_relative}: not tracked by git")
    assert not problems, "\n".join(problems)
