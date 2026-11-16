"""`bench` command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import apimatic
from .build import build
from .config import Bench, BenchError
from .diffcheck import diffcheck, diffcheck_overlay
from .export_dep_agent import change_matrix, export
from .lock import load_lock, pending_versions
from .nuget_wait import wait_for_version
from .plugin_push import push_plugin
from .proc import github_token
from .release import ReleaseOptions, build_and_diffcheck, compile_check, release_api
from .validate import print_problems, validate_all


def cmd_validate(bench: Bench, args) -> int:
    problems = validate_all(bench, args.api or None)
    print_problems(problems)
    return 0 if problems.ok else 1


def cmd_build(bench: Bench, args) -> int:
    result = build(bench, args.api, args.version)
    print(result.project_dir)
    return 0


def cmd_diffcheck(bench: Bench, args) -> int:
    api = bench.api(args.api)
    if args.overlay:
        result = build(bench, api.name, "0.0.0", overlay_ids=(args.overlay,),
                       project_dir=bench.build_dir / api.name / "_overlay" / args.overlay)
    else:
        result = build(bench, api.name, args.version)
    diff = diffcheck(api.base_spec_path, result.spec_path, list(result.change_types), bench.catalog)
    print(diff.report())
    return 0 if diff.ok else 1


def cmd_diffcheck_overlays(bench: Bench, args) -> int:
    """Every overlay, applied to the base (after the overlays it requires), must produce exactly its
    declared change type."""
    failed = []
    for name in args.api or bench.api_names():
        api = bench.api(name)
        for overlay_id in bench.overlay_ids(api):
            diff = diffcheck_overlay(bench, name, overlay_id, bench.build_dir / name / "_overlay" / overlay_id)
            print(f"--- {name}/{overlay_id}\n{diff.report()}")
            if not diff.ok:
                failed.append(f"{name}/{overlay_id}")
    if failed:
        print(f"overlays whose diff does not match their x-change.type: {', '.join(failed)}")
        return 1
    return 0


def cmd_pending(bench: Bench, args) -> int:
    pending = {}
    for name in args.api or bench.api_names():
        api = bench.api(name)
        if args.publishable and not api.profile_id:
            print(f"{name}: no profile_id in api.yaml yet; not publishable", file=sys.stderr)
            continue
        versions = pending_versions(bench.releases(api), load_lock(api))
        if versions:
            pending[name] = versions
    if args.json:
        print(json.dumps(pending))
    elif args.github_matrix:
        print(json.dumps({"include": [{"api": a, "version": v} for a, vs in pending.items() for v in vs]}))
    elif args.apis_json:
        print(json.dumps(sorted(pending)))
    else:
        for name, versions in pending.items():
            print(f"{name}: {', '.join(versions)}")
    return 0


def cmd_check(bench: Bench, args) -> int:
    """What a PR runs for a new release: build, diffcheck, apimatic api validate, generate + compile."""
    api = bench.api(args.api)
    result, _ = build_and_diffcheck(bench, api, args.version)
    if args.offline:
        print("offline: skipping apimatic api validate and the compile check")
        return 0
    key = apimatic.api_key(required=False)
    apimatic.validate(result.project_dir, key, api.validate_fails_on)
    if not args.no_compile:
        compile_check(result.project_dir, key)
    return 0


def cmd_release(bench: Bench, args) -> int:
    opts = ReleaseOptions(
        version=args.version,
        allow_drift=args.allow_drift,
        commit_lock=args.commit_lock,
        push_lock=not args.no_push,
        dry_run=args.dry_run,
        wait_for_nuget=args.wait_for_nuget,
        nuget_timeout_s=args.nuget_timeout * 60,
    )
    release_api(bench, args.api, opts)
    return 0


def cmd_nuget_wait(bench: Bench, args) -> int:
    wait_for_version(args.package_id, args.version, timeout_s=args.timeout * 60)
    return 0


def cmd_plugin_push(bench: Bench, args) -> int:
    api = bench.api(args.api)
    result = push_plugin(
        Path(args.plugin_dir) if args.plugin_dir else bench.build_dir / api.name / args.version / "plugin",
        repo=api.plugin_repo,
        plugin_id=api.plugin_id,
        version=args.version,
        branch=api.plugin_branch,
        token=github_token(),
    )
    print(json.dumps(result.__dict__, indent=2))
    return 0


def cmd_export_dep_agent(bench: Bench, args) -> int:
    if args.changes:
        print(json.dumps(change_matrix(bench, args.api or None), indent=2))
    else:
        sys.stdout.write(export(bench, args.api or None))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench", description="api-upgrade-bench: spec -> SDK -> Context Plugin")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="schema + rule checks over the catalog and API folders")
    p.add_argument("api", nargs="*")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("build", help="write build/<api>/<version>/src for the APIMatic CLI")
    p.add_argument("api")
    p.add_argument("version")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("diffcheck", help="oasdiff base vs built spec, held to the declared change types")
    p.add_argument("api")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("version", nargs="?")
    g.add_argument("--overlay", help="check one overlay applied alone to the base")
    p.set_defaults(func=cmd_diffcheck)

    p = sub.add_parser("diffcheck-overlays", help="diffcheck every overlay on its own")
    p.add_argument("api", nargs="*")
    p.set_defaults(func=cmd_diffcheck_overlays)

    p = sub.add_parser("pending", help="declared versions missing from the lock")
    p.add_argument("api", nargs="*")
    p.add_argument("--publishable", action="store_true", help="only APIs whose api.yaml has a profile_id")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help='{"api": ["version", ...]}')
    fmt.add_argument("--github-matrix", action="store_true", help='{"include": [{"api", "version"}]}')
    fmt.add_argument("--apis-json", action="store_true", help='["api", ...] with something pending')
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("check", help="PR check for one version: build, diffcheck, api validate, generate + dotnet build")
    p.add_argument("api")
    p.add_argument("version")
    p.add_argument("--offline", action="store_true", help="only build + diffcheck (no APIMatic calls)")
    p.add_argument("--no-compile", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("release", help="publish SDK + plugin for pending versions and lock them")
    p.add_argument("api")
    p.add_argument("--version", help="release only this version")
    p.add_argument("--allow-drift", action="store_true")
    p.add_argument("--commit-lock", action="store_true", help="commit releases.lock.yaml after each version")
    p.add_argument("--no-push", action="store_true", help="with --commit-lock: commit but do not push")
    p.add_argument("--dry-run", action="store_true", help="build, check and generate; publish nothing")
    p.add_argument("--wait-for-nuget", action="store_true",
                   help="after publishing, wait until NuGet lists the version (off: publishing is enough)")
    p.add_argument("--nuget-timeout", type=int, default=30, help="minutes to wait with --wait-for-nuget (default 30)")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("nuget-wait", help="wait until NuGet lists a package version")
    p.add_argument("package_id")
    p.add_argument("version")
    p.add_argument("--timeout", type=int, default=30, help="minutes")
    p.set_defaults(func=cmd_nuget_wait)

    p = sub.add_parser("plugin-push", help="push a generated plugin dir to the API's plugin repo and tag it")
    p.add_argument("api")
    p.add_argument("version")
    p.add_argument("--plugin-dir")
    p.set_defaults(func=cmd_plugin_push)

    p = sub.add_parser("export-dep-agent", help="print dep-agent plugins.yaml entries")
    p.add_argument("api", nargs="*")
    p.add_argument("--changes", action="store_true", help="print the per-version change-type matrix (JSON) instead")
    p.set_defaults(func=cmd_export_dep_agent)

    args = parser.parse_args(argv)
    try:
        bench = Bench.discover()
        return args.func(bench, args)
    except BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
