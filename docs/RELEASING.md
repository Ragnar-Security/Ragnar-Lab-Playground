# Releasing

A release is a `vX.Y.Z` tag on `main`. Pushing it runs
`.github/workflows/release.yml`, which publishes:

- `ghcr.io/ragnar-security/lab-playground:X.Y.Z`, plus `X.Y` and `latest`
  for a stable release, for linux/amd64 and linux/arm64, with BuildKit
  provenance (`mode=max`), an SBOM, and a GitHub build provenance attestation;
- a GitHub Release whose body is the version's `CHANGELOG.md` section, with
  `lab-playground-X.Y.Z-deploy.tar.gz` (attested) and `SHA256SUMS` attached.

Nothing deploys automatically. Production keeps building from its checkout
until an operator chooses a release image ("Running a released image" in
`docs/PRODUCTION_DEPLOYMENT.md`).

Only members of the `lab-playground-maintainers` team can create, move or
delete `refs/tags/v*`: a tag ruleset enforces it. Anyone else's tag push is
rejected, so the workflow never runs for it.

## Versioning

Semantic versioning: a breaking change for operators or students (a removed
or newly required request field, a required migration with no rollback, a
renamed environment variable) bumps the major version; a migration or new
setting bumps the minor version; fixes bump the patch version.

## Checklist

1. **On a branch, bump the version in both places.** They must agree; a
   test fails otherwise.
   - `app/__init__.py`: `__version__ = "X.Y.Z"`
   - `pyproject.toml`: `version = "X.Y.Z"`, then `uv lock` (the lock records
     the project version; CI runs `uv lock --check`).
2. **Write the CHANGELOG section.** Rename `## [Unreleased]` to
   `## [X.Y.Z] - YYYY-MM-DD` (the planned tag date), start a new empty
   `## [Unreleased]` above it, and update the link references at the bottom.
   The section needs a non-empty `### Upgrade notes` subsection: migrations
   to run and in which order, new or changed environment variables with
   their defaults, breaking changes and who notices them, and whether a
   rollback needs a database restore. Write "No action needed." when that is
   true. Check it the way the workflow will:

   ```bash
   python3 scripts/release_notes.py X.Y.Z
   python3 scripts/release_version.py          # prints X.Y.Z
   ```

3. **Pull request, CI, merge.** Merge to `main` once CI is green.
4. **Tag the merged commit and push the tag.** Use an annotated tag on the
   `main` commit that CI tested:

   ```bash
   git fetch origin
   git checkout main && git merge --ff-only origin/main
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

5. **Watch the workflow.**

   ```bash
   gh run watch "$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
   ```

   `verify` refuses a tag that does not equal `v` + the version, a CHANGELOG
   section without upgrade notes, and a commit that is not on `origin/main`.
   `test` reruns the whole CI workflow on the tag. `image` and `bundle`
   publish only after both pass, and `release` only after both of those.
6. **Verify what was published.**

   ```bash
   gh attestation verify oci://ghcr.io/ragnar-security/lab-playground:X.Y.Z \
     -R Ragnar-Security/Ragnar-Lab-Playground
   gh release download vX.Y.Z -R Ragnar-Security/Ragnar-Lab-Playground
   sha256sum -c SHA256SUMS
   gh attestation verify lab-playground-X.Y.Z-deploy.tar.gz \
     -R Ragnar-Security/Ragnar-Lab-Playground
   ```

   The bundle is reproducible: `python3 scripts/build_release_bundle.py
   X.Y.Z /tmp/check --ref vX.Y.Z` on any clone gives the published bytes.

## First release only: make the package public

The first push of a GHCR package from an organization creates it
**private**, even from a public repository. After the first release, a
maintainer opens the package (Organization > Packages > `lab-playground` >
Package settings), sets its visibility to public, and confirms that the
package is linked to this repository (the image's
`org.opencontainers.image.source` label links it). Until then, anonymous
`docker pull` and `gh attestation verify oci://...` from outside the
organization fail.

## Dry runs and prereleases

Actions > Release > Run workflow builds everything with `dry_run` checked
(the default) and publishes nothing: no image push, no attestation, no
release. From a branch it skips the tag comparison and only warns about the
ancestry check. Use it before tagging when the workflow or `Dockerfile.prod`
changed.

The tag filter matches `vX.Y.Z` only. To publish a prerelease, tag
`vX.Y.Z-rc.N` (with a matching version and CHANGELOG section), push it, then
run the workflow on that tag with `dry_run` unchecked. A prerelease gets
only its exact image tag (no `X.Y`, no `latest`) and a GitHub prerelease.

## When a release goes wrong

Never move or reuse a published tag: operators and attestations refer to
it. If the workflow failed before `release`, fix the cause on `main` and
release the next patch version. If only the `release` job failed (the image
is already published), rerun the failed jobs from the workflow run page.
