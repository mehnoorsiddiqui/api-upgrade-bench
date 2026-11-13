"""releases.lock.yaml: what CI actually published for each version. Written by CI only."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import Api, BenchError, Releases, load_yaml

HEADER = (
    "# Written by CI (`bench release`). Do not edit by hand.\n"
    "# One entry per published version: what it contains and where it was published.\n"
)


def load_lock(api: Api) -> dict[str, dict]:
    """Version -> entry. A missing or empty lock file has no entries."""
    if not api.lock_path.is_file():
        return {}
    data = load_yaml(api.lock_path) or {}
    releases = data.get("releases") or {}
    if not isinstance(releases, dict):
        raise BenchError(f"{api.lock_path}: 'releases' must be a mapping of version -> entry")
    return {str(version): entry for version, entry in releases.items()}


def save_lock(api: Api, entries: dict[str, dict]) -> None:
    body = yaml.safe_dump({"releases": entries}, sort_keys=False, allow_unicode=True, width=120)
    api.lock_path.write_text(HEADER + body, encoding="utf-8", newline="\n")


def pending_versions(releases: Releases, lock: dict[str, dict]) -> list[str]:
    """Declared versions not yet in the lock: the base first, then file order."""
    return [v for v in releases.versions() if v not in lock]


def workflow_run_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def make_entry(
    *,
    overlays: list[str],
    spec_sha256: str,
    change_types: list[str],
    oasdiff_checks: list[str],
    cli_version: str,
    sdk: dict[str, Any],
    plugin: dict[str, Any],
    generator_drift: bool,
    publish_record: str,
) -> dict[str, Any]:
    return {
        "overlays": list(overlays),
        "spec_sha256": spec_sha256,
        "change_types": list(change_types),
        "oasdiff_checks": list(oasdiff_checks),
        "cli_version": cli_version,
        "sdk": sdk,
        "plugin": plugin,
        "generator_drift": generator_drift,
        "publish_record": publish_record,
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "workflow_run_url": workflow_run_url(),
    }


def append_entry(api: Api, version: str, entry: dict[str, Any]) -> None:
    entries = load_lock(api)
    if version in entries:
        raise BenchError(f"{api.lock_path.name} already has {version}; a locked version is never rewritten")
    entries[version] = entry
    save_lock(api, entries)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)


def commit_lock(root: Path, api: Api, version: str, push: bool = True, attempts: int = 5) -> None:
    """Commit the lock file to the bench repo (`[skip ci]`) and push, rebasing over concurrent pushes."""
    rel = api.lock_path.relative_to(root).as_posix()
    for args in (
        ("config", "user.name", os.environ.get("BENCH_GIT_NAME", "github-actions[bot]")),
        ("config", "user.email", os.environ.get("BENCH_GIT_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com")),
    ):
        _git(root, *args)
    _git(root, "add", rel)
    result = _git(root, "commit", "-m", f"lock: {api.name} {version} [skip ci]")
    if result.returncode != 0 and "nothing to commit" not in result.stdout + result.stderr:
        raise BenchError(f"git commit of {rel} failed:\n{result.stdout}{result.stderr}")
    if not push:
        return
    for attempt in range(1, attempts + 1):
        pushed = _git(root, "push", "origin", "HEAD")
        if pushed.returncode == 0:
            return
        print(f"push of {rel} rejected (attempt {attempt}/{attempts}); rebasing", flush=True)
        rebased = _git(root, "pull", "--rebase", "origin", _current_branch(root))
        if rebased.returncode != 0:
            raise BenchError(f"rebase before pushing {rel} failed:\n{rebased.stdout}{rebased.stderr}")
        time.sleep(2 * attempt)
    raise BenchError(f"could not push {rel} after {attempts} attempts")


def _current_branch(root: Path) -> str:
    out = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return out if out and out != "HEAD" else os.environ.get("GITHUB_REF_NAME", "main")
