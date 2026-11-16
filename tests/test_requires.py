"""`x-change.requires`: an overlay that only applies after other overlays (imported histories)."""

import pytest

from bench.config import Bench, BenchError
from bench.diffcheck import diffcheck_overlay
from bench.validate import validate_all

from conftest import needs_oasdiff, write_yaml

ADD_SIZE_UNIT = {
    "overlay": "1.0.0",
    "info": {"title": "Thing gains size_unit", "version": "1.0.0"},
    "x-change": {"type": "add-optional-property", "breaking": False, "targets": ["schema:Thing.size_unit"]},
    "actions": [{"target": "$['components']['schemas']['Thing']['properties']", "update": {"size_unit": {"type": "string"}}}],
}

# changes the property ADD_SIZE_UNIT adds: only applies after it
SIZE_UNIT_NULLABLE = {
    "overlay": "1.0.0",
    "info": {"title": "Thing.size_unit becomes nullable", "version": "1.0.0"},
    "x-change": {
        "type": "nullability-change",
        "breaking": True,
        "targets": ["schema:Thing.size_unit"],
        "requires": ["add-size-unit"],
    },
    "actions": [{"target": "$['components']['schemas']['Thing']['properties']['size_unit']", "update": {"nullable": True}}],
}


@pytest.fixture
def history(mini_repo):
    overlays = mini_repo.root / "apis" / "mini" / "overlays"
    write_yaml(overlays / "add-size-unit.yaml", ADD_SIZE_UNIT)
    write_yaml(overlays / "size-unit-nullable.yaml", SIZE_UNIT_NULLABLE)
    write_yaml(
        mini_repo.root / "apis" / "mini" / "releases.yaml",
        {"base": "1.0.0", "releases": [
            {"version": "1.1.0", "overlays": ["add-size-unit"]},
            {"version": "2.0.0", "overlays": ["add-size-unit", "size-unit-nullable"]},
        ]},
    )
    return Bench(mini_repo.root)


def errors(bench):
    return validate_all(Bench(bench.root)).errors


def test_an_overlay_is_validated_after_what_it_requires(history):
    assert errors(history) == []
    api = history.api("mini")
    assert history.prerequisites(api, "size-unit-nullable") == ("add-size-unit",)
    assert history.prerequisites(api, "add-size-unit") == ()


def test_without_requires_the_dependent_overlay_does_not_apply(history):
    doc = dict(SIZE_UNIT_NULLABLE, **{"x-change": {k: v for k, v in SIZE_UNIT_NULLABLE["x-change"].items() if k != "requires"}})
    write_yaml(history.root / "apis" / "mini" / "overlays" / "size-unit-nullable.yaml", doc)
    assert any("size-unit-nullable.yaml: does not apply to the base spec" in e for e in errors(history))


def test_a_release_lists_requirements_first(history):
    write_yaml(
        history.root / "apis" / "mini" / "releases.yaml",
        {"base": "1.0.0", "releases": [{"version": "2.0.0", "overlays": ["size-unit-nullable", "add-size-unit"]}]},
    )
    assert any("must list what an overlay requires before it" in e for e in errors(history))


def test_unknown_and_cyclic_requirements_are_errors(history):
    overlays = history.root / "apis" / "mini" / "overlays"
    write_yaml(overlays / "size-unit-nullable.yaml",
               {**SIZE_UNIT_NULLABLE, "x-change": {**SIZE_UNIT_NULLABLE["x-change"], "requires": ["nope"]}})
    assert any("requires names unknown overlays: nope" in e for e in errors(history))

    write_yaml(overlays / "size-unit-nullable.yaml", SIZE_UNIT_NULLABLE)
    write_yaml(overlays / "add-size-unit.yaml",
               {**ADD_SIZE_UNIT, "x-change": {**ADD_SIZE_UNIT["x-change"], "requires": ["size-unit-nullable"]}})
    bench = Bench(history.root)
    with pytest.raises(BenchError, match="cycle"):
        bench.prerequisites(bench.api("mini"), "size-unit-nullable")
    assert any("cycle" in e for e in errors(history))


@needs_oasdiff
def test_diffcheck_compares_against_the_prerequisites(history, tmp_path):
    # against the bare base this diff would also show the property being added
    diff = diffcheck_overlay(history, "mini", "size-unit-nullable", tmp_path)
    assert diff.ok, diff.report()
    assert diff.detected == ["response-property-became-nullable"]
