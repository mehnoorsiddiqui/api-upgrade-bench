"""Thin wrapper over the APIMatic CLI (`@apimatic/cli@2`, v4 codegen)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
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


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def validate(project_dir: Path, key: str | None, fails_on: str = "errors") -> None:
    """`apimatic api validate`. With fails_on="errors" (the default) any error fails, as the CLI's exit
    code says. With "blocking", only the CLI's *Blocking* section does: a real API's spec can carry
    errors the generator handles (Square's recursive CatalogObject is a "circular reference" error)."""
    if fails_on == "errors":
        run([exe(), "api", "validate", "-i", project_dir, *_auth(key)])
        return
    result = run([exe(), "api", "validate", "-i", project_dir, *_auth(key)], check=False, capture=True)
    text = ANSI.sub("", (result.stdout or "") + (result.stderr or ""))
    tail = text[-4000:]
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"  # the CLI draws boxes; a cp1252 console cannot
    print(tail.encode(encoding, "replace").decode(encoding), flush=True)
    if result.returncode == 0:
        return
    # fail on a Blocking section, and on any failure that is not a validation report at all
    # (auth, network): only a report whose worst section is "Errors" is let through
    if "Blocking" in text or "Errors" not in text:
        raise BenchError(f"apimatic api validate failed for {project_dir} (see above)")
    else:
        print("apimatic api validate reported non-blocking errors; continuing (validate_fails_on: blocking)", flush=True)


def sdk_generate(project_dir: Path, destination: Path, key: str | None) -> None:
    run([exe(), "sdk", "generate", "-i", project_dir, "-d", destination, "--language", "csharp", "--force", *_auth(key)])


def sdk_publish(
    project_dir: Path,
    destination: Path,
    profile_id: str,
    version: str,
    *,
    dry_run: bool = False,
    publish_types: tuple[str, ...] = ("package", "sourcecode"),
) -> None:
    args = [
        exe(), "sdk", "publish",
        "-i", project_dir,
        "-d", destination,
        "--profile-id", profile_id,
        "--language", "csharp",
        "--version", version,
        *[arg for kind in publish_types for arg in ("--publish-type", kind)],
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
    # some CLI versions write the SDK into a <language> folder under -d
    for pattern in ("*.slnx", "*.sln", "*/*.slnx", "*/*.sln"):
        found = sorted(sdk_dir.glob(pattern))
        if found:
            return found[0]
    raise BenchError(f"no .slnx/.sln in generated SDK {sdk_dir}")
