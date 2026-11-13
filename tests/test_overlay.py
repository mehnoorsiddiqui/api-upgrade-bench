import copy

import pytest

from bench.overlay import OverlayError, apply_overlay, apply_overlays

from conftest import MINI_SPEC, RENAME_OP, RENAME_PARAM


def overlay(*actions):
    return {"overlay": "1.0.0", "info": {"title": "t", "version": "1"}, "actions": list(actions)}


def test_update_merges_into_object():
    out = apply_overlay(MINI_SPEC, RENAME_OP)
    op = out["paths"]["/things/{id}"]["get"]
    assert op["operationId"] == "fetchThing"
    assert op["parameters"] == MINI_SPEC["paths"]["/things/{id}"]["get"]["parameters"]


def test_input_is_not_modified():
    before = copy.deepcopy(MINI_SPEC)
    apply_overlay(MINI_SPEC, RENAME_OP)
    assert MINI_SPEC == before


def test_filter_target_updates_matching_array_item():
    out = apply_overlay(MINI_SPEC, RENAME_PARAM)
    names = [p["name"] for p in out["paths"]["/things/{id}"]["get"]["parameters"]]
    assert names == ["id", "detailed"]


def test_nested_merge_recurses_and_concatenates_arrays():
    out = apply_overlay(
        MINI_SPEC,
        overlay(
            {
                "target": "$.components.schemas.Thing",
                "update": {"required": ["size"], "properties": {"size": {"format": "int64"}, "colour": {"type": "string"}}},
            }
        ),
    )
    thing = out["components"]["schemas"]["Thing"]
    assert thing["required"] == ["id", "size"]
    assert thing["properties"]["size"] == {"type": "integer", "format": "int64"}
    assert thing["properties"]["colour"] == {"type": "string"}


def test_update_on_array_target_appends():
    out = apply_overlay(
        MINI_SPEC,
        overlay(
            {
                "target": "$.paths['/things/{id}'].get.parameters",
                "update": {"name": "x", "in": "header", "schema": {"type": "string"}},
            }
        ),
    )
    assert [p["name"] for p in out["paths"]["/things/{id}"]["get"]["parameters"]] == ["id", "verbose", "x"]


def test_remove_object_member_and_array_items():
    out = apply_overlay(
        MINI_SPEC,
        overlay(
            {"target": "$.components.schemas.Thing.properties.size", "remove": True},
            {"target": "$.paths['/things/{id}'].get.parameters[*]", "remove": True},
        ),
    )
    assert "size" not in out["components"]["schemas"]["Thing"]["properties"]
    assert out["paths"]["/things/{id}"]["get"]["parameters"] == []


def test_actions_apply_in_order():
    out = apply_overlay(
        MINI_SPEC,
        overlay(
            {"target": "$.components.schemas.Thing.properties.size", "remove": True},
            {"target": "$.components.schemas.Thing.properties", "update": {"count": {"type": "integer"}}},
        ),
    )
    assert list(out["components"]["schemas"]["Thing"]["properties"]) == ["id", "count"]


def test_target_matching_nothing_is_an_error():
    with pytest.raises(OverlayError, match="matches nothing"):
        apply_overlay(MINI_SPEC, overlay({"target": "$.paths['/nope'].get", "update": {"operationId": "x"}}))


def test_filter_matching_nothing_is_an_error():
    with pytest.raises(OverlayError, match="matches nothing"):
        apply_overlay(
            MINI_SPEC,
            overlay({"target": "$.paths['/things/{id}'].get.parameters[?(@.name == 'nope')]", "remove": True}),
        )


@pytest.mark.parametrize(
    "action",
    [
        {"target": "$.info"},
        {"target": "$.info", "update": {"x": 1}, "remove": True},
        {"target": "info", "update": {"x": 1}},
    ],
)
def test_malformed_actions_are_rejected(action):
    with pytest.raises(OverlayError):
        apply_overlay(MINI_SPEC, overlay(action))


def test_scalar_target_cannot_be_updated():
    with pytest.raises(OverlayError, match="scalar"):
        apply_overlay(MINI_SPEC, overlay({"target": "$.info.title", "update": "x"}))


def test_not_an_overlay_document():
    with pytest.raises(OverlayError):
        apply_overlay(MINI_SPEC, {"actions": [{"target": "$.info", "update": {}}]})


def test_overlays_compose():
    out = apply_overlays(MINI_SPEC, [("a", RENAME_OP), ("b", RENAME_PARAM)])
    op = out["paths"]["/things/{id}"]["get"]
    assert op["operationId"] == "fetchThing"
    assert op["parameters"][1]["name"] == "detailed"
