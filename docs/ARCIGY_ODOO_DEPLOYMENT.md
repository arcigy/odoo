# arcigy-odoo: what actually runs and how it is deployed

Verified against the live CapRover service `srv-captain--arcigy-odoo` on 2026-10-17.
Re-verify with the commands at the end before relying on this after a deploy.

## Composition

| Part | Source of truth | How it reaches the container |
|---|---|---|
| Odoo base | `odoo:19.0-20260630@sha256:f54272f3…` | `Dockerfile` `FROM` (pinned) |
| Extra Python packages | `config/python-constraints.txt` | `Dockerfile` `pip3 install --constraint` |
| `geotherm_chatbot`, `geotherm_drive`, `arcigy_saas_control_center` | this repo, `addons/` | baked into the image |
| `odoo.conf`, `start-odoo.sh` | this repo | baked into the image |
| `arcigy_feedback` | `arcigy/kitchen_app`, `odoo-addons/arcigy_feedback` at commit `44a541b8` (branch `feature/arc-003-feedback-webhook`) | read-only bind mount from host `/opt/arcigy-feedback/releases/44a541b8/odoo-addons/arcigy_feedback` |
| Feedback webhook signer keys | host `/etc/arcigy-feedback-webhook/signer` | read-only bind mount to `/etc/arcigy-feedback-webhook` |
| Feedback webhook receiver socket | host systemd unit `arcigy-feedback-webhook.service` (release `6559ea6f`) | read-only bind mount of `/run/arcigy-feedback-webhook`, container supplementary group `987` |
| Filestore | Docker volume `captain--arcigy-odoo-data` | mounted at `/var/lib/odoo` |
| Database | CapRover app `arcigy-odoo-db` (PostgreSQL), database `geotherm_erp` | env `HOST`, `PORT`, `USER`, `PASSWORD`, `ODOO_DB_NAME` |

The three bind mounts and the extra group come from the CapRover **Service Update Override**
of the `arcigy-odoo` app. CapRover re-applies it on every deploy, so a normal deploy from this
repository keeps `arcigy_feedback` and the webhook transport. Do not clear or edit that override
without replacing what it mounts.

The bind mount shadows any `arcigy_feedback` copy inside an image. The currently deployed image
`img-captain-arcigy-odoo-feedback:20261008-arc003-44a541b8` contains an identical copy, which is
irrelevant while the mount exists. Images built from this repository do not contain it at all.

## Deploy paths

- **This repository:** push to `main` → CI validates and tests → `git archive` upload → CapRover
  builds `Dockerfile` → service update with the override re-applied. Pull requests only run the
  validation job; nothing deploys from a branch.
- **`arcigy_feedback` and the webhook daemon:** released from `arcigy/kitchen_app` into
  `/opt/arcigy-feedback/releases/<commit>` on the host, then the override's source path is switched
  to the new release. This repository does not ship that addon.
- **Module install or upgrade:** set `ODOO_UPDATE_MODULES` (upgrade) or `ODOO_INIT=1` with
  `ODOO_INIT_MODULES` (install) on the CapRover app, restart once, then remove or reset the variable.
  Take and verify a backup first (`docs/SAAS_CONTROL_CENTER_DEPLOY_RUNBOOK.md`).
- **Required environment:** `start-odoo.sh` refuses to start when `ODOO_MASTER_PASSWORD` is unset
  or empty, so never remove it from the CapRover app, including while cleaning up other variables.

## Adding a new Arcigy addon

1. Put it under `addons/<name>` with `'installable': True` (required by `validate_odoo_addons.py`).
2. Add it to the CI `--init` list and to `--test-tags` in `.github/workflows/deploy-caprover.yml`.
3. Add its manifest to the required files in `integrations/create_odoo_deploy_archive.sh`.
4. Test the install and upgrade on a restored copy of the production database before merging to
   `main`, because the merge deploys.

## Reproducibility check (passed 2026-10-17)

- Base image: all 9 layers of the pinned `odoo:19.0-20260630` image match the first 9 layers of the
  live image.
- Files: `/mnt/extra-addons` (except `arcigy_feedback`), `/etc/odoo/odoo.conf` and
  `/opt/geotherm/start-odoo.sh` in an image built from this repository are identical to the live
  container.
- Python: `pip3 freeze` output is identical (93 packages).

```sh
# Build locally as the server does (amd64)
docker build --platform linux/amd64 -t arcigy-odoo-repro:local .

# Extract comparable files from the built image
docker run --rm --platform linux/amd64 --entrypoint tar arcigy-odoo-repro:local \
  -C / -cf - mnt/extra-addons etc/odoo/odoo.conf opt/geotherm/start-odoo.sh > built.tar

# Same from production (read-only; run on the CapRover host)
docker exec "$(docker ps -q --filter name=srv-captain--arcigy-odoo.1)" \
  tar -C / -cf - mnt/extra-addons etc/odoo/odoo.conf opt/geotherm/start-odoo.sh > live.tar

# Compare after extracting both, excluding __pycache__ and arcigy_feedback;
# compare `pip3 freeze` of both containers the same way.
```

## Known gaps

- Commit `44a541b8` of `arcigy_feedback` is not on GitHub; it exists only on a local branch and in
  the host release directory.
- `arcigy_saas_control_center` is installed at `19.0.1.23.0` in the database while the code is
  `19.0.1.24.0`; the module upgrade has not been run.
- The image and the app still carry the Geotherm addons and Geotherm environment variables, and the
  database is still named `geotherm_erp` from the clone.
- Bumping the Odoo base image means changing the digest deliberately and testing on a restored copy
  of the database first.
