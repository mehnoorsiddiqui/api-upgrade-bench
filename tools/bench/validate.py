"""`bench validate`: schema and rule checks over the catalog and every API folder.

Release rules (releases.yaml):
* versions are MAJOR.MINOR.PATCH, unique, and greater than the base;
* the base carries no overlays (it is not listed under `releases`);
* every overlay a release names exists and applies cleanly to the base;
* a release containing a breaking change has a greater major than the base;
* a version in the lock is never removed or edited: it must still be declared with the same
  overlays, and rebuilding it must give the spec hash the lock recorded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator

from .build import spec_sha256
from .config import Bench, BenchError, load_yaml, parse_semver, releases_from_data
from .lock import load_lock


@dataclass
class Problems:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


def _schema(bench: Bench, name: str) -> Draft202012Validator:
    schema = json.loads((bench.root / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _check_schema(validator: Draft202012Validator, data, where: str, problems: Problems) -> bool:
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    for err in errors:
        loc = "/".join(str(p) for p in err.path) or "(root)"
        problems.error(f"{where}: {loc}: {err.message}")
    return not errors


def validate_catalog(bench: Bench, problems: Problems) -> None:
    if not _check_schema(_schema(bench, "catalog"), bench.catalog_data, "catalog/change-types.yaml", problems):
        return
    try:
        bench.catalog  # noqa: B018 - duplicate ids raise here
    except BenchError as exc:
        problems.error(str(exc))


def validate_api(bench: Bench, name: str, problems: Problems) -> None:
    where = f"apis/{name}"
    data = bench.api_data(name)
    if not _check_schema(_schema(bench, "api"), data, f"{where}/api.yaml", problems):
        return
    if data.get("name") != name:
        problems.error(f"{where}/api.yaml: name {data.get('name')!r} must equal the folder name {name!r}")
    api = bench.api(name)
    if api.profile_id is None:
        problems.warn(f"{where}/api.yaml: profile_id is not set; releases of {name} cannot be published yet")
    for path in (api.base_spec_path, api.meta_path):
        if not path.is_file():
            problems.error(f"{where}: missing {path.relative_to(api.dir).as_posix()}")
    if not problems.ok:
        return

    try:
        base_spec = bench.base_spec(api)
    except BenchError as exc:
        problems.error(str(exc))
        return
    info_version = str((base_spec.get("info") or {}).get("version", ""))

    # overlays: schema, catalog membership, and each applies on its own to the base
    overlay_schema = _schema(bench, "overlay")
    catalog = bench.catalog
    good_overlays: set[str] = set()
    for overlay_id in bench.overlay_ids(api):
        rel = f"{where}/overlays/{overlay_id}.yaml"
        doc = load_yaml(api.overlays_dir / f"{overlay_id}.yaml")
        if not _check_schema(overlay_schema, doc, rel, problems):
            continue
        overlay = bench.overlay(api, overlay_id)
        if overlay.type not in catalog.types:
            problems.error(f"{rel}: x-change.type {overlay.type!r} is not in catalog/change-types.yaml")
            continue
        if overlay.breaking != catalog.types[overlay.type].breaking:
            problems.error(
                f"{rel}: x-change.breaking is {overlay.breaking} but change type {overlay.type!r} "
                f"is breaking={catalog.types[overlay.type].breaking} in the catalog"
            )
            continue
        try:
            spec_sha256(bench, api, "0.0.0", (overlay_id,))
        except BenchError as exc:
            problems.error(f"{rel}: does not apply to the base spec: {exc}")
            continue
        good_overlays.add(overlay_id)

    # releases.yaml
    rel_where = f"{where}/releases.yaml"
    releases_data = load_yaml(api.releases_path)
    if not _check_schema(_schema(bench, "releases"), releases_data, rel_where, problems):
        return
    releases = releases_from_data(releases_data, rel_where)
    base = parse_semver(releases.base)
    if info_version and info_version != releases.base:
        problems.warn(f"{where}/spec/openapi.yaml: info.version {info_version} differs from base {releases.base} (build overrides it)")
    seen: set[str] = set()
    for release in releases.releases:
        version = release.version
        if version in seen or version == releases.base:
            problems.error(f"{rel_where}: version {version} is declared more than once (the base counts)")
            continue
        seen.add(version)
        parsed = parse_semver(version)
        if parsed <= base:
            problems.error(f"{rel_where}: {version} must be greater than the base {releases.base}")
        missing = [o for o in release.overlays if o not in bench.overlay_ids(api)]
        if missing:
            problems.error(f"{rel_where}: {version} names unknown overlays: {', '.join(missing)}")
            continue
        broken = [o for o in release.overlays if o not in good_overlays]
        if broken:
            problems.error(f"{rel_where}: {version} uses invalid overlays: {', '.join(broken)}")
            continue
        breaking = [o for o in release.overlays if bench.overlay(api, o).breaking]
        if breaking and parsed[0] <= base[0]:
            problems.error(
                f"{rel_where}: {version} contains breaking changes ({', '.join(breaking)}) "
                f"so its major must be greater than the base's ({base[0]})"
            )
        types = [bench.overlay(api, o).type for o in release.overlays]
        dupes = sorted({t for t in types if types.count(t) > 1})
        if dupes:
            problems.warn(f"{rel_where}: {version} has more than one overlay of type {', '.join(dupes)}")
        try:
            spec_sha256(bench, api, version, release.overlays)
        except BenchError as exc:
            problems.error(f"{rel_where}: {version}: overlays do not compose: {exc}")

    # lock: locked versions are immutable
    try:
        lock = load_lock(api)
    except BenchError as exc:
        problems.error(str(exc))
        return
    for version, entry in lock.items():
        lock_where = f"{where}/releases.lock.yaml: {version}"
        try:
            declared = releases.overlays_for(version)
        except BenchError:
            problems.error(f"{lock_where} was published but is no longer declared in releases.yaml (never remove a released version)")
            continue
        locked_overlays = tuple(entry.get("overlays") or ())
        if declared != locked_overlays:
            problems.error(
                f"{lock_where} was published with overlays {list(locked_overlays)} "
                f"but releases.yaml now says {list(declared)} (never edit a released version)"
            )
            continue
        try:
            sha = spec_sha256(bench, api, version, declared)
        except BenchError as exc:
            problems.error(f"{lock_where}: cannot be rebuilt: {exc}")
            continue
        if entry.get("spec_sha256") and sha != entry["spec_sha256"]:
            problems.error(
                f"{lock_where}: rebuilding gives spec sha256 {sha[:12]}, the lock has {str(entry['spec_sha256'])[:12]}: "
                "the base spec or an overlay it uses was edited after release"
            )


def validate_all(bench: Bench, apis: list[str] | None = None) -> Problems:
    problems = Problems()
    validate_catalog(bench, problems)
    if not problems.ok:
        return problems
    for name in apis or bench.api_names():
        try:
            validate_api(bench, name, problems)
        except BenchError as exc:
            problems.error(str(exc))
    return problems


def print_problems(problems: Problems) -> None:
    for w in problems.warnings:
        print(f"warning: {w}")
    for e in problems.errors:
        print(f"error: {e}")
    print("validate: OK" if problems.ok else f"validate: {len(problems.errors)} error(s)")


def require_valid(bench: Bench, apis: list[str] | None = None) -> None:
    problems = validate_all(bench, apis)
    if not problems.ok:
        print_problems(problems)
        raise BenchError("validation failed")
    for w in problems.warnings:
        print(f"warning: {w}")

