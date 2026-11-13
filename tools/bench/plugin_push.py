"""Publish a generated Context Plugin to its GitHub repo.

`apimatic plugin publish` only prints the git commands; this runs the equivalent: replace the repo's
contents with the generated plugin (keeping `.git`), check the plugin's version is the SDK version,
commit `Publish <plugin_id> <version>`, annotate-tag `v<version>`, push with `--follow-tags`.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import BenchError
from .proc import remote_tag_commit, repo_url, run


@dataclass(frozen=True)
class PluginPushResult:
    repo: str
    tag: str
    commit: str
    version: str
    skipped: bool  # the tag already existed


def plugin_manifest(plugin_dir: Path) -> dict:
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        raise BenchError(f"generated plugin has no .claude-plugin/plugin.json ({plugin_dir})")
    return json.loads(manifest.read_text(encoding="utf-8"))


def verify_plugin(plugin_dir: Path, version: str) -> dict:
    manifest = plugin_manifest(plugin_dir)
    if manifest.get("version") != version:
        raise BenchError(
            f".claude-plugin/plugin.json version is {manifest.get('version')!r}, expected {version!r} "
            "(the plugin version must equal the SDK version: dep-agent uses stamp_field: version)"
        )
    return manifest


def _replace_contents(checkout: Path, plugin_dir: Path) -> None:
    for entry in checkout.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for entry in plugin_dir.iterdir():
        if entry.name == ".git":
            continue
        target = checkout / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copy2(entry, target)


def push_plugin(
    plugin_dir: Path,
    *,
    repo: str,
    plugin_id: str,
    version: str,
    branch: str = "main",
    token: str | None = None,
    workdir: Path | None = None,
) -> PluginPushResult:
    tag = f"v{version}"
    existing = remote_tag_commit(repo, tag, token)
    if existing:
        print(f"{repo} already has {tag} ({existing[:12]}); not pushing", flush=True)
        return PluginPushResult(repo=repo, tag=tag, commit=existing, version=version, skipped=True)

    verify_plugin(plugin_dir, version)
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        checkout = Path(tmp) / "plugin"
        run(["git", "clone", "--quiet", "--depth", "1", repo_url(repo, token), checkout])
        has_commits = run(["git", "rev-parse", "--verify", "HEAD"], cwd=checkout, check=False, capture=True, quiet=True)
        if has_commits.returncode != 0:  # empty repo
            run(["git", "symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=checkout)
        else:
            run(["git", "checkout", "-B", branch], cwd=checkout, quiet=True)

        _replace_contents(checkout, plugin_dir)
        identity = [
            "-c", f"user.name={os.environ.get('BENCH_GIT_NAME', 'github-actions[bot]')}",
            "-c", f"user.email={os.environ.get('BENCH_GIT_EMAIL', '41898282+github-actions[bot]@users.noreply.github.com')}",
        ]
        run(["git", "add", "-A"], cwd=checkout)
        # --allow-empty: a regenerated plugin identical to the previous release still gets its own release commit.
        run(["git", *identity, "commit", "--quiet", "--allow-empty", "-m", f"Publish {plugin_id} {version}"], cwd=checkout)
        run(["git", *identity, "tag", "-a", tag, "-m", f"Publish {plugin_id} {version}"], cwd=checkout)
        run(["git", "push", "--follow-tags", "origin", f"HEAD:refs/heads/{branch}"], cwd=checkout)
        commit = run(["git", "rev-parse", "HEAD"], cwd=checkout, capture=True, quiet=True).stdout.strip()
    return PluginPushResult(repo=repo, tag=tag, commit=commit, version=version, skipped=False)
