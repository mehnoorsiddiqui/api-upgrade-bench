"""`bench validate`: schema and rule checks over the catalog and every API folder.

Release rules (releases.yaml):
* versions are MAJOR.MINOR.PATCH, unique, and greater than the base;
* the base carries no overlays (it is not listed under `releases`);
* every overlay a release names exists and applies cleanly to the base, after the overlays it
  requires (`x-change.requires`), which the release must list before it;
* a release containing a breaking change has a greater major than the base;
* a version in the lock is never removed or edited: it must still be declared with the same
  overlays, and rebuilding it must give the spec hash the lock recorded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from jsonschema import Draft7Validator, Draft202012Validator

from .build import apimatic_config, spec_sha256
from .config import Api, Bench, BenchError, load_json, load_yaml, parse_semver, releases_from_data
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


def validate_config_template(bench: Bench, api: Api, problems: Problems) -> None:
    """apis/<api>/apimatic.json is optional; bench owns its plugin identity, version and languages."""
    path = api.config_template_path
    if not path.is_file():
        return
    where = f"apis/{api.name}/apimatic.json"
    try:
        template = load_json(path)
    except BenchError as exc:
        problems.error(str(exc))
        return
    if not isinstance(template, dict):
        problems.error(f"{where}: must be a JSON object")
        return

    plugin = template.get("plugin") or {}
    for key, expected in (("pluginId", api.plugin_id), ("pluginName", api.plugin_name)):
        if key in plugin and plugin[key] != expected:
            problems.error(f"{where}: plugin.{key} is {plugin[key]!r} but api.yaml says {expected!r} (api.yaml wins; make them agree or drop it here)")
    if "pluginVersion" in plugin:
        problems.warn(f"{where}: plugin.pluginVersion is ignored; each release sets it to its own version")
    for language, entry in (template.get("languages") or {}).items():
        if language != "csharp":
            problems.error(f"{where}: languages.{language}: the bench publishes C# only")
        elif isinstance(entry, dict) and "publishing" in entry:
            problems.error(
                f"{where}: languages.csharp.publishing must not be in the template: `sdk publish` writes it for each "
                "release, and a copied record would point `plugin generate` at another SDK"
            )

    # what the CLI will actually read: the template with the per-version parts filled in
    config = apimatic_config(api, bench.releases(api).base)
    validator = Draft7Validator(json.loads((bench.root / "schemas" / "apimatic.schema.json").read_text(encoding="utf-8")))
    for err in sorted(validator.iter_errors(config), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "(root)"
        problems.error(f"{where}: {loc}: {err.message} (checked against the APIMatic CLI's apimatic.schema.json)")


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
    if not api.base_spec_path.is_file():
        problems.error(f"{where}: missing spec/openapi.yaml")
    if api.meta_path.is_file():
        meta = load_json(api.meta_path)
        if isinstance(meta, dict) and ("CodeGenSettings" in meta or "TestGenSettings" in meta):
            problems.warn(
                f"{where}/spec/APIMATIC-META.json: CodeGenSettings/TestGenSettings are ignored by Codegen v4 "
                "(the SDK name comes from info.title)"
            )
    validate_config_template(bench, api, problems)
    if not problems.ok:
        return

    try:
        base_spec = bench.base_spec(api)
    except BenchError as exc:
        problems.error(str(exc))
        return
    info_version = str((base_spec.get("info") or {}).get("version", ""))

    # overlays: schema, catalog membership, and each applies to the base on its own, or after
    # the overlays it requires
    overlay_schema = _schema(bench, "overlay")
    catalog = bench.catalog
    good_overlays: set[str] = set()
    known_overlays = set(bench.overlay_ids(api))
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
        unknown = [r for r in overlay.requires if r not in known_overlays]
        if unknown:
            problems.error(f"{rel}: x-change.requires names unknown overlays: {', '.join(unknown)}")
            continue
        try:
            prerequisites = bench.prerequisites(api, overlay_id)
        except BenchError as exc:
            problems.error(f"{rel}: {exc}")
            continue
        try:
            spec_sha256(bench, api, "0.0.0", prerequisites + (overlay_id,))
        except BenchError as exc:
            after = f" after the {len(prerequisites)} overlay(s) it requires" if prerequisites else ""
            problems.error(f"{rel}: does not apply to the base spec{after}: {exc}")
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
        position = {o: i for i, o in enumerate(release.overlays)}
        out_of_order = [
            f"{o} (needs {r})"
            for o in release.overlays
            for r in bench.prerequisites(api, o)
            if position.get(r, len(release.overlays)) > position[o]
        ]
        if out_of_order:
            problems.error(f"{rel_where}: {version} must list what an overlay requires before it: {'; '.join(out_of_order[:5])}")
            continue
        breaking = [o for o in release.overlays if bench.overlay(api, o).breaking]
        if breaking and parsed[0] <= base[0]:
            problems.error(
                f"{rel_where}: {version} contains breaking changes ({', '.join(breaking)}) "
                f"so its major must be greater than the base's ({base[0]})"
            )
        # An imported history (overlays with `requires`) has one overlay per type per step by design.
        types = [bench.overlay(api, o).type for o in release.overlays if not bench.overlay(api, o).requires]
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
        if entry.get("adopted"):
            # Published before/outside the bench (e.g. an existing SDK the bench took over): it is in
            # the lock only so it is never published again, and nothing about it can be rebuilt.
            if version != releases.base and any(r.version == version for r in releases.releases):
                problems.error(f"{lock_where} is an adopted (already published) version; don't declare it under releases")
            continue
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

