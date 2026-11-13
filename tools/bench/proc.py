"""Running external tools (apimatic, git, gh) with secrets kept out of the logs."""

from __future__ import annotations

import os
import shutil
import subprocess
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


def create_remote_tag(repo: str, tag: str, sha: str, token: str | None) -> None:
    """A lightweight tag through the REST API (no clone needed)."""
    env = {"GH_TOKEN": token} if token else None
    run(
        ["gh", "api", "-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/tags/{tag}", "-f", f"sha={sha}"],
        env=env,
        capture=True,
    )
