"""Generator drift: does today's server-side codegen still produce the published base SDK?

The codegen version cannot be pinned from the CLI, so a change in generator output between two
releases would show up in the benchmark as if it were part of the API change. Once per release
run, the base spec is regenerated exactly as it was published (`sdk publish --dry-run` with the
same profile and version, so package metadata matches) and compared with the SDK repo's
`v<base>` tag.
"""

from __future__ import annotations

import fnmatch
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import apimatic
from .build import build
from .config import Api, Bench
from .proc import remote_tag_commit, repo_url, run

ALWAYS_IGNORED = (".git", ".git/**")


@dataclass
class DriftResult:
    checked: bool
    against: str | None = None
    differences: list[str] = field(default_factory=list)

    @property
    def drift(self) -> bool:
        return bool(self.differences)


def _files(root: Path, ignore: tuple[str, ...]) -> dict[str, Path]:
    found = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.split("/", 1)[0] == ".git" or any(fnmatch.fnmatch(rel, g) for g in ignore):
            continue
        found[rel] = path
    return found


def _normalised(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def compare_trees(generated: Path, published: Path, ignore: tuple[str, ...] = ()) -> list[str]:
    ignore = tuple(ignore) + ALWAYS_IGNORED
    ours, theirs = _files(generated, ignore), _files(published, ignore)
    differences = [f"only in regenerated SDK: {p}" for p in sorted(ours.keys() - theirs.keys())]
    differences += [f"only in published SDK:   {p}" for p in sorted(theirs.keys() - ours.keys())]
    differences += [f"changed:                 {p}" for p in sorted(ours.keys() & theirs.keys()) if _normalised(ours[p]) != _normalised(theirs[p])]
    return differences


def check_drift(bench: Bench, api: Api, *, profile_id: str, token: str | None) -> DriftResult:
    base = bench.releases(api).base
    tag = f"v{base}"
    if not remote_tag_commit(api.sdk_repo, tag, token):
        print(f"drift: {api.sdk_repo} has no {tag} yet; nothing to compare against", flush=True)
        return DriftResult(checked=False)

    project = build(bench, api.name, base, project_dir=bench.build_dir / api.name / "_drift" / base)
    regenerated = project.project_dir / "sdk" / "csharp"
    apimatic.sdk_publish(project.project_dir, regenerated, profile_id, base, dry_run=True)

    with tempfile.TemporaryDirectory() as tmp:
        published = Path(tmp) / "published"
        run(["git", "clone", "--quiet", "--depth", "1", "--branch", tag, repo_url(api.sdk_repo, token), published])
        differences = compare_trees(regenerated, published, api.drift_ignore)

    result = DriftResult(checked=True, against=f"{api.sdk_repo}@{tag}", differences=differences)
    if result.drift:
        print(f"drift: regenerating {base} differs from {result.against} in {len(differences)} file(s):", flush=True)
        for line in differences[:50]:
            print(f"  {line}")
    else:
        print(f"drift: none (regenerated {base} matches {result.against})", flush=True)
    return result
