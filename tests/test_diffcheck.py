"""oasdiff -> catalog mapping.

The pure tests feed `evaluate` recorded oasdiff output (tests/fixtures/oasdiff/*.json, captured
with the pinned oasdiff from the petstore overlays). The `needs_oasdiff` tests run the real binary.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from bench.build import build
from bench.config import Bench
from bench.diffcheck import diffcheck, diffcheck_overlay, evaluate, oasdiff_exe

from conftest import FIXTURES, REPO, needs_oasdiff, write_yaml

RECORDED = FIXTURES / "oasdiff"


def recorded(name):
    return json.loads((RECORDED / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def catalog():
    return Bench(REPO).catalog


@pytest.mark.parametrize(
    "overlay, change_type",
    [
        ("rename-get-pet", "rename-operation"),
        ("rename-status-param", "rename-parameter"),
        ("require-inventory-store-id", "add-required-parameter"),
        ("rename-order-quantity", "rename-model-property"),
        ("order-pet-id-to-string", "change-property-type"),
        ("api-key-to-bearer", "auth-scheme-change"),
    ],
)
def test_recorded_diff_matches_its_change_type(catalog, overlay, change_type):
    result = evaluate(recorded(overlay), [change_type], catalog)
    assert result.ok, result.report()


def test_version_checks_are_ignored(catalog):
    changes = recorded("require-inventory-store-id") + [{"id": "api-version-not-bumped"}, {"id": "api-version-decreased"}]
    result = evaluate(changes, ["add-required-parameter"], catalog)
    assert result.ok
    assert "api-version-not-bumped" not in result.detected


def test_mislabelled_change_fails_both_ways(catalog):
    # a parameter rename declared as an operation rename
    result = evaluate(recorded("rename-status-param"), ["rename-operation"], catalog)
    assert not result.ok
    assert set(result.unlabelled) == {"request-parameter-removed", "new-optional-request-parameter"}
    assert result.missing == [("rename-operation", ("api-operation-id-removed",))]


def test_unlabelled_extra_change_fails(catalog):
    changes = recorded("rename-get-pet") + recorded("require-inventory-store-id")
    result = evaluate(changes, ["rename-operation"], catalog)
    assert result.unlabelled == ["new-required-request-parameter"]
    assert evaluate(changes, ["rename-operation", "add-required-parameter"], catalog).ok


def test_declared_change_that_did_not_happen_fails(catalog):
    result = evaluate(recorded("rename-get-pet"), ["rename-operation", "auth-scheme-change"], catalog)
    assert [t for t, _ in result.missing] == ["auth-scheme-change", "auth-scheme-change"]


def test_add_required_parameter_is_not_a_rename(catalog):
    # rename-parameter requires the old parameter to disappear
    result = evaluate(recorded("require-inventory-store-id"), ["rename-parameter"], catalog)
    assert not result.ok
    assert ("rename-parameter", ("request-parameter-removed", "request-parameter-removed-with-deprecation")) in result.missing


def test_base_release_must_have_no_changes(catalog):
    assert evaluate([], [], catalog).ok
    assert evaluate(recorded("rename-get-pet"), [], catalog).unlabelled == ["api-operation-id-removed"]


def test_report_mentions_failures(catalog):
    report = evaluate(recorded("rename-status-param"), ["rename-operation"], catalog).report()
    assert "UNLABELLED request-parameter-removed" in report
    assert "MISSING   rename-operation" in report
    assert report.endswith("diffcheck: FAILED")


# ----------------------------------------------------------------------------- with oasdiff


@needs_oasdiff
def test_catalog_check_ids_exist_in_pinned_oasdiff(catalog):
    out = subprocess.run([oasdiff_exe(), "checks", "changelog", "--format", "json"], capture_output=True, text=True, check=True)
    known = {c["id"] for c in json.loads(out.stdout)}
    used = set(catalog.ignored_checks)
    for change_type in catalog.types.values():
        used |= change_type.all_ids
    assert sorted(used - known) == []


@pytest.fixture(scope="module")
def repo_bench():
    # one Bench for every overlay: it caches the base spec and built prefixes, which an imported
    # history (dozens of overlays on a multi-megabyte spec) needs
    return Bench(REPO)


@needs_oasdiff
@pytest.mark.parametrize("overlay_path", sorted((REPO / "apis").glob("*/overlays/*.yaml")), ids=lambda p: f"{p.parent.parent.name}/{p.stem}")
def test_every_overlay_produces_exactly_its_change_type(tmp_path, repo_bench, overlay_path: Path):
    diff = diffcheck_overlay(repo_bench, overlay_path.parent.parent.name, overlay_path.stem, tmp_path)
    assert diff.ok, diff.report()


@needs_oasdiff
def test_mislabelled_overlay_fails_diffcheck(mini_repo):
    path = mini_repo.root / "apis" / "mini" / "overlays" / "rename-param.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["x-change"]["type"] = "rename-operation"
    write_yaml(path, doc)
    bench = Bench(mini_repo.root)
    api = bench.api("mini")
    result = build(bench, "mini", "2.0.0", overlay_ids=("rename-param",))
    diff = diffcheck(api.base_spec_path, result.spec_path, list(result.change_types), bench.catalog)
    assert not diff.ok
    assert "request-parameter-removed" in diff.unlabelled
