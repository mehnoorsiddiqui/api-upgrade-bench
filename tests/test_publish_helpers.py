"""Plugin push (against a local bare repo standing in for GitHub), drift comparison, NuGet parsing."""

import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from bench import nuget_wait
from bench.config import BenchError
from bench.drift import compare_trees
from bench.plugin_push import push_plugin, verify_plugin
from bench.proc import remote_tag_commit


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_plugin(path: Path, version: str, name="bench-mini") -> Path:
    (path / ".claude-plugin").mkdir(parents=True)
    (path / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name, "version": version}))
    (path / "skills" / "dotnet" / "x").mkdir(parents=True)
    (path / "skills" / "dotnet" / "x" / "SKILL.md").write_text(f"skill for {version}\n")
    return path


@pytest.fixture
def fake_github(tmp_path, monkeypatch):
    base = tmp_path / "github"
    (base / "owner").mkdir(parents=True)
    git("init", "--bare", "--quiet", "-b", "main", str(base / "owner" / "plugin.git"))
    monkeypatch.setenv("BENCH_GIT_BASE", base.as_uri())
    return base


def test_push_plugin_to_empty_repo_then_next_version(tmp_path, fake_github):
    first = push_plugin(make_plugin(tmp_path / "p1", "1.0.0"), repo="owner/plugin", plugin_id="bench-mini", version="1.0.0")
    assert not first.skipped
    assert remote_tag_commit("owner/plugin", "v1.0.0") == first.commit

    plugin2 = make_plugin(tmp_path / "p2", "2.0.0")
    (plugin2 / "skills" / "dotnet" / "x" / "SKILL.md").unlink()
    (plugin2 / "README.md").write_text("new\n")
    second = push_plugin(plugin2, repo="owner/plugin", plugin_id="bench-mini", version="2.0.0")

    clone = tmp_path / "clone"
    git("clone", "--quiet", (fake_github / "owner" / "plugin.git").as_uri(), str(clone))
    assert git("log", "--format=%s", cwd=clone).splitlines() == ["Publish bench-mini 2.0.0", "Publish bench-mini 1.0.0"]
    assert git("rev-parse", "v2.0.0^{commit}", cwd=clone) == second.commit
    assert git("cat-file", "-t", "v2.0.0", cwd=clone) == "tag"  # annotated
    files = sorted(p.relative_to(clone).as_posix() for p in clone.rglob("*") if p.is_file() and ".git" not in p.parts)
    assert files == [".claude-plugin/plugin.json", "README.md"]  # old content replaced, not merged


def test_push_plugin_is_idempotent(tmp_path, fake_github):
    first = push_plugin(make_plugin(tmp_path / "p1", "1.0.0"), repo="owner/plugin", plugin_id="bench-mini", version="1.0.0")
    again = push_plugin(make_plugin(tmp_path / "p1b", "1.0.0"), repo="owner/plugin", plugin_id="bench-mini", version="1.0.0")
    assert again.skipped and again.commit == first.commit


def test_plugin_version_must_equal_sdk_version(tmp_path, fake_github):
    with pytest.raises(BenchError, match="expected '2.0.0'"):
        push_plugin(make_plugin(tmp_path / "p", "0.1.0"), repo="owner/plugin", plugin_id="bench-mini", version="2.0.0")
    assert remote_tag_commit("owner/plugin", "v2.0.0") is None


def test_verify_plugin_needs_manifest(tmp_path):
    with pytest.raises(BenchError, match="no .claude-plugin/plugin.json"):
        verify_plugin(tmp_path, "1.0.0")


def test_compare_trees(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        (root / "src").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text(str(root))
    (a / "src" / "Same.cs").write_bytes(b"line1\r\nline2\r\n")
    (b / "src" / "Same.cs").write_bytes(b"line1\nline2\n")  # line endings are not drift
    (a / "src" / "Changed.cs").write_text("v1")
    (b / "src" / "Changed.cs").write_text("v2")
    (a / "only-new.txt").write_text("x")
    (b / "only-old.txt").write_text("x")
    (b / "ignored.md").write_text("x")
    (a / ".apimatic").mkdir()  # CLI bookkeeping a dry run writes; never published
    (a / ".apimatic" / "generation.json").write_text("{}")
    (a / ".apimatic" / ".gitattributes").write_text("x")
    diffs = compare_trees(a, b, ignore=("ignored.md",))
    assert diffs == [
        "only in regenerated SDK: only-new.txt",
        "only in published SDK:   only-old.txt",
        "changed:                 src/Changed.cs",
    ]
    assert compare_trees(a, a) == []


def test_nuget_versions(monkeypatch):
    def fake_urlopen(request, timeout):
        assert request.full_url == "https://api.nuget.org/v3-flatcontainer/apimaticbench.mini/index.json"
        return io.BytesIO(b'{"versions": ["1.0.0", "2.0.0-Beta"]}')

    monkeypatch.setattr(nuget_wait.urllib.request, "urlopen", fake_urlopen)
    assert nuget_wait.has_version("ApimaticBench.Mini", "2.0.0-beta")
    assert not nuget_wait.has_version("ApimaticBench.Mini", "3.0.0")


def test_nuget_unknown_package(monkeypatch):
    def not_found(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, None)

    monkeypatch.setattr(nuget_wait.urllib.request, "urlopen", not_found)
    assert nuget_wait.nuget_versions("ApimaticBench.Nope") == []
