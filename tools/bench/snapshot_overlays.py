"""Express the difference between two OpenAPI 3.0 documents as labelled overlays.

Used to bring a real API's release history into the bench: `old` is the previous snapshot (or the
base), `new` the next one. Every difference is split into *atomic edits* on disjoint parts of the
document, each edit is labelled with one catalog change type, and the edits of one type become one
Overlay 1.0 document. Applying all of them (in `order_overlays` order) to `old` gives `new`, up to
the orderings `equivalent` ignores.

Atoms never overlap, so the overlays of one step commute, except that new schemas must exist before
anything references them (`add-model` goes first) and removed schemas must be unreferenced
(`remove-model` goes last).

Labelling rules, by where the difference is:

* paths / operations: new or removed path or method -> add-endpoint / remove-endpoint;
  `deprecated` / `x-deprecation` -> deprecate-endpoint; operationId -> rename-operation;
  security -> auth-scheme-change; parameters matched by (name, in): new -> add-optional-parameter /
  add-required-parameter, removed -> remove-parameter, `required` flipped -> parameter-became-required
  / parameter-became-optional, schema -> as for schemas below.
* components.schemas: new / removed schema -> add-model / remove-model.
* inside any schema: a different `$ref` or `type` (a "shape" change) -> became-enum when the new
  shape is an enum and the old one was not; change-property-model when a `$ref` points at another
  schema; else change-property-type (change-parameter-type for a parameter); properties added / removed -> add-optional-property / add-required-property /
  remove-property; a name entering / leaving `required` -> property-became-required /
  property-became-optional; `enum` -> add-enum-value / remove-enum-value; `nullable` ->
  nullability-change; `readOnly` / `writeOnly` set -> property-became-read-only, unset ->
  property-became-writable; validation keywords and `default` -> change-constraint.
* `x-release-status`, and `deprecated` / `x-deprecation` on a schema -> release-status-change;
  descriptions, summaries, examples, tags, titles and other `x-` extensions -> docs-and-metadata.

Anything the rules do not cover raises, so a new kind of difference cannot slip through unlabelled.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# keys whose change is a change of the value's shape (what the SDK type is)
SHAPE_KEYS = ("$ref", "type", "format", "items", "allOf", "oneOf", "anyOf", "additionalProperties", "properties", "enum", "required")
CONSTRAINT_KEYS = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "pattern",
    "minItems", "maxItems", "uniqueItems", "multipleOf", "minProperties", "maxProperties", "default",
}
DOC_KEYS = {"description", "title", "example", "examples", "externalDocs", "summary", "tags", "discriminator", "xml"}
RELEASE_STATUS_KEYS = {"x-release-status"}

# Overlay order within a step: schemas are added first and removed last.
FIRST, LAST = "add-model", "remove-model"


class SnapshotDiffError(Exception):
    pass


# --------------------------------------------------------------------------- paths


@dataclass(frozen=True)
class Filter:
    """A JSONPath filter segment selecting one list item, e.g. a parameter by name and location."""

    expr: str


Path = tuple  # of str | Filter


def _quote(key: str) -> str:
    if "'" in key or "\\" in key:
        raise SnapshotDiffError(f"cannot express key {key!r} in a JSONPath")
    return f"['{key}']"


def jsonpath(path: Path) -> str:
    out = "$"
    for seg in path:
        out += f"[?({seg.expr})]" if isinstance(seg, Filter) else _quote(str(seg))
    return out


def param_filter(param: dict) -> Filter:
    return Filter(f"@.name == '{param['name']}' && @.in == '{param['in']}'")


# --------------------------------------------------------------------------- atoms


@dataclass
class Atom:
    type: str
    target: str  # a label for x-change.targets, e.g. schema:Payment.amount_money
    actions: list[dict]


@dataclass
class StepDiff:
    atoms: list[Atom] = field(default_factory=list)

    def add(self, type_: str, target: str, *actions: dict) -> None:
        self.atoms.append(Atom(type_, target, list(actions)))

    def by_type(self) -> dict[str, list[Atom]]:
        out: dict[str, list[Atom]] = {}
        for atom in self.atoms:
            out.setdefault(atom.type, []).append(atom)
        return out


def _set(path: Path, key: str, old_parent: dict, value: Any) -> list[dict]:
    """Actions that make parent[key] == value (in place when the old value is a scalar)."""
    actions = []
    current = old_parent.get(key, _MISSING) if isinstance(old_parent, dict) else _MISSING
    if current is not _MISSING and isinstance(current, (dict, list)):
        # update merges objects and appends to arrays, so a container is replaced by remove + add
        actions.append({"target": jsonpath(path + (key,)), "remove": True})
    actions.append({"target": jsonpath(path), "update": {key: copy.deepcopy(value)}})
    return actions


def _remove(path: Path, key: str) -> dict:
    return {"target": jsonpath(path + (key,)), "remove": True}


_MISSING = object()


# --------------------------------------------------------------------------- schemas


def _is_enum_shape(schema: dict, schemas: dict) -> bool:
    if not isinstance(schema, dict):
        return False
    if "enum" in schema:
        return True
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        return "enum" in schemas.get(ref.rsplit("/", 1)[-1], {})
    if schema.get("type") == "array":
        return _is_enum_shape(schema.get("items"), schemas)
    return False


def _classify_key(key: str) -> str | None:
    if key in CONSTRAINT_KEYS:
        return "change-constraint"
    if key == "nullable":
        return "nullability-change"
    if key in RELEASE_STATUS_KEYS or key in ("deprecated", "x-deprecation"):
        return "release-status-change"
    if key in DOC_KEYS or key.startswith("x-"):
        return "docs-and-metadata"
    return None


def diff_schema(out: StepDiff, path: Path, old: dict, new: dict, label: str, ctx: "Ctx", in_param: bool = False) -> None:
    if old == new:
        return
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise SnapshotDiffError(f"{label}: schema is not an object")

    shape_changed = old.get("$ref") != new.get("$ref") or old.get("type") != new.get("type") or any(
        old.get(k) != new.get(k) for k in ("allOf", "oneOf", "anyOf")
    ) or (not isinstance(old.get("additionalProperties", {}), dict) or not isinstance(new.get("additionalProperties", {}), dict)) and old.get("additionalProperties") != new.get("additionalProperties")
    if shape_changed:
        became_enum = _is_enum_shape(new, ctx.new_schemas) and not _is_enum_shape(old, ctx.old_schemas)
        model_swap = "$ref" in old and "$ref" in new and not became_enum
        if became_enum:
            type_ = "became-enum"
        elif in_param:
            type_ = "change-parameter-type"
        elif model_swap:
            type_ = "change-property-model"
        else:
            type_ = "change-property-type"
        actions = []
        for key in SHAPE_KEYS:
            if old.get(key, _MISSING) == new.get(key, _MISSING):
                continue
            if key in new:
                actions += _set(path, key, old, new[key])
            else:
                actions.append(_remove(path, key))
        out.add(type_, label, *actions)
        handled = set(SHAPE_KEYS)
    else:
        handled = set()

    keys = list(old) + [k for k in new if k not in old]
    for key in keys:
        if key in handled:
            continue
        o, n = old.get(key, _MISSING), new.get(key, _MISSING)
        if o == n:
            continue
        if key == "properties":
            _diff_properties(out, path, old, new, label, ctx)
        elif key == "required":
            _diff_required(out, path, old, new, label)
        elif key == "enum":
            o_set = set(map(_hashable, o if o is not _MISSING else []))
            n_set = set(map(_hashable, n if n is not _MISSING else []))
            type_ = "remove-enum-value" if o_set - n_set else "add-enum-value"
            out.add(type_, f"{label} enum", *(_set(path, key, old, n) if n is not _MISSING else [_remove(path, key)]))
        elif key in ("readOnly", "writeOnly"):
            type_ = "property-became-read-only" if n is True else "property-became-writable"
            out.add(type_, f"{label} {key}", *(_set(path, key, old, n) if n is not _MISSING else [_remove(path, key)]))
        elif key in ("items", "additionalProperties") and isinstance(o, dict) and isinstance(n, dict):
            diff_schema(out, path + (key,), o, n, f"{label}[{key}]", ctx, in_param)
        else:
            type_ = _classify_key(key)
            if type_ is None:
                raise SnapshotDiffError(f"{label}: no change type for schema keyword {key!r}")
            out.add(type_, f"{label} {key}", *(_set(path, key, old, n) if n is not _MISSING else [_remove(path, key)]))


def _hashable(v):
    return repr(v) if isinstance(v, (dict, list)) else v


def _diff_properties(out: StepDiff, path: Path, old: dict, new: dict, label: str, ctx: "Ctx") -> None:
    op = old.get("properties") or {}
    np_ = new.get("properties") or {}
    new_required = set(new.get("required") or [])
    if "properties" not in old:  # the first properties arrive together, in one edit
        type_ = "add-required-property" if new_required & set(np_) else "add-optional-property"
        out.add(type_, f"{label}.*", {"target": jsonpath(path), "update": {"properties": copy.deepcopy(np_)}})
        return
    for name in op:
        if name not in np_:
            out.add("remove-property", f"{label}.{name}", _remove(path + ("properties",), name))
    for name, schema in np_.items():
        if name not in op:
            type_ = "add-required-property" if name in new_required else "add-optional-property"
            out.add(type_, f"{label}.{name}", {"target": jsonpath(path + ("properties",)), "update": {name: copy.deepcopy(schema)}})
        else:
            diff_schema(out, path + ("properties", name), op[name], schema, f"{label}.{name}", ctx)
    if "properties" in old and "properties" not in new:
        out.add("remove-property", f"{label}.*", _remove(path, "properties"))


def _diff_required(out: StepDiff, path: Path, old: dict, new: dict, label: str) -> None:
    o = list(old.get("required") or [])
    n = list(new.get("required") or [])
    old_props, new_props = old.get("properties") or {}, new.get("properties") or {}
    if "required" in old and "required" not in new:
        # the whole list goes in one edit: removing the names one by one would leave `required: []`,
        # which the generator rejects
        gone = [name for name in o if name in old_props and name not in new_props]
        type_ = "remove-property" if o and len(gone) == len(o) else "property-became-optional"
        out.add(type_, f"{label}.{','.join(o)} required", _remove(path, "required"))
        return
    for name in o:
        if name not in n:
            type_ = "remove-property" if name in old_props and name not in new_props else "property-became-optional"
            out.add(type_, f"{label}.{name} required", {"target": jsonpath(path + ("required", Filter(f"@ == '{name}'"))), "remove": True})
    added = [name for name in n if name not in o]
    for name in added:
        type_ = "add-required-property" if name in new_props and name not in old_props else "property-became-required"
        if "required" in old:
            action = {"target": jsonpath(path + ("required",)), "update": [name]}
        else:
            action = {"target": jsonpath(path), "update": {"required": [name]}}
        out.add(type_, f"{label}.{name} required", action)


# --------------------------------------------------------------------------- operations


METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


@dataclass
class Ctx:
    old_schemas: dict
    new_schemas: dict


def _op_label(p: str, m: str) -> str:
    return f"{m.upper()} {p}"


def diff_operation(out: StepDiff, path: Path, old: dict, new: dict, p: str, m: str, ctx: Ctx) -> None:
    label = _op_label(p, m)
    keys = list(old) + [k for k in new if k not in old]
    for key in keys:
        o, n = old.get(key, _MISSING), new.get(key, _MISSING)
        if o == n:
            continue
        if key == "parameters":
            _diff_parameters(out, path, o if o is not _MISSING else [], n if n is not _MISSING else [], old, label, ctx)
        elif key == "requestBody":
            if o is _MISSING or n is _MISSING:
                raise SnapshotDiffError(f"{label}: request body added or removed")
            _diff_media_holder(out, path + ("requestBody",), o, n, f"{label} body", ctx)
        elif key == "responses":
            for code in list(o) + [c for c in n if c not in o]:
                if o.get(code) == n.get(code):
                    continue
                if code not in o or code not in n:
                    raise SnapshotDiffError(f"{label}: response {code} added or removed")
                _diff_media_holder(out, path + ("responses", code), o[code], n[code], f"{label} {code}", ctx)
        elif key == "security":
            out.add("auth-scheme-change", f"operation:{label}", *(_set(path, key, old, n) if n is not _MISSING else [_remove(path, key)]))
        elif key == "operationId":
            out.add("rename-operation", f"operation:{label}", *_set(path, key, old, n))
        elif key in ("deprecated", "x-deprecation"):
            out.add("deprecate-endpoint", f"operation:{label}", *(_set(path, key, old, n) if n is not _MISSING else [_remove(path, key)]))
        else:
            type_ = _classify_key(key)
            if type_ is None:
                raise SnapshotDiffError(f"{label}: no change type for operation key {key!r}")
            out.add(type_, f"operation:{label} {key}", *(_set(path, key, old, n) if n is not _MISSING else [_remove(path, key)]))


def _diff_media_holder(out: StepDiff, path: Path, old: dict, new: dict, label: str, ctx: Ctx) -> None:
    for key in list(old) + [k for k in new if k not in old]:
        o, n = old.get(key, _MISSING), new.get(key, _MISSING)
        if o == n:
            continue
        if key == "content" and o is not _MISSING and n is not _MISSING and set(o) == set(n):
            for media in o:
                if o[media] == n[media]:
                    continue
                mo, mn = o[media], n[media]
                for mkey in list(mo) + [k for k in mn if k not in mo]:
                    if mo.get(mkey) == mn.get(mkey):
                        continue
                    if mkey == "schema":
                        diff_schema(out, path + ("content", media, "schema"), mo["schema"], mn["schema"], label, ctx)
                    else:
                        type_ = _classify_key(mkey)
                        if type_ is None:
                            raise SnapshotDiffError(f"{label}: no change type for media key {mkey!r}")
                        out.add(type_, f"{label} {mkey}", *(_set(path + ("content", media), mkey, mo, mn[mkey]) if mkey in mn else [_remove(path + ("content", media), mkey)]))
        elif key == "required":
            out.add("change-constraint", f"{label} required", *(_set(path, key, old, n) if n is not _MISSING else [_remove(path, key)]))
        else:
            type_ = _classify_key(key)
            if type_ is None:
                raise SnapshotDiffError(f"{label}: no change type for {key!r}")
            out.add(type_, f"{label} {key}", *(_set(path, key, old, n) if n is not _MISSING else [_remove(path, key)]))


def _diff_parameters(out: StepDiff, path: Path, old: list, new: list, old_op: dict, label: str, ctx: Ctx) -> None:
    key = lambda prm: (prm["name"], prm["in"])  # noqa: E731
    o = {key(prm): prm for prm in old}
    n = {key(prm): prm for prm in new}
    for k, prm in o.items():
        if k not in n:
            out.add("remove-parameter", f"parameter:{label}?{k[0]}", {"target": jsonpath(path + ("parameters", param_filter(prm))), "remove": True})
    for k, prm in n.items():
        plabel = f"parameter:{label}?{k[0]}"
        if k not in o:
            type_ = "add-required-parameter" if prm.get("required") else "add-optional-parameter"
            if "parameters" in old_op:
                action = {"target": jsonpath(path + ("parameters",)), "update": [copy.deepcopy(prm)]}
            else:
                action = {"target": jsonpath(path), "update": {"parameters": [copy.deepcopy(prm)]}}
            out.add(type_, plabel, action)
            continue
        op_ = o[k]
        if op_ == prm:
            continue
        ppath = path + ("parameters", param_filter(op_))
        for pk in list(op_) + [x for x in prm if x not in op_]:
            po, pn = op_.get(pk, _MISSING), prm.get(pk, _MISSING)
            if po == pn:
                continue
            if pk == "schema":
                diff_schema(out, ppath + ("schema",), po, pn, plabel, ctx, in_param=True)
            elif pk == "required":
                out.add("parameter-became-required" if pn else "parameter-became-optional", plabel,
                        *(_set(ppath, pk, op_, pn) if pn is not _MISSING else [_remove(ppath, pk)]))
            else:
                type_ = _classify_key(pk) if pk not in ("style", "explode") else "change-parameter-type"
                if type_ is None:
                    raise SnapshotDiffError(f"{plabel}: no change type for parameter key {pk!r}")
                out.add(type_, f"{plabel} {pk}", *(_set(ppath, pk, op_, pn) if pn is not _MISSING else [_remove(ppath, pk)]))


# --------------------------------------------------------------------------- documents


def diff_documents(old: dict, new: dict) -> StepDiff:
    """All atomic edits that turn `old` into `new` (info.version and servers are left alone)."""
    out = StepDiff()
    ctx = Ctx(old.get("components", {}).get("schemas", {}), new.get("components", {}).get("schemas", {}))

    for key in list(old) + [k for k in new if k not in old]:
        o, n = old.get(key, _MISSING), new.get(key, _MISSING)
        if o == n or key in ("openapi",):
            continue
        if key == "info":
            for ik in list(o) + [k for k in n if k not in o]:
                if ik == "version" or o.get(ik) == n.get(ik):
                    continue
                out.add("docs-and-metadata", f"info.{ik}", *(_set(("info",), ik, o, n[ik]) if ik in n else [_remove(("info",), ik)]))
        elif key == "servers":
            raise SnapshotDiffError("servers differ; normalize them before diffing")
        elif key == "paths":
            _diff_paths(out, o, n, ctx)
        elif key == "components":
            _diff_components(out, o, n, ctx)
        elif key == "security":
            out.add("auth-scheme-change", "security", *(_set((), key, old, n) if n is not _MISSING else [_remove((), key)]))
        elif key in DOC_KEYS or key.startswith("x-"):
            out.add("docs-and-metadata", key, *(_set((), key, old, n) if n is not _MISSING else [_remove((), key)]))
        else:
            raise SnapshotDiffError(f"no change type for top-level key {key!r}")
    return out


def _diff_paths(out: StepDiff, old: dict, new: dict, ctx: Ctx) -> None:
    root = ("paths",)
    for p, item in old.items():
        if p not in new:
            out.add("remove-endpoint", f"path:{p}", _remove(root, p))
    for p, item in new.items():
        if p not in old:
            out.add("add-endpoint", f"path:{p}", {"target": jsonpath(root), "update": {p: copy.deepcopy(item)}})
            continue
        oi = old[p]
        if oi == item:
            continue
        for m in list(oi) + [x for x in item if x not in oi]:
            om, nm = oi.get(m, _MISSING), item.get(m, _MISSING)
            if om == nm:
                continue
            if m not in METHODS:
                raise SnapshotDiffError(f"path {p}: no change type for path-item key {m!r}")
            if nm is _MISSING:
                out.add("remove-endpoint", f"operation:{_op_label(p, m)}", _remove(root + (p,), m))
            elif om is _MISSING:
                out.add("add-endpoint", f"operation:{_op_label(p, m)}", {"target": jsonpath(root + (p,)), "update": {m: copy.deepcopy(nm)}})
            else:
                diff_operation(out, root + (p, m), om, nm, p, m, ctx)


def _diff_components(out: StepDiff, old: dict, new: dict, ctx: Ctx) -> None:
    for section in list(old) + [s for s in new if s not in old]:
        o, n = old.get(section, {}), new.get(section, {})
        if o == n:
            continue
        root = ("components", section)
        if section == "schemas":
            for name in o:
                if name not in n:
                    out.add("remove-model", f"schema:{name}", _remove(root, name))
            for name, schema in n.items():
                if name not in o:
                    out.add("add-model", f"schema:{name}", {"target": jsonpath(root), "update": {name: copy.deepcopy(schema)}})
                elif o[name] != schema:
                    diff_schema(out, root + (name,), o[name], schema, f"schema:{name}", ctx)
        elif section == "securitySchemes":
            for name in list(o) + [x for x in n if x not in o]:
                so, sn = o.get(name, _MISSING), n.get(name, _MISSING)
                if so == sn:
                    continue
                if so is _MISSING or sn is _MISSING:
                    raise SnapshotDiffError(f"security scheme {name} added or removed")
                for sk in list(so) + [x for x in sn if x not in so]:
                    if so.get(sk) == sn.get(sk):
                        continue
                    type_ = _classify_key(sk) or "auth-scheme-change"
                    out.add(type_, f"securityScheme:{name} {sk}",
                            *(_set(root + (name,), sk, so, sn[sk]) if sk in sn else [_remove(root + (name,), sk)]))
        elif section.startswith("x-"):
            out.add("docs-and-metadata", f"components.{section}",
                    *(_set(("components",), section, old, n) if section in new else [_remove(("components",), section)]))
        else:
            raise SnapshotDiffError(f"no change type for components.{section}")


# --------------------------------------------------------------------------- overlays


def order_types(types: list[str]) -> list[str]:
    middle = sorted(t for t in types if t not in (FIRST, LAST))
    return ([FIRST] if FIRST in types else []) + middle + ([LAST] if LAST in types else [])


def overlays_for_step(diff: StepDiff, prefix: str, title: str, catalog_breaking: dict[str, bool],
                      previous: list[str]) -> list[tuple[str, dict]]:
    """[(overlay id, overlay doc)] in apply order. Each overlay requires the previous step's overlays
    (`previous`) and, within the step, `add-model` before and the rest before `remove-model`."""
    by_type = diff.by_type()
    ordered = order_types(list(by_type))
    ids = {t: f"{prefix}-{t}" for t in ordered}
    docs = []
    for t in ordered:
        if t not in catalog_breaking:
            raise SnapshotDiffError(f"change type {t!r} is not in the catalog")
        requires = list(previous)
        if t != FIRST and FIRST in by_type:
            requires.append(ids[FIRST])
        if t == LAST:
            requires += [ids[x] for x in ordered if x not in (FIRST, LAST)]
        atoms = by_type[t]
        targets = list(dict.fromkeys(a.target for a in atoms))
        change = {"type": t, "breaking": catalog_breaking[t], "targets": targets}
        if requires:
            change["requires"] = requires
        doc = {
            "overlay": "1.0.0",
            "info": {"title": f"{title}: {t} ({len(targets)})", "version": "1.0.0"},
            "x-change": change,
            "actions": [action for a in atoms for action in a.actions],
        }
        docs.append((ids[t], doc))
    return docs


# --------------------------------------------------------------------------- verification


def canonical(doc: Any, path: tuple = ()) -> Any:
    """`doc` with the orderings overlays cannot reproduce removed: object key order, the order of
    `required` names, and the order of operation parameters (matched by name and location)."""
    if isinstance(doc, dict):
        out = {}
        for k in sorted(doc):
            v = doc[k]
            if k == "required" and isinstance(v, list) and all(isinstance(x, str) for x in v):
                out[k] = sorted(v)
            elif k == "parameters" and isinstance(v, list) and all(isinstance(x, dict) and "name" in x for x in v):
                out[k] = [canonical(x) for x in sorted(v, key=lambda x: (x.get("name"), x.get("in")))]
            else:
                out[k] = canonical(v, path + (k,))
        return out
    if isinstance(doc, list):
        return [canonical(v) for v in doc]
    return doc


def equivalent(a: dict, b: dict) -> bool:
    a = {k: v for k, v in a.items()}
    b = {k: v for k, v in b.items()}
    for d in (a, b):
        d["info"] = {k: v for k, v in d.get("info", {}).items() if k != "version"}
    return canonical(a) == canonical(b)
