"""Apply OpenAPI Overlay 1.0 documents (``update`` / ``remove`` actions).

Semantics follow the Overlay 1.0.0 specification:

* actions run in document order, each against the result of the previous one;
* ``remove: true`` deletes every node the target matches from its parent;
* ``update`` on an object target is merged recursively into it (objects merge, arrays
  concatenate, scalars replace); ``update`` on an array target appends the value (a list
  value appends each item).

Unlike a lenient implementation, a target that matches nothing is an error: an overlay that
silently stops applying (because the base spec moved) would make a release lie about what
it contains.
"""

from __future__ import annotations

import copy
from typing import Any

from jsonpath_ng import DatumInContext, Fields, Index
from jsonpath_ng.ext import parse as parse_jsonpath


class OverlayError(Exception):
    pass


def _deep_merge(target: dict, update: dict) -> None:
    for key, value in update.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _deep_merge(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            current.extend(copy.deepcopy(value))
        else:
            target[key] = copy.deepcopy(value)


def _parent_and_key(match: DatumInContext) -> tuple[Any, Any]:
    parent = match.context.value if match.context is not None else None
    path = match.path
    if isinstance(path, Fields) and len(path.fields) == 1:
        return parent, path.fields[0]
    if isinstance(path, Index):
        indices = getattr(path, "indices", None)
        if indices is None:  # jsonpath-ng < 1.6
            indices = (path.index,)
        if len(indices) == 1:
            return parent, indices[0]
    raise OverlayError(f"cannot locate parent of match at {match.full_path}")


def _apply_action(doc: Any, action: dict, where: str) -> None:
    target = action.get("target")
    if not isinstance(target, str) or not target.startswith("$"):
        raise OverlayError(f"{where}: 'target' must be a JSONPath starting with '$'")
    has_update = "update" in action
    remove = action.get("remove", False)
    if has_update == bool(remove):
        raise OverlayError(f"{where}: an action needs exactly one of 'update' or 'remove: true'")

    try:
        expr = parse_jsonpath(target)
    except Exception as exc:  # jsonpath-ng raises several exception types
        raise OverlayError(f"{where}: invalid JSONPath {target!r}: {exc}") from exc

    matches = expr.find(doc)
    if not matches:
        raise OverlayError(f"{where}: target {target!r} matches nothing")

    if remove:
        # Delete list items from the highest index down so earlier deletions do not shift later ones.
        located = [_parent_and_key(m) for m in matches]
        located.sort(key=lambda pk: pk[1] if isinstance(pk[1], int) else -1, reverse=True)
        for parent, key in located:
            if parent is None:
                raise OverlayError(f"{where}: cannot remove the document root")
            del parent[key]
        return

    update = action["update"]
    for match in matches:
        node = match.value
        if isinstance(node, dict):
            if not isinstance(update, dict):
                raise OverlayError(f"{where}: update for object target {target!r} must be an object")
            _deep_merge(node, update)
        elif isinstance(node, list):
            node.extend(copy.deepcopy(update if isinstance(update, list) else [update]))
        else:
            raise OverlayError(f"{where}: target {target!r} is a scalar; update an object or array instead")


def _apply_in_place(doc: Any, overlay: dict, name: str) -> None:
    if not isinstance(overlay, dict) or not str(overlay.get("overlay", "")).startswith("1."):
        raise OverlayError(f"{name}: not an Overlay 1.x document (missing 'overlay: 1.0.0')")
    actions = overlay.get("actions")
    if not isinstance(actions, list) or not actions:
        raise OverlayError(f"{name}: 'actions' must be a non-empty list")
    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            raise OverlayError(f"{name}: actions[{i}] must be an object")
        _apply_action(doc, action, f"{name}: actions[{i}]")


def apply_overlay(spec: Any, overlay: dict, name: str = "overlay") -> Any:
    """Return a new document with ``overlay``'s actions applied to ``spec`` (which is not modified)."""
    doc = copy.deepcopy(spec)
    _apply_in_place(doc, overlay, name)
    return doc


def apply_overlays(spec: Any, overlays: list[tuple[str, dict]]) -> Any:
    """Return a new document with the overlays applied in order (``spec`` is not modified).

    The document is copied once, not once per overlay: a real API's history can mean dozens of
    overlays on a multi-megabyte spec. Every action copies the values it inserts, so no overlay's
    data ends up shared with the result."""
    doc = copy.deepcopy(spec)
    for name, overlay in overlays:
        _apply_in_place(doc, overlay, name)
    return doc
