"""NuGet: is a package version listed yet, and wait until it is."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .config import BenchError

FLAT_CONTAINER = "https://api.nuget.org/v3-flatcontainer"


def nuget_versions(package_id: str, timeout: float = 30) -> list[str]:
    url = f"{FLAT_CONTAINER}/{package_id.lower()}/index.json"
    request = urllib.request.Request(url, headers={"User-Agent": "api-upgrade-bench"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return [v.lower() for v in json.load(response).get("versions", [])]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise


def has_version(package_id: str, version: str) -> bool:
    return version.lower() in nuget_versions(package_id)


def wait_for_version(package_id: str, version: str, timeout_s: int = 1800, interval_s: int = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if has_version(package_id, version):
                print(f"NuGet lists {package_id} {version}", flush=True)
                return
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"NuGet poll failed ({exc}); retrying", flush=True)
        if time.monotonic() >= deadline:
            raise BenchError(f"{package_id} {version} did not appear on NuGet within {timeout_s // 60} min")
        print(f"waiting for NuGet to list {package_id} {version} ...", flush=True)
        time.sleep(interval_s)
