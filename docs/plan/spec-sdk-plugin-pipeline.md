# api-upgrade-bench: spec → SDK → Context Plugin pipeline on APIMatic CLI v2 (v4 codegen)

## Context

dep-agent (`C:\Users\mehno\source\repos\dep-agent`) is benchmarked by having it upgrade integration apps
to new SDK versions using the APIMatic Context Plugin (CP). Today every new SDK/CP version is made by
hand. This plan builds a new repo, **api-upgrade-bench**, where a version is declared as
`base spec + labelled overlays`. GitHub Actions then builds the spec, publishes the C# SDK (NuGet +
GitHub source) and the CP (GitHub), and writes a lock file recording which change types each version
contains, so benchmark results can be scored per change type.

Decisions taken: **fresh bench-only repos/packages per API** (existing petstore repos untouched),
**GitHub Actions** as the runner, **star topology** (every version = base + its own overlays; apps stay
pinned at 1.0.0 and dep-agent runs `upgrade --to X`).

## Facts the design relies on (APIMatic CLI `@apimatic/cli@2`, verified in docs.apimatic.io/v4 and local `apimatic-cli` source)

- Install `npm i -g @apimatic/cli@2`; needs Node 24+, Git 2.38+. v2 generates v4 SDKs only; `--codegen-version`/`--zip` are gone.
- Project = `<input>/src/` with `src/spec/<openapi file>` (+ optional `APIMATIC-META*.json`) and `src/apimatic.json` (schema: `apimatic-cli/apimatic.schema.json`; replaces `APIMATIC-BUILD.json`).
- CI auth: `apimatic auth login --auth-key=$APIMATIC_API_KEY` (no env var is read automatically). `sdk publish` has **no** `--auth-key`, so login first.
- `apimatic sdk publish --profile-id <id> --language csharp --version X.Y.Z --publish-type package --publish-type sourcecode [-i dir] [-d dir] [--force] [--dry-run]` (`src/commands/sdk/publish.ts`). Flag mode never prompts. NuGet package id and GitHub repo/branch come from the **publishing profile** (created only in the APIMatic web app; credentials live there, not in CI). On success it records `languages.csharp.publishing.{source,package.version,packageConfiguration}` in `src/apimatic.json` (`RecordPublishedSdkAction`). Existing SDK repo `petstore-sdk-csharp` has tags `v1.0.1`/`v1.0.2`, so source publishing appears to tag `v<version>` — verify in step 0.
- Merge of SDK customizations happens on every flag-mode publish via the SDK dir's own git snapshots; a fresh empty `-d` dir per release means nothing to merge (what we want: pure generator output).
- `apimatic plugin generate [-i dir] [-d dir] [--force] [-k key]` (`src/actions/plugin/generate.ts`). Without a terminal it **fails unless `apimatic.json` already has `plugin.pluginId`, `plugin.pluginName` and at least one language** → CI must write the `plugin` block itself. It uses the published-language records (repo URL, package version) that `sdk publish` wrote.
- `apimatic plugin publish` only **prints** git commands (`git add . && git commit -m "Publish <id> <ver>" && git tag -a v<ver>` + push --follow-tags). We run the equivalent ourselves.
- CP output layout (from `petstore-context-plugin`): `.claude-plugin/plugin.json` (`name`, `version`, `skills`, `agents`), `.codex-plugin/`, `.cursor-plugin/`, `plugin.json`, `skills/dotnet/*/SKILL.md`, `scoped/agents/*.md`. dep-agent already consumes this as a single-plugin repo with `stamp_field: version` (`dep-agent/config/plugins.yaml`).
- Generation runs server-side; the codegen version cannot be pinned from the CLI → generator drift between releases must be detected (see step 6).

## Repo layout (new: `C:\Users\mehno\source\repos\api-upgrade-bench`, GitHub `mehnoorsiddiqui/api-upgrade-bench`)

```
catalog/change-types.yaml        # id, category, breaking, description, expected oasdiff check ids
apis/<api>/
  api.yaml                       # profile_id, nuget_package_id, sdk_repo, plugin_repo, plugin_id, plugin_name
  spec/openapi.yaml              # base (v1.0.0)
  spec/APIMATIC-META.json        # codegen settings (C# namespace/project name) – copied from current petstore build
  overlays/<change-id>.yaml      # OpenAPI Overlay 1.0 + x-change {type, breaking, targets}
  releases.yaml                  # version -> [overlay ids]  (hand-edited)
  releases.lock.yaml             # written by CI only
tools/bench/                     # Python package (3.11, uv), CLI entry `bench`
  overlay.py  build.py  diffcheck.py  release.py  nuget_wait.py  plugin_push.py  lock.py  export_dep_agent.py
tests/                           # pytest for overlay apply, catalog/release validation, diff mapping
.github/workflows/validate.yml  release.yml
```

## File formats

- `api.yaml`: `{ name: petstore, profile_id: <24-hex>, nuget_package_id: ApimaticBench.Petstore, sdk_repo: mehnoorsiddiqui/bench-petstore-sdk-csharp, plugin_repo: mehnoorsiddiqui/bench-petstore-context-plugin, plugin_id: bench-petstore, plugin_name: "Bench Petstore" }`
- Overlay: standard `overlay: 1.0.0` + `actions` (`target` JSONPath, `update`/`remove`) plus top-level `x-change: {type, breaking, targets}`; `type` must exist in the catalog.
- `releases.yaml`: `base: 1.0.0`, `releases: [{version: 2.0.0, overlays: [rename-get-pet]}, …]`. Rules (validated): versions unique, never removed/edited once in the lock, base has no overlays, breaking change ⇒ new major.
- `releases.lock.yaml` per version: `spec_sha256`, `change_types`, `oasdiff_checks`, `cli_version`, `sdk: {package_id, version, repo, tag, commit}`, `plugin: {repo, tag, commit, version}`, `generator_drift: bool`, `published_at`, `workflow_run_url`.

## Tools (Python, in `tools/bench/`)

1. `overlay.py` – apply Overlay 1.0 `update`/`remove` actions with `jsonpath-ng`; refuse targets that match nothing.
2. `build.py <api> <version>` – write a fresh `build/<api>/<version>/src/` containing `spec/openapi.json` (base + overlays, `info.version` = version), `spec/APIMATIC-META.json`, and `apimatic.json`:
   `{"schemaVersion":1, "plugin":{"pluginId", "pluginName", "pluginVersion":<version>, "license":"MIT"}, "languages":{"csharp":{}}}` → plugin version is in lockstep with the SDK version (matches dep-agent `stamp_field: version`).
3. `diffcheck.py` – run pinned `oasdiff breaking` base vs built spec; compare detected check ids with the catalog's expected ids for the declared types; fail on unlabelled or missing changes.
4. `release.py <api> <version>` – the sequence in the next section; idempotent: skips a step whose output already exists (NuGet version present, tag present) so a re-run after a partial failure completes instead of re-publishing.
5. `nuget_wait.py` – poll `https://api.nuget.org/v3-flatcontainer/<id-lower>/index.json` until the version appears (timeout 30 min).
6. `plugin_push.py` – clone plugin repo, replace contents with generated plugin dir (keep `.git`), verify `.claude-plugin/plugin.json` `version` == SDK version, commit `Publish <plugin_id> <version>`, annotated tag `v<version>`, `git push --follow-tags`.
7. `lock.py` – append the lock entry. `export_dep_agent.py` – print `plugins.yaml` entries (single-plugin repo, `stamp_field: version`) for dep-agent.

## Per-release sequence (`release.py`)

1. `bench build <api> <ver>` and `diffcheck`.
2. `apimatic api validate -i build/<api>/<ver> --auth-key=$KEY` (blocking rules fail fast).
3. `apimatic auth login --auth-key=$APIMATIC_API_KEY`.
4. `apimatic sdk publish -i build/<api>/<ver> -d build/<api>/<ver>/sdk/csharp --profile-id <profile_id> --language csharp --version <ver> --publish-type package --publish-type sourcecode --force`.
5. Verify: `src/apimatic.json` now has `languages.csharp.publishing.package.version == ver`; `gh api repos/<sdk_repo>/git/ref/tags/v<ver>` exists (create it on the published commit if APIMatic did not); `nuget_wait`.
6. Drift check (base only once per run): `apimatic sdk generate` of the **base** spec into a temp dir and diff against the `v1.0.0` tag of the SDK repo (ignoring version strings); set `generator_drift` in the lock and fail the run unless `allow_drift` input is set.
7. `apimatic plugin generate -i build/<api>/<ver> -d build/<api>/<ver>/plugin --force --auth-key=$KEY`.
8. `plugin_push`, then `lock` and commit `releases.lock.yaml` to the bench repo (`[skip ci]`).

## Workflows

- `validate.yml` (pull_request): setup Python/uv, Node 24, .NET 8, oasdiff; `pytest`; schema-validate catalog/overlays/releases; for each **new** release: build, diffcheck, `apimatic api validate`, `apimatic sdk generate` + `dotnet build` (compile only, nothing published).
- `release.yml` (push to main + `workflow_dispatch` with `api`, `version`, `allow_drift`): compute releases in `releases.yaml` missing from the lock; run `release.py` for each, **serially**, `concurrency: release-<api>` (shared `apimatic.json` state and repos). Secrets: `APIMATIC_API_KEY`, `PLUGIN_PUSH_TOKEN` (fine-grained PAT, contents:write on `bench-*-context-plugin` repos, and on `bench-*-sdk-csharp` for the tag fallback). NuGet and SDK-repo GitHub credentials stay inside the APIMatic publishing profile.

## One-time manual setup (per API, documented in README)

1. Create empty GitHub repos `bench-<api>-sdk-csharp` and `bench-<api>-context-plugin`.
2. In app.apimatic.io Publishing Center: add NuGet API-key credential (scoped to `ApimaticBench.*` push) and GitHub credential; create profile "bench-<api>" with C# enabled, package id `ApimaticBench.<Api>`, source repo `bench-<api>-sdk-csharp`, branch `main`.
3. `apimatic publishing profile list` → copy the id into `apis/<api>/api.yaml`.
4. Add `apis/<api>/spec/openapi.yaml` + `APIMATIC-META.json`, `releases.yaml` with only `base: 1.0.0` → merge → 1.0.0 published.

## Implementation order

0. **Spike (manual, petstore, before writing tools):** run steps 2–8 by hand with version 1.0.0 against the fresh profile; confirm: SDK repo tag/branch behaviour, `apimatic.json` publishing record, that `plugin generate` runs non-interactively with the pre-written `plugin` block, plugin `version` field, C# namespace from `APIMATIC-META.json`. Adjust this plan's facts if any differ.
1. Repo scaffold, catalog with ~6 change types (rename-operation, rename-parameter, add-required-parameter, rename-model-property, change-property-type, auth-scheme-change), overlay applier + tests.
2. build + diffcheck + validate.yml.
3. release.py + release.yml; publish petstore 1.0.0, then 2.0.0 (one overlay).
4. export_dep_agent.py; add entries to `dep-agent/config/plugins.yaml`; point a petstore integration app (pinned 1.0.0, package `ApimaticBench.Petstore`) at it.
5. Second API (invented API) to prove "add an API = add a folder".

## Verification

- `pytest` in the bench repo (overlay apply, release-file validation, oasdiff→catalog mapping using fixture specs).
- PR with a new release: validate.yml green; a deliberately mislabelled overlay makes diffcheck fail.
- After merge: NuGet shows `ApimaticBench.Petstore 2.0.0`; `bench-petstore-sdk-csharp` has tag `v2.0.0`; `bench-petstore-context-plugin` has tag `v2.0.0` with `.claude-plugin/plugin.json` version `2.0.0`; `releases.lock.yaml` entry lists `change_types: [rename-operation]`.
- Re-running release.yml for 2.0.0 is a no-op (idempotency).
- End to end: `dep-agent run --dry-run` reports plugin ready for 2.0.0; `dep-agent upgrade --repo <petstore-app> --package ApimaticBench.Petstore --to 2.0.0` resolves the CP commit by version.
