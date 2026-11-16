"""`bench release` end to end against fakes: APIMatic CLI, NuGet and GitHub (local bare repos).

Real: build, diffcheck (oasdiff), the release sequence, plugin push, tag lookups, lock writing.
"""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bench import release as release_mod
from bench.config import Bench, BenchError
from bench.lock import load_lock
from bench.release import ReleaseOptions, release_api

from conftest import needs_oasdiff, write_yaml


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeWorld:
    def __init__(self, tmp: Path):
        self.github = tmp / "github"
        for repo in ("bench-mini-sdk-csharp", "bench-mini-context-plugin"):
            git("init", "--bare", "--quiet", "-b", "main", str(self.github / "owner" / f"{repo}.git"))
        self.nuget: set[str] = set()
        self.calls: list[str] = []
        self.fail_plugin_push_once = False
        self.publish_types: list[tuple] = []

    def dispatch_workflow(self, repo, workflow, ref, inputs, token):
        """The SDK repo's NuGet workflow: publishes inputs['version'] and tags the branch head."""
        self.calls.append(f"dispatch {repo} {workflow} {inputs['version']}")
        self.nuget.add(inputs["version"])
        head = git("rev-parse", ref, cwd=self.bare(repo))
        git("tag", f"v{inputs['version']}", head, cwd=self.bare(repo))

    def bare(self, repo):
        return self.github / f"{repo}.git"

    # ---- apimatic CLI
    def generated_files(self, project_dir: Path, version: str) -> dict[str, str]:
        spec = (project_dir / "src" / "spec" / "openapi.json").read_bytes()
        return {"README.md": f"SDK {version}\n", "spec.sha": hashlib.sha256(spec).hexdigest() + "\n"}

    def sdk_publish(self, project_dir, destination, profile_id, version, *, dry_run=False,
                    publish_types=("package", "sourcecode")):
        self.calls.append(f"sdk_publish {version}{' dry' if dry_run else ''}")
        self.publish_types.append(tuple(publish_types))
        destination.mkdir(parents=True, exist_ok=True)
        for name, text in self.generated_files(project_dir, version).items():
            (destination / name).write_text(text)
        if dry_run:
            return
        work = project_dir / "_sdkpush"
        git("clone", "--quiet", self.bare("owner/bench-mini-sdk-csharp").as_uri(), str(work))
        git("checkout", "-B", "main", cwd=work)
        for name in self.generated_files(project_dir, version):
            shutil.copy(destination / name, work / name)
        git("add", "-A", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"v{version}", cwd=work)
        git("push", "-q", "origin", "main", cwd=work)  # APIMatic does not tag here: exercises the fallback
        record = {
            "source": {"repositoryUrl": "https://github.com/owner/bench-mini-sdk-csharp", "branch": "main"},
            "packageConfiguration": {"packageId": "ApimaticBench.Mini"},
        }
        if "package" in publish_types:  # a source-only publish records no package version
            self.nuget.add(version)
            record["package"] = {"version": version}
        config_path = project_dir / "src" / "apimatic.json"
        config = json.loads(config_path.read_text())
        config["languages"]["csharp"]["publishing"] = record
        config_path.write_text(json.dumps(config))

    def plugin_generate(self, project_dir, destination, key):
        config = json.loads((project_dir / "src" / "apimatic.json").read_text())
        assert config["languages"]["csharp"]["publishing"]["package"]["version"] == config["plugin"]["pluginVersion"]
        self.calls.append(f"plugin_generate {config['plugin']['pluginVersion']}")
        (destination / ".claude-plugin").mkdir(parents=True)
        (destination / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": config["plugin"]["pluginId"], "version": config["plugin"]["pluginVersion"]})
        )

    def create_remote_tag(self, repo, tag, sha, token):
        self.calls.append(f"tag {repo} {tag}")
        git("tag", tag, sha, cwd=self.bare(repo))


@pytest.fixture
def world(mini_repo, tmp_path, monkeypatch):
    w = FakeWorld(tmp_path)
    monkeypatch.setenv("BENCH_GIT_BASE", w.github.as_uri())
    monkeypatch.setenv("APIMATIC_API_KEY", "test-key-123456")
    ap = release_mod.apimatic
    monkeypatch.setattr(ap, "cli_version", lambda: "2.2.0")
    monkeypatch.setattr(ap, "login", lambda key: w.calls.append("login"))
    monkeypatch.setattr(ap, "validate", lambda project_dir, key, fails_on="errors": w.calls.append("validate"))
    monkeypatch.setattr(ap, "sdk_publish", w.sdk_publish)
    monkeypatch.setattr(ap, "plugin_generate", w.plugin_generate)
    monkeypatch.setattr(release_mod, "has_version", lambda pkg, v: v in w.nuget)
    monkeypatch.setattr(release_mod, "wait_for_version", lambda pkg, v, timeout_s: None)
    monkeypatch.setattr(release_mod, "create_remote_tag", w.create_remote_tag)
    monkeypatch.setattr(release_mod, "dispatch_workflow", w.dispatch_workflow)

    real_push = release_mod.push_plugin

    def push(*args, **kwargs):
        if w.fail_plugin_push_once:
            w.fail_plugin_push_once = False
            raise BenchError("simulated push failure")
        return real_push(*args, **kwargs)

    monkeypatch.setattr(release_mod, "push_plugin", push)
    return w


def tags(world, repo):
    return git("tag", "--list", cwd=world.bare(repo)).split()


@needs_oasdiff
def test_release_publishes_locks_and_is_idempotent(mini_repo, world):
    released = release_api(mini_repo, "mini", ReleaseOptions())
    assert released == ["1.0.0", "2.0.0"]
    assert tags(world, "owner/bench-mini-sdk-csharp") == ["v1.0.0", "v2.0.0"]
    assert tags(world, "owner/bench-mini-context-plugin") == ["v1.0.0", "v2.0.0"]

    lock = load_lock(mini_repo.api("mini"))
    assert list(lock) == ["1.0.0", "2.0.0"]
    assert lock["1.0.0"]["change_types"] == []
    assert lock["2.0.0"]["change_types"] == ["rename-operation"]
    assert lock["2.0.0"]["oasdiff_checks"] == ["api-operation-id-removed"]
    assert lock["2.0.0"]["sdk"]["commit"] == git("rev-parse", "v2.0.0^{commit}", cwd=world.bare("owner/bench-mini-sdk-csharp"))
    assert lock["2.0.0"]["plugin"]["version"] == "2.0.0"
    assert lock["2.0.0"]["publish_record"] == "published"
    assert lock["2.0.0"]["generator_drift"] is False

    world.calls.clear()
    assert release_api(Bench(mini_repo.root), "mini", ReleaseOptions()) == []
    assert world.calls == []  # nothing remote touched


@needs_oasdiff
def test_rerun_after_partial_failure_completes_without_republishing(mini_repo, world):
    release_api(mini_repo, "mini", ReleaseOptions())
    write_yaml(
        mini_repo.root / "apis" / "mini" / "releases.yaml",
        {
            "base": "1.0.0",
            "releases": [
                {"version": "2.0.0", "overlays": ["rename-op"]},
                {"version": "3.0.0", "overlays": ["rename-param"]},
            ],
        },
    )
    world.fail_plugin_push_once = True
    world.calls.clear()
    with pytest.raises(BenchError, match="simulated"):
        release_api(Bench(mini_repo.root), "mini", ReleaseOptions())
    assert "sdk_publish 1.0.0 dry" in world.calls  # drift check ran once the base was published
    assert "sdk_publish 3.0.0" in world.calls
    assert "3.0.0" not in load_lock(mini_repo.api("mini"))

    world.calls.clear()
    assert release_api(Bench(mini_repo.root), "mini", ReleaseOptions()) == ["3.0.0"]
    assert "sdk_publish 3.0.0" not in world.calls  # SDK not published twice
    assert "plugin_generate 3.0.0" in world.calls
    entry = load_lock(mini_repo.api("mini"))["3.0.0"]
    assert entry["publish_record"] == "reconstructed"
    assert entry["change_types"] == ["rename-parameter"]
    assert tags(world, "owner/bench-mini-context-plugin") == ["v1.0.0", "v2.0.0", "v3.0.0"]


@needs_oasdiff
def test_generator_drift_stops_the_release(mini_repo, world, monkeypatch):
    release_api(mini_repo, "mini", ReleaseOptions(version="1.0.0"))
    original = world.generated_files

    def drifted(project_dir, version):
        files = original(project_dir, version)
        files["README.md"] = "generator changed\n"
        return files

    monkeypatch.setattr(world, "generated_files", drifted)
    with pytest.raises(BenchError, match="generator drift"):
        release_api(Bench(mini_repo.root), "mini", ReleaseOptions())
    assert "sdk_publish 2.0.0" not in world.calls  # stopped before publishing

    release_api(Bench(mini_repo.root), "mini", ReleaseOptions(allow_drift=True))
    assert load_lock(mini_repo.api("mini"))["2.0.0"]["generator_drift"] is True


@needs_oasdiff
def test_dry_run_publishes_nothing(mini_repo, world):
    assert release_api(mini_repo, "mini", ReleaseOptions(dry_run=True)) == ["1.0.0", "2.0.0"]
    assert world.calls.count("sdk_publish 1.0.0 dry") == 1
    assert not any(c.startswith(("plugin_generate", "tag")) for c in world.calls)
    assert world.nuget == set()
    assert load_lock(mini_repo.api("mini")) == {}


@needs_oasdiff
def test_mislabelled_release_is_not_published(mini_repo, world):
    path = mini_repo.root / "apis" / "mini" / "overlays" / "rename-op.yaml"
    import yaml

    doc = yaml.safe_load(path.read_text())
    doc["actions"].append({"target": "$.paths['/things/{id}'].get.parameters[?(@.name == 'verbose')]", "remove": True})
    write_yaml(path, doc)
    with pytest.raises(BenchError, match="does not match the declared change types"):
        release_api(Bench(mini_repo.root), "mini", ReleaseOptions(version="2.0.0"))
    assert not any(c.startswith("sdk_publish 2.0.0") for c in world.calls)


@needs_oasdiff
def test_nuget_published_by_the_sdk_repo_workflow(mini_repo, world):
    import yaml

    path = mini_repo.root / "apis" / "mini" / "api.yaml"
    data = yaml.safe_load(path.read_text())
    data["nuget_publisher"] = "sdk-repo-workflow"
    write_yaml(path, data)
    bench = Bench(mini_repo.root)

    assert release_api(bench, "mini", ReleaseOptions()) == ["1.0.0", "2.0.0"]
    assert set(world.publish_types) == {("sourcecode",)}  # APIMatic never pushes the package
    assert "dispatch owner/bench-mini-sdk-csharp publish-nuget.yml 2.0.0" in world.calls
    assert not any(c.startswith("tag ") for c in world.calls)  # the workflow's own tag is used
    assert world.nuget == {"1.0.0", "2.0.0"}
    entry = load_lock(bench.api("mini"))["2.0.0"]
    assert entry["sdk"]["commit"] == git("rev-parse", "v2.0.0^{commit}", cwd=world.bare("owner/bench-mini-sdk-csharp"))
    assert entry["publish_record"] == "published"
