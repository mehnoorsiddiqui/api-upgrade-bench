"""Loading the bench repo's declarative files: catalog, api.yaml, overlays, releases, lock."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

# libyaml when available: a real API's base spec and overlays run to megabytes
YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class BenchError(Exception):
    """A problem the user must fix; printed without a traceback."""


def parse_semver(version: str) -> tuple[int, int, int]:
    match = SEMVER.match(str(version))
    if not match:
        raise BenchError(f"{version!r} is not a MAJOR.MINOR.PATCH version")
    return int(match[1]), int(match[2]), int(match[3])


def load_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.load(fh, Loader=YAML_LOADER)  # noqa: S506 - a safe loader
    except FileNotFoundError as exc:
        raise BenchError(f"missing file: {path}") from exc
    except yaml.YAMLError as exc:
        raise BenchError(f"{path}: invalid YAML: {exc}") from exc


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BenchError(f"{path}: invalid JSON: {exc}") from exc


def find_root(start: Path | None = None) -> Path:
    env = os.environ.get("BENCH_ROOT")
    if env:
        return Path(env).resolve()
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "catalog" / "change-types.yaml").is_file():
            return candidate
    raise BenchError("not inside the bench repo (no catalog/change-types.yaml above the current directory)")


# --------------------------------------------------------------------------- catalog


@dataclass(frozen=True)
class ChangeType:
    id: str
    category: str
    breaking: bool
    description: str
    required: tuple[tuple[str, ...], ...]  # each group: one of these ids must be detected
    allowed: frozenset[str]  # required ids plus optional extras

    @property
    def all_ids(self) -> frozenset[str]:
        return self.allowed | {i for group in self.required for i in group}


@dataclass(frozen=True)
class Catalog:
    types: dict[str, ChangeType]
    ignored_checks: frozenset[str]

    def get(self, type_id: str) -> ChangeType:
        if type_id not in self.types:
            raise BenchError(f"unknown change type {type_id!r} (not in catalog/change-types.yaml)")
        return self.types[type_id]


def catalog_from_data(data: Any) -> Catalog:
    types: dict[str, ChangeType] = {}
    for entry in (data or {}).get("change_types") or []:
        oasdiff = entry.get("oasdiff") or {}
        required = tuple(
            tuple(group) if isinstance(group, list) else (group,) for group in oasdiff.get("required") or []
        )
        type_id = entry["id"]
        if type_id in types:
            raise BenchError(f"catalog: duplicate change type id {type_id!r}")
        types[type_id] = ChangeType(
            id=type_id,
            category=entry.get("category", ""),
            breaking=bool(entry["breaking"]),
            description=entry.get("description", ""),
            required=required,
            allowed=frozenset(oasdiff.get("allowed") or []),
        )
    return Catalog(types=types, ignored_checks=frozenset((data or {}).get("ignored_checks") or []))


# --------------------------------------------------------------------------- api


@dataclass(frozen=True)
class Api:
    name: str
    dir: Path
    profile_id: str | None
    nuget_package_id: str
    sdk_repo: str
    plugin_repo: str
    plugin_id: str
    plugin_name: str
    sdk_branch: str = "main"
    plugin_branch: str = "main"
    drift_ignore: tuple[str, ...] = ()
    # Who pushes the NuGet package: the APIMatic publishing profile (default), or a workflow in the
    # SDK repo that bench dispatches with the version after APIMatic publishes the source.
    nuget_publisher: str = "apimatic"
    nuget_workflow: str = "publish-nuget.yml"
    # what makes `apimatic api validate` fail a check or release: any error, or only blocking ones
    validate_fails_on: str = "errors"

    @property
    def publish_types(self) -> tuple[str, ...]:
        """What `apimatic sdk publish` publishes for this API."""
        return ("sourcecode",) if self.nuget_publisher == "sdk-repo-workflow" else ("package", "sourcecode")

    @property
    def base_spec_path(self) -> Path:
        return self.dir / "spec" / "openapi.yaml"

    @property
    def meta_path(self) -> Path:
        return self.dir / "spec" / "APIMATIC-META.json"

    @property
    def config_template_path(self) -> Path:
        """Optional `apimatic.json` template; `bench build` fills in `plugin` and `languages` per version."""
        return self.dir / "apimatic.json"

    @property
    def overlays_dir(self) -> Path:
        return self.dir / "overlays"

    @property
    def releases_path(self) -> Path:
        return self.dir / "releases.yaml"

    @property
    def lock_path(self) -> Path:
        return self.dir / "releases.lock.yaml"


# --------------------------------------------------------------------------- overlays / releases


@dataclass(frozen=True)
class Overlay:
    id: str
    path: Path
    doc: dict
    type: str
    breaking: bool
    targets: tuple[str, ...]
    # Overlays that must be applied before this one (`x-change.requires`). A star-topology version
    # never needs it; a version history imported from a real API does, because a later step can
    # change what an earlier step added.
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class Release:
    version: str
    overlays: tuple[str, ...]


@dataclass(frozen=True)
class Releases:
    base: str
    releases: tuple[Release, ...]

    def versions(self) -> list[str]:
        """The base first, then the releases in file order."""
        return [self.base, *(r.version for r in self.releases)]

    def overlays_for(self, version: str) -> tuple[str, ...]:
        if version == self.base:
            return ()
        for release in self.releases:
            if release.version == version:
                return release.overlays
        raise BenchError(f"version {version} is not declared in releases.yaml")


def releases_from_data(data: Any, where: str = "releases.yaml") -> Releases:
    if not isinstance(data, dict) or "base" not in data:
        raise BenchError(f"{where}: needs a 'base' version")
    releases = []
    for i, entry in enumerate(data.get("releases") or []):
        if not isinstance(entry, dict) or "version" not in entry:
            raise BenchError(f"{where}: releases[{i}] needs a 'version'")
        overlays = entry.get("overlays") or []
        if not isinstance(overlays, list):
            raise BenchError(f"{where}: releases[{i}].overlays must be a list")
        releases.append(Release(version=str(entry["version"]), overlays=tuple(str(o) for o in overlays)))
    return Releases(base=str(data["base"]), releases=tuple(releases))


# --------------------------------------------------------------------------- the repo


@dataclass
class Bench:
    root: Path
    _overlay_cache: dict = field(default_factory=dict, repr=False)
    _base_cache: dict = field(default_factory=dict, repr=False)
    _built_cache: dict = field(default_factory=dict, repr=False)  # (api, overlay ids) -> spec

    @classmethod
    def discover(cls) -> "Bench":
        return cls(find_root())

    @property
    def build_dir(self) -> Path:
        return Path(os.environ.get("BENCH_BUILD_DIR", self.root / "build"))

    @cached_property
    def catalog_data(self) -> Any:
        return load_yaml(self.root / "catalog" / "change-types.yaml")

    @cached_property
    def catalog(self) -> Catalog:
        return catalog_from_data(self.catalog_data)

    def api_names(self) -> list[str]:
        apis_dir = self.root / "apis"
        if not apis_dir.is_dir():
            return []
        return sorted(p.name for p in apis_dir.iterdir() if (p / "api.yaml").is_file())

    def api_data(self, name: str) -> Any:
        return load_yaml(self.root / "apis" / name / "api.yaml")

    def api(self, name: str) -> Api:
        api_dir = self.root / "apis" / name
        if not (api_dir / "api.yaml").is_file():
            raise BenchError(f"unknown api {name!r} (no apis/{name}/api.yaml)")
        data = self.api_data(name) or {}
        try:
            return Api(
                name=name,
                dir=api_dir,
                profile_id=data.get("profile_id") or None,
                nuget_package_id=data["nuget_package_id"],
                sdk_repo=data["sdk_repo"],
                plugin_repo=data["plugin_repo"],
                plugin_id=data["plugin_id"],
                plugin_name=data["plugin_name"],
                sdk_branch=data.get("sdk_branch", "main"),
                plugin_branch=data.get("plugin_branch", "main"),
                drift_ignore=tuple(data.get("drift_ignore") or ()),
                nuget_publisher=data.get("nuget_publisher", "apimatic"),
                nuget_workflow=data.get("nuget_workflow", "publish-nuget.yml"),
                validate_fails_on=data.get("validate_fails_on", "errors"),
            )
        except KeyError as exc:
            raise BenchError(f"apis/{name}/api.yaml: missing {exc.args[0]!r}") from exc

    def overlay_ids(self, api: Api) -> list[str]:
        if not api.overlays_dir.is_dir():
            return []
        return sorted(p.stem for p in api.overlays_dir.glob("*.yaml"))

    def overlay(self, api: Api, overlay_id: str) -> Overlay:
        key = (api.name, overlay_id)
        if key in self._overlay_cache:
            return self._overlay_cache[key]
        path = api.overlays_dir / f"{overlay_id}.yaml"
        if not path.is_file():
            raise BenchError(f"apis/{api.name}: overlay {overlay_id!r} not found ({path.name})")
        doc = load_yaml(path)
        change = (doc or {}).get("x-change") or {}
        overlay = Overlay(
            id=overlay_id,
            path=path,
            doc=doc,
            type=str(change.get("type", "")),
            breaking=bool(change.get("breaking", False)),
            targets=tuple(change.get("targets") or ()),
            requires=tuple(str(r) for r in change.get("requires") or ()),
        )
        self._overlay_cache[key] = overlay
        return overlay

    def prerequisites(self, api: Api, overlay_id: str) -> tuple[str, ...]:
        """Every overlay `overlay_id` needs (`x-change.requires`, transitively), in apply order:
        each one after everything it requires itself. `overlay_id` is not included."""
        order: list[str] = []
        visiting: list[str] = []

        def visit(oid: str) -> None:
            if oid in order:
                return
            if oid in visiting:
                cycle = " -> ".join(visiting[visiting.index(oid):] + [oid])
                raise BenchError(f"apis/{api.name}: overlays require each other in a cycle: {cycle}")
            visiting.append(oid)
            for dep in self.overlay(api, oid).requires:
                visit(dep)
            visiting.pop()
            order.append(oid)

        visit(overlay_id)
        return tuple(order[:-1])

    def releases(self, api: Api) -> Releases:
        return releases_from_data(load_yaml(api.releases_path), f"apis/{api.name}/releases.yaml")

    def base_spec(self, api: Api) -> dict:
        """The parsed base spec, read once per Bench. Shared: callers must not modify it."""
        if api.name not in self._base_cache:
            spec = load_yaml(api.base_spec_path)
            if not isinstance(spec, dict) or "openapi" not in spec:
                raise BenchError(f"{api.base_spec_path}: not an OpenAPI document")
            self._base_cache[api.name] = spec
        return self._base_cache[api.name]
