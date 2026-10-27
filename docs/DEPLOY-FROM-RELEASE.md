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
GitHub Release, then:

```bash
sha256sum -c SHA256SUMS
gh attestation verify lab-playground-<version>-deploy.tar.gz \
  -R Ragnar-Security/Ragnar-Lab-Playground
gh attestation verify oci://ghcr.io/ragnar-security/lab-playground:<version> \
  -R Ragnar-Security/Ragnar-Lab-Playground
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
for example `1.0.0@sha256:<digest>`. Edit `templates.yaml` for your Proxmox
cluster, then check `.env` against the production rules:

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
notes say the migrations cannot be undone by a code rollback alone.
