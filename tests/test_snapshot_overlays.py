"""Turning the difference between two specs into labelled overlays."""

import copy

import pytest

from bench.config import Bench
from bench.overlay import apply_overlays
from bench.snapshot_overlays import SnapshotDiffError, diff_documents, equivalent, overlays_for_step

from conftest import MINI_SPEC, REPO


@pytest.fixture(scope="module")
def breaking_of():
    return {t.id: t.breaking for t in Bench(REPO).catalog.types.values()}


def next_version(old):
    new = copy.deepcopy(old)
    schemas = new["components"]["schemas"]
    op = new["paths"]["/things/{id}"]["get"]
    # a new enum schema, and an existing property that becomes it
    schemas["Unit"] = {"type": "string", "enum": ["CM", "IN"]}
    schemas["Thing"]["properties"]["size"] = {"$ref": "#/components/schemas/Unit"}
    # a new optional property, a required property that becomes nullable, a docs edit
    schemas["Thing"]["properties"]["label"] = {"type": "string", "description": "Shown to people."}
    schemas["Thing"]["properties"]["id"]["nullable"] = True
    schemas["Thing"]["description"] = "A thing."
    # a parameter goes, another arrives; a new endpoint
    op["parameters"] = [p for p in op["parameters"] if p["name"] != "verbose"] + [
        {"name": "fields", "in": "query", "required": False, "schema": {"type": "string"}}
    ]
    new["paths"]["/things"] = {"get": {"operationId": "listThings", "responses": {"200": {"description": "ok"}}}}
    return new


def test_overlays_rebuild_the_new_spec_with_one_label_per_change(breaking_of):
    old, new = MINI_SPEC, next_version(MINI_SPEC)
    docs = overlays_for_step(diff_documents(old, new), "s1", "Mini v2", breaking_of, previous=[])
    by_type = {d["x-change"]["type"]: d for _, d in docs}
    assert set(by_type) == {
        "add-model", "became-enum", "add-optional-property", "nullability-change",
        "docs-and-metadata", "remove-parameter", "add-optional-parameter", "add-endpoint",
    }
    assert [oid for oid, _ in docs][0] == "s1-add-model"  # schemas exist before anything uses them
    assert by_type["became-enum"]["x-change"]["targets"] == ["schema:Thing.size"]
    assert by_type["became-enum"]["x-change"]["breaking"] is True
    assert by_type["became-enum"]["x-change"]["requires"] == ["s1-add-model"]
    assert by_type["add-optional-parameter"]["x-change"]["targets"] == ["parameter:GET /things/{id}?fields"]
    assert equivalent(apply_overlays(old, docs), new)


def test_each_overlay_applies_after_only_what_it_requires(breaking_of):
    old, new = MINI_SPEC, next_version(MINI_SPEC)
    docs = dict(overlays_for_step(diff_documents(old, new), "s1", "Mini v2", breaking_of, previous=[]))
    for oid, doc in docs.items():
        needed = [(r, docs[r]) for r in doc["x-change"].get("requires", [])]
        apply_overlays(old, needed + [(oid, doc)])  # raises if a target is missing


def test_a_later_step_requires_the_previous_steps_overlays(breaking_of):
    v2 = next_version(MINI_SPEC)
    v3 = copy.deepcopy(v2)
    v3["components"]["schemas"]["Unit"]["enum"].append("MM")
    docs = overlays_for_step(diff_documents(v2, v3), "s2", "Mini v3", breaking_of, previous=["s1-add-model", "s1-became-enum"])
    (oid, doc), = docs
    assert oid == "s2-add-enum-value"
    assert doc["x-change"]["requires"] == ["s1-add-model", "s1-became-enum"]


def test_a_dropped_required_list_goes_as_a_whole(breaking_of):
    new = copy.deepcopy(MINI_SPEC)
    del new["components"]["schemas"]["Thing"]["required"]
    (oid, doc), = overlays_for_step(diff_documents(MINI_SPEC, new), "s1", "x", breaking_of, previous=[])
    assert oid == "s1-property-became-optional"
    out = apply_overlays(MINI_SPEC, [(oid, doc)])
    assert "required" not in out["components"]["schemas"]["Thing"]  # not `required: []`
    assert equivalent(out, new)


def test_an_unlabelled_kind_of_change_is_an_error():
    new = copy.deepcopy(MINI_SPEC)
    new["servers"] = [{"url": "https://example.com"}]
    with pytest.raises(SnapshotDiffError, match="servers"):
        diff_documents(MINI_SPEC, new)


def test_equivalent_ignores_only_orderings_overlays_cannot_keep():
    a = copy.deepcopy(MINI_SPEC)
    b = copy.deepcopy(MINI_SPEC)
    b["components"]["schemas"]["Thing"]["required"] = list(reversed(a["components"]["schemas"]["Thing"]["required"] + ["size"]))
    a["components"]["schemas"]["Thing"]["required"].append("size")
    b["info"]["version"] = "9.9.9"
    assert equivalent(a, b)
    b["components"]["schemas"]["Thing"]["properties"]["size"]["type"] = "number"
    assert not equivalent(a, b)
