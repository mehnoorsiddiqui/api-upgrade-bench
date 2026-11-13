import yaml

from bench.build import build
from bench.lock import load_lock, make_entry, save_lock
from bench.validate import validate_all

from conftest import REPO, write_yaml
from bench.config import Bench


def releases(bench, data):
    write_yaml(bench.root / "apis" / "mini" / "releases.yaml", data)


def errors(bench):
    return validate_all(Bench(bench.root)).errors


def lock_version(bench, version, overlays, sha=None):
    api = bench.api("mini")
    entries = load_lock(api)
    result = build(bench, "mini", version, overlay_ids=tuple(overlays))
    entries[version] = make_entry(
        overlays=overlays,
        spec_sha256=sha or result.spec_sha256,
        change_types=list(result.change_types),
        oasdiff_checks=[],
        cli_version="2.2.0",
        sdk={},
        plugin={},
        generator_drift=False,
        publish_record="published",
    )
    save_lock(api, entries)


def test_the_real_repo_is_valid():
    problems = validate_all(Bench(REPO))
    assert problems.errors == []


def test_mini_repo_is_valid(mini_repo):
    assert errors(mini_repo) == []


def test_breaking_release_needs_a_new_major(mini_repo):
    releases(mini_repo, {"base": "1.0.0", "releases": [{"version": "1.1.0", "overlays": ["rename-op"]}]})
    assert any("major must be greater" in e for e in errors(mini_repo))


def test_versions_are_unique_and_base_is_not_a_release(mini_repo):
    releases(
        mini_repo,
        {
            "base": "1.0.0",
            "releases": [
                {"version": "2.0.0", "overlays": ["rename-op"]},
                {"version": "2.0.0", "overlays": ["rename-param"]},
                {"version": "1.0.0", "overlays": ["rename-param"]},
            ],
        },
    )
    assert sum("declared more than once" in e for e in errors(mini_repo)) == 2


def test_release_must_be_greater_than_base(mini_repo):
    releases(mini_repo, {"base": "1.0.0", "releases": [{"version": "0.9.0", "overlays": ["rename-op"]}]})
    assert any("greater than the base" in e for e in errors(mini_repo))


def test_unknown_overlay(mini_repo):
    releases(mini_repo, {"base": "1.0.0", "releases": [{"version": "2.0.0", "overlays": ["nope"]}]})
    assert any("unknown overlays: nope" in e for e in errors(mini_repo))


def test_release_needs_overlays(mini_repo):
    releases(mini_repo, {"base": "1.0.0", "releases": [{"version": "2.0.0", "overlays": []}]})
    assert errors(mini_repo)


def test_overlay_type_must_be_in_catalog(mini_repo):
    path = mini_repo.root / "apis" / "mini" / "overlays" / "rename-op.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["x-change"]["type"] = "made-up"
    write_yaml(path, doc)
    assert any("not in catalog" in e for e in errors(mini_repo))


def test_overlay_breaking_flag_must_match_catalog(mini_repo):
    path = mini_repo.root / "apis" / "mini" / "overlays" / "rename-op.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["x-change"]["breaking"] = False
    write_yaml(path, doc)
    assert any("x-change.breaking is False" in e for e in errors(mini_repo))


def test_overlay_that_no_longer_applies(mini_repo):
    path = mini_repo.root / "apis" / "mini" / "overlays" / "rename-op.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["actions"][0]["target"] = "$.paths['/gone'].get"
    write_yaml(path, doc)
    assert any("does not apply" in e for e in errors(mini_repo))


def test_overlay_schema(mini_repo):
    write_yaml(mini_repo.root / "apis" / "mini" / "overlays" / "bad.yaml", {"overlay": "1.0.0", "actions": []})
    assert any("overlays/bad.yaml" in e for e in errors(mini_repo))


def test_api_name_must_match_folder(mini_repo):
    path = mini_repo.root / "apis" / "mini" / "api.yaml"
    data = yaml.safe_load(path.read_text())
    data["name"] = "other"
    write_yaml(path, data)
    assert any("must equal the folder name" in e for e in errors(mini_repo))


def test_missing_profile_id_is_only_a_warning(mini_repo):
    path = mini_repo.root / "apis" / "mini" / "api.yaml"
    data = yaml.safe_load(path.read_text())
    data["profile_id"] = None
    write_yaml(path, data)
    problems = validate_all(Bench(mini_repo.root))
    assert problems.ok and any("profile_id" in w for w in problems.warnings)


def test_locked_version_cannot_be_removed(mini_repo):
    lock_version(mini_repo, "2.0.0", ["rename-op"])
    releases(mini_repo, {"base": "1.0.0", "releases": []})
    assert any("no longer declared" in e for e in errors(mini_repo))


def test_locked_version_overlays_cannot_change(mini_repo):
    lock_version(mini_repo, "2.0.0", ["rename-op"])
    releases(mini_repo, {"base": "1.0.0", "releases": [{"version": "2.0.0", "overlays": ["rename-op", "rename-param"]}]})
    assert any("never edit a released version" in e for e in errors(mini_repo))


def test_locked_version_spec_cannot_change(mini_repo):
    lock_version(mini_repo, "1.0.0", [])
    lock_version(mini_repo, "2.0.0", ["rename-op"])
    assert errors(mini_repo) == []
    path = mini_repo.root / "apis" / "mini" / "overlays" / "rename-op.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["actions"][0]["update"]["operationId"] = "loadThing"
    write_yaml(path, doc)
    errs = errors(mini_repo)
    assert any("2.0.0: rebuilding gives spec sha256" in e for e in errs)
    assert not any("1.0.0: rebuilding" in e for e in errs)


def test_base_spec_edit_after_release_is_caught(mini_repo):
    lock_version(mini_repo, "1.0.0", [])
    path = mini_repo.root / "apis" / "mini" / "spec" / "openapi.yaml"
    spec = yaml.safe_load(path.read_text())
    spec["info"]["title"] = "Changed"
    write_yaml(path, spec)
    assert any("1.0.0: rebuilding gives spec sha256" in e for e in errors(mini_repo))
