"""`bench build <api> <version>`: materialise an APIMatic CLI project for one version.

Layout written (always fresh):

    build/<api>/<version>/src/spec/openapi.json        base + the version's overlays, info.version = version
    build/<api>/<version>/src/spec/APIMATIC-META.json  copied from apis/<api>/spec/
    build/<api>/<version>/src/apimatic.json            plugin identity (version in lockstep with the SDK) + csharp
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Api, Bench, BenchError, parse_semver
from .overlay import OverlayError, apply_overlays


@dataclass(frozen=True)
class BuildResult:
    api: str
    version: str
    project_dir: Path  # what `apimatic ... -i` takes
    spec_path: Path
    spec_sha256: str
    overlays: tuple[str, ...]
    change_types: tuple[str, ...]

    @property
    def apimatic_json(self) -> Path:
        return self.project_dir / "src" / "apimatic.json"


def spec_bytes(spec: dict) -> bytes:
    """The canonical serialisation; its sha256 is what the lock records."""
    return (json.dumps(spec, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def build_spec(bench: Bench, api: Api, version: str, overlay_ids: tuple[str, ...] | None = None) -> dict:
    parse_semver(version)
    if overlay_ids is None:
        overlay_ids = bench.releases(api).overlays_for(version)
    overlays = [(f"overlays/{oid}.yaml", bench.overlay(api, oid).doc) for oid in overlay_ids]
    try:
        spec = apply_overlays(bench.base_spec(api), overlays)
    except OverlayError as exc:
        raise BenchError(f"apis/{api.name} {version}: {exc}") from exc
    spec.setdefault("info", {})["version"] = version
    return spec


def apimatic_config(api: Api, version: str) -> dict:
    return {
        "schemaVersion": 1,
        "plugin": {
            "pluginId": api.plugin_id,
            "pluginName": api.plugin_name,
            "pluginVersion": version,
            "license": "MIT",
        },
        "languages": {"csharp": {}},
    }


def remove_tree(path: Path) -> None:
    """rmtree that also removes read-only files (git objects in a generated SDK dir, on Windows)."""

    def make_writable_and_retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=make_writable_and_retry)


def write_project(project_dir: Path, api: Api, spec: dict, version: str) -> bytes:
    if project_dir.exists():
        remove_tree(project_dir)
    spec_dir = project_dir / "src" / "spec"
    spec_dir.mkdir(parents=True)
    data = spec_bytes(spec)
    (spec_dir / "openapi.json").write_bytes(data)
    if not api.meta_path.is_file():
        raise BenchError(f"missing {api.meta_path}")
    shutil.copyfile(api.meta_path, spec_dir / "APIMATIC-META.json")
    (project_dir / "src" / "apimatic.json").write_text(
        json.dumps(apimatic_config(api, version), indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return data


def build(
    bench: Bench,
    api_name: str,
    version: str,
    *,
    overlay_ids: tuple[str, ...] | None = None,
    project_dir: Path | None = None,
) -> BuildResult:
    api = bench.api(api_name)
    if overlay_ids is None:
        overlay_ids = bench.releases(api).overlays_for(version)
    spec = build_spec(bench, api, version, overlay_ids)
    project_dir = project_dir or (bench.build_dir / api.name / version)
    data = write_project(project_dir, api, spec, version)
    change_types = tuple(dict.fromkeys(bench.overlay(api, oid).type for oid in overlay_ids))
    return BuildResult(
        api=api.name,
        version=version,
        project_dir=project_dir,
        spec_path=project_dir / "src" / "spec" / "openapi.json",
        spec_sha256=hashlib.sha256(data).hexdigest(),
        overlays=tuple(overlay_ids),
        change_types=change_types,
    )


def spec_sha256(bench: Bench, api: Api, version: str, overlay_ids: tuple[str, ...]) -> str:
    return hashlib.sha256(spec_bytes(build_spec(bench, api, version, overlay_ids))).hexdigest()
