import json

import pytest

from bench.build import build
from bench.config import BenchError
from bench.export_dep_agent import change_matrix, export
from bench.lock import HEADER, append_entry, load_lock, make_entry, pending_versions
from bench.release import ReleaseOptions, _same_repo, release_api


def entry(**overrides):
    base = dict(
        overlays=[],
        spec_sha256="x",
        change_types=[],
        oasdiff_checks=[],
        cli_version="2.2.0",
        sdk={"package_id": "ApimaticBench.Mini", "version": "1.0.0", "repo": "r", "tag": "v1.0.0", "commit": "abc"},
        plugin={"repo": "p", "tag": "v1.0.0", "commit": "def", "version": "1.0.0"},
        generator_drift=False,
        publish_record="published",
    )
    base.update(overrides)
    return make_entry(**base)


def test_build_writes_an_apimatic_project(mini_repo):
    result = build(mini_repo, "mini", "2.0.0")
    src = result.project_dir / "src"
    spec = json.loads((src / "spec" / "openapi.json").read_text())
    assert spec["info"]["version"] == "2.0.0"
    assert spec["paths"]["/things/{id}"]["get"]["operationId"] == "fetchThing"
    assert not (src / "spec" / "APIMATIC-META.json").exists()  # optional, and the mini API has none
    config = json.loads((src / "apimatic.json").read_text())
    assert config == {
        "schemaVersion": 1,
        "plugin": {"pluginId": "bench-mini", "pluginName": "Bench Mini", "pluginVersion": "2.0.0", "license": "MIT"},
        "languages": {"csharp": {}},
    }
    assert result.change_types == ("rename-operation",)


def test_build_is_fresh_and_deterministic(mini_repo):
    first = build(mini_repo, "mini", "2.0.0")
    stray = first.project_dir / "sdk" / "leftover.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("x")
    second = build(mini_repo, "mini", "2.0.0")
    assert not stray.exists()
    assert first.spec_sha256 == second.spec_sha256


def test_base_build_has_no_overlays(mini_repo):
    result = build(mini_repo, "mini", "1.0.0")
    assert result.overlays == () and result.change_types == ()


def test_undeclared_version_is_rejected(mini_repo):
    with pytest.raises(BenchError, match="not declared"):
        build(mini_repo, "mini", "3.0.0")


def test_lock_round_trip_and_pending(mini_repo):
    api = mini_repo.api("mini")
    releases = mini_repo.releases(api)
    assert pending_versions(releases, load_lock(api)) == ["1.0.0", "2.0.0"]
    append_entry(api, "1.0.0", entry())
    assert api.lock_path.read_text().startswith(HEADER)
    assert pending_versions(releases, load_lock(api)) == ["2.0.0"]
    assert load_lock(api)["1.0.0"]["sdk"]["commit"] == "abc"
    with pytest.raises(BenchError, match="never rewritten"):
        append_entry(api, "1.0.0", entry())


def test_workflow_run_url(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    assert entry()["workflow_run_url"] == "https://github.com/o/r/actions/runs/42"


def test_release_is_a_no_op_when_everything_is_locked(mini_repo, monkeypatch):
    api = mini_repo.api("mini")
    append_entry(api, "1.0.0", entry())
    append_entry(api, "2.0.0", entry(overlays=["rename-op"], spec_sha256=build(mini_repo, "mini", "2.0.0").spec_sha256))
    # spec sha for 1.0.0 must match too, or validation (which release runs first) fails
    entries = load_lock(api)
    entries["1.0.0"]["spec_sha256"] = build(mini_repo, "mini", "1.0.0").spec_sha256
    from bench.lock import save_lock

    save_lock(api, entries)
    monkeypatch.delenv("APIMATIC_API_KEY", raising=False)  # nothing remote may be touched
    assert release_api(mini_repo, "mini", ReleaseOptions()) == []
    assert release_api(mini_repo, "mini", ReleaseOptions(version="2.0.0")) == []


def test_release_refuses_without_profile(mini_repo):
    import yaml

    path = mini_repo.root / "apis" / "mini" / "api.yaml"
    data = yaml.safe_load(path.read_text())
    data["profile_id"] = None
    path.write_text(yaml.safe_dump(data))
    from bench.config import Bench

    with pytest.raises(BenchError, match="no profile_id"):
        release_api(Bench(mini_repo.root), "mini", ReleaseOptions())


def test_same_repo():
    assert _same_repo("https://github.com/Owner/Repo.git", "owner/repo")
    assert _same_repo("https://github.com/owner/repo/", "owner/repo")
    assert not _same_repo("https://github.com/owner/other", "owner/repo")
    assert not _same_repo(None, "owner/repo")


def test_export_dep_agent(mini_repo):
    api = mini_repo.api("mini")
    append_entry(api, "1.0.0", entry())
    text = export(mini_repo)
    assert text.startswith("ApimaticBench.Mini:\n")
    assert "  marketplace: https://github.com/owner/bench-mini-context-plugin\n" in text
    assert "  plugin: bench-mini" in text
    assert "  stamp_field: version\n" in text
    assert "released: 1.0.0" in text
    assert change_matrix(mini_repo)[0]["version"] == "1.0.0"


def test_apimatic_json_matches_the_cli_schema(mini_repo):
    """schemas/apimatic.schema.json is a copy of @apimatic/cli@2's apimatic.schema.json."""
    from jsonschema import Draft7Validator

    from bench import apimatic
    from conftest import REPO

    validator = Draft7Validator(json.loads((REPO / "schemas" / "apimatic.schema.json").read_text(encoding="utf-8")))
    result = build(mini_repo, "mini", "2.0.0")
    assert list(validator.iter_errors(json.loads(result.apimatic_json.read_text()))) == []

    # the stand-in record written when an interrupted run already published the SDK
    record = apimatic.reconstructed_record("owner/bench-mini-sdk-csharp", "main", "ApimaticBench.Mini", "2.0.0")
    apimatic.write_publish_record(result.project_dir, record)
    config = json.loads(result.apimatic_json.read_text())
    assert list(validator.iter_errors(config)) == []
    assert apimatic.publish_record(result.project_dir)["package"]["version"] == "2.0.0"


def test_metadata_file_is_copied_when_present(mini_repo):
    meta = mini_repo.root / "apis" / "mini" / "spec" / "APIMATIC-META.json"
    meta.write_text('{"ImportSettings": {"ImportAdditionalHeader": false}}\n', encoding="utf-8")
    result = build(mini_repo, "mini", "1.0.0")
    assert (result.project_dir / "src" / "spec" / "APIMATIC-META.json").read_text() == meta.read_text()


def _template(mini_repo, data):
    (mini_repo.root / "apis" / "mini" / "apimatic.json").write_text(json.dumps(data), encoding="utf-8")


def test_apimatic_json_template_is_kept_and_per_version_parts_are_set(mini_repo):
    portal = {"site": {"name": "Mini"}, "ai": {"pageActions": False}}
    _template(mini_repo, {"$schema": "x", "schemaVersion": 1, "portal": portal,
                          "plugin": {"author": {"name": "Bench"}, "license": "Apache-2.0"}})
    config = json.loads(build(mini_repo, "mini", "2.0.0").apimatic_json.read_text())
    assert config["$schema"] == "x" and config["portal"] == portal
    assert config["plugin"] == {
        "author": {"name": "Bench"},
        "license": "Apache-2.0",
        "pluginId": "bench-mini",
        "pluginName": "Bench Mini",
        "pluginVersion": "2.0.0",
    }
    assert config["languages"] == {"csharp": {}}


def test_apimatic_json_template_rules(mini_repo):
    from bench.config import Bench
    from bench.validate import validate_all

    _template(mini_repo, {"schemaVersion": 1, "portal": {"site": {"name": "Mini"}}})
    assert validate_all(Bench(mini_repo.root)).errors == []

    _template(mini_repo, {
        "schemaVersion": 1,
        "plugin": {"pluginId": "other", "pluginName": "Bench Mini", "pluginVersion": "9.9.9"},
        "languages": {"csharp": {"publishing": {"package": {"version": "1.0.2"}}}, "python": {}},
    })
    problems = validate_all(Bench(mini_repo.root))
    assert any("plugin.pluginId is 'other'" in e for e in problems.errors)
    assert any("languages.csharp.publishing must not be in the template" in e for e in problems.errors)
    assert any("languages.python" in e for e in problems.errors)
    assert any("pluginVersion is ignored" in w for w in problems.warnings)

    _template(mini_repo, {"schemaVersion": 1, "portal": {"brand": {"colorMode": "purple"}}})
    assert any("apimatic.schema.json" in e for e in validate_all(Bench(mini_repo.root)).errors)
