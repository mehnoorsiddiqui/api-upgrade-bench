"""Turn one Square api.json snapshot into the generator input the bench uses.

Every rule changes the generator input, never the API on the wire, and all of them run on every
snapshot, so none of them can appear as a change between two steps. In order:

1. info.title -> "Square". It is "Square Connect API" up to S2 and "Square" from S3; Codegen v4
   names the C# project, namespace and client after it, so the whole SDK would rename at S3.
2. Dangling schema $refs are resolved, using the later snapshots:
   * S0-S2 point 31 short names (e.g. `LocationCustomAttributeDeleteRequest`) at definitions that
     exist under qualified names. Square fixed them in S3: each is pointed where the first later
     snapshot points the same JSON path, provided that target already exists here.
   * A definition missing here but defined in a later snapshot is copied from it
     (S5: CatalogModifierToggleOverrideType).
   * Anything still missing becomes an open object marked `x-bench-placeholder`
     (S6: AppFeeAllocation, CurrencyExchange; missing from Square's spec to this day).
3. `$ref`s to Square's CDN inside `example` values are dropped (examples are data, not schemas).
4. Swagger 2.0 (S0-S2) -> OpenAPI 3.0.0, laid out the way Square's own OpenAPI 3 files are
   (Codegen v4 rejects Square's Swagger 2.0 files). Square's own switch (commit 9bb37051bc) also
   turned `x-read-only` into `readOnly` and dropped `x-is-beta` / `x-is-deprecated` (they repeat
   `x-release-status`); the conversion does the same, and drops the per-operation
   `x-oauthpermissions` / `x-sq-version` that Square's OpenAPI 3 files no longer carry.
   An operation missing a parameter its path names gets it from another operation on the path
   (from S3 on, PUT /v2/vendors/{vendor_id} does not define `vendor_id`).
5. Auth: operations secured by Square's `oauth2` scheme use a `bearerAuth` (HTTP bearer) scheme
   instead, which carries the `Square-Version` header block Square puts on `oauth2`. Codegen v4
   turns `oauth2` into an interactive authorization-code flow with no way to pass an access token,
   and integrations (like the nopCommerce plugin) run OAuth themselves and hand the SDK a token,
   which is what Square's own APIMatic SDK did (`BearerAuthModel`). `oauth2ClientSecret` stays.
6. Servers: Production and Sandbox (the spec lists Production only).
7. info.version is set by the caller (the bench version).

Usage: python normalize.py SNAPSHOT OUT --version X.Y.Z [--later LATER.json ...] [--report R.json]
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

SERVERS = [
    {"url": "https://connect.squareup.com", "description": "Production"},
    {"url": "https://connect.squareupsandbox.com", "description": "Sandbox"},
]
BEARER_DESCRIPTION = "A Square OAuth access token or personal access token, sent as `Authorization: Bearer <token>`."


# --------------------------------------------------------------------------- helpers


def schemas_of(spec: dict) -> tuple[dict, str]:
    if "definitions" in spec:
        return spec["definitions"], "#/definitions/"
    return spec["components"]["schemas"], "#/components/schemas/"


def walk_refs(node, path, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                out.append((tuple(path), node))
            else:
                walk_refs(value, path + [key], out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            walk_refs(value, path + [i], out)


# --------------------------------------------------------------------------- 1-3: title, refs, examples


def fix_refs(spec: dict, later: list[dict], report: dict) -> None:
    defs, prefix = schemas_of(spec)
    later_defs = [schemas_of(s)[0] for s in later]
    refs: list = []
    walk_refs(defs, [], refs)
    for path, holder in refs:
        ref = holder["$ref"]
        if not ref.startswith(prefix) or ref[len(prefix):] in defs:
            continue
        name = ref[len(prefix):]
        # where a later snapshot points the same JSON path (relative to the schema map)
        target = None
        for ldefs in later_defs:
            node = ldefs
            try:
                for p in path:
                    node = node[p]
                candidate = node["$ref"].rsplit("/", 1)[-1]
            except (KeyError, IndexError, TypeError):
                continue
            if candidate in defs:
                target = candidate
                break
        if target:
            holder["$ref"] = prefix + target
            report["retargeted"].append({"at": "/".join(map(str, path)), "from": name, "to": target})
            continue
        source = next((ld[name] for ld in later_defs if name in ld), None)
        if source is not None:
            defs[name] = json.loads(json.dumps(source).replace("#/components/schemas/", prefix))
            report["copied"].append(name)
        else:
            defs[name] = {
                "type": "object",
                "additionalProperties": True,
                "description": f"{name} is referenced but not defined in Square's published spec; "
                "added as an open object by the benchmark normalizer.",
                "x-bench-placeholder": True,
            }
            report["placeholders"].append(name)
    refs = []
    walk_refs(spec, [], refs)
    dangling = sorted({h["$ref"] for _, h in refs if h["$ref"].startswith(prefix) and h["$ref"][len(prefix):] not in defs})
    if dangling:
        raise SystemExit(f"still dangling after the fix: {dangling}")


def strip_example_refs(node, in_example=False) -> int:
    removed = 0
    if isinstance(node, dict):
        if in_example and isinstance(node.get("$ref"), str) and node["$ref"].startswith("http"):
            del node["$ref"]
            removed += 1
        for key, value in node.items():
            removed += strip_example_refs(value, in_example or key in ("example", "examples", "x-example"))
    elif isinstance(node, list):
        for value in node:
            removed += strip_example_refs(value, in_example)
    return removed


# --------------------------------------------------------------------------- 4: Swagger 2.0 -> OpenAPI 3.0

PARAM_SCHEMA_KEYS = (
    "type", "format", "items", "enum", "default", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems", "multipleOf",
)
DROPPED_OP_KEYS = {"x-oauthpermissions", "x-sq-version", "consumes", "produces", "schemes"}
FLOWS = {"accessCode": "authorizationCode", "application": "clientCredentials", "implicit": "implicit", "password": "password"}


def _rewrite_schema_keys(node):
    """x-read-only -> readOnly (same position); drop x-is-beta / x-is-deprecated; fix $ref prefixes."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("x-is-beta", "x-is-deprecated"):
                continue
            if k == "x-read-only":
                out["readOnly"] = v
            elif k == "$ref" and isinstance(v, str):
                out[k] = v.replace("#/definitions/", "#/components/schemas/")
            else:
                out[k] = _rewrite_schema_keys(v)
        return out
    if isinstance(node, list):
        return [_rewrite_schema_keys(v) for v in node]
    return node


def _convert_param(p: dict) -> dict:
    out, schema = {}, {}
    for k, v in p.items():
        if k in PARAM_SCHEMA_KEYS:
            schema[k] = v
        elif k == "collectionFormat":
            out["style"], out["explode"] = ("form", True) if v == "multi" else ("form", False)
        else:
            out[k] = v
    ordered: dict = {}
    for k, v in out.items():  # Square's OpenAPI 3 files: name, description, schema, in, required
        ordered[k] = v
        if k == "description":
            ordered["schema"] = schema
    ordered.setdefault("schema", schema)
    return ordered


def _convert_op(op: dict, media: str) -> dict:
    out: dict = {}
    for k, v in op.items():
        if k in DROPPED_OP_KEYS:
            continue
        if k == "parameters":
            params, body = [], None
            for p in v:
                if p.get("in") == "body":
                    body = p
                elif p.get("in") == "formData":
                    raise SystemExit("formData parameters are not handled (Square S0-S2 have none)")
                else:
                    params.append(_convert_param(p))
            out["parameters"] = params
            if body is not None:
                rb = {k2: body[k2] for k2 in ("required", "description") if k2 in body}
                rb["content"] = {media: {"schema": body["schema"]}}
                out["requestBody"] = rb
        elif k == "responses":
            responses = {}
            for code, r in v.items():
                nr = {kk: vv for kk, vv in r.items() if kk not in ("schema", "examples", "headers")}
                if "schema" in r:
                    nr["content"] = {media: {"schema": r["schema"]}}
                if "headers" in r:
                    nr["headers"] = {
                        h: {**{kk: vv for kk, vv in hv.items() if kk not in PARAM_SCHEMA_KEYS},
                            "schema": {kk: vv for kk, vv in hv.items() if kk in PARAM_SCHEMA_KEYS}}
                        for h, hv in r["headers"].items()
                    }
                responses[code] = nr
            out["responses"] = responses
        else:
            out[k] = v
    return out


def swagger2_to_oas3(src: dict) -> dict:
    versions = sorted({op.get("x-sq-version") for item in src["paths"].values() for op in item.values()
                       if isinstance(op, dict) and op.get("x-sq-version")})
    media = (src.get("consumes") or ["application/json"])[0]
    out: dict = {"openapi": "3.0.0", "info": src["info"]}
    if "externalDocs" in src:
        out["externalDocs"] = src["externalDocs"]
    out["servers"] = [{"url": f"{(src.get('schemes') or ['https'])[0]}://{src['host']}{src.get('basePath', '')}"}]
    schemes = {}
    for name, sd in src.get("securityDefinitions", {}).items():
        if sd.get("type") == "oauth2":
            flow = {k: sd[k] for k in ("authorizationUrl", "tokenUrl") if k in sd}
            flow["scopes"] = sd.get("scopes", {})
            ns: dict = {"type": "oauth2"}
            if versions:  # where Square's OpenAPI 3 files keep the API version
                ns["x-additional-headers"] = [{"name": "Square-Version", "description": "Square Connect API versions",
                                               "schema": {"default": versions[-1]}}]
            ns["flows"] = {FLOWS[sd["flow"]]: flow}
            schemes[name] = ns
        else:
            schemes[name] = dict(sd)
    out["components"] = {"securitySchemes": schemes, "schemas": src.get("definitions", {})}
    out["paths"] = {p: {m: (_convert_op(op, media) if isinstance(op, dict) and m != "parameters" else op)
                        for m, op in item.items()} for p, item in src["paths"].items()}
    for k, v in src.items():
        if k.startswith("x-") or k == "tags":
            out[k] = v
    return _rewrite_schema_keys(out)


# --------------------------------------------------------------------------- 4b: path parameters

PATH_TEMPLATE = re.compile(r"\{([^}]+)\}")


def fix_path_params(spec: dict, report: dict) -> None:
    """An operation whose path names a parameter it does not define gets the definition from another
    operation on the same path (from S3 on, PUT /v2/vendors/{vendor_id} lacks `vendor_id`; GET has it).
    Without it the SDK method cannot fill in the URL."""
    for path, item in spec["paths"].items():
        names = PATH_TEMPLATE.findall(path)
        ops = {m: op for m, op in item.items() if isinstance(op, dict) and m != "parameters"}
        shared = [p for p in item.get("parameters", []) if p.get("in") == "path"]
        for method, op in ops.items():
            defined = {p["name"] for p in op.get("parameters", []) + shared if p.get("in") == "path"}
            for name in names:
                if name in defined:
                    continue
                donor = next((p for other in ops.values() for p in other.get("parameters", [])
                              if p.get("in") == "path" and p.get("name") == name), None)
                param = copy.deepcopy(donor) if donor else {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
                op.setdefault("parameters", []).insert(0, param)
                report["path_params_added"].append(f"{method.upper()} {path} {name}")


# --------------------------------------------------------------------------- 5-7: auth, servers, version


def use_bearer(spec: dict) -> int:
    schemes = spec["components"]["securitySchemes"]
    oauth2 = schemes.pop("oauth2")
    bearer = {"type": "http", "scheme": "bearer", "description": BEARER_DESCRIPTION}
    if "x-additional-headers" in oauth2:
        bearer["x-additional-headers"] = oauth2["x-additional-headers"]
    spec["components"]["securitySchemes"] = {"bearerAuth": bearer, **schemes}
    changed = 0
    for item in spec["paths"].values():
        for op in item.values():
            if not isinstance(op, dict) or "security" not in op:
                continue
            new = []
            for requirement in op["security"]:
                if "oauth2" in requirement:
                    requirement = {("bearerAuth" if k == "oauth2" else k): ([] if k == "oauth2" else v)
                                   for k, v in requirement.items()}
                    changed += 1
                new.append(requirement)
            op["security"] = new
    return changed


def normalize(snapshot: dict, later: list[dict], version: str) -> tuple[dict, dict]:
    spec = copy.deepcopy(snapshot)
    report: dict = {"title": [spec["info"].get("title"), "Square"], "retargeted": [], "copied": [], "placeholders": []}
    spec["info"]["title"] = "Square"
    fix_refs(spec, later, report)
    report["example_refs_removed"] = strip_example_refs(spec)
    if spec.get("swagger") == "2.0":
        spec = swagger2_to_oas3(spec)
        report["converted"] = "swagger 2.0 -> openapi 3.0.0"
    report["path_params_added"] = []
    fix_path_params(spec, report)
    report["bearer_operations"] = use_bearer(spec)
    spec["servers"] = copy.deepcopy(SERVERS)
    spec["info"]["version"] = version
    return spec, report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot")
    ap.add_argument("out")
    ap.add_argument("--version", required=True)
    ap.add_argument("--later", nargs="*", default=[], help="later snapshots, nearest first")
    ap.add_argument("--report")
    args = ap.parse_args()
    load = lambda p: json.loads(Path(p).read_text(encoding="utf-8"))  # noqa: E731
    spec, report = normalize(load(args.snapshot), [load(p) for p in args.later], args.version)
    Path(args.out).write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: (len(v) if isinstance(v, list) and k != "title" else v) for k, v in report.items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
