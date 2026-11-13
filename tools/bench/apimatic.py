"""Thin wrapper over the APIMatic CLI (`@apimatic/cli@2`, v4 codegen)."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .config import BenchError
from .proc import register_secret, run


def exe() -> str:
    found = os.environ.get("APIMATIC_CLI") or shutil.which("apimatic")
    if not found:
        raise BenchError("apimatic CLI not found: npm i -g @apimatic/cli@2 (or set APIMATIC_CLI)")
    return found


def api_key(required: bool = True) -> str | None:
    key = os.environ.get("APIMATIC_API_KEY")
    if not key and required:
        raise BenchError("APIMATIC_API_KEY is not set")
    register_secret(key)
    return key


def cli_version() -> str:
    out = run([exe(), "--version"], capture=True, quiet=True).stdout.strip()
    match = re.search(r"@apimatic/cli/(\S+)", out)
    return match[1] if match else out


def login(key: str) -> None:
    # `sdk publish` has no --auth-key flag, so CI logs in once per job.
    run([exe(), "auth", "login", f"--auth-key={key}"])


def _auth(key: str | None) -> list[str]:
    """Without a key the CLI uses whatever `apimatic auth login` stored (handy locally)."""
    return [f"--auth-key={key}"] if key else []


def validate(project_dir: Path, key: str | None) -> None:
    run([exe(), "api", "validate", "-i", project_dir, *_auth(key)])


def sdk_generate(project_dir: Path, destination: Path, key: str | None) -> None:
    run([exe(), "sdk", "generate", "-i", project_dir, "-d", destination, "--language", "csharp", "--force", *_auth(key)])


def sdk_publish(project_dir: Path, destination: Path, profile_id: str, version: str, *, dry_run: bool = False) -> None:
    args = [
        exe(), "sdk", "publish",
        "-i", project_dir,
        "-d", destination,
        "--profile-id", profile_id,
        "--language", "csharp",
        "--version", version,
        "--publish-type", "package",
        "--publish-type", "sourcecode",
        "--force",
    ]
    if dry_run:
        args.append("--dry-run")
    run(args)


def plugin_generate(project_dir: Path, destination: Path, key: str | None) -> None:
    run([exe(), "plugin", "generate", "-i", project_dir, "-d", destination, "--force", *_auth(key)])


# --------------------------------------------------------------------------- src/apimatic.json


def _config_path(project_dir: Path) -> Path:
    return project_dir / "src" / "apimatic.json"


def read_config(project_dir: Path) -> dict:
    path = _config_path(project_dir)
    text = path.read_text(encoding="utf-8-sig")
    return json.loads(text)


def publish_record(project_dir: Path) -> dict | None:
    """`languages.csharp.publishing`, which `sdk publish` writes on success."""
    return ((read_config(project_dir).get("languages") or {}).get("csharp") or {}).get("publishing")


def write_publish_record(project_dir: Path, record: dict) -> None:
    """Stand-in for the record of an SDK published by an earlier, interrupted run.

    `plugin generate` reads the published languages' repo URL and package version from here.
    """
    config = read_config(project_dir)
    config.setdefault("languages", {}).setdefault("csharp", {})["publishing"] = record
    _config_path(project_dir).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")


def reconstructed_record(sdk_repo: str, branch: str, package_id: str, version: str) -> dict:
    return {
        "source": {"repositoryUrl": f"https://github.com/{sdk_repo}", "branch": branch},
        "package": {"version": version},
        "packageConfiguration": {"packageId": package_id},
    }


def find_solution(sdk_dir: Path) -> Path:
    for pattern in ("*.slnx", "*.sln"):
        found = sorted(sdk_dir.glob(pattern))
        if found:
            return found[0]
    raise BenchError(f"no .slnx/.sln in generated SDK {sdk_dir}")
