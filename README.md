# api-upgrade-bench

Benchmark API versions for [dep-agent](../dep-agent), declared as **base spec + labelled overlays**.
For each version, GitHub Actions builds the spec, publishes the C# SDK (NuGet and GitHub source) and
the APIMatic Context Plugin (GitHub) with APIMatic CLI v2 (v4 codegen), and records in a lock file
which change types the version contains. Benchmark results can then be scored per change type.

Versions use a **star topology**: every version is the base plus its own overlays, never the
previous version plus more. Integration apps stay pinned at the base (`1.0.0`) and dep-agent runs
`upgrade --to X`.

## Layout

```
catalog/change-types.yaml        change types: id, category, breaking, expected oasdiff check ids
schemas/*.schema.json            JSON Schemas for api.yaml, overlays, releases.yaml, the catalog
apis/<api>/
  api.yaml                       profile_id, nuget_package_id, sdk_repo, plugin_repo, plugin_id, plugin_name
  apimatic.json                  optional template for src/apimatic.json (portal settings, plugin author/license);
                                 bench sets plugin id/name/version and `languages` for each version
  spec/openapi.yaml              the base spec (version 1.0.0); info.title names the SDK project/namespace
  spec/APIMATIC-META.json        optional: import/merge/validation settings only (Codegen v4 ignores CodeGenSettings)
  overlays/<change-id>.yaml      OpenAPI Overlay 1.0 + x-change {type, breaking, targets, requires?}
  releases.yaml                  version -> [overlay ids]   (edited by hand, or written by an importer)
  releases.lock.yaml             what was published         (written by CI only)
  import/                        optional: the importer of a real API's release history (apis/square)
tools/bench/                     the `bench` CLI (Python 3.11, uv)
  snapshot_overlays.py           spec diff -> labelled overlays, for imported histories
tests/                           pytest
.github/workflows/               validate.yml (PRs), release.yml (main + manual)
```

APIs today: `petstore` (the Swagger Petstore 3.0 build used so far, renamed `ApimaticBench.Petstore`),
with one example overlay per change type, and `square`, Square's real release history from 2024-04-17
to 2026-09-16, imported as described in [Imported histories](#imported-histories-square).

## Using it

```sh
uv sync
uv run pytest
uv run bench validate                        # schemas + release rules + lock immutability
uv run bench diffcheck-overlays              # each overlay alone produces exactly its declared type
uv run bench pending                         # declared versions not in the lock
uv run bench build petstore 2.0.0            # -> build/petstore/2.0.0/src (an APIMatic CLI project)
uv run bench diffcheck petstore 2.0.0
uv run bench check petstore 2.0.0            # + apimatic api validate, sdk generate, dotnet build
uv run bench release petstore --dry-run      # everything up to publishing, publishes nothing
uv run bench export-dep-agent                # plugins.yaml entries for dep-agent
uv run bench export-dep-agent --changes      # per-version change types, for scoring
```

`bench diffcheck` needs [oasdiff](https://github.com/oasdiff/oasdiff) 1.35.0 on `PATH` (or `OASDIFF=/path/to/oasdiff`).
The APIMatic commands need `npm i -g @apimatic/cli@2` (Node 24+, Git 2.38+) and either
`APIMATIC_API_KEY` or a stored `apimatic auth login`. `bench check` compiles with the .NET 10 SDK
(the generated SDK uses C# 14).

### Add a release

1. Write `apis/<api>/overlays/<change-id>.yaml`:

   ```yaml
   overlay: 1.0.0
   info: {title: Rename getPetById to getPet, version: 1.0.0}
   x-change:
     type: rename-operation        # must exist in catalog/change-types.yaml
     breaking: true                # must equal the catalog's value
     targets: ["operation:GET /pet/{petId}"]
   actions:
     - target: $.paths['/pet/{petId}'].get
       update: {operationId: getPet}
   ```

   Actions are Overlay 1.0 `update` (objects merge recursively, arrays append) and `remove: true`.
   A target that matches nothing is an error.
2. Append to `releases.yaml`: `- {version: 2.0.0, overlays: [rename-get-pet]}`.
3. Open a PR. `validate.yml` checks the overlay and, for each new version, builds it, runs
   diffcheck, `apimatic api validate`, `apimatic sdk generate` and `dotnet build`. Nothing is published.
4. Merge. `release.yml` publishes the version and commits its lock entry.

Rules (`bench validate`): versions are unique and greater than the base; the base has no overlays;
a breaking change needs a new major; a version in the lock is never removed or edited (same
overlays, and rebuilding it must reproduce the locked spec hash, so editing the base spec or a used
overlay after release fails).

**diffcheck** runs `oasdiff changelog` (all levels, not `oasdiff breaking`: oasdiff rates an
operationId change as INFO although it renames the SDK method) between the base and the built
spec. It fails when a detected check id is not covered by any declared change type (an
unlabelled change) or when a declared type's required checks are not detected (a missing change).

### Add an API

1. Add `apis/<api>/` with `api.yaml` (`profile_id: null` for now), `spec/openapi.yaml`, and
   `releases.yaml` with only `base: 1.0.0`. Optionally add `apimatic.json` for settings such as
   `portal`; leave out `plugin.pluginVersion` and `languages.*.publishing`: `bench build` writes those
   for every version (and `bench validate` rejects a copied publishing record).
2. Do the one-time setup below, then put the profile id in `api.yaml` and merge: `1.0.0` is published.

An API whose `profile_id` is still `null` is validated but never released.

## Imported histories (Square)

`apis/square` is not hand-written: `apis/square/import/import_square.py` builds it from seven commits
of Square's public spec (`square/connect-api-specification`, listed in `import/snapshots.yaml`).

| Version | Step | Square API | Square's own .NET SDK |
| --- | --- | --- | --- |
| 1.0.1 (base) | S0 | 2024-04-17 | 36.0.0 |
| 1.1.0 | S1 | 2024-05-15 | 37.0.0 |
| 1.2.0 | S2 | 2024-08-21 | 38.0.0 |
| 2.0.0 | S3 | 2024-11-20 (Swagger 2.0 -> OpenAPI 3.0) | 39.0.0 |
| 3.0.0 | S4 | 2025-01-23 | 40.0.0 |
| 4.0.0 | S5 | 2025-10-16 | 42.2.0 |
| 5.0.0 | S6 | 2026-09-16 | 47.0.0 |

```sh
uv run python apis/square/import/import_square.py            # fetch, normalize, write, verify
uv run python apis/square/import/import_square.py --verify   # check the committed files only
```

1. **Normalize** each snapshot into a generator input (`import/normalize.py`; the same rules for every
   step, so none of them shows up as a change). Codegen v4 rejects Square's Swagger 2.0 files, so
   S0-S2 are converted to OpenAPI 3.0 the way Square converted them itself in S3; 31 dangling `$ref`s
   in S0-S2 (and 1 in S5, 2 in S6) are resolved from later snapshots; the title is fixed to `Square`
   (the C# namespace); `oauth2` operations use an HTTP bearer scheme (Codegen v4 turns `oauth2` into an
   interactive authorization-code flow that cannot take an existing access token); Production and
   Sandbox servers are declared.
2. **Diff** each step against the previous one (`tools/bench/snapshot_overlays.py`). Every difference
   is split into atomic edits on disjoint parts of the spec, each labelled with one catalog change type
   (rules in the module docstring); an unknown kind of difference stops the import. Edits of one type
   become one overlay, `overlays/s<k>-<change-type>.yaml`, with every target listed in `x-change.targets`.
3. **Release** k lists the overlays of steps 1..k, so base + overlays rebuilds step k's spec exactly
   (`--verify` checks this through `bench build`). A step with a breaking change type bumps the major,
   any other step the minor. Patch versions are left for re-publishing a step itself.

Every version — the base included — is published by the `square-api` profile (source and NuGet
`ApimaticBench.Square`), and its context plugin carries the same version and points at the SDK's
`v<version>` tag. The base is `1.0.1`: `1.0.0` was the same S0 spec published by hand before the
profile existed (NuGet through a workflow in the SDK repo, assembly `Square.dll` where the profile
builds `ApimaticBench.Square.dll`); it stays on nuget.org and in the lock as adopted history, since
nuget.org versions cannot be deleted.

Because a later step can change what an earlier step added, an imported overlay declares
`x-change.requires`: the previous step's overlays, plus that step's `add-model` overlay (new schemas
come first) — `remove-model` requires the rest of its step (schemas go last). `bench validate` and
`bench diffcheck-overlays` apply an overlay after what it requires (transitively) instead of to the bare
base, and a release must list an overlay's requirements before it. Change types oasdiff cannot see
(documentation, release status, an unreferenced new schema) have `required: []` in the catalog.

## One-time setup

Per API:

1. Create empty GitHub repos `bench-<api>-sdk-csharp` and `bench-<api>-context-plugin`.
2. In app.apimatic.io → Publishing Center: add a NuGet API-key credential (scoped to push
   `ApimaticBench.*`) and a GitHub credential; create profile `bench-<api>` with C# enabled, package
   id `ApimaticBench.<Api>`, source repo `bench-<api>-sdk-csharp`, branch `main`.
3. `apimatic publishing profile list` → copy the id into `apis/<api>/api.yaml` (`profile_id`).

For the repo (once):

- Secrets: `APIMATIC_API_KEY`; `PLUGIN_PUSH_TOKEN`, a fine-grained PAT with contents:write on the
  `bench-*-context-plugin` repos (push + tag) and the `bench-*-sdk-csharp` repos (tag fallback, drift clone).
  NuGet and SDK-repo GitHub credentials stay inside the APIMatic publishing profile.
- An API with `nuget_publisher: sdk-repo-workflow` (petstore, for now) leaves NuGet to a workflow in
  its SDK repo: APIMatic publishes the source only, then bench runs that workflow with
  `-f version=<version>` and waits for the tag the workflow creates after its NuGet push. The PAT then also
  needs **Actions: read and write** on that SDK repo, and the workflow must not also run on `push`
  (otherwise APIMatic's push publishes a second, auto-numbered version).
- `release.yml` commits `releases.lock.yaml` to `main` with `GITHUB_TOKEN`; if `main` is protected,
  allow GitHub Actions to push to it.

## The release run (`bench release <api>`)

For each version declared but not in the lock, serially (`concurrency: release-<api>`):

1. build + diffcheck; `apimatic api validate`
2. `apimatic auth login --auth-key=$APIMATIC_API_KEY` (once; `sdk publish` has no `--auth-key`)
3. generator drift (once per run, before anything is published, and only once the base is
   published): regenerate the base exactly as published (`sdk publish --dry-run`, same profile and
   version) and compare with the SDK repo's `v<base>` tag (line endings and `drift_ignore` globs
   ignored). Drift fails the run unless `allow_drift` is set; the lock records `generator_drift`.
4. `apimatic sdk publish --publish-type package --publish-type sourcecode` into a fresh, empty SDK dir
   (nothing to merge: pure generator output)
5. check `languages.csharp.publishing` in `src/apimatic.json` (version, repo, package id), tag
   `v<version>` in the SDK repo (created on the published commit if APIMatic did not). The run does not
   wait for NuGet to list the version (a successful publish already pushed it; indexing takes minutes);
   `bench release --wait-for-nuget` waits when something after the release must restore it at once
6. `apimatic plugin generate`; the `plugin` block in `apimatic.json` is written by `bench build`, so
   it runs without a terminal, with `pluginVersion` = the SDK version; the publishing record's source
   branch is replaced by the tag `v<version>`, so the plugin describes the SDK at that version
7. push the plugin repo (contents replaced, `.claude-plugin/plugin.json` version checked), commit
   `Publish <plugin_id> <version>`, annotated tag `v<version>`
8. append the lock entry and commit it (`[skip ci]`)

Idempotent: a locked version is skipped, and within a version a step whose result exists (NuGet
version, SDK tag, plugin tag) is not repeated. When the SDK was published by an earlier, interrupted
run, its publishing record is reconstructed from `api.yaml` (`publish_record: reconstructed` in the lock).
APIMatic reports the source and the package separately, so a run can stop with only one of them out:
on NuGet but untagged, the re-run publishes the source only; tagged but not on NuGet, the package only.
An SDK tag counts whether it is `v<version>` (the bench's) or `<version>` (the one an APIMatic source
publish creates).

Lock entry (`releases.lock.yaml`):

```yaml
releases:
  2.0.0:
    overlays: [rename-get-pet]
    spec_sha256: …
    change_types: [rename-operation]
    oasdiff_checks: [api-operation-id-removed]
    cli_version: 2.2.0
    sdk: {package_id: ApimaticBench.Petstore, version: 2.0.0, repo: …, tag: v2.0.0, commit: …}
    plugin: {repo: …, tag: v2.0.0, commit: …, version: 2.0.0}
    generator_drift: false
    publish_record: published
    published_at: …
    workflow_run_url: …
```

## dep-agent

After a release, `uv run bench export-dep-agent` prints the `config/plugins.yaml` entries
(single-plugin repo, `stamp_field: version`). Add them to dep-agent and point an integration app at
`ApimaticBench.<Api>` pinned to `1.0.0`.

## Step 0: the spike (before the first real release)

Things this repo assumes about the CLI that need confirming against the fresh `bench-petstore`
profile, with version `1.0.0`:

- [ ] `apimatic sdk publish` (flag mode) pushes the source to `bench-petstore-sdk-csharp` `main` and
      tags `v1.0.0`; if it does not tag, the fallback tags the branch head.
- [ ] it records `languages.csharp.publishing.{source,package.version,packageConfiguration}` in `src/apimatic.json`.
- [ ] `apimatic plugin generate` runs non-interactively with the pre-written `plugin` block and
      writes `.claude-plugin/plugin.json` with `"version": "1.0.0"` and `"name": "bench-petstore"`
      (dep-agent matches `plugin:` against `name`; `bench export-dep-agent` assumes it is `plugin_id`).
- [ ] the C# project/namespace comes from the spec's `info.title` (expected `SwaggerPetstoreOpenApi310`).
- [ ] `sdk publish --dry-run` output equals the published `v1.0.0` tree; any files source
      publishing adds go into `drift_ignore` in `api.yaml`.

Run it as `uv run bench release petstore --dry-run` first, then `uv run bench release petstore`
locally (without `--commit-lock`) or through `release.yml` with `workflow_dispatch`.
