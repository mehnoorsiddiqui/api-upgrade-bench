"""`bench release <api>`: publish every declared version that is not in the lock yet, serially.

Per version:
  1. build + diffcheck                      (fails on unlabelled or missing changes)
  2. apimatic api validate
  3. apimatic auth login                    (once per run; `sdk publish` has no --auth-key)
  4. apimatic sdk publish (package + sourcecode) into a fresh, empty SDK dir
  5. verify the publishing record in src/apimatic.json, the SDK repo tag v<version>
     (created on the published commit if APIMatic did not), and wait for NuGet
  6. generator drift                        (once per run, before anything is published)
  7. apimatic plugin generate
  8. push the plugin, tag v<version>; append to releases.lock.yaml and commit it ([skip ci])

Idempotent: a version already in the lock is skipped; within a version, a step whose result
already exists (NuGet version, SDK tag, plugin tag) is not repeated, so re-running after a
partial failure completes the release instead of publishing twice. A version already on NuGet
but without its SDK tag (APIMatic published the package but not the source) gets a source-only
publish.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import apimatic
from .build import BuildResult, build
from .config import Api, Bench, BenchError
from .diffcheck import DiffResult, diffcheck
from .drift import DriftResult, check_drift
from .lock import append_entry, commit_lock, load_lock, make_entry, pending_versions
from .nuget_wait import has_version, wait_for_version
from .plugin_push import push_plugin
from .proc import (
    create_remote_tag,
    dispatch_workflow,
    github_token,
    remote_branch_commit,
    remote_tag_commit,
    run,
    wait_for_tag,
)
from .validate import require_valid


@dataclass
class ReleaseOptions:
    version: str | None = None
    allow_drift: bool = False
    commit_lock: bool = False
    push_lock: bool = True
    dry_run: bool = False
    # Off by default: a successful publish already put the package on nuget.org, and nothing later in
    # the release needs it to be listed (indexing takes minutes per version). Turn it on when whatever
    # runs after the release must restore the package straight away.
    wait_for_nuget: bool = False
    nuget_timeout_s: int = 1800


def _banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def build_and_diffcheck(bench: Bench, api: Api, version: str) -> tuple[BuildResult, DiffResult]:
    result = build(bench, api.name, version)
    print(f"built {result.spec_path} (sha256 {result.spec_sha256[:12]}, overlays: {', '.join(result.overlays) or 'none'})")
    diff = diffcheck(api.base_spec_path, result.spec_path, list(result.change_types), bench.catalog)
    print(diff.report(), flush=True)
    if not diff.ok:
        raise BenchError(f"{api.name} {version}: the spec diff does not match the declared change types")
    return result, diff


def compile_check(project_dir: Path, key: str | None) -> None:
    """Generate the SDK (nothing published) and make sure it compiles."""
    sdk_dir = project_dir / "sdk-check" / "csharp"
    apimatic.sdk_generate(project_dir, sdk_dir, key)
    run(["dotnet", "build", apimatic.find_solution(sdk_dir), "-c", "Release", "--nologo"])


def _same_repo(url: str | None, repo: str) -> bool:
    if not url:
        return False
    return url.lower().removesuffix(".git").rstrip("/").endswith("/" + repo.lower())


def _publish_sdk(
    api: Api, b: BuildResult, profile_id: str, token: str | None, nuget_timeout_s: int, wait_for_nuget: bool = False
) -> tuple[dict, str]:
    """Publish (or recognise an earlier publish of) the SDK. Returns (sdk lock entry, publish_record kind)."""
    version, tag = b.version, f"v{b.version}"
    sdk_dir = b.project_dir / "sdk" / "csharp"
    on_nuget = has_version(api.nuget_package_id, version)
    sdk_commit = remote_tag_commit(api.sdk_repo, tag, token)

    if sdk_commit:
        print(
            f"SDK {version} was already published (NuGet: {'yes' if on_nuget else 'no'}, "
            f"{api.sdk_repo}@{tag}: yes); not publishing again",
            flush=True,
        )
        apimatic.write_publish_record(
            b.project_dir, apimatic.reconstructed_record(api.sdk_repo, api.sdk_branch, api.nuget_package_id, version)
        )
        record_kind = "reconstructed"
    else:
        by_workflow = api.nuget_publisher == "sdk-repo-workflow"
        # On NuGet but no tag: an earlier publish got the package out but not the source (APIMatic
        # reports the two targets separately). NuGet versions are permanent, so publish the source only.
        source_only = on_nuget or by_workflow
        if on_nuget:
            print(
                f"{api.nuget_package_id} {version} is already on NuGet but {api.sdk_repo} has no {tag}: "
                "publishing the source code only",
                flush=True,
            )
        apimatic.sdk_publish(
            b.project_dir, sdk_dir, profile_id, version, publish_types=("sourcecode",) if source_only else api.publish_types
        )
        record = apimatic.publish_record(b.project_dir) or {}
        if source_only:
            # A source-only publish records no package version (the package is already on NuGet, or the
            # SDK repo's workflow publishes it). Record it ourselves so `plugin generate` sees it.
            record.setdefault("packageConfiguration", {}).setdefault("packageId", api.nuget_package_id)
            record["package"] = {"version": version}
            apimatic.write_publish_record(b.project_dir, record)
        published_version = (record.get("package") or {}).get("version")
        if published_version != version:
            raise BenchError(
                f"after sdk publish, src/apimatic.json records package version {published_version!r}, expected {version!r}"
            )
        source_url = (record.get("source") or {}).get("repositoryUrl")
        if not _same_repo(source_url, api.sdk_repo):
            raise BenchError(
                f"the publishing profile pushed the source to {source_url!r}, but api.yaml says sdk_repo is {api.sdk_repo!r}"
            )
        package_id = (record.get("packageConfiguration") or {}).get("packageId")
        if package_id and package_id.lower() != api.nuget_package_id.lower():
            raise BenchError(
                f"the publishing profile published package {package_id!r}, but api.yaml says {api.nuget_package_id!r}"
            )
        record_kind = "published"
        if by_workflow and not on_nuget:
            # The workflow builds the commit APIMatic just pushed, publishes `version`, then tags it.
            dispatch_workflow(api.sdk_repo, api.nuget_workflow, api.sdk_branch, {"version": version}, token)
            # the workflow tags only after its NuGet push succeeded, so the tag is the proof it published
            sdk_commit = wait_for_tag(api.sdk_repo, tag, token, timeout_s=600)
        else:
            sdk_commit = remote_tag_commit(api.sdk_repo, tag, token)
        if not sdk_commit:
            branch = (record.get("source") or {}).get("branch") or api.sdk_branch
            head = remote_branch_commit(api.sdk_repo, branch, token)
            if not head:
                raise BenchError(f"{api.sdk_repo} has neither tag {tag} nor branch {branch} after publishing")
            print(f"{api.sdk_repo} has no {tag}; tagging the commit just published to {branch} ({head[:12]})", flush=True)
            create_remote_tag(api.sdk_repo, tag, head, token)
            sdk_commit = head

    if wait_for_nuget and not on_nuget:
        wait_for_version(api.nuget_package_id, version, timeout_s=nuget_timeout_s)

    return {
        "package_id": api.nuget_package_id,
        "version": version,
        "repo": api.sdk_repo,
        "tag": tag,
        "commit": sdk_commit,
    }, record_kind


def _publish_plugin(api: Api, b: BuildResult, key: str, token: str | None) -> dict:
    version, tag = b.version, f"v{b.version}"
    existing = remote_tag_commit(api.plugin_repo, tag, token)
    if existing:
        print(f"plugin {version} already published ({api.plugin_repo}@{tag}); not generating again", flush=True)
        commit = existing
    else:
        plugin_dir = b.project_dir / "plugin"
        # the SDK is tagged by now: make the plugin describe that tag, not the profile's moving branch
        apimatic.pin_source_ref(b.project_dir, tag)
        apimatic.plugin_generate(b.project_dir, plugin_dir, key)
        pushed = push_plugin(
            plugin_dir,
            repo=api.plugin_repo,
            plugin_id=api.plugin_id,
            version=version,
            branch=api.plugin_branch,
            token=token,
        )
        commit = pushed.commit
    return {"repo": api.plugin_repo, "tag": tag, "commit": commit, "version": version}



def release_version(
    bench: Bench, api: Api, version: str, *, cli_version: str, drift: DriftResult, opts: ReleaseOptions
) -> None:
    _banner(f"{api.name} {version}")
    key = apimatic.api_key()
    token = github_token()

    b, diff = build_and_diffcheck(bench, api, version)
    apimatic.validate(b.project_dir, key, api.validate_fails_on)

    if opts.dry_run:
        apimatic.sdk_publish(
            b.project_dir, b.project_dir / "sdk" / "csharp", api.profile_id, version,
            dry_run=True, publish_types=api.publish_types,
        )
        print(f"dry run: {api.name} {version} built, checked and generated; nothing published", flush=True)
        return

    sdk, record_kind = _publish_sdk(api, b, api.profile_id, token, opts.nuget_timeout_s, opts.wait_for_nuget)
    plugin = _publish_plugin(api, b, key, token)

    entry = make_entry(
        overlays=list(b.overlays),
        spec_sha256=b.spec_sha256,
        change_types=list(b.change_types),
        oasdiff_checks=diff.detected,
        cli_version=cli_version,
        sdk=sdk,
        plugin=plugin,
        generator_drift=drift.drift,
        publish_record=record_kind,
    )
    append_entry(api, version, entry)
    print(f"locked {api.name} {version}:\n{json.dumps(entry, indent=2)}", flush=True)
    if opts.commit_lock:
        commit_lock(bench.root, api, version, push=opts.push_lock)


def release_api(bench: Bench, api_name: str, opts: ReleaseOptions) -> list[str]:
    require_valid(bench, [api_name])
    api = bench.api(api_name)
    releases = bench.releases(api)
    lock = load_lock(api)

    if opts.version:
        if opts.version in lock:
            print(f"{api.name} {opts.version} is already in the lock; nothing to do")
            return []
        releases.overlays_for(opts.version)  # raises if undeclared
        if releases.base not in lock and opts.version != releases.base:
            print(f"warning: the base {releases.base} has not been released yet", flush=True)
        pending = [opts.version]
    else:
        pending = pending_versions(releases, lock)

    if not pending:
        print(f"{api.name}: every declared version is in the lock; nothing to do")
        return []
    print(f"{api.name}: releasing {', '.join(pending)}{' (dry run)' if opts.dry_run else ''}", flush=True)

    if not api.profile_id:
        raise BenchError(f"apis/{api.name}/api.yaml has no profile_id; create the publishing profile first (see README)")
    key = apimatic.api_key()
    cli_version = apimatic.cli_version()
    print(f"APIMatic CLI {cli_version}", flush=True)
    apimatic.login(key)

    drift = DriftResult(checked=False)
    if not opts.dry_run and any(v != releases.base for v in pending):
        _banner(f"{api.name}: generator drift")
        drift = check_drift(bench, api, profile_id=api.profile_id, token=github_token())
        if drift.drift and not opts.allow_drift:
            raise BenchError(
                f"generator drift: regenerating the base SDK no longer matches {drift.against}. "
                "Releases made now would mix generator changes into the benchmarked API changes. "
                "Re-run with allow_drift to publish anyway (the lock records generator_drift: true)."
            )

    released = []
    for version in pending:
        release_version(bench, api, version, cli_version=cli_version, drift=drift, opts=opts)
        released.append(version)
    return released
