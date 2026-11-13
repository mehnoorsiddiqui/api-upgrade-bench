"""`bench export-dep-agent`: the dep-agent `config/plugins.yaml` entries for bench APIs.

Each bench plugin repo is a single plugin (`.claude-plugin/plugin.json` at the root) versioned in
lockstep with its SDK, so dep-agent reads the plugin's own `version` as the SDK stamp.
"""

from __future__ import annotations

from .config import Bench
from .lock import load_lock


def export(bench: Bench, apis: list[str] | None = None) -> str:
    blocks = []
    for name in apis or bench.api_names():
        api = bench.api(name)
        lock = load_lock(api)
        released = ", ".join(lock) if lock else "none yet"
        blocks.append(
            "\n".join(
                [
                    f"{api.nuget_package_id}:",
                    f"  # api-upgrade-bench apis/{api.name} - released: {released}",
                    f"  marketplace: https://github.com/{api.plugin_repo}",
                    f"  plugin: {api.plugin_id}             # the `name` in its .claude-plugin/plugin.json",
                    "  stamp_field: version",
                ]
            )
        )
    return "\n\n".join(blocks) + "\n"


def change_matrix(bench: Bench, apis: list[str] | None = None) -> list[dict]:
    """Per released version: which change types it contains (for scoring benchmark runs)."""
    rows = []
    for name in apis or bench.api_names():
        api = bench.api(name)
        for version, entry in load_lock(api).items():
            rows.append(
                {
                    "api": name,
                    "package_id": api.nuget_package_id,
                    "version": version,
                    "change_types": entry.get("change_types") or [],
                    "overlays": entry.get("overlays") or [],
                    "plugin_commit": (entry.get("plugin") or {}).get("commit"),
                    "generator_drift": entry.get("generator_drift", False),
                }
            )
    return rows
