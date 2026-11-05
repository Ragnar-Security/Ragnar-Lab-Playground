# Deploying from a release bundle

This bundle runs one released version of Ragnar Lab Playground from its
published image, `ghcr.io/ragnar-security/lab-playground:<version>`, without a
git checkout. It holds the production Compose stack, the release override, the
Caddy configuration, example configuration files, the database backup script,
every migration script, and `CHANGELOG.md`. It holds no site configuration:
you create `.env` and `templates.yaml` yourself, and the bundle never
overwrites them.

The full production guide, including TLS, Proxmox credentials and post-deploy
checks, is `docs/PRODUCTION_DEPLOYMENT.md` in the repository.

Requirements: Docker with the Compose v2 plugin, version 2.24.0 or later
(`docker compose version`), and a DNS name for the site pointing at the host.

## 1. Verify what you downloaded

Download `lab-playground-<version>-deploy.tar.gz` and `SHA256SUMS` from the
GitHub Release, then check that the release workflow built both from the
release tag. `-R` alone would accept an attestation from any workflow on any
branch of the repository; `--signer-workflow` and `--source-ref` close that.
The release notes repeat these commands with the tag's commit filled in as
`--source-digest`.

```bash
sha256sum -c SHA256SUMS
gh attestation verify lab-playground-<version>-deploy.tar.gz \
  -R Ragnar-Security/Ragnar-Lab-Playground \
  --signer-workflow Ragnar-Security/Ragnar-Lab-Playground/.github/workflows/release.yml \
  --source-ref refs/tags/v<version>
gh attestation verify oci://ghcr.io/ragnar-security/lab-playground:<version> \
  -R Ragnar-Security/Ragnar-Lab-Playground \
  --signer-workflow Ragnar-Security/Ragnar-Lab-Playground/.github/workflows/release.yml \
  --source-ref refs/tags/v<version>
```

## 2. Unpack and configure

Pick a directory and keep its name: Compose names the database volume after
it, so a renamed directory starts with an empty database unless `.env` sets
`COMPOSE_PROJECT_NAME`.

```bash
tar -xzf lab-playground-<version>-deploy.tar.gz
mv lab-playground-<version> /opt/lab-playground
cd /opt/lab-playground
cp .env.example .env && chmod 600 .env
cp templates.example.yaml templates.yaml && chmod 644 templates.yaml
mkdir -p data backups
```

Edit `.env` for your site, and add these two lines so that a plain
`docker compose` uses the release image:

```bash
COMPOSE_FILE=docker-compose.prod.yml:docker-compose.release.yml
LAB_PLAYGROUND_VERSION=<version>
```

`LAB_PLAYGROUND_VERSION` may also pin the image digest from the release notes,
for example `1.0.0@sha256:<digest>`. It must name an exact release: the
preflight below refuses a moving tag such as `latest`, which would let any
restart change what runs. Edit `templates.yaml` for your Proxmox
cluster.

To place labs on AWS or Google Cloud as well, or instead, set
`ENABLED_BACKENDS`, `LAB_SITE_ID` and the enabled cloud's `AWS_*` / `GCP_*`
settings from the cloud section of `.env.example`, and add the backend blocks
shown by `templates.example.yaml`'s `multi-backend-example` group. The cloud
base infrastructure must exist first. The runbook, `docs/CLOUD_BACKENDS.md` in
the repository (not in this bundle), covers it and the credentials. A host
without an instance role or metadata-server identity needs its own
`docker-compose.cloud.yml` override, named last:
`COMPOSE_FILE=docker-compose.prod.yml:docker-compose.release.yml:docker-compose.cloud.yml`.
Keep `.env.example`'s `ENABLED_BACKENDS=proxmox` for a Proxmox-only portal.

Then check `.env` against the production rules:

```bash
python3 scripts/preflight_env.py .env
```

## 3. Start

```bash
docker compose pull
docker compose up -d
docker compose ps                                  # all three healthy
curl -s https://lab.example.edu/auth/health        # "version": "<version>"
```

## 4. Back up

`scripts/backup-database.sh` writes a gzipped MySQL dump to `/root/backups`.
To run it nightly, install the units in `scripts/systemd/` and set
`COMPOSE_DIR=/opt/lab-playground` in `/etc/default/vm-automation-backup`
(see `vm-automation-backup.env.example`).

## Upgrading to a later release

1. Read every `### Upgrade notes` section in the new `CHANGELOG.md` between
   your version and the new one.
2. Back up: `scripts/backup-database.sh`. Your current version is the
   rollback target.
3. Unpack the new bundle over the directory. It replaces only the files it
   ships; `.env`, `templates.yaml`, `data/` and `backups/` are untouched:
   `tar -xzf lab-playground-<new>-deploy.tar.gz --strip-components=1 -C /opt/lab-playground`.
4. Set `LAB_PLAYGROUND_VERSION` in `.env` to the new version, then pull it:
   `docker compose pull vm-automation`.
5. Run the migrations the upgrade notes list from the new image, before it
   starts serving:

   ```bash
   docker compose run --rm vm-automation python scripts/<migration>.py
   ```

6. `docker compose up -d`, then check `/auth/health` reports the new version.

To roll back, set `LAB_PLAYGROUND_VERSION` to the previous version and run
`docker compose up -d`. Restore the database backup only when the upgrade
notes say the migrations cannot be undone by a code rollback alone. A version
without cloud backends (v1.0.0) ignores the backend columns but never destroys
a cloud machine, and sets a cloud line in the failed-deploy survivor files
aside as corrupt. A cloud guest left by a failed deploy during a database
outage can exist only in those files, with no record. Roll back to such a
version only when **both** hold: no live cloud record, and no cloud line in
`data/untracked-survivors.jsonl` or `.importing`. Run the two checks in step
6, "Roll back", of "Redeploy an Existing Production Host" in
`docs/PRODUCTION_DEPLOYMENT.md` in the repository. Until both are clean, tear
the clouds down (`docs/CLOUD_BACKENDS.md`, "Teardown and incident response")
and let the current release import and destroy the survivors.
