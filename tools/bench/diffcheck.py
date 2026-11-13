"""`bench diffcheck`: hold the spec diff to the change types a version declares.

Runs the pinned oasdiff's `changelog` (all levels: oasdiff rates several SDK-breaking edits, such
as an operationId change, as INFO, so `oasdiff breaking` would miss them) between the base spec
and the built spec, then compares the detected check ids with the catalog:

* unlabelled: a detected id that no declared change type requires or allows;
* missing:    a declared change type with a required group none of whose ids were detected.

Either one fails the check.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Catalog, BenchError


def oasdiff_exe() -> str:
    exe = os.environ.get("OASDIFF") or shutil.which("oasdiff")
    if not exe:
        raise BenchError("oasdiff not found: install the pinned version or set OASDIFF=/path/to/oasdiff")
    return exe


def run_oasdiff(base: Path, revision: Path) -> list[dict]:
    result = subprocess.run(
        [oasdiff_exe(), "changelog", str(base), str(revision), "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise BenchError(f"oasdiff changelog failed ({result.returncode}):\n{result.stderr or result.stdout}")
    out = result.stdout.strip()
    if not out:
        return []
    changes = json.loads(out)
    if changes is None:
        return []
    if not isinstance(changes, list):
        raise BenchError(f"unexpected oasdiff output: {out[:200]}")
    return changes


def oasdiff_version() -> str:
    result = subprocess.run([oasdiff_exe(), "--version"], capture_output=True, text=True)
    return result.stdout.strip().removeprefix("oasdiff version ").strip()


@dataclass
class DiffResult:
    declared_types: list[str]
    detected: list[str]  # sorted unique check ids, ignored ones removed
    unlabelled: list[str] = field(default_factory=list)
    missing: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unlabelled and not self.missing

    def report(self) -> str:
        lines = [
            f"declared change types: {', '.join(self.declared_types) or '(none)'}",
            f"detected oasdiff checks: {', '.join(self.detected) or '(none)'}",
        ]
        for check_id in self.unlabelled:
            lines.append(f"  UNLABELLED {check_id}: no declared change type covers it")
            for change in [c for c in self.changes if c.get("id") == check_id][:3]:
                where = " ".join(filter(None, [change.get("operation"), change.get("path")]))
                lines.append(f"      e.g. {where}: {change.get('text', '')}")
        for type_id, group in self.missing:
            lines.append(f"  MISSING   {type_id}: expected one of [{', '.join(group)}], none detected")
        lines.append("diffcheck: OK" if self.ok else "diffcheck: FAILED")
        return "\n".join(lines)


def evaluate(changes: list[dict], declared_types: list[str], catalog: Catalog) -> DiffResult:
    detected = sorted({c["id"] for c in changes if c.get("id") and c["id"] not in catalog.ignored_checks})
    types = [catalog.get(t) for t in dict.fromkeys(declared_types)]
    covered = set().union(*(t.all_ids for t in types)) if types else set()
    result = DiffResult(declared_types=list(dict.fromkeys(declared_types)), detected=detected, changes=changes)
    result.unlabelled = [i for i in detected if i not in covered]
    found = set(detected)
    for change_type in types:
        for group in change_type.required:
            if not found.intersection(group):
                result.missing.append((change_type.id, group))
    return result


def diffcheck(base: Path, revision: Path, declared_types: list[str], catalog: Catalog) -> DiffResult:
    return evaluate(run_oasdiff(base, revision), declared_types, catalog)
