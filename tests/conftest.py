from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from bench.config import Bench

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

MINI_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Mini", "version": "1.0.0"},
    "paths": {
        "/things/{id}": {
            "get": {
                "operationId": "getThing",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "verbose", "in": "query", "required": False, "schema": {"type": "boolean"}},
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}},
                    }
                },
            }
        }
    },
    "components": {
        "schemas": {
            "Thing": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "string"}, "size": {"type": "integer"}},
            }
        }
    },
}

RENAME_OP = {
    "overlay": "1.0.0",
    "info": {"title": "rename getThing", "version": "1.0.0"},
    "x-change": {"type": "rename-operation", "breaking": True, "targets": ["operation:GET /things/{id}"]},
    "actions": [{"target": "$.paths['/things/{id}'].get", "update": {"operationId": "fetchThing"}}],
}

RENAME_PARAM = {
    "overlay": "1.0.0",
    "info": {"title": "rename verbose", "version": "1.0.0"},
    "x-change": {"type": "rename-parameter", "breaking": True, "targets": ["parameter:GET /things/{id}?verbose"]},
    "actions": [
        {"target": "$.paths['/things/{id}'].get.parameters[?(@.name == 'verbose')]", "update": {"name": "detailed"}}
    ],
}


def write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture
def mini_repo(tmp_path: Path, monkeypatch) -> Bench:
    """A bench repo with the real catalog and schemas and one small API, `mini`."""
    root = tmp_path / "bench"
    shutil.copytree(REPO / "catalog", root / "catalog")
    shutil.copytree(REPO / "schemas", root / "schemas")
    api = root / "apis" / "mini"
    write_yaml(
        api / "api.yaml",
        {
            "name": "mini",
            "profile_id": "0123456789abcdef01234567",
            "nuget_package_id": "ApimaticBench.Mini",
            "sdk_repo": "owner/bench-mini-sdk-csharp",
            "plugin_repo": "owner/bench-mini-context-plugin",
            "plugin_id": "bench-mini",
            "plugin_name": "Bench Mini",
        },
    )
    write_yaml(api / "spec" / "openapi.yaml", MINI_SPEC)
    write_yaml(api / "overlays" / "rename-op.yaml", RENAME_OP)
    write_yaml(api / "overlays" / "rename-param.yaml", RENAME_PARAM)
    write_yaml(
        api / "releases.yaml",
        {"base": "1.0.0", "releases": [{"version": "2.0.0", "overlays": ["rename-op"]}]},
    )
    monkeypatch.setenv("BENCH_ROOT", str(root))
    monkeypatch.setenv("BENCH_BUILD_DIR", str(tmp_path / "build"))
    return Bench(root)


def oasdiff_available() -> bool:
    return bool(os.environ.get("OASDIFF") or shutil.which("oasdiff"))


needs_oasdiff = pytest.mark.skipif(not oasdiff_available(), reason="oasdiff not installed (set OASDIFF)")
