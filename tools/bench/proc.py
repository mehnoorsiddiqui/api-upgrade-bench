"""Running external tools (apimatic, git, gh) with secrets kept out of the logs."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Sequence

from .config import BenchError

_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in _SECRETS:
        text = text.replace(secret, "***")
    return text


def which(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise BenchError(f"{name} not found on PATH")
    return exe


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = False,
    quiet: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command, echoing it (redacted). With ``capture`` the output is returned instead of streamed."""
    args = [str(a) for a in args]
    if not quiet:
        print("+ " + redact(" ".join(args)), flush=True)
    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        args,
        cwd=cwd,
        env=full_env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        capture_output=capture,
    )
    if check and result.returncode != 0:
        detail = redact((result.stdout or "") + (result.stderr or "")) if capture else ""
        raise BenchError(f"command failed ({result.returncode}): {redact(' '.join(args))}\n{detail}".rstrip())
    return result


# --------------------------------------------------------------------------- GitHub


def github_token() -> str | None:
    return os.environ.get("PLUGIN_PUSH_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def repo_url(repo: str, token: str | None = None) -> str:
    # BENCH_GIT_BASE points the git operations somewhere other than GitHub (tests use a local dir).
    base = os.environ.get("BENCH_GIT_BASE")
    if base:
        return f"{base.rstrip('/')}/{repo}.git"
    if token:
        register_secret(token)
        return f"https://x-access-token:{token}@github.com/{repo}.git"
    return f"https://github.com/{repo}.git"


def remote_tag_commit(repo: str, tag: str, token: str | None = None) -> str | None:
    """The commit a tag points at (annotated tags dereferenced), or None if the tag does not exist."""
    result = run(
        ["git", "ls-remote", "--tags", repo_url(repo, token), f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
        capture=True,
        quiet=True,
    )
    refs = {}
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        refs[ref] = sha
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def remote_branch_commit(repo: str, branch: str, token: str | None = None) -> str | None:
    result = run(["git", "ls-remote", "--heads", repo_url(repo, token), f"refs/heads/{branch}"], capture=True, quiet=True)
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}":
            return sha
    return None


def dispatch_workflow(repo: str, workflow: str, ref: str, inputs: dict[str, str], token: str | None) -> None:
    """Start a workflow_dispatch run (the token needs Actions: write on the repo)."""
    args = ["gh", "workflow", "run", workflow, "-R", repo, "--ref", ref]
    for key, value in inputs.items():
        args += ["-f", f"{key}={value}"]
    run(args, env={"GH_TOKEN": token} if token else None, capture=True)


def version_tag(repo: str, version: str, token: str | None = None) -> tuple[str | None, str | None]:
    """The SDK repo's tag for `version` and the commit it points at, or (None, None).

    Two names count: `v<version>`, which the bench creates, and `<version>`, which an APIMatic source
    publish creates. Accepting both keeps a version from being republished, or tagged twice, because
    of how its tag is spelled."""
    for tag in (f"v{version}", version):
        commit = remote_tag_commit(repo, tag, token)
        if commit:
            return tag, commit
    return None, None


def wait_for_tag(repo: str, tag: str, token: str | None, timeout_s: int = 600, interval_s: int = 15) -> str | None:
    """The tag's commit once it exists, or None after the timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        commit = remote_tag_commit(repo, tag, token)
        if commit or time.monotonic() >= deadline:
            return commit
        print(f"waiting for {repo} to get tag {tag} ...", flush=True)
        time.sleep(interval_s)


def create_remote_tag(repo: str, tag: str, sha: str, token: str | None) -> None:
    """A lightweight tag through the REST API (no clone needed)."""
    env = {"GH_TOKEN": token} if token else None
    run(
        ["gh", "api", "-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/tags/{tag}", "-f", f"sha={sha}"],
        env=env,
        capture=True,
    )
