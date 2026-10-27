# Releasing

A release is a `vX.Y.Z` tag on `main`. Pushing it runs
`.github/workflows/release.yml`, which publishes:

- `ghcr.io/ragnar-security/lab-playground:X.Y.Z` for linux/amd64 and
  linux/arm64, with BuildKit provenance (`mode=max`), an SBOM, and a GitHub
  build provenance attestation. The image also takes `X.Y` when it is the
  highest stable release of X.Y, and `latest` when it is the highest stable
  release of all;
- a GitHub Release whose body is the version's `CHANGELOG.md` section, with
  `lab-playground-X.Y.Z-deploy.tar.gz` (attested) and `SHA256SUMS` attached.

Nothing deploys automatically. Production keeps building from its checkout
until an operator chooses a release image ("Running a released image" in
`docs/PRODUCTION_DEPLOYMENT.md`).

## Who can publish

Three controls stand between a commit and a published release:

- **The tag ruleset.** Only the `lab-playground-maintainers` team can
  create, move or delete `refs/tags/v*`. Anyone else's tag push is rejected,
  so the workflow never runs for it.
- **Tag pushes only.** Only a pushed `v*` tag publishes. Running the workflow
  by hand (Actions > Release > Run workflow) is always a dry run: every job
  it starts holds `contents: read` only, and the publishing jobs are skipped.
- **The `release` environment.** Every job that can write (the image push,
  both attestations, the GitHub Release) uses the `release` environment.
  Only `v*` tags may deploy to it, and a member of
  `lab-playground-maintainers` must approve each run.

**Residual risk.** Anyone with write access to the repository can still push
a branch whose workflow asks for `packages: write` on `GITHUB_TOKEN` and
pushes to the GHCR package, even under the `X.Y.Z` or `latest` tags. That
run cannot come from a `v*` tag, which only maintainers can create, so the
bound verification commands below (`--source-ref refs/tags/vX.Y.Z`) reject
what it publishes; an operator who skips verification and pulls by tag
would not notice. Closing this needs a maintainer decision: limit
repository write access to the maintainers, or publish with a
package-scoped credential that other workflows cannot reach.

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
4. **Optional dry run.** When `release.yml` or `Dockerfile.prod` changed,
   run the workflow on `main` first (`gh workflow run release.yml --ref
   main`). It builds the image for both platforms and the bundle, and
   publishes nothing.
5. **Tag the merged commit and push the tag.** Use an annotated tag on the
   `main` commit that CI tested:

   ```bash
   git fetch origin
   git checkout main && git merge --ff-only origin/main
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

6. **Watch the workflow and approve it.**

   ```bash
   gh run watch "$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
   ```

   `verify` refuses a tag that does not equal `v` + the version, a CHANGELOG
   section without upgrade notes, and a commit that is not on `origin/main`.
   `test` reruns the whole CI workflow on the tag. Then the run stops at the
   `release` environment: open the run page, choose **Review deployments**,
   tick `release` and approve. `Publish image` and `Attest bundle` wait
   together; `GitHub Release` asks again once both have finished. Reject
   instead if anything in the run looks wrong; nothing has been published
   before the first approval.

   Publishing runs queue behind each other, one at a time, so two tags
   pushed close together publish in order and never move the aliases at
   the same time. Which aliases a version takes comes from the existing
   `v*` tags (`scripts/release_version.py aliases`), so a patch to an older
   line never takes `latest`, whichever run finishes last.
7. **Verify what was published.** The flags bind the attestation to
   `release.yml` running for this tag; `-R` alone would accept any workflow
   in the repository.

   ```bash
   gh attestation verify oci://ghcr.io/ragnar-security/lab-playground:X.Y.Z \
     -R Ragnar-Security/Ragnar-Lab-Playground \
     --signer-workflow Ragnar-Security/Ragnar-Lab-Playground/.github/workflows/release.yml \
     --source-ref refs/tags/vX.Y.Z
   gh release download vX.Y.Z -R Ragnar-Security/Ragnar-Lab-Playground
   sha256sum -c SHA256SUMS
   gh attestation verify lab-playground-X.Y.Z-deploy.tar.gz \
     -R Ragnar-Security/Ragnar-Lab-Playground \
     --signer-workflow Ragnar-Security/Ragnar-Lab-Playground/.github/workflows/release.yml \
     --source-ref refs/tags/vX.Y.Z
   ```

   The release notes print the same commands with `--source-digest <commit>`
   added. The bundle is reproducible: `python3 scripts/build_release_bundle.py
   X.Y.Z /tmp/check --ref vX.Y.Z` on any clone gives the published bytes.

## First release only: make the package public

The first push of a GHCR package from an organization creates it
**private**, even from a public repository. After the first release, a
maintainer opens the package (Organization > Packages > `lab-playground` >
Package settings), sets its visibility to public, and confirms that the
package is linked to this repository (the image's
`org.opencontainers.image.source` label links it). Until then, anonymous
`docker pull` and attestation checks of the image from outside the
organization fail.

## Prereleases

Set the version to `X.Y.Z-rc.N` in both places, give it a CHANGELOG section
with upgrade notes, merge, then tag and push `vX.Y.Z-rc.N` like a release.
A prerelease gets only its exact image tag (no `X.Y`, no `latest`) and a
GitHub prerelease, and never becomes the GitHub "Latest release".

## When a release goes wrong

Never move or reuse a published tag: operators and attestations refer to
it. If the workflow failed or was rejected before anything was published,
fix the cause on `main` and release the next patch version. If only the
`GitHub Release` job failed (the image is already published), rerun the
failed jobs from the workflow run page; the rerun asks for approval again.
