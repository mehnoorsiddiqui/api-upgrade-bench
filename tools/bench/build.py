"""`bench build <api> <version>`: materialise an APIMatic CLI project for one version.

Layout written (always fresh):

    build/<api>/<version>/src/spec/openapi.json        base + the version's overlays, info.version = version
    build/<api>/<version>/src/apimatic.json            apis/<api>/apimatic.json (optional template) + plugin
                                                       identity (version in lockstep with the SDK) + csharp
    build/<api>/<version>/src/spec/APIMATIC-META.json  only if apis/<api>/spec/ has one (optional)

Project settings live in `apimatic.json` (it replaced CLI v1's APIMATIC-BUILD.json). Codegen v4 does
not read `CodeGenSettings`, so the SDK's project name and namespace come from the spec's info.title.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Api, Bench, BenchError, load_json, parse_semver
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
    """`src/apimatic.json` for one version: apis/<api>/apimatic.json (if any) with the per-version parts set.

    Everything in the template is kept (portal settings, plugin author/license/pluginKey, ...), except:
    * plugin.pluginId / pluginName come from api.yaml, and pluginVersion is the release version
      (the plugin is versioned in lockstep with the SDK);
    * languages is exactly {"csharp": {}}: the publishing record must be the one this run's
      `sdk publish` writes, never one copied from elsewhere.
    """
    template = load_json(api.config_template_path) if api.config_template_path.is_file() else {}
    if not isinstance(template, dict):
        raise BenchError(f"{api.config_template_path}: must be a JSON object")
    config = copy.deepcopy(template)
    config["schemaVersion"] = 1
    plugin = config.get("plugin") if isinstance(config.get("plugin"), dict) else {}
    config["plugin"] = {
        **plugin,
        "pluginId": api.plugin_id,
        "pluginName": api.plugin_name,
        "pluginVersion": version,
        "license": plugin.get("license", "MIT"),
    }
    config["languages"] = {"csharp": {}}
    return config


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
    if api.meta_path.is_file():  # optional: import/merge/validation settings only
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
